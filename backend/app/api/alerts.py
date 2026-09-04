"""
Alertmanager Webhook Bridge API

接收 Alertmanager 推送的告警，转换为飞书卡片消息转发到指定用户。

链路：Prometheus 告警规则触发 → Alertmanager 聚合 → POST /api/alerts/webhook
    → Bridge 验证共享密钥 → 解析告警 → 用飞书自建应用发卡片消息给用户

配置（环境变量）：
- FEISHU_APP_ID:     飞书自建应用 app_id
- FEISHU_APP_SECRET: 飞书自建应用 app_secret
- FEISHU_ALERT_OPEN_ID: 接收告警的用户 open_id
- ALERT_WEBHOOK_SECRET: webhook 共享密钥（Alertmanager 侧通过
  http_config.authorization.credentials 以 Bearer 头携带）。
  未配置时 webhook 端点拒绝处理（503），防止伪造告警/刷飞书 API。

未配置飞书时端点返回 503，避免 alertmanager 重复推送无效请求。
"""
import asyncio
import hmac
import re
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from loguru import logger
from pydantic import BaseModel, Field

from ..core.auth import UserResponse, require_admin
from ..core.config import settings
from ..core.database import db
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


# ========== 告警自动诊断（Incident 生命周期：初诊 → 重诊 → 恢复摘要）==========
# 诊断挂在"事故实体"上而非单条告警上：
#   新告警(fingerprint 未见) → 创建 incident → 全量初诊
#   新告警归入活跃 incident（同 service + 时间窗）/ severity 升级 → 增量重诊
#   重复 firing（心跳）→ 低置信 + 间隔满足时重诊
#   resolved → 安静期确认不复燃 → 生成恢复摘要（含复盘草稿）
# 成本护栏：重诊次数硬上限、severity 门槛、抖动复用、增量输入（1-2 次调用而非全量）
# 风暴防护：全局并发 1（Semaphore，风暴时排队而非并发打爆 LLM 配额）
_diag_semaphore = asyncio.Semaphore(1)

_SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
_TRIGGER_PRIORITY = {"initial": 0, "escalation": 1, "repeat": 2}


def _route_metric(level: str):
    """路由级别计数（误路由可测量性）：fingerprint/flap/join/new"""
    try:
        from ..observability.metrics import get_metrics
        get_metrics().increment("ops_alert_route_total", 1, labels={"level": level})
    except Exception:
        pass


def _cooldown_key(alert: Dict[str, Any]) -> str:
    """告警指纹键：fingerprint 优先（Alertmanager 天然唯一），兜底 alertname:instance"""
    fp = alert.get("fingerprint") or ""
    if fp:
        return f"fp:{fp}"
    labels = alert.get("labels", {})
    return f"{labels.get('alertname', 'unknown')}:{labels.get('instance', '-')}"


def _severity_rank(severity: str) -> int:
    return _SEVERITY_RANK.get((severity or "").lower(), 1)


def _severity_ok(alert: Dict[str, Any]) -> bool:
    """severity 门槛：低于阈值（默认 warning）的告警不进 Agent（零成本拦截噪声）"""
    rank = _severity_rank(alert.get("labels", {}).get("severity", "warning"))
    return rank >= _severity_rank(settings.ALERT_MIN_SEVERITY)


def _extract_service(alert: Dict[str, Any]) -> str:
    """从告警 labels 提取服务名（service > job > instance 主机名剥离端口/序号）

    ALERT_SERVICE_MAP（JSON）可对任意候选值做静态映射覆盖，
    用于告警规则无 service 标签的环境（如 node-exporter → host-infra）。
    """
    import json as _json

    labels = alert.get("labels", {})
    instance = labels.get("instance", "")
    host = instance.split(":", 1)[0] if ":" in instance else instance
    host = re.sub(r"[-_]\d+$", "", host).strip()
    candidates = [labels.get("service"), labels.get("job"), host]

    mapping: Dict[str, str] = {}
    if getattr(settings, "ALERT_SERVICE_MAP", None):
        try:
            mapping = _json.loads(settings.ALERT_SERVICE_MAP)
        except Exception:
            logger.warning("ALERT_SERVICE_MAP 不是合法 JSON，忽略")
    # 映射优先：按候选顺序查映射表，命中即返回映射值
    for cand in candidates:
        if cand and cand in mapping:
            return mapping[cand]
    # 无映射：返回第一个非空候选
    for cand in candidates:
        if cand:
            return cand
    return ""


# ========== B3 服务重要性矩阵（影响度 × 紧急度 → P1-P4） ==========
#
# ITIL 共识："优先级 = 影响度 × 紧急度"——业界事故前写死分级矩阵。
# 影响度 = 服务关键度（critical/normal/low，来自 SERVICE_CRITICALITY 配置）；
# 紧急度 = 告警级别（critical/warning/info，来自告警 labels.severity）。
#
# 矩阵（行=影响度，列=紧急度 severity）：
#            info  warning  critical
# critical    P3     P2       P1
# normal      P4     P3       P2
# low         P4     P4       P3
#
# 消费点：诊断卡片显示 P 级、escalation 判定用 P 级而非原始 severity
# （P1/P2 触发升级重诊门槛低于 P3/P4——核心服务 warning 等同边缘服务 critical）

_CRITICALITY_RANK = {"low": 0, "normal": 1, "critical": 2}
# P 级矩阵：[criticality_rank][severity_rank] → P1-P4
# 列顺序 = _SEVERITY_RANK: [info=0, warning=1, critical=2]
_PRIORITY_MATRIX = [
    # low:        info→P4, warning→P4, critical→P3
    [4, 4, 3],
    # normal:     info→P4, warning→P3, critical→P2
    [4, 3, 2],
    # critical:   info→P3, warning→P2, critical→P1
    [3, 2, 1],
]


def _service_criticality(service: str) -> str:
    """从 SERVICE_CRITICALITY 配置查服务关键度（默认 normal）"""
    import json as _json

    raw = getattr(settings, "SERVICE_CRITICALITY", None)
    if not raw:
        return "normal"
    try:
        mapping = _json.loads(raw)
    except Exception:
        logger.warning("SERVICE_CRITICALITY 不是合法 JSON，按 normal 处理")
        return "normal"
    return mapping.get(service or "", "normal")


def _impact_priority(service: str, severity: str) -> str:
    """B3 影响等级 = 服务关键度 × 告警级别 → P1-P4

    Args:
        service: 服务名（查 SERVICE_CRITICALITY 配置）
        severity: 告警级别（critical/warning/info）

    Returns:
        "P1" / "P2" / "P3" / "P4"
    """
    crit_rank = _CRITICALITY_RANK.get(
        _service_criticality(service).lower(), 1,
    )
    sev_rank = _severity_rank(severity)
    # severity_rank 可能超出 0-2（未知 severity 默认 1=warning），clamp 到 0-2
    sev_rank = max(0, min(2, sev_rank))
    p_level = _PRIORITY_MATRIX[crit_rank][sev_rank]
    return f"P{p_level}"


# ========== B4 主嫌疑告警标记（culprit） ==========
#
# BigPanda 根因打分的规则版（Open Box 可解释）——诊断完成后规则选主嫌疑：
# 1. 候选 = incident 内所有告警（fingerprint × alertname × severity）
# 2. 打分 = severity 权重 + root_cause 关键词与 alertname 匹配加分
# 3. 同分时取 fingerprint 最早加入的（first_seen 最早）
#
# 写入 incidents.culprit_fingerprint，卡片标注"🎯 主嫌疑告警"。
# 价值：多告警风暴时响应者一眼定位最可能的触发源，而非逐一排查。

# root_cause 关键词 → 告警名模式映射（命中加分）
# 常见运维根因关键词与告警名的关联（可扩展）
_CULPRIT_KEYWORD_MAP = {
    "延迟": ["Latency", "Slow", "Duration", "ResponseTime"],
    "慢查询": ["Latency", "Slow", "Duration"],
    "超时": ["Timeout", "Latency", "Duration"],
    "连接池": ["Pool", "Connection", "Exhausted"],
    "内存": ["Memory", "OOM", "Heap"],
    "CPU": ["CPU", "Load", "HighCpu"],
    "磁盘": ["Disk", "Storage", "Space"],
    "错误率": ["Error", "ErrorRate", "5xx", "Failed"],
    "5xx": ["Error", "5xx", "ErrorRate"],
    "不可用": ["Down", "Unavailable", "Instance"],
    "重启": ["Restart", "Down", "Instance"],
}


def _select_culprit(incident: Dict[str, Any], root_cause: str) -> Optional[str]:
    """B4 主嫌疑告警选择——规则打分（Open Box 可解释）

    Args:
        incident: 事故文档（含 alert_details: [{fingerprint, alertname, severity, first_seen_at}]）
        root_cause: 最近一次诊断的根因文本

    Returns:
        主嫌疑告警的 fingerprint，或 None（无告警详情/打分全零时）
    """
    alert_details = incident.get("alert_details") or []
    if not alert_details:
        # 兜底：无 alert_details 时取第一个 fingerprint（旧数据兼容）
        fps = incident.get("fingerprints") or []
        return fps[0] if fps else None

    rc_plain = root_cause or ""

    best_fp = None
    best_score = -1
    for alert in alert_details:
        fp = alert.get("fingerprint", "")
        alertname = alert.get("alertname", "")
        severity = alert.get("severity", "warning")
        first_seen = alert.get("first_seen_at")

        # 基础分：severity 权重（critical=10, warning=5, info=1）
        score = _severity_rank(severity) * 5 + 1

        # 关键词匹配加分：root_cause 含关键词 → alertname 命中关联模式加分
        for keyword, patterns in _CULPRIT_KEYWORD_MAP.items():
            if keyword in rc_plain:
                for pat in patterns:
                    if pat.lower() in alertname.lower():
                        score += 5
                        break

        # 时间最早加分（越小越早，加 0-3 分）
        try:
            from datetime import datetime as _dt
            if first_seen:
                fs = first_seen if isinstance(first_seen, _dt) else _dt.fromisoformat(str(first_seen))
                # 距今越早分越高（最早告警 +3，最晚 +0）
                now = _dt.now()
                age_seconds = (now - fs).total_seconds()
                if age_seconds > 0:
                    # 最早的多加分（用相对值，这里简化：age 越大分越高，上限 3）
                    score += min(3, age_seconds / 3600)
        except Exception:
            pass

        if score > best_score:
            best_score = score
            best_fp = fp

    return best_fp


def _lookup_runbook(service: str) -> Optional[Dict[str, Any]]:
    """诊断卡片附 runbook/SOP（PagerDuty runbook-attach-to-service 模式）

    告警服务命中后检索处置预案 top1 直接附卡片——响应者第一步就有 SOP 可查。
    不经过 LLM（零推理成本）；知识库未初始化/检索失败返回 None（宁缺勿错）。
    """
    try:
        from ..shared_services import get_knowledge_store
        store = get_knowledge_store()
        if store is None:
            return None
        meta_filter: Optional[Dict[str, Any]] = {"doc_type": "sop"}
        if service and service != "unknown":
            meta_filter["service"] = service
        results = store.hybrid_search_parent_child(
            f"{service or '服务'} 故障处置预案", top_k=1,
            rewrite_query=False, metadata_filter=meta_filter,
        )
        if not results:
            results = store.hybrid_search_parent_child(
                "故障处置预案 SOP", top_k=1,
                rewrite_query=False, metadata_filter={"doc_type": "sop"},
            )
        if results:
            r = results[0]
            meta = r.get("metadata") or {}
            return {
                "title": r.get("title") or meta.get("title", ""),
                "doc_id": meta.get("document_id", ""),
                "score": round(r.get("score", 0), 3),
            }
    except Exception as e:
        logger.debug(f"runbook 检索失败（不影响诊断卡片）: {e}")
    return None


def _build_diagnosis_prompt(alert: Dict[str, Any]) -> str:
    """初诊输入：从告警构造诊断请求（现象描述风格，触发 5 阶段诊断工作流）"""
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    parts = [
        "线上监控告警触发，请立即诊断根因并给出处置方案。",
        f"告警名称: {labels.get('alertname', 'Unknown')}",
        f"严重级别: {labels.get('severity', '-')}",
        f"实例: {labels.get('instance', '-')}",
    ]
    if labels.get("service"):
        parts.append(f"服务: {labels['service']}")
    if annotations.get("summary"):
        parts.append(f"现象摘要: {annotations['summary']}")
    if annotations.get("description"):
        parts.append(f"详细信息: {annotations['description']}")
    if alert.get("startsAt"):
        parts.append(f"开始时间: {alert['startsAt']}")
    return "\n".join(parts)


def _build_rediagnosis_prompt(incident: Dict[str, Any], new_alerts: List[Dict[str, Any]]) -> str:
    """重诊输入（增量）：上次诊断结论 + 新证据，要求只回答确认/修正/推翻

    这是重诊成本可控的关键：1-2 次 LLM 调用替代全量 ReAct 重跑。
    """
    history = incident.get("diagnosis_history") or []
    last = history[-1] if history else {}
    parts = [
        "你之前诊断的事故有了新进展，请基于新证据更新诊断结论。",
        f"事故编号: {incident.get('incident_id')}",
        f"关联告警: {', '.join(incident.get('alertnames') or [])}",
        f"最高严重级别: {incident.get('max_severity', '-')}",
    ]
    if last:
        parts += [
            f"上次诊断（{last.get('trigger', '?')}）结论: {last.get('root_cause', '（无记录）')}",
            f"上次置信度: {last.get('confidence_level', 'unknown')}",
        ]
    # B5 维护窗口感知：故障窗内存在计划内变更 → 提示区分变更引发 vs 独立故障
    if incident.get("planned_change") and incident.get("recent_changes"):
        parts.append(
            f"⚠️ 存在计划内变更: {incident['recent_changes']}。"
            "请重点区分本次故障是变更引发（变更 → 影响的因果链）还是独立故障（与变更无关）。"
        )
    parts.append("新证据:")
    for a in new_alerts:
        labels = a.get("labels", {})
        annotations = a.get("annotations", {})
        parts.append(
            f"- [{'/'.join(filter(None, [labels.get('alertname'), labels.get('severity')]))}] "
            f"{annotations.get('summary') or annotations.get('description') or '（无描述）'}"
        )
    parts.append(
        "请只做三选一并说明理由：确认（维持原结论）/ 修正（调整根因）/ 推翻（原结论错误，给出新根因）。"
        "输出仍按诊断报告结构（### 现象 / 证据 / 根因分析 / 处置方案 / 置信度）。"
    )
    return "\n".join(parts)


def _build_summary_prompt(incident: Dict[str, Any]) -> str:
    """恢复摘要输入：时间线 + 历次诊断演变 + 复盘要点"""

    parts = [
        "线上事故已恢复，请生成事故摘要与复盘要点。",
        f"事故编号: {incident.get('incident_id')}",
        f"关联告警: {', '.join(incident.get('alertnames') or [])}",
        f"最高严重级别: {incident.get('max_severity', '-')}",
        f"开始时间: {_fmt_dt(incident.get('first_seen_at'))}",
        f"恢复时间: {_fmt_dt(incident.get('resolved_at'))}",
        "历次诊断结论演变:",
    ]
    for d in incident.get("diagnosis_history") or []:
        parts.append(
            f"- [{_fmt_dt(d.get('at'))}] ({d.get('trigger', '?')}) "
            f"{d.get('root_cause') or d.get('content', '')[:120]}"
        )
    # B5 维护窗口感知：复盘时标注是否存在计划内变更（变更引发 vs 独立故障的复盘要点）
    if incident.get("planned_change") and incident.get("recent_changes"):
        parts.append(f"⚠️ 事故窗口内存在计划内变更: {incident['recent_changes']}")
        parts.append("复盘请明确：本次故障是变更引发还是独立故障；若变更引发，评估变更流程改进项。")
    parts.append(
        "请输出（blameless 复盘结构，对事不对人）："
        "### 事故时间线（检测/响应/恢复的关键时刻）"
        " / ### 影响（受影响服务/接口/告警级别/持续时长，能量化则量化）"
        " / ### 最可能根因（综合历次诊断，标注置信度）"
        " / ### 处置回顾（恢复动作是否与根因吻合）"
        " / ### 行动项（按【检测】【预防】【缓解】三类列出，每条格式："
        "- 【类别】行动描述（负责人: X，期限: Y），无明确负责人写 待定；"
        "质量标准：完成它是否会改变系统）"
        " / ### 经验教训（What went well / What went wrong / Where we got lucky）"
    )
    parts.append(
        "\n【强制要求】最后必须单独输出 ### 行动项 章节，至少 3 条，"
        "每条一行，格式严格如下（示例）："
        "\n### 行动项"
        "\n- 【检测】为支付接口增加 P95 延迟告警（负责人: 张三，期限: 2026-09-10）"
        "\n- 【预防】给连接池饱和度加入提前扩容阈值（负责人: 待定，期限: 2026-09-15）"
        "\n- 【缓解】准备连接池耗尽的快速止血 runbook（负责人: 李四，期限: 2026-09-08）"
    )
    return "\n".join(parts)


def _parse_action_items(content: str) -> List[Dict[str, Any]]:
    """从恢复摘要解析结构化行动项（blameless postmortem 的 Action Items）

    业界质量标准：每个行动项有负责人与期限并在工单系统跟踪——
    "行动项不跟踪，复盘等于白写"。解析宽容：缺负责人/期限记 待定。
    """
    import re as _re
    if not content:
        return []
    items: List[Dict[str, Any]] = []
    section = _re.search(r"#{2,4}\s*行动项(.*?)(?=\n#{2,4}|\Z)", content, _re.DOTALL)
    if not section:
        return items
    for line in section.group(1).splitlines():
        line = line.strip()
        if not line.startswith(("-", "•", "*")):
            continue
        text = line.lstrip("-•* ").strip()
        if not text:
            continue
        category = "预防"
        cat_match = _re.match(r"[【\[]([^】\]]+)[】\]]\s*(.*)", text)
        if cat_match:
            category = cat_match.group(1).strip()
            text = cat_match.group(2).strip()
        owner_m = _re.search(r"负责人[:：]\s*([^，,；;）)]+)", text)
        deadline_m = _re.search(r"(?:期限|deadline)[:：]\s*([^，,；;）)]+)", text)
        clean = _re.sub(r"（负责人.*?）|\(owner[^）]*\)", "", text).strip(" ；;")
        if not clean:
            continue
        items.append({
            "item": clean,
            "category": category if category in ("检测", "预防", "缓解") else "预防",
            "owner": owner_m.group(1).strip() if owner_m else "待定",
            "deadline": deadline_m.group(1).strip() if deadline_m else "待定",
            "status": "pending",
        })
    return items


def _fmt_dt(value) -> str:
    """datetime → 可读字符串（None 安全）"""
    if not value:
        return "-"
    try:
        return value.strftime("%Y-%m-%d %H:%M:%S") if hasattr(value, "strftime") else str(value)
    except Exception:
        return str(value)


# ---- Incident 路由 ----

async def _route_alert_to_incident(alert: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    """将一条 firing 告警路由到事故实体，返回 (incident, 触发类型)

    触发类型：initial（新建事故）/ escalation（归入活跃事故）/ repeat（已知事故心跳/复燃）
    """
    fp = _cooldown_key(alert)
    alertname = alert.get("labels", {}).get("alertname", "Unknown")
    severity = alert.get("labels", {}).get("severity", "warning")
    service = _extract_service(alert)

    # 1. 活跃/安静期事故包含此 fingerprint → 心跳（安静期事故顺手复燃）
    incident = await db.find_incident_by_fingerprint(fp, statuses=["active", "resolving"])
    if incident:
        if incident.get("status") == "resolving":
            # 安静期内复燃：回到 active，不产生新初诊
            await db.update_incident_fields(incident["incident_id"], {
                "status": "active", "resolved_at": None,
            })
            logger.info(f"事故 {incident['incident_id']} 安静期内复燃，重新激活")
        _route_metric("fingerprint")
        return incident, "repeat"

    # 2. 最近 resolved 的事故包含此 fingerprint（抖动）→ 重新打开
    incident = await db.find_incident_by_fingerprint(
        fp, resolved_within_seconds=settings.INCIDENT_FLAPPING_WINDOW,
    )
    if incident:
        resolved_fps = [f for f in (incident.get("resolved_fps") or []) if f != fp]
        await db.update_incident_fields(incident["incident_id"], {
            "status": "active", "resolved_at": None,
            "resolved_fps": resolved_fps,
        })
        incident = await db.get_incident(incident["incident_id"])
        logger.info(f"事故 {incident['incident_id']} 抖动复燃（{settings.INCIDENT_FLAPPING_WINDOW}s 内），重新打开")
        _route_metric("flap")
        return incident, "repeat"

    # 3. 同服务的活跃事故（关联窗口内）→ 归入（升级信号）
    incident = await db.find_active_incident_by_service(
        service, within_seconds=settings.INCIDENT_SERVICE_JOIN_WINDOW,
    )
    if incident and fp not in (incident.get("fingerprints") or []):
        incident = await db.add_incident_fingerprint(incident["incident_id"], fp, alertname, severity)
        # B4 主嫌疑：归入时追加告警详情（供诊断后 culprit 打分）
        from datetime import datetime as _dt_join
        alert_details = list(incident.get("alert_details") or [])
        alert_details.append({
            "fingerprint": fp,
            "alertname": alertname,
            "severity": severity,
            "first_seen_at": _dt_join.now(),
        })
        await db.update_incident_fields(incident["incident_id"], {"alert_details": alert_details})
        incident = await db.get_incident(incident["incident_id"])
        logger.info(f"告警 {alertname} 归入事故 {incident['incident_id']}（service={service}）")
        _route_metric("join")
        return incident, "escalation"

    # 4. 新建事故
    import uuid as _uuid
    from datetime import datetime as _dt
    incident_id = f"INC-AUTO-{_uuid.uuid4().hex[:8].upper()}"
    # B5 维护窗口感知：检查故障服务是否有计划内变更（变更 → 故障的因果线索）
    planned_change, change_summary = _check_planned_change(service or "")
    incident = {
        "incident_id": incident_id,
        "status": "active",
        "service": service or "unknown",
        "fingerprints": [fp],
        "resolved_fps": [],
        "alertnames": [alertname] if alertname else [],
        "max_severity": severity,
        # B3 影响等级：服务关键度 × 告警级别 → P1-P4（escalation/卡片消费）
        "impact_priority": _impact_priority(service or "unknown", severity),
        # B4 主嫌疑：告警详情（供诊断后 culprit 打分）
        "alert_details": [{
            "fingerprint": fp,
            "alertname": alertname,
            "severity": severity,
            "first_seen_at": _dt.now(),
        }],
        "first_seen_at": _dt.now(),
        "last_seen_at": _dt.now(),
        "resolved_at": None,
        "diag_count": 0,
        "last_diag_at": None,
        "last_confidence_level": "unknown",
        "diagnosis_history": [],
        "summary": None,
        # B5: 计划内变更标记（故障窗内有变更 → 诊断时区分变更引发 vs 独立故障）
        "planned_change": planned_change,
        "recent_changes": change_summary,
    }
    try:
        await db.save_incident(incident)
    except Exception as e:
        # 并发创建同服务活跃事故被唯一索引拒绝 → 转为归入（B2 保障）
        if "DuplicateKeyError" in type(e).__name__ or "E11000" in str(e):
            incident = await db.find_active_incident_by_service(
                service, within_seconds=settings.INCIDENT_SERVICE_JOIN_WINDOW)
            if incident and fp not in (incident.get("fingerprints") or []):
                incident = await db.add_incident_fingerprint(
                    incident["incident_id"], fp, alertname, severity)
                # B4 主嫌疑：并发归入也追加告警详情
                from datetime import datetime as _dt_dup
                alert_details = list(incident.get("alert_details") or [])
                alert_details.append({
                    "fingerprint": fp, "alertname": alertname,
                    "severity": severity, "first_seen_at": _dt_dup.now(),
                })
                await db.update_incident_fields(
                    incident["incident_id"], {"alert_details": alert_details})
                incident = await db.get_incident(incident["incident_id"])
                _route_metric("join")
                logger.warning(f"并发创建撞唯一索引，转为归入事故 {incident['incident_id']}")
                return incident, "escalation"
            if incident:
                _route_metric("fingerprint")
                return incident, "repeat"
        raise
    _route_metric("new")
    try:
        from ..observability.metrics import get_metrics
        get_metrics().increment("ops_incidents_created_total", 1)
    except Exception:
        pass
    logger.info(f"新建事故 {incident_id}: service={service or 'unknown'}, 告警={alertname}")
    return incident, "initial"


def _check_planned_change(service: str) -> Tuple[bool, str]:
    """B5 维护窗口感知：检查事故服务在故障时间窗内是否有计划内变更

    复用 get_recent_changes 工具的变更事件数据源（同一份变更台账）。
    若存在近期变更，返回 (True, 变更摘要) 供 incident 标记 planned_change
    + 诊断 prompt 注入"区分变更引发 vs 独立故障"提示。

    注意：当前变更数据为 mock（与 get_recent_changes 工具一致）。生产化时
    替换为真实变更源（CMDB/CI-CD webhook/变更管理平台 API）即可，调用方无需改动。

    Args:
        service: 服务名

    Returns:
        (has_planned_change, changes_summary)
        - has_planned_change: True 表示故障窗口内存在计划内变更
        - changes_summary: 变更摘要文本（无变更时为空字符串）
    """
    if not service:
        return False, ""
    try:
        # 复用 get_recent_changes 工具的同一数据源（避免数据分裂）
        import json as _json

        from ..langgraph_agent.tools import get_recent_changes
        # 工具返回 JSON 字符串，解析取 changes
        raw = get_recent_changes.invoke({"service": service, "hours": 24})
        data = _json.loads(raw) if isinstance(raw, str) else raw
        changes = data.get("changes") or []
        if not changes:
            return False, ""
        # 取最近一条变更作为摘要（故障窗内最相关的变更）
        latest = changes[0]
        summary = f"[{latest.get('type', '?')}] {latest.get('description', '')}（{latest.get('time', '?')}）"
        return True, summary
    except Exception as e:
        logger.debug(f"查询计划内变更失败（不影响路由）: {e}")
        return False, ""


def _should_diagnose(incident: Dict[str, Any], trigger: str) -> Tuple[bool, str]:
    """诊断资格判定（成本护栏），返回 (是否诊断, 原因)"""
    diag_count = incident.get("diag_count") or 0

    # 硬上限：初诊 1 次 + 重诊 DIAG_MAX_REDIAG_PER_INCIDENT 次
    if diag_count >= 1 + settings.DIAG_MAX_REDIAG_PER_INCIDENT:
        return False, f"重诊次数已达上限（{diag_count} 次）"

    if trigger == "initial":
        return True, ""

    # 最小间隔（升级/心跳共用）：上次诊断后 N 秒内不再诊断
    last_diag_at = incident.get("last_diag_at")
    if last_diag_at:
        from datetime import datetime as _dt
        if isinstance(last_diag_at, str):
            last_diag_at = _dt.fromisoformat(last_diag_at)
        elapsed = (_dt.now() - last_diag_at).total_seconds()
        if elapsed < settings.ALERT_DIAG_COOLDOWN_SECONDS:
            return False, f"距上次诊断 {int(elapsed)}s < 冷却 {settings.ALERT_DIAG_COOLDOWN_SECONDS}s"

    # 心跳（repeat）：上次结论已高置信则不重诊（升级信号不受此限——新症状值得再看一眼）
    if trigger == "repeat" and incident.get("last_confidence_level") == "high":
        return False, "上次结论已高置信，无需心跳重诊"

    return True, ""


async def enqueue_diagnosis_tasks(alerts_data: List[Dict[str, Any]]) -> int:
    """告警路由到事故并入队诊断任务（任务落 Mongo，重启可恢复）

    同一批告警归属同一事故时只创建一个任务（取最高优先级触发类型）。
    返回创建的任务数。
    """
    if not settings.ALERT_AUTO_DIAGNOSIS_ENABLED:
        return 0

    import uuid as _uuid

    grouped: Dict[str, Dict[str, Any]] = {}
    for alert in alerts_data:
        if not _severity_ok(alert):
            logger.info(f"告警 {alert.get('labels', {}).get('alertname')} 低于级别门槛，跳过诊断")
            continue
        incident, trigger = await _route_alert_to_incident(alert)
        g = grouped.setdefault(incident["incident_id"], {
            "incident_id": incident["incident_id"], "trigger": trigger, "alerts": [],
        })
        if _TRIGGER_PRIORITY[trigger] < _TRIGGER_PRIORITY[g["trigger"]]:
            g["trigger"] = trigger
        g["alerts"].append(alert)

    created = 0
    for g in grouped.values():
        await db.save_diagnosis_task({
            "task_id": f"dtask_{_uuid.uuid4().hex[:12]}",
            "kind": "diagnosis",
            "incident_id": g["incident_id"],
            "trigger": g["trigger"],
            "alerts": g["alerts"],
            "last_error": "",
        })
        created += 1
    if created:
        logger.info(f"已入队 {created} 个诊断任务: "
                    f"{[(g['incident_id'], g['trigger']) for g in grouped.values()]}")
    return created


_instance_id = f"{__import__('socket').gethostname()}-{__import__('uuid').uuid4().hex[:6]}"
_worker_wakeup = asyncio.Event()
_worker_task: Optional[asyncio.Task] = None


async def _enqueue_and_wake(alerts_data: List[Dict[str, Any]]):
    """BackgroundTasks 入口：入队 + 唤醒 worker（低延迟；worker 轮询是兜底）"""
    try:
        created = await enqueue_diagnosis_tasks(alerts_data)
        if created:
            _worker_wakeup.set()
    except Exception as e:
        logger.error("诊断任务入队失败: {}", e, exc_info=True)


async def _drain_pending_tasks():
    """认领并处理所有 pending 任务（FIFO，直到队列为空）"""
    while True:
        task = await db.claim_next_diagnosis_task(_instance_id)
        if not task:
            return
        await _process_diagnosis_task(task)


async def diagnosis_worker_loop():
    """诊断 worker 主循环（lifespan 启动，随应用关闭取消）

    空闲时按 DIAG_TASK_POLL_SECONDS 轮询兜底；入队时通过事件立即唤醒。
    """
    while True:
        try:
            await _drain_pending_tasks()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("诊断 worker 循环异常（继续运行）: {}", e, exc_info=True)
        try:
            await asyncio.wait_for(_worker_wakeup.wait(),
                                   timeout=settings.DIAG_TASK_POLL_SECONDS)
        except asyncio.TimeoutError:
            pass
        _worker_wakeup.clear()


async def stale_incident_sweep_loop():
    """卡死事故自动闭案扫描（B6 事故卡死保护）

    成员告警在源头被删 / Alertmanager 重启丢状态 / 手动 webhook 测试时，事故
    依赖"全部成员 fingerprint 收到 resolved"才能闭案的链路断裂——事故永远停留
    active。本循环定期扫描 active 事故，last_seen_at 超过
    INCIDENT_STALE_AUTO_CLOSE_HOURS 的强制闭案（标记 auto_resolved）。
    """
    # 扫描间隔 = 配置超时的一半，但不少于 10 分钟（避免高频扫库）
    sweep_interval = max(600, settings.INCIDENT_STALE_AUTO_CLOSE_HOURS * 3600 // 2)
    while True:
        try:
            stale_secs = settings.INCIDENT_STALE_AUTO_CLOSE_HOURS * 3600
            stale = await db.find_stale_active_incidents(stale_secs)
            for inc in stale:
                incident_id = inc.get("incident_id", "?")
                await db.force_resolve_incident(
                    incident_id, by_user="stale-sweep", auto=True,
                )
                logger.warning(
                    f"事故 {incident_id} 超时 {settings.INCIDENT_STALE_AUTO_CLOSE_HOURS}h "
                    f"无新告警，自动闭案（auto_resolved）——service={inc.get('service')}, "
                    f"last_seen_at={inc.get('last_seen_at')}"
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("卡死事故扫描循环异常（继续运行）: {}", e, exc_info=True)
        await asyncio.sleep(sweep_interval)


async def _process_diagnosis_task(task: Dict[str, Any]):
    """处理单个诊断任务：诊断（初诊/重诊）或恢复摘要

    失败重试语义：attempts < 上限 → 回 pending（其他实例/本实例稍后重试）；
    达到上限 → dead（人工排查）。重试的初诊走"继续完成"提示
    （checkpoint 保留了中断前的会话现场，不重复取证）。
    """
    task_id = task["task_id"]
    try:
        if task.get("kind") == "summary":
            incident = await db.get_incident(task["incident_id"])
            if incident and incident.get("status") == "resolving":
                await _generate_incident_summary(incident)
            else:
                logger.info(f"摘要任务跳过: 事故 {task['incident_id']} 不在 resolving 状态")
            await db.update_diagnosis_task(task_id, {"status": "done"})
            return

        incident = await db.get_incident(task["incident_id"])
        if not incident:
            await db.update_diagnosis_task(task_id, {
                "status": "done", "last_error": "事故不存在（可能已清理）"})
            return

        ok, reason = _should_diagnose(incident, task["trigger"])
        if not ok:
            # 任务可能因重启延迟被重复消费，护栏在执行前再判一次（幂等）
            logger.info(f"任务 {task_id} 跳过诊断（{task['trigger']}）: {reason}")
            await db.update_diagnosis_task(task_id, {"status": "done", "last_error": f"skipped: {reason}"})
            return

        from ..api.langgraph import get_agent
        agent = get_agent()

        async with _diag_semaphore:
            incident = await db.get_incident(task["incident_id"]) or incident
            trigger = task["trigger"]
            attempts = task.get("attempts") or 1
            if trigger == "initial" and attempts > 1:
                # 中断重试：checkpoint 已有半途现场，走"继续完成"而非重新构造
                prompt = ("上一次诊断执行被中断，请基于已有上下文继续完成本次诊断，"
                          "并严格按诊断报告结构（### 现象 / 证据 / 根因分析 / 处置方案 / 置信度）输出最终结论。")
            elif trigger == "initial":
                prompt = _build_diagnosis_prompt(task["alerts"][0])
            else:
                prompt = _build_rediagnosis_prompt(incident, task["alerts"])

            result = await agent.run(
                user_input=prompt,
                session_id=f"incident_{task['incident_id']}",
                context={
                    "user_id": "alert-webhook",
                    "org_id": getattr(settings, "DIAGNOSIS_ORG_ID", "") or "",
                },
                use_web_search=False,
            )
            content = result.get("content", "")
            if not content:
                raise RuntimeError("Agent 诊断无输出")

            report = result.get("diagnosis_report") or {}
            suff = result.get("evidence_sufficiency") or {}
            await db.add_incident_diagnosis(task["incident_id"], {
                "trigger": trigger,
                "alertnames": list(incident.get("alertnames") or []),
                "root_cause": report.get("root_cause", "")[:300],
                "confidence_level": report.get("confidence_level", "unknown"),
                "sufficiency_score": suff.get("score"),
                "sufficiency_level": suff.get("level"),
                "content": content[:800],
            })

            # B4 主嫌疑告警标记：诊断后规则选主嫌疑（severity + root_cause 关键词匹配）
            root_cause = report.get("root_cause", "")
            culprit_fp = _select_culprit(incident, root_cause)
            if culprit_fp:
                await db.update_incident_fields(
                    task["incident_id"], {"culprit_fingerprint": culprit_fp})
                logger.info(f"事故 {task['incident_id']} 主嫌疑告警: {culprit_fp}")

            # B4 卡片标注：主嫌疑 fingerprint → alertname（无则回退 fingerprint）
            culprit_name = ""
            if culprit_fp:
                culprit_name = next(
                    (a.get("alertname") or culprit_fp
                     for a in (incident.get("alert_details") or [])
                     if a.get("fingerprint") == culprit_fp),
                    culprit_fp,
                )
            runbook = _lookup_runbook(incident.get("service", ""))
            card = FeishuClient.build_diagnosis_card(
                task["alerts"][0], result, trigger=trigger,
                incident_id=task["incident_id"], runbook=runbook,
                impact_priority=incident.get("impact_priority"),
                culprit_alertname=culprit_name,
            )
            client = _get_feishu_client()
            open_id = getattr(settings, "FEISHU_ALERT_OPEN_ID", "") or ""
            if client is not None and open_id:
                if client.send_card(open_id, card):
                    logger.info(f"事故 {task['incident_id']} 诊断报告已推送飞书: "
                                f"trigger={trigger}, tools={result.get('tools_used') or []}")
                else:
                    logger.warning(f"事故 {task['incident_id']} 诊断报告推送失败（不影响诊断结果）")
            else:
                logger.info(f"事故 {task['incident_id']} 诊断完成（飞书未配置，跳过推送）")
        await db.update_diagnosis_task(task_id, {"status": "done"})
        try:
            from ..observability.metrics import get_metrics
            get_metrics().increment("ops_diagnosis_total", 1, labels={
                "trigger": trigger, "result": "done"})
            # 上 H3 定位段时长（首次诊断完成时刻 - first_seen_at）——"定位空转段"直接度量
            if trigger == "initial":
                from datetime import datetime as _dt
                first_seen = incident.get("first_seen_at")
                if first_seen:
                    start = first_seen if isinstance(first_seen, _dt) else _dt.fromisoformat(str(first_seen))
                    get_metrics().observe(
                        "ops_incident_diag_time_seconds",
                        (_dt.now() - start).total_seconds(),
                        labels={"service": incident.get("service", "unknown")})
        except Exception:
            pass
    except Exception as e:
        attempts = task.get("attempts") or 1
        if attempts >= settings.DIAG_TASK_MAX_ATTEMPTS:
            await db.update_diagnosis_task(task_id, {
                "status": "dead", "last_error": str(e)[:500]})
            try:
                from ..observability.metrics import get_metrics
                get_metrics().increment("ops_diagnosis_total", 1, labels={
                    "trigger": task.get("trigger", "unknown"), "result": "dead"})
            except Exception:
                pass
            logger.error(
                "诊断任务 {} 达到重试上限，标记 dead: {}",
                task_id, e, exc_info=True)
        else:
            from datetime import datetime as _dt
            await db.update_diagnosis_task(task_id, {
                "status": "pending",
                "not_before": _dt.now() + timedelta(
                    seconds=settings.DIAG_TASK_RETRY_BACKOFF_SECONDS),
                "last_error": str(e)[:500]})
            # e 文本可能含花括号（loguru 二次 format 会当占位符）→ 用占位符传参
            logger.warning(
                "诊断任务 {} 失败，回队列重试"
                "（attempts={}/{}，退避 {}s）: {}",
                task_id, attempts, settings.DIAG_TASK_MAX_ATTEMPTS,
                settings.DIAG_TASK_RETRY_BACKOFF_SECONDS, e)


async def handle_resolved_alerts(alerts_data: List[Dict[str, Any]]):
    """后台任务：处理 resolved 告警——全部恢复后安静期闭案并生成恢复摘要"""
    if not settings.ALERT_AUTO_DIAGNOSIS_ENABLED:
        return

    try:
        for alert in alerts_data:
            fp = _cooldown_key(alert)
            incident = await db.find_incident_by_fingerprint(fp, statuses=["active", "resolving"])
            if not incident:
                continue
            incident_id = incident["incident_id"]
            resolved_fps = list(incident.get("resolved_fps") or [])
            if fp not in resolved_fps:
                resolved_fps.append(fp)
            fingerprints = list(incident.get("fingerprints") or [])
            from datetime import datetime as _dt
            updates: Dict[str, Any] = {
                "resolved_fps": resolved_fps,
                "last_seen_at": _dt.now(),
            }
            if all(f in resolved_fps for f in fingerprints):
                updates["status"] = "resolving"
                updates["resolved_at"] = _dt.now()
                await db.update_incident_fields(incident_id, updates)
                # 上 H3 MTTR（恢复耗时 = resolved_at - first_seen_at，首个恢复信号即计）
                try:
                    from ..observability.metrics import get_metrics
                    first_seen = incident.get("first_seen_at")
                    if first_seen:
                        start = first_seen if isinstance(first_seen, _dt) else _dt.fromisoformat(str(first_seen))
                        get_metrics().observe(
                            "ops_incident_mttr_seconds",
                            (_dt.now() - start).total_seconds(),
                            labels={"service": incident.get("service", "unknown")})
                except Exception as e:
                    logger.debug(f"MTTR 指标计算失败（不影响闭案）: {e}")
                logger.info(f"事故 {incident_id} 全部告警恢复，进入安静期"
                            f"（{settings.INCIDENT_RESOLVE_QUIET_PERIOD}s 后生成摘要）")
                # 安静期：确认不复燃再闭案。sleep 在事件循环中不阻塞其他请求；
                # 安静期本身随进程存活（重启丢失由下次 resolved 心跳补救），
                # 摘要生成本身入任务表持久化（kind=summary）
                await asyncio.sleep(settings.INCIDENT_RESOLVE_QUIET_PERIOD)
                incident = await db.get_incident(incident_id)
                if not incident or incident.get("status") != "resolving":
                    continue  # 安静期内复燃（被路由逻辑改回 active）或已生成
                import uuid as _uuid
                await db.save_diagnosis_task({
                    "task_id": f"dtask_{_uuid.uuid4().hex[:12]}",
                    "kind": "summary",
                    "incident_id": incident_id,
                    "trigger": "summary",
                    "alerts": [],
                    "last_error": "",
                })
                _worker_wakeup.set()
            else:
                await db.update_incident_fields(incident_id, updates)
    except Exception as e:
        logger.error("resolved 告警处理异常: {}", e, exc_info=True)


async def _generate_incident_summary(incident: Dict[str, Any]):
    """生成恢复摘要：Agent 总结时间线/根因/复盘要点 → 飞书卡片 + 落库"""
    client = _get_feishu_client()
    open_id = getattr(settings, "FEISHU_ALERT_OPEN_ID", "") or ""
    if client is None or not open_id:
        logger.warning("恢复摘要跳过：飞书通知未配置")
        await db.update_incident_fields(incident["incident_id"], {"status": "resolved"})
        return

    incident_id = incident["incident_id"]
    try:
        from ..api.langgraph import get_agent
        agent = get_agent()
        async with _diag_semaphore:
            result = await agent.run(
                user_input=_build_summary_prompt(incident),
                session_id=f"incident_{incident_id}",  # 与诊断共享会话记忆
                context={"user_id": "alert-webhook"},
                use_web_search=False,
            )
        content = result.get("content", "")
        report = result.get("diagnosis_report") or {}

        await db.add_incident_diagnosis(incident_id, {
            "trigger": "summary",
            "alertnames": list(incident.get("alertnames") or []),
            "root_cause": report.get("root_cause", "")[:300],
            "confidence_level": report.get("confidence_level", "unknown"),
            "content": content[:800],
        })
        action_items = _parse_action_items(content)
        # B7：行动项解析落空时定向重试——只要求 LLM 输出行动项列表（按格式）
        # deepseek-flash 对六段格式遵循不稳定，摘要全文可能未含"行动项"章节
        if not action_items:
            logger.warning(f"事故 {incident_id} 摘要未解析到行动项，触发定向重试")
            try:
                retry_prompt = (
                    "上一份事故摘要未包含行动项章节。请只输出行动项，不要其他内容。"
                    "格式严格如下（至少 3 条，每条一行）：\n"
                    "### 行动项\n"
                    "- 【检测】行动描述（负责人: X，期限: Y）\n"
                    "- 【预防】行动描述（负责人: X，期限: Y）\n"
                    "- 【缓解】行动描述（负责人: X，期限: Y）\n"
                    f"事故背景：{incident.get('alertnames') or []}，"
                    f"根因：{report.get('root_cause', '未知')}"
                )
                async with _diag_semaphore:
                    retry_result = await agent.run(
                        user_input=retry_prompt,
                        session_id=f"incident_{incident_id}",
                        context={"user_id": "alert-webhook"},
                        use_web_search=False,
                    )
                retry_items = _parse_action_items(retry_result.get("content", ""))
                if retry_items:
                    action_items = retry_items
                    logger.info(f"事故 {incident_id} 定向重试解析到 {len(action_items)} 条行动项")
                else:
                    logger.warning(
                        f"事故 {incident_id} 行动项定向重试仍为空——摘要质量待人工复核"
                    )
            except Exception as retry_err:
                logger.warning(f"事故 {incident_id} 行动项重试失败（不影响闭案）: {retry_err}")
        await db.update_incident_fields(incident_id, {
            "status": "resolved",
            "summary": content[:2000],
            "action_items": action_items,
        })

        if content:
            card = FeishuClient.build_incident_summary_card(incident, result)
            if client.send_card(open_id, card):
                logger.info(f"事故 {incident_id} 恢复摘要已推送飞书")

            # 方向3 经验回流：恢复摘要经质量门自动写入知识库（doc_type=incident）
            # to_thread 脱钩避免嵌入计算阻塞事件循环；质量门与失败兜底都在 ingest 内自判
            try:
                from ..knowledge.incident_ingest import ingest_incident_into_knowledge
                await asyncio.to_thread(
                    ingest_incident_into_knowledge, incident, report, action_items, content,
                )
            except Exception as ingest_err:
                logger.warning(
                    f"事故 {incident_id} 知识入库失败（不影响闭案）: {ingest_err}")
    except Exception as e:
        logger.error("事故 {} 恢复摘要生成异常: {}", incident_id, e, exc_info=True)
        await db.update_incident_fields(incident_id, {"status": "resolved"})


# ========== 端点 ==========
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
    except Exception:
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
