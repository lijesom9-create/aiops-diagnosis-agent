"""事故经验回流（方向3）：高质量恢复摘要自动写入知识库，回灌下次诊断。

安全边界（可在 config.py 的 INCIDENT_KNOWLEDGE_* 调整）：
- 质量门：根因置信度/充分度达标才入库；low/unknown 直接跳过；
- 中等置信度：标记 `_needs_review` 并在内容附"低置信度待复核"注记，人工复核兜底；
- 幂等：按 incident_id 生成稳定 doc_id，`unified_store.add` 为 upsert，绝不静默删除旧知识；
- 每次入库后失效查询/重写缓存，避免下次检索读到旧结果。

纯逻辑（should_ingest / _build_knowledge_item）与副作用（入库+指标）分离，便于单测。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.knowledge.unified_store import KnowledgeItem

logger = None  # 延迟导入，避免模块加载期日志副作用


def _log():
    global logger
    if logger is None:
        from loguru import logger as _lg
        logger = _lg
    return logger


# 质量级别序（未知按最低处理）
_LEVEL_RANK = {"low": 0, "unknown": 0, "medium": 1, "high": 2}


def _rank(level: str) -> int:
    return _LEVEL_RANK.get((level or "unknown").lower(), 0)


def extract_sufficiency(incident: Dict[str, Any]) -> Optional[str]:
    """取该事故最近一次非摘要诊断的充分度（摘要是事后总结，不代表诊断时刻质量）"""
    hist = incident.get("diagnosis_history") or []
    for entry in reversed(hist):
        if entry.get("trigger") == "summary":
            continue
        level = entry.get("sufficiency_level")
        if level:
            return level
    return None


def should_ingest(
    confidence_level: str,
    sufficiency_level: Optional[str],
    min_confidence: str = "medium",
    min_sufficiency: str = "medium",
) -> Tuple[bool, bool]:
    """质量门判定。返回 (是否入库, 是否需标记待复核)。

    - 入库：置信度 >= min_confidence，且充分度（若可得）>= min_sufficiency；
      充分度缺失时以置信度为准（不因无法判定而阻塞）。
    - 待复核：任一项未到 high → 标记人工复核并在内容附低置信度注记。
    """
    conf_ok = _rank(confidence_level) >= _rank(min_confidence)
    suff_ok = sufficiency_level is None or _rank(sufficiency_level) >= _rank(min_sufficiency)
    ingest_ok = conf_ok and suff_ok
    needs_review = ingest_ok and (
        _rank(confidence_level) < _rank("high")
        or (sufficiency_level is not None and _rank(sufficiency_level) < _rank("high"))
    )
    return ingest_ok, needs_review


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _fmt_action_items(action_items: List[Any]) -> str:
    if not action_items:
        return "（无）"
    lines = []
    for it in action_items:
        if isinstance(it, dict):
            item = _clean(it.get("item") or it.get("action") or str(it))
            owner = _clean(it.get("owner") or "")
            deadline = _clean(it.get("deadline") or "")
            lines.append(f"- {item}（负责人: {owner or '未定'}，期限: {deadline or '未定'}）")
        else:
            lines.append(f"- {_clean(str(it))}")
    return "\n".join(lines)


def _build_content(
    incident: Dict[str, Any],
    report: Dict[str, Any],
    action_items: List[Any],
    summary: str,
    needs_review: bool,
) -> str:
    service = incident.get("service") or "unknown"
    alertnames = incident.get("alertnames") or []
    root_cause = _clean(report.get("root_cause") or incident.get("summary") or "未知根因")[:300]
    confidence = report.get("confidence_level", "unknown")
    suff = extract_sufficiency(incident) or "unknown"

    parts = [
        f"【事故复盘】服务 {service}",
        f"- 现象：{'、'.join(alertnames) if alertnames else '见告警'}"
        f"（影响等级 {incident.get('impact_priority') or 'P4'}）",
        f"- 根因：{root_cause}",
        f"- 诊断质量：充分度={suff}，置信度={confidence}",
    ]
    if incident.get("planned_change"):
        parts.append("- 故障窗内存在计划内变更（变更引发 vs 独立故障，复盘时已区分）")
    if needs_review:
        parts.append("- ⚠️ 低置信度，待人工复核后引用")
    parts.append("- 时间线：")
    if incident.get("first_seen_at"):
        parts.append(f"    - 检测 {incident['first_seen_at']}")
    if incident.get("acked_at"):
        parts.append(f"    - 认领 {incident['acked_at']}")
    if incident.get("resolved_at"):
        parts.append(f"    - 恢复 {incident['resolved_at']}")
    parts.append("- 行动项：")
    parts.append(_fmt_action_items(action_items))
    if summary:
        parts.append(f"- 摘要：{_clean(summary)[:400]}")
    return "\n".join(parts)


def _build_knowledge_item(
    incident: Dict[str, Any],
    report: Dict[str, Any],
    action_items: List[Any],
    summary: str,
    valid_days: int,
) -> KnowledgeItem:
    incident_id = incident.get("incident_id") or ""
    service = incident.get("service") or "unknown"
    confidence = report.get("confidence_level", "unknown")
    suff = extract_sufficiency(incident) or "unknown"
    _, needs_review = should_ingest(
        confidence, suff,
        min_confidence="medium", min_sufficiency="medium",
    )
    now = datetime.now()
    return KnowledgeItem(
        id=f"incident_{incident_id}",
        title=f"[事故复盘] {service} - {_clean(report.get('root_cause') or '未知根因')[:60]}",
        content=_build_content(incident, report, action_items, summary, needs_review),
        source="user_document",
        metadata={
            "doc_type": "incident",
            "service": service,
            "severity": incident.get("max_severity") or incident.get("severity") or "",
            "impact_priority": incident.get("impact_priority"),
            "incident_id": incident_id,
            "alertnames": list(incident.get("alertnames") or []),
            "confidence_level": confidence,
            "sufficiency_level": suff,
            "first_seen_at": incident.get("first_seen_at"),
            "resolved_at": incident.get("resolved_at"),
            "planned_change": bool(incident.get("planned_change")),
            "valid_until": (now + timedelta(days=max(1, valid_days))).isoformat(),
            "_needs_review": needs_review,
        },
    )


def ingest_incident_into_knowledge(
    incident: Dict[str, Any],
    report: Dict[str, Any],
    action_items: List[Any],
    summary: str,
) -> str:
    """把恢复摘要经质量门写入知识库。返回 "ingested" / "skipped" / "failed"。

    副作用（均在 ingest 内）：写库 + 失效缓存 + 埋点 ops_knowledge_ingest_total。
    调用方应经线程池/后台任务脱钩，避免阻塞事件循环（嵌入计算耗时）。
    """
    log = _log()
    from app.core.config import settings
    from app.observability.metrics import get_metrics
    from app.shared_services import get_knowledge_store

    def _count(result: str) -> None:
        try:
            get_metrics().increment("ops_knowledge_ingest_total", 1, labels={"result": result})
        except Exception:
            pass

    if not getattr(settings, "INCIDENT_KNOWLEDGE_ENABLED", True):
        _count("skipped")
        return "skipped"

    store = get_knowledge_store()
    if store is None:
        log.debug("经验回流跳过：知识库未初始化")
        _count("skipped")
        return "skipped"

    try:
        confidence = report.get("confidence_level", "unknown")
        suff = extract_sufficiency(incident)
        ingest_ok, _ = should_ingest(
            confidence,
            suff,
            min_confidence=getattr(settings, "INCIDENT_KNOWLEDGE_MIN_CONFIDENCE", "medium"),
            min_sufficiency=getattr(settings, "INCIDENT_KNOWLEDGE_MIN_SUFFICIENCY", "medium"),
        )
        if not ingest_ok:
            log.info(
                f"经验回流跳过：质量未达标 incident={incident.get('incident_id')} "
                f"confidence={confidence} suff={suff}"
            )
            _count("skipped_low_quality")
            return "skipped"

        item = _build_knowledge_item(
            incident, report, action_items, summary,
            valid_days=getattr(settings, "INCIDENT_KNOWLEDGE_VALID_DAYS", 90),
        )
        store.add(item)
        if callable(getattr(store, "invalidate_caches", None)):
            store.invalidate_caches()
        log.info(f"经验回流已入库: {item.id}（doc_type=incident）")
        _count("ingested")
        return "ingested"
    except Exception as e:
        log.error("经验回流失败 incident={}: {}", incident.get("incident_id", "?"), e, exc_info=True)
        _count("failed")
        return "failed"