"""
Alertmanager Webhook Bridge API

接收 Alertmanager 推送的告警，转换为飞书卡片消息转发到指定用户。

链路：Prometheus 告警规则触发 → Alertmanager 聚合 → POST /api/alerts/webhook
    → Bridge 解析告警 → 用飞书自建应用发卡片消息给用户

配置（环境变量）：
- FEISHU_APP_ID:     飞书自建应用 app_id
- FEISHU_APP_SECRET: 飞书自建应用 app_secret
- FEISHU_ALERT_OPEN_ID: 接收告警的用户 open_id

未配置时端点返回 503，避免 alertmanager 重复推送无效请求。
"""
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, Request, HTTPException, status
from pydantic import BaseModel, Field
from loguru import logger

from ..core.config import settings
from ..notify.feishu import FeishuClient

router = APIRouter(prefix="/api/alerts", tags=["告警通知"])


# ========== 飞书客户端单例（延迟初始化）==========
# 首次请求时创建，配置缺失则返回 None，端点返回 503
_feishu_client: Optional[FeishuClient] = None


def _get_feishu_client() -> Optional[FeishuClient]:
    """获取飞书客户端单例（配置缺失返回 None）"""
    global _feishu_client
    if _feishu_client is not None:
        return _feishu_client
    app_id = getattr(settings, "FEISHU_APP_ID", "") or ""
    app_secret = getattr(settings, "FEISHU_APP_SECRET", "") or ""
    if not app_id or not app_secret:
        return None
    _feishu_client = FeishuClient(app_id=app_id, app_secret=app_secret)
    logger.info("飞书通知客户端初始化完成")
    return _feishu_client


def reset_feishu_client():
    """重置客户端（配置变更后用，测试场景）"""
    global _feishu_client
    _feishu_client = None


# ========== Alertmanager webhook payload 模型 ==========
# 文档：https://prometheus.io/docs/alerting/latest/configuration/#webhook_config
class AlertItem(BaseModel):
    """单条告警"""
    status: str = Field(default="firing", description="firing / resolved")
    labels: Dict[str, str] = Field(default_factory=dict)
    annotations: Dict[str, str] = Field(default_factory=dict)
    startsAt: str = Field(default="")
    endsAt: str = Field(default="")
    generatorURL: str = Field(default="")
    fingerprint: str = Field(default="")


class AlertmanagerWebhook(BaseModel):
    """Alertmanager webhook 推送的完整 payload"""
    version: str = Field(default="4")
    groupKey: str = Field(default="")
    status: str = Field(default="firing", description="整体状态 firing / resolved")
    receiver: str = Field(default="")
    groupLabels: Dict[str, str] = Field(default_factory=dict)
    commonLabels: Dict[str, str] = Field(default_factory=dict)
    commonAnnotations: Dict[str, str] = Field(default_factory=dict)
    externalURL: str = Field(default="")
    alerts: List[AlertItem] = Field(default_factory=list)


# ========== 端点 ==========
@router.post("/webhook")
async def alertmanager_webhook(payload: AlertmanagerWebhook):
    """接收 Alertmanager 推送的告警，转发到飞书

    Alertmanager 配置 webhook_configs 后，告警触发/恢复时自动 POST 到本端点。
    本端点解析 alerts 列表，构建飞书卡片消息，发送给配置的接收人。
    """
    # 飞书配置检查
    client = _get_feishu_client()
    open_id = getattr(settings, "FEISHU_ALERT_OPEN_ID", "") or ""
    if client is None or not open_id:
        logger.warning("飞书通知未配置（FEISHU_APP_ID/SECRET/OPEN_ID 缺失），丢弃告警")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="飞书通知未配置",
        )

    alerts = payload.alerts
    if not alerts:
        logger.info("收到空告警列表，跳过")
        return {"status": "ok", "sent": 0, "reason": "empty_alerts"}

    logger.info(
        f"收到 Alertmanager 告警推送: status={payload.status}, "
        f"alerts={len(alerts)}, groupKey={payload.groupKey}"
    )

    # 构建卡片（把整体 status 传进去用于标题颜色判断）
    alerts_data = [a.model_dump() for a in alerts]
    # 标记整体状态（firing/resolved），用于卡片标题
    for a in alerts_data:
        a["overall_status"] = payload.status
    card = FeishuClient.build_alert_card(alerts_data)

    # 发送
    ok = client.send_card(open_id, card)
    if not ok:
        logger.error(f"飞书告警通知发送失败: alerts={len(alerts)}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="飞书发消息失败",
        )

    alert_names = [a.labels.get("alertname", "?") for a in alerts]
    logger.info(f"告警通知已发送到飞书: {len(alerts)} 条 - {alert_names}")
    return {
        "status": "ok",
        "sent": len(alerts),
        "alert_names": alert_names,
    }


@router.get("/test")
async def test_feishu_notification():
    """测试飞书通知链路（手动触发一条测试告警）

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
