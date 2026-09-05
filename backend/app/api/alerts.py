"""
Alertmanager Webhook Bridge API（薄 handler 层）

接收 Alertmanager 推送的告警，转换为飞书卡片消息转发到指定用户。

分层：业务逻辑（事故生命周期 / 自动诊断 / 摘要生成）已下沉到
    services/alert_service.py
本文件仅保留：
- 端点路由（/webhook, /test）
- webhook 共享密钥校验（HTTP 层关注点，secure by default）
- 对外符号转发（测试 / main 依赖 app.api.alerts 的符号保持不变）

链路：Prometheus 告警规则触发 → Alertmanager 聚合 → POST /api/alerts/webhook
    → Bridge 验证共享密钥 → 解析告警 → 飞书卡片通知 + 触发自动诊断
"""
import hmac

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from loguru import logger

from ..core.auth import UserResponse, require_admin
from ..core.config import settings
from ..notify.feishu import FeishuClient
from ..services.alert_service import (
    # handler 直接依赖
    AlertItem,
    AlertmanagerWebhook,
    # 向后兼容转发（测试 / main 依赖 app.api.alerts 的符号）
    _build_diagnosis_prompt,
    _build_rediagnosis_prompt,
    _build_summary_prompt,
    _check_planned_change,
    _cooldown_key,
    _drain_pending_tasks,
    _enqueue_and_wake,
    _extract_service,
    _fmt_dt,
    _generate_incident_summary,
    _get_feishu_client,
    _impact_priority,
    _lookup_runbook,
    _parse_action_items,
    _process_diagnosis_task,
    _route_alert_to_incident,
    _select_culprit,
    _service_criticality,
    _severity_ok,
    _severity_rank,
    _should_diagnose,
    diagnosis_worker_loop,
    enqueue_diagnosis_tasks,
    handle_resolved_alerts,
    reset_feishu_client,
    stale_incident_sweep_loop,
)  # noqa: F401,F811

router = APIRouter(prefix="/api/alerts", tags=["告警通知"])


def _verify_webhook_secret(request: Request) -> None:
    """验证 webhook 共享密钥（防伪造告警 / 刷飞书 API 配额）

    - 服务端未配置 ALERT_WEBHOOK_SECRET → 503 拒绝处理（secure by default）
    - 密钥支持两种携带方式（Alertmanager 原生支持后者）：
      1. Authorization: Bearer <secret>（alertmanager http_config.authorization）
      2. X-Webhook-Secret: <secret> 自定义头
    - 不匹配 → 401，用 hmac.compare_digest 防时序侧信道
    """
    secret = getattr(settings, "ALERT_WEBHOOK_SECRET", "") or ""
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="告警 webhook 鉴权未配置（ALERT_WEBHOOK_SECRET 缺失），拒绝处理",
        )

    provided = request.headers.get("X-Webhook-Secret") or ""
    if not provided:
        auth_header = request.headers.get("Authorization") or ""
        if auth_header.startswith("Bearer "):
            provided = auth_header[7:]

    if not provided or not hmac.compare_digest(provided, secret):
        logger.warning("告警 webhook 鉴权失败（密钥无效或缺失）")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="webhook 密钥无效",
        )


@router.post("/webhook")
async def alertmanager_webhook(payload: AlertmanagerWebhook, request: Request,
                               background_tasks: BackgroundTasks):
    """接收 Alertmanager 推送的告警，转发到飞书

    Alertmanager 配置 webhook_configs 后，告警触发/恢复时自动 POST 到本端点。
    本端点先验证共享密钥（ALERT_WEBHOOK_SECRET），再解析 alerts 列表，
    构建飞书卡片消息，发送给配置的接收人。

    告警卡片发送成功后，对 firing 告警自动触发 Agent 诊断（BackgroundTasks），
    诊断报告作为第二张卡片推送（告警风暴防护：fingerprint 冷却 + 全局并发 1）。
    """
    _verify_webhook_secret(request)

    alerts = payload.alerts
    if not alerts:
        logger.info("收到空告警列表，跳过")
        return {"status": "ok", "sent": 0, "reason": "empty_alerts"}

    logger.info(
        f"收到 Alertmanager 告警推送: status={payload.status}, "
        f"alerts={len(alerts)}, groupKey={payload.groupKey}"
    )

    alerts_data = [a.model_dump() for a in alerts]
    for a in alerts_data:
        a["overall_status"] = payload.status
    alert_names = [a.labels.get("alertname", "?") for a in alerts]

    # A4 降噪率口径：原始告警接收计数（与 ops_incidents_created_total 组成压缩比）
    try:
        from ..observability.metrics import get_metrics
        for a in alerts_data:
            get_metrics().increment("ops_alerts_received_total", 1, labels={
                "status": a.get("status", "firing")})
    except Exception:  # 指标采集失败不影响告警接收
        pass

    # 自动诊断（Incident 生命周期 + 持久化任务表）
    # 必须在飞书通知之前入队——飞书通知失败不应阻止事故创建
    if settings.ALERT_AUTO_DIAGNOSIS_ENABLED:
        if payload.status == "firing":
            firing = [a for a in alerts_data if a.get("status") == "firing"]
            if firing:
                background_tasks.add_task(_enqueue_and_wake, firing)
        elif payload.status == "resolved":
            background_tasks.add_task(handle_resolved_alerts, alerts_data)

    # 飞书通知（非阻断：失败只记日志，不影响事故创建和诊断）
    client = _get_feishu_client()
    open_id = getattr(settings, "FEISHU_ALERT_OPEN_ID", "") or ""
    feishu_sent = False
    if client is not None and open_id:
        try:
            card = FeishuClient.build_alert_card(alerts_data)
            ok = client.send_card(open_id, card)
            if ok:
                feishu_sent = True
                logger.info(f"告警通知已发送到飞书: {len(alerts)} 条 - {alert_names}")
            else:
                logger.warning(f"飞书告警通知发送失败（不影响事故处理）: alerts={len(alerts)}")
        except Exception as e:
            logger.warning("飞书告警通知异常（不影响事故处理）: {}", e)
    else:
        logger.warning("飞书通知未配置（FEISHU_APP_ID/SECRET/OPEN_ID 缺失），跳过通知")

    return {
        "status": "ok",
        "sent": len(alerts),
        "alert_names": alert_names,
        "feishu_sent": feishu_sent,
    }


@router.get("/test")
async def test_feishu_notification(current_user: UserResponse = Depends(require_admin)):
    """测试飞书通知链路（手动触发一条测试告警，仅管理员）

    用于验证 Bridge 服务和飞书应用配置是否正确。
    不依赖 Alertmanager，直接构造一条假告警发到飞书。
    """
    client = _get_feishu_client()
    open_id = getattr(settings, "FEISHU_ALERT_OPEN_ID", "") or ""
    if client is None or not open_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="飞书通知未配置（FEISHU_APP_ID/SECRET/OPEN_ID 缺失）",
        )

    # 构造一条测试告警
    test_alert = {
        "status": "firing",
        "overall_status": "firing",
        "labels": {
            "alertname": "TestAlert",
            "severity": "warning",
            "instance": "host",
            "category": "test",
        },
        "annotations": {
            "summary": "这是一条测试告警（Bridge 链路验证）",
            "description": "如果你在飞书收到这条卡片消息，说明告警通知链路已打通",
        },
        "startsAt": "2026-08-07T03:00:00Z",
        "fingerprint": "test-001",
    }
    card = FeishuClient.build_alert_card([test_alert])
    ok = client.send_card(open_id, card)
    if not ok:
        raise HTTPException(status_code=502, detail="飞书发消息失败")
    return {"status": "ok", "message": "测试告警已发送到飞书，请检查是否收到"}


__all__ = [
    # 端点
    "router",
    "alertmanager_webhook", "test_feishu_notification",
    "_verify_webhook_secret",
    # 模型
    "AlertItem", "AlertmanagerWebhook",
    # 转发：业务引擎符号（业务已下沉，测试 / main 依赖 app.api.alerts 的符号）
    "reset_feishu_client",
    "diagnosis_worker_loop", "stale_incident_sweep_loop",
    "enqueue_diagnosis_tasks", "handle_resolved_alerts",
    "_build_diagnosis_prompt", "_build_rediagnosis_prompt",
    "_build_summary_prompt", "_check_planned_change", "_cooldown_key",
    "_drain_pending_tasks", "_enqueue_and_wake", "_extract_service", "_fmt_dt",
    "_generate_incident_summary", "_get_feishu_client", "_impact_priority",
    "_lookup_runbook", "_parse_action_items", "_process_diagnosis_task",
    "_route_alert_to_incident", "_select_culprit", "_service_criticality",
    "_severity_ok", "_severity_rank", "_should_diagnose",
]