"""
Incident Management API - 事故管理

业界事故三态的中间态补齐：triggered（告警触发/active）→ acknowledged（认领）→ resolved（恢复）。
- 认领把告警从"广播状态"变为"处理中状态"，acked_at - first_seen_at 即 MTTA
- 事故详情对登录用户开放（诊断历史/行动项是团队协作信息）
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from pydantic import BaseModel

from ..core.auth import UserResponse, get_current_user
from ..core.database import Database, get_db
from ..observability.metrics import get_metrics

router = APIRouter(prefix="/api/incidents", tags=["事故管理"])


class AckResponse(BaseModel):
    """认领响应"""
    incident_id: str
    acked_by: str
    acked_at: str
    first_ack: bool  # False = 已有人先认领（本请求未覆盖）


class IncidentDetailResponse(BaseModel):
    """事故详情"""
    incident_id: str
    status: str
    service: str
    fingerprints: list
    alertnames: list
    max_severity: str
    first_seen_at: Optional[str] = None
    resolved_at: Optional[str] = None
    acked_by: Optional[str] = None
    acked_at: Optional[str] = None
    diag_count: int = 0
    last_confidence_level: str = "unknown"
    diagnosis_history: list = []
    action_items: list = []
    summary: Optional[str] = None


def _iso(value) -> Optional[str]:
    """datetime → ISO 字符串（None 安全；clean_mongo_doc 已把 Mongo 日期转 str 时直接透传）"""
    if value is None:
        return None
    return value if isinstance(value, str) else value.isoformat()


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
        first_seen_at=_iso(incident.get("first_seen_at")),
        resolved_at=_iso(incident.get("resolved_at")),
        acked_by=incident.get("acked_by"),
        acked_at=_iso(incident.get("acked_at")),
        diag_count=incident.get("diag_count", 0),
        last_confidence_level=incident.get("last_confidence_level", "unknown"),
        diagnosis_history=incident.get("diagnosis_history") or [],
        action_items=incident.get("action_items") or [],
        summary=incident.get("summary"),
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
