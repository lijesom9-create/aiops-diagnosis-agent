"""Database IncidentsMixin 领域 Mixin（T3-B② 从 database.py 拆分）。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional

from app.core.db_utils import clean_mongo_doc


class IncidentsMixin:
    async def ensure_incident_indexes(self):
        """active 事故唯一索引：同服务并发创建被 DB 层拒绝（B2——路由层转归入）

        partial unique：仅对 status="active" 的文档生效——每个服务同时最多一个
        活跃事故；resolved/resolving 不受约束。内存模式无索引语义（单线程测试）。
        """
        await self.connect()
        if not self._use_mongo:
            return
        from pymongo import ASCENDING
        await self._mongo.incidents.create_index(
            [("service", ASCENDING), ("status", ASCENDING)],
            unique=True,
            partialFilterExpression={"status": "active"},
            name="uniq_active_service",
        )
    async def save_incident(self, incident_data: dict) -> str:
        """创建事故记录"""
        await self.connect()
        incident_data.setdefault("created_at", datetime.now())
        incident_data["updated_at"] = datetime.now()
        if self._use_mongo:
            data_to_save = incident_data.copy()
            await self._mongo.incidents.insert_one(data_to_save)
        else:
            # 模拟 uniq_active_service 唯一索引：同服务并发第二个 active 抛错
            for existing in self._incidents:
                if (existing.get("status") == "active"
                        and existing.get("service") == incident_data.get("service")
                        and existing.get("incident_id") != incident_data.get("incident_id")):
                    from pymongo.errors import DuplicateKeyError
                    raise DuplicateKeyError("uniq_active_service (memory emulation)")
            self._incidents.append(incident_data.copy())
        return incident_data["incident_id"]
    async def get_incident(self, incident_id: str) -> Optional[dict]:
        """获取事故记录"""
        await self.connect()
        if self._use_mongo:
            doc = await self._mongo.incidents.find_one({"incident_id": incident_id})
            return clean_mongo_doc(doc)
        for inc in self._incidents:
            if inc.get("incident_id") == incident_id:
                return inc
        return None
    async def update_incident_fields(self, incident_id: str, fields: dict) -> bool:
        """更新事故的指定字段（$set 语义）"""
        await self.connect()
        fields = {**fields, "updated_at": datetime.now()}
        if self._use_mongo:
            result = await self._mongo.incidents.update_one(
                {"incident_id": incident_id}, {"$set": fields},
            )
            return result.matched_count > 0
        for inc in self._incidents:
            if inc.get("incident_id") == incident_id:
                inc.update(fields)
                return True
        return False
    async def find_incident_by_fingerprint(
        self, fingerprint: str, statuses: Optional[List[str]] = None,
        resolved_within_seconds: Optional[int] = None,
    ) -> Optional[dict]:
        """按告警 fingerprint 查找事故

        Args:
            statuses: 限定状态列表（默认 active）
            resolved_within_seconds: 查"最近 N 秒内 resolved"的事故（抖动复用），
                传入时忽略 statuses
        """
        await self.connect()
        if self._use_mongo:
            query: Dict = {"fingerprints": fingerprint}
            if resolved_within_seconds is not None:
                cutoff = datetime.now() - timedelta(seconds=resolved_within_seconds)
                query["status"] = "resolved"
                query["resolved_at"] = {"$gte": cutoff}
            else:
                query["status"] = {"$in": statuses or ["active"]}
            doc = await self._mongo.incidents.find_one(
                query, sort=[("updated_at", -1)],
            )
            return clean_mongo_doc(doc)
        candidates = [
            inc for inc in self._incidents
            if fingerprint in (inc.get("fingerprints") or [])
        ]
        if resolved_within_seconds is not None:
            cutoff = datetime.now() - timedelta(seconds=resolved_within_seconds)
            candidates = [
                inc for inc in candidates
                if inc.get("status") == "resolved" and inc.get("resolved_at")
                and inc["resolved_at"] >= cutoff
            ]
        else:
            wanted = statuses or ["active"]
            candidates = [inc for inc in candidates if inc.get("status") in wanted]
        candidates.sort(key=lambda x: x.get("updated_at") or datetime.min, reverse=True)
        return candidates[0] if candidates else None
    async def find_active_incident_by_service(
        self, service: str, within_seconds: int = 1800,
    ) -> Optional[dict]:
        """按服务名查找进行中的事故（新告警归入的关联依据）"""
        if not service:
            return None
        await self.connect()
        cutoff = datetime.now() - timedelta(seconds=within_seconds)
        if self._use_mongo:
            doc = await self._mongo.incidents.find_one(
                {"status": "active", "service": service, "last_seen_at": {"$gte": cutoff}},
                sort=[("updated_at", -1)],
            )
            return clean_mongo_doc(doc)
        candidates = [
            inc for inc in self._incidents
            if inc.get("status") == "active" and inc.get("service") == service
            and inc.get("last_seen_at") and inc["last_seen_at"] >= cutoff
        ]
        candidates.sort(key=lambda x: x.get("updated_at") or datetime.min, reverse=True)
        return candidates[0] if candidates else None
    async def list_incidents(
        self, service: Optional[str] = None, status: Optional[str] = None,
        limit: int = 50,
    ) -> List[dict]:
        """列出事故（按 first_seen_at 倒序，支持按 service/status 过滤）"""
        await self.connect()
        if self._use_mongo:
            query: dict = {}
            if service:
                query["service"] = service
            if status:
                query["status"] = status
            cursor = self._mongo.incidents.find(query).sort("first_seen_at", -1).limit(limit)
            docs = await cursor.to_list(length=limit)
            return [clean_mongo_doc(d) for d in docs]
        candidates = list(self._incidents)
        if service:
            candidates = [i for i in candidates if i.get("service") == service]
        if status:
            candidates = [i for i in candidates if i.get("status") == status]
        candidates.sort(key=lambda x: x.get("first_seen_at") or datetime.min, reverse=True)
        return candidates[:limit]
    async def add_incident_fingerprint(
        self, incident_id: str, fingerprint: str,
        alertname: str, severity: str,
    ) -> Optional[dict]:
        """向事故归入新告警（fingerprint 去重 + 最高 severity 跟踪 + 时间线更新）"""
        await self.connect()
        incident = await self.get_incident(incident_id)
        if not incident:
            return None
        fingerprints = list(incident.get("fingerprints") or [])
        is_new = fingerprint not in fingerprints
        if is_new:
            fingerprints.append(fingerprint)
        alertnames = list(incident.get("alertnames") or [])
        if alertname and alertname not in alertnames:
            alertnames.append(alertname)

        _rank = {"info": 0, "warning": 1, "critical": 2}
        max_severity = max(
            incident.get("max_severity", "info"),
            severity,
            key=lambda s: _rank.get(s, 1),
        )
        fields = {
            "fingerprints": fingerprints,
            "alertnames": alertnames,
            "max_severity": max_severity,
            "last_seen_at": datetime.now(),
        }
        await self.update_incident_fields(incident_id, fields)
        return await self.get_incident(incident_id)
    async def ack_incident(self, incident_id: str, user_id: str) -> tuple:
        """人工认领事故（首认领生效，幂等）

        业界事故三态 triggered → acknowledged → resolved 的中间态：
        acked_at - first_seen_at 即 MTTA（Mean Time To Acknowledge）。

        Returns:
            (事故文档, 是否本次首次认领)；事故不存在返回 (None, False)
        """
        incident = await self.get_incident(incident_id)
        if not incident:
            return None, False
        if incident.get("acked_at"):
            return incident, False  # 已有认领，首认领不覆盖

        from datetime import datetime as _dt
        now = _dt.now()
        await self.update_incident_fields(incident_id, {
            "acked_by": user_id,
            "acked_at": now,
        })
        updated = await self.get_incident(incident_id)
        return updated, True
    async def find_stale_active_incidents(
        self, older_than_seconds: int,
    ) -> List[dict]:
        """查找卡死的活跃事故（B6 事故卡死保护）

        last_seen_at 超过 older_than_seconds 仍为 active 的事故——成员告警在
        源头被删 / Alertmanager 重启丢状态 / 手动 webhook 测试时，事故永远
        停留 active，依赖全部成员 fingerprint 收到 resolved 才能闭案的链路断裂。
        """
        await self.connect()
        cutoff = datetime.now() - timedelta(seconds=older_than_seconds)
        if self._use_mongo:
            cursor = self._mongo.incidents.find(
                {"status": "active", "last_seen_at": {"$lt": cutoff}},
            )
            return [clean_mongo_doc(d) async for d in cursor]
        return [
            inc for inc in self._incidents
            if inc.get("status") == "active"
            and inc.get("last_seen_at") and inc["last_seen_at"] < cutoff
        ]
    async def force_resolve_incident(
        self, incident_id: str, by_user: str, auto: bool = False,
    ) -> Optional[dict]:
        """强制闭案（B6 事故卡死保护）

        管理员手动强制闭案或 worker 超时自动闭案——绕过"全部成员 fingerprint
        收到 resolved"的前置条件。标记 auto_resolved 区分正常闭案与强制闭案。
        已 resolved 的事故幂等返回（不重复改状态）。
        """
        incident = await self.get_incident(incident_id)
        if not incident:
            return None
        if incident.get("status") == "resolved":
            return incident
        from datetime import datetime as _dt
        now = _dt.now()
        fields: dict = {
            "status": "resolved",
            "resolved_at": now,
            "last_seen_at": now,
            "auto_resolved": auto,
            "force_resolved_by": by_user,
        }
        await self.update_incident_fields(incident_id, fields)
        return await self.get_incident(incident_id)
    async def add_incident_diagnosis(self, incident_id: str, entry: dict) -> bool:
        """向事故追加一条诊断记录（初诊/重诊/摘要），并更新诊断统计"""
        await self.connect()
        incident = await self.get_incident(incident_id)
        if not incident:
            return False
        now = datetime.now()
        fields = {
            "diag_count": (incident.get("diag_count") or 0) + 1,
            "last_diag_at": now,
            "last_confidence_level": entry.get("confidence_level", "unknown"),
        }
        if self._use_mongo:
            entry["at"] = now
            await self._mongo.incidents.update_one(
                {"incident_id": incident_id},
                {"$push": {"diagnosis_history": entry}, "$set": fields},
            )
            return True
        incident.update(fields)
        entry["at"] = now
        incident.setdefault("diagnosis_history", []).append(entry)
        return True
    async def aggregate_incident_patterns(
        self, days: int = 30, limit: int = 10,
    ) -> List[dict]:
        """D1 根因模式统计——按 root_cause 聚合，回答"本月哪个根因反复出现"

        Args:
            days: 时间窗（天），只统计 first_seen_at 在该窗口内的事故
            limit: 返回最多 N 条模式（按频次降序）

        Returns:
            List[{root_cause, count, services, first_seen, last_seen, incident_ids}]
            count = 命中该根因的不同事故数（去重后）
        """
        await self.connect()
        cutoff = datetime.now() - timedelta(days=days)

        if self._use_mongo:
            pipeline = [
                {"$match": {"first_seen_at": {"$gte": cutoff}}},
                {"$unwind": "$diagnosis_history"},
                {"$match": {
                    "diagnosis_history.root_cause": {"$exists": True, "$ne": ""},
                    "diagnosis_history.trigger": {"$ne": "summary"},
                }},
                # 去重：同一 (root_cause, incident_id) 只保留最早一条
                {"$group": {
                    "_id": {
                        "root_cause": "$diagnosis_history.root_cause",
                        "incident_id": "$incident_id",
                    },
                    "service": {"$first": "$service"},
                    "at": {"$min": "$diagnosis_history.at"},
                }},
                # 按 root_cause 聚合：count = 不同事故数
                {"$group": {
                    "_id": "$_id.root_cause",
                    "count": {"$sum": 1},
                    "services": {"$addToSet": "$service"},
                    "first_seen": {"$min": "$at"},
                    "last_seen": {"$max": "$at"},
                    "incident_ids": {"$addToSet": "$_id.incident_id"},
                }},
                {"$sort": {"count": -1}},
                {"$limit": limit},
            ]
            cursor = self._mongo.incidents.aggregate(pipeline)
            results = []
            async for doc in cursor:
                results.append({
                    "root_cause": doc.get("_id", ""),
                    "count": doc.get("count", 0),
                    "services": doc.get("services") or [],
                    "first_seen": doc.get("first_seen"),
                    "last_seen": doc.get("last_seen"),
                    "incident_ids": doc.get("incident_ids") or [],
                })
            return results

        # 内存模式：等价 Python 逻辑
        from collections import defaultdict
        pairs: Dict = {}  # (root_cause, incident_id) → {service, at}
        for inc in self._incidents:
            first_seen = inc.get("first_seen_at")
            if not first_seen or first_seen < cutoff:
                continue
            inc_id = inc.get("incident_id", "")
            service = inc.get("service", "unknown")
            for entry in inc.get("diagnosis_history") or []:
                if entry.get("trigger") == "summary":
                    continue
                rc = (entry.get("root_cause") or "").strip()
                if not rc:
                    continue
                at = entry.get("at")
                key = (rc, inc_id)
                existing = pairs.get(key)
                if existing is None or (at and existing.get("at") and at < existing["at"]):
                    pairs[key] = {"service": service, "at": at}

        groups: Dict = defaultdict(lambda: {
            "count": 0, "services": set(), "first_seen": None,
            "last_seen": None, "incident_ids": set(),
        })
        for (rc, inc_id), info in pairs.items():
            g = groups[rc]
            g["count"] += 1
            g["services"].add(info["service"])
            g["incident_ids"].add(inc_id)
            at = info.get("at")
            if at:
                if g["first_seen"] is None or at < g["first_seen"]:
                    g["first_seen"] = at
                if g["last_seen"] is None or at > g["last_seen"]:
                    g["last_seen"] = at

        sorted_groups = sorted(
            groups.items(), key=lambda x: x[1]["count"], reverse=True,
        )[:limit]
        return [
            {
                "root_cause": rc,
                "count": g["count"],
                "services": sorted(g["services"]),
                "first_seen": g["first_seen"],
                "last_seen": g["last_seen"],
                "incident_ids": sorted(g["incident_ids"]),
            }
            for rc, g in sorted_groups
        ]
    async def aggregate_diagnosis_quality(self, days: int = 30) -> List[dict]:
        """D2 诊断质量分层统计

        按 sufficiency_level (low/medium/high/unknown) 分层，每层统计：
        - count: 事故总数
        - acked: 已认领事故数（acked_by 非空）
        - resolved: 已闭案事故数
        - ack_rate: 认领率 = acked / count（采纳率核心指标）
        - resolve_rate: 闭案率 = resolved / count

        Args:
            days: 时间窗（天），只统计 first_seen_at 在该窗口内的事故

        Returns:
            List[{sufficiency_level, count, acked, resolved, ack_rate, resolve_rate}]
            按 sufficiency_level 排序（high → medium → low → unknown）
        """
        await self.connect()
        cutoff = datetime.now() - timedelta(days=days)
        # 固定分层顺序（high 最优先验证假设）
        level_order = {"high": 0, "medium": 1, "low": 2, "unknown": 3}

        if self._use_mongo:
            pipeline = [
                {"$match": {"first_seen_at": {"$gte": cutoff}}},
                {"$unwind": {
                    "path": "$diagnosis_history", "includeArrayIndex": "idx",
                }},
                {"$match": {
                    "diagnosis_history.trigger": {"$ne": "summary"},
                    "diagnosis_history.sufficiency_level": {"$exists": True, "$nin": [None, ""]},
                }},
                # 按 idx 降序：$first 取最近一次非摘要诊断
                {"$sort": {"idx": -1}},
                {"$group": {
                    "_id": "$incident_id",
                    "sufficiency_level": {"$first": "$diagnosis_history.sufficiency_level"},
                    "sufficiency_score": {"$first": "$diagnosis_history.sufficiency_score"},
                    "acked_by": {"$first": "$acked_by"},
                    "status": {"$first": "$status"},
                }},
                {"$group": {
                    "_id": "$sufficiency_level",
                    "count": {"$sum": 1},
                    "acked": {"$sum": {"$cond": [{"$ne": ["$acked_by", None]}, 1, 0]}},
                    "resolved": {"$sum": {"$cond": [{"$eq": ["$status", "resolved"]}, 1, 0]}},
                }},
            ]
            cursor = self._mongo.incidents.aggregate(pipeline)
            raw = {doc["_id"]: doc async for doc in cursor}
        else:
            # 内存模式：遍历事故，取最近一次非摘要诊断的 sufficiency_level
            from collections import defaultdict
            stats: Dict = defaultdict(lambda: {"count": 0, "acked": 0, "resolved": 0})
            for inc in self._incidents:
                first_seen = inc.get("first_seen_at")
                if not first_seen or first_seen < cutoff:
                    continue
                # 取最近一次非摘要诊断（diagnosis_history 按 append 顺序，末尾最新）
                latest_level = None
                for entry in reversed(inc.get("diagnosis_history") or []):
                    if entry.get("trigger") == "summary":
                        continue
                    level = entry.get("sufficiency_level")
                    if level:
                        latest_level = level
                        break
                if not latest_level:
                    latest_level = "unknown"
                g = stats[latest_level]
                g["count"] += 1
                if inc.get("acked_by"):
                    g["acked"] += 1
                if inc.get("status") == "resolved":
                    g["resolved"] += 1
            raw = {level: {"_id": level, **s} for level, s in stats.items()}

        # 统一构造返回（含 ack_rate / resolve_rate，按固定顺序排列）
        results = []
        for level in sorted(raw.keys(), key=lambda x: level_order.get(x, 99)):
            s = raw[level]
            count = s.get("count", 0)
            acked = s.get("acked", 0)
            resolved = s.get("resolved", 0)
            results.append({
                "sufficiency_level": level,
                "count": count,
                "acked": acked,
                "resolved": resolved,
                "ack_rate": round(acked / count, 3) if count > 0 else 0.0,
                "resolve_rate": round(resolved / count, 3) if count > 0 else 0.0,
            })
        return results
