"""
Incident Management API - 事故管理

业界事故三态的中间态补齐：triggered（告警触发/active）→ acknowledged（认领）→ resolved（恢复）。
- 认领把告警从"广播状态"变为"处理中状态"，acked_at - first_seen_at 即 MTTA
- 事故详情对登录用户开放（诊断历史/行动项是团队协作信息）
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from pydantic import BaseModel

from ..core.auth import UserResponse, get_current_user, require_admin
from ..core.database import Database, get_db
from ..observability.metrics import get_metrics

router = APIRouter(prefix="/api/incidents", tags=["事故管理"])


class AckResponse(BaseModel):
    """认领响应"""
    incident_id: str
    acked_by: str
    acked_at: str
    first_ack: bool  # False = 已有人先认领（本请求未覆盖）


class ForceResolveResponse(BaseModel):
    """强制闭案响应"""
    incident_id: str
    status: str
    resolved_at: str
    auto_resolved: bool
    force_resolved_by: str


class IncidentDetailResponse(BaseModel):
    """事故详情"""
    incident_id: str
    status: str
    service: str
    fingerprints: list
    alertnames: list
    max_severity: str
    # B3 影响等级：服务关键度 × 告警级别 → P1-P4（ITIL 优先级矩阵）
    impact_priority: Optional[str] = None
    # B4 主嫌疑告警指纹（诊断后规则选最高分告警，Open Box 可解释）
    culprit_fingerprint: Optional[str] = None
    first_seen_at: Optional[str] = None
    resolved_at: Optional[str] = None
    acked_by: Optional[str] = None
    acked_at: Optional[str] = None
    diag_count: int = 0
    last_confidence_level: str = "unknown"
    diagnosis_history: list = []
    action_items: list = []
    summary: Optional[str] = None
    # B5 维护窗口感知：计划内变更标记（故障窗内有变更 → 诊断/复盘时区分变更引发 vs 独立故障）
    planned_change: bool = False
    recent_changes: Optional[str] = None


class IncidentSummary(BaseModel):
    """事故列表摘要"""
    incident_id: str
    status: str
    service: str
    max_severity: str
    impact_priority: Optional[str] = None
    first_seen_at: Optional[str] = None
    resolved_at: Optional[str] = None
    acked_by: Optional[str] = None
    acked_at: Optional[str] = None
    diag_count: int = 0
    last_confidence_level: str = "unknown"
    alertnames: list = []


class RootCausePattern(BaseModel):
    """根因模式（D1 问题管理入口）"""
    root_cause: str
    count: int  # 命中该根因的不同事故数（同事故多次诊断去重）
    services: list = []
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    incident_ids: list = []


class DiagnosisQualityTier(BaseModel):
    """诊断质量分层（D2 验证"高充分度 → 高采纳率"假设）"""
    sufficiency_level: str  # high/medium/low/unknown
    count: int
    acked: int  # 已认领事故数
    resolved: int  # 已闭案事故数
    ack_rate: float  # 认领率 = acked / count
    resolve_rate: float  # 闭案率 = resolved / count


def _iso(value) -> Optional[str]:
    """datetime → ISO 字符串（None 安全；clean_mongo_doc 已把 Mongo 日期转 str 时直接透传）"""
    if value is None:
        return None
    return value if isinstance(value, str) else value.isoformat()


@router.get("/patterns", response_model=list[RootCausePattern])
async def get_incident_patterns(
    days: int = 30,
    limit: int = 10,
    current_user: UserResponse = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """D1 根因模式统计——回答"本月哪个根因反复出现"

    ITIL 问题管理入口：识别反复出现的根因，触发"建基础设施消除"的主动改进。
    统计口径见 db.aggregate_incident_patterns（同事故多次诊断去重，排除摘要条目）。
    """
    patterns = await db.aggregate_incident_patterns(days=days, limit=limit)
    return [
        RootCausePattern(
            root_cause=p.get("root_cause", ""),
            count=p.get("count", 0),
            services=p.get("services") or [],
            first_seen=_iso(p.get("first_seen")),
            last_seen=_iso(p.get("last_seen")),
            incident_ids=p.get("incident_ids") or [],
        )
        for p in patterns
    ]


@router.get("/quality", response_model=list[DiagnosisQualityTier])
async def get_diagnosis_quality(
    days: int = 30,
    current_user: UserResponse = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """D2 诊断质量分层统计——验证"高充分度 → 高采纳率"假设

    产品价值假设链：H1 诊断命中 → H2 响应者采纳。
    按最近一次诊断的 sufficiency_level 分层统计 ack 采纳率——
    若"高充分度 → 高采纳率"不成立，说明检索/诊断质量与响应者信任存在断点。
    """
    tiers = await db.aggregate_diagnosis_quality(days=days)
    return [
        DiagnosisQualityTier(
            sufficiency_level=t.get("sufficiency_level", "unknown"),
            count=t.get("count", 0),
            acked=t.get("acked", 0),
            resolved=t.get("resolved", 0),
            ack_rate=t.get("ack_rate", 0.0),
            resolve_rate=t.get("resolve_rate", 0.0),
        )
        for t in tiers
    ]


@router.get("", response_model=List[IncidentSummary])
async def list_incidents(
    service: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    current_user: UserResponse = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """列出事故（支持按 service/status 过滤，按 first_seen_at 倒序）"""
    incidents = await db.list_incidents(service=service, status=status, limit=limit)
    return [
        IncidentSummary(
            incident_id=inc.get("incident_id", ""),
            status=inc.get("status", "unknown"),
            service=inc.get("service", "unknown"),
            max_severity=inc.get("max_severity", "-"),
            impact_priority=inc.get("impact_priority"),
            first_seen_at=_iso(inc.get("first_seen_at")),
            resolved_at=_iso(inc.get("resolved_at")),
            acked_by=inc.get("acked_by"),
            acked_at=_iso(inc.get("acked_at")),
            diag_count=inc.get("diag_count", 0),
            last_confidence_level=inc.get("last_confidence_level", "unknown"),
            alertnames=inc.get("alertnames") or [],
        )
        for inc in incidents
    ]


@router.get("/{incident_id}", response_model=IncidentDetailResponse)
async def get_incident(
    incident_id: str,
    current_user: UserResponse = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """事故详情（含诊断历史/行动项/认领信息）"""
    incident = await db.get_incident(incident_id)
    if not incident:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="事故不存在")

    return IncidentDetailResponse(
        incident_id=incident.get("incident_id", incident_id),
        status=incident.get("status", "unknown"),
        service=incident.get("service", "unknown"),
        fingerprints=incident.get("fingerprints") or [],
        alertnames=incident.get("alertnames") or [],
        max_severity=incident.get("max_severity", "-"),
        impact_priority=incident.get("impact_priority"),
        culprit_fingerprint=incident.get("culprit_fingerprint"),
        first_seen_at=_iso(incident.get("first_seen_at")),
        resolved_at=_iso(incident.get("resolved_at")),
        acked_by=incident.get("acked_by"),
        acked_at=_iso(incident.get("acked_at")),
        diag_count=incident.get("diag_count", 0),
        last_confidence_level=incident.get("last_confidence_level", "unknown"),
        diagnosis_history=incident.get("diagnosis_history") or [],
        action_items=incident.get("action_items") or [],
        summary=incident.get("summary"),
        planned_change=bool(incident.get("planned_change", False)),
        recent_changes=incident.get("recent_changes"),
    )


@router.post("/{incident_id}/ack", response_model=AckResponse)
async def acknowledge_incident(
    incident_id: str,
    current_user: UserResponse = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """人工认领事故（首认领生效）

    认领 = "我接管了这次事故的处理"——业界 MTTA（Mean Time To Acknowledge）
    的度量点，也是诊断采纳率统计的行为信号。
    """
    incident, first_ack = await db.ack_incident(incident_id, current_user.user_id)
    if incident is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="事故不存在")

    if first_ack:
        # MTTA 上盘（认领耗时 = acked_at - first_seen_at）
        try:
            from datetime import datetime as _dt
            first_seen = incident.get("first_seen_at")
            acked_at = incident.get("acked_at")
            if first_seen and acked_at:
                start = first_seen if isinstance(first_seen, _dt) else _dt.fromisoformat(str(first_seen))
                end = acked_at if isinstance(acked_at, _dt) else _dt.fromisoformat(str(acked_at))
                get_metrics().observe("ops_incident_mtta_seconds", (end - start).total_seconds(),
                                      labels={"service": incident.get("service", "unknown")})
        except Exception as e:
            logger.debug(f"MTTA 指标计算失败（不影响 ack）: {e}")
        logger.info(f"事故 {incident_id} 已被 {current_user.user_id} 认领")
    else:
        logger.info(f"事故 {incident_id} 重复认领（已被 {incident.get('acked_by')} 先认领），忽略")

    return AckResponse(
        incident_id=incident_id,
        acked_by=incident.get("acked_by", ""),
        acked_at=_iso(incident.get("acked_at")) or "",
        first_ack=first_ack,
    )


@router.post("/{incident_id}/force-resolve", response_model=ForceResolveResponse)
async def force_resolve_incident(
    incident_id: str,
    current_user: UserResponse = Depends(require_admin),
    db: Database = Depends(get_db),
):
    """管理员强制闭案（B6 事故卡死保护）

    当成员告警在源头被删 / Alertmanager 重启丢状态 / 手动 webhook 测试导致
    事故永远停留 active 时，管理员可强制闭案——绕过"全部成员 fingerprint 收到
    resolved"的前置条件。worker 也会对超时活跃事故自动闭案（auto_resolved=true）。
    """
    incident = await db.force_resolve_incident(
        incident_id, by_user=current_user.user_id, auto=False,
    )
    if incident is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="事故不存在")

    logger.info(f"事故 {incident_id} 被管理员 {current_user.user_id} 强制闭案")
    return ForceResolveResponse(
        incident_id=incident_id,
        status=incident.get("status", "resolved"),
        resolved_at=_iso(incident.get("resolved_at")) or "",
        auto_resolved=bool(incident.get("auto_resolved", False)),
        force_resolved_by=incident.get("force_resolved_by", current_user.user_id),
    )
