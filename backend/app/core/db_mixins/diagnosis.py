"""Database DiagnosisMixin 领域 Mixin（T3-B② 从 database.py 拆分）。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional

from app.core.db_utils import clean_mongo_doc


class DiagnosisMixin:
    async def save_tool_audit_log(self, entry: dict) -> str:
        """记录 Agent 工具调用审计日志（工具名 + 参数 + 调用者）

        用途：Agent 每次工具调用（查询监控数据/知识库/创建工单等）留痕，
        满足企业安全评审"哪些数据被发给了外部 LLM、谁触发的"的可追溯要求。
        """
        await self.connect()
        entry.setdefault("created_at", datetime.now())
        if self._use_mongo:
            data_to_save = entry.copy()
            await self._mongo.tool_audit_logs.insert_one(data_to_save)
        else:
            self._tool_audit_logs.append(entry)
        return entry.get("audit_id", "")
    async def save_feedback(self, feedback_data: dict) -> str:
        """保存答案反馈（点赞/点踩 + 评论）"""
        await self.connect()
        feedback_data.setdefault("created_at", datetime.now())
        if self._use_mongo:
            data_to_save = feedback_data.copy()
            await self._mongo.feedback.insert_one(data_to_save)
        else:
            self._feedback: List[Dict] = getattr(self, "_feedback", [])
            self._feedback.append(feedback_data)
        return feedback_data.get("feedback_id", "")
    async def mark_documents_for_review(self, document_ids: List[str],
                                        reason: str = "negative_feedback",
                                        feedback_id: str = "") -> int:
        """将文档标记为待复核（负反馈 → 知识库质量闭环）

        Args:
            document_ids: 需要复核的文档 ID 列表
            reason: 标记原因
            feedback_id: 关联的反馈 ID（可追溯）

        Returns:
            实际标记的文档数
        """
        if not document_ids:
            return 0
        await self.connect()
        marked = 0
        for doc_id in document_ids:
            if self._use_mongo:
                result = await self._mongo.documents.update_one(
                    {"document_id": doc_id},
                    {"$set": {
                        "needs_review": True,
                        "review_reason": reason,
                        "review_feedback_id": feedback_id,
                        "reviewed_at": datetime.now(),
                    }},
                )
                marked += result.matched_count
            else:
                self._knowledge.append({
                    "document_id": doc_id, "needs_review": True,
                    "review_reason": reason, "review_feedback_id": feedback_id,
                })
                marked += 1
        return marked
    async def save_diagnosis_task(self, task_data: dict) -> str:
        """创建诊断任务（status=pending）"""
        await self.connect()
        task_data.setdefault("status", "pending")
        task_data.setdefault("attempts", 0)
        task_data.setdefault("created_at", datetime.now())
        task_data["updated_at"] = datetime.now()
        if self._use_mongo:
            data_to_save = task_data.copy()
            await self._mongo.diagnosis_tasks.insert_one(data_to_save)
        else:
            self._diagnosis_tasks.append(task_data.copy())
        return task_data["task_id"]
    async def claim_next_diagnosis_task(self, claimed_by: str) -> Optional[dict]:
        """原子认领最早的 pending 任务（FIFO）

        Mongo find_one_and_update 是单文档原子操作：多实例并发认领时
        只有一个能成功，其余实例拿到下一个或 None——跨实例互斥的实现基础。
        认领即计一次尝试（attempts+1）。
        跳过退避中的任务（not_before 未到）：失败重试不立即再消费。
        """
        await self.connect()
        now = datetime.now()
        if self._use_mongo:
            from pymongo.collection import ReturnDocument
            doc = await self._mongo.diagnosis_tasks.find_one_and_update(
                {"status": "pending",
                 "$or": [{"not_before": None}, {"not_before": {"$exists": False}},
                         {"not_before": {"$lte": now}}]},
                {"$set": {
                    "status": "running",
                    "claimed_by": claimed_by,
                    "claimed_at": now,
                    "updated_at": now,
                }, "$inc": {"attempts": 1}},
                sort=[("created_at", 1)],
                return_document=ReturnDocument.AFTER,
            )
            return clean_mongo_doc(doc)
        candidates = [
            t for t in self._diagnosis_tasks
            if t.get("status") == "pending"
            and (not t.get("not_before") or t["not_before"] <= now)
        ]
        if not candidates:
            return None
        task = min(candidates, key=lambda t: t.get("created_at") or now)
        task["status"] = "running"
        task["claimed_by"] = claimed_by
        task["claimed_at"] = now
        task["attempts"] = (task.get("attempts") or 0) + 1
        task["updated_at"] = now
        return task
    async def recover_stale_diagnosis_tasks(self, stale_seconds: int) -> int:
        """将卡在 running 的僵尸任务重置为 pending（实例崩溃恢复）

        claimed_at 超过 stale_seconds 仍为 running 的任务视为处理实例已死，
        回到 pending 队列由存活实例重新认领（attempts 不变——认领时才计数）。
        启动时调用（此时全部 running 都是上个进程的遗留）。
        """
        await self.connect()
        cutoff = datetime.now() - timedelta(seconds=stale_seconds)
        if self._use_mongo:
            result = await self._mongo.diagnosis_tasks.update_many(
                {"status": "running", "claimed_at": {"$lt": cutoff}},
                {"$set": {"status": "pending", "claimed_by": "", "updated_at": datetime.now()}},
            )
            return result.modified_count
        recovered = 0
        for task in self._diagnosis_tasks:
            if task.get("status") == "running" and task.get("claimed_at") \
                    and task["claimed_at"] < cutoff:
                task["status"] = "pending"
                task["claimed_by"] = ""
                task["updated_at"] = datetime.now()
                recovered += 1
        return recovered
    async def update_diagnosis_task(self, task_id: str, fields: dict) -> bool:
        """更新任务字段（状态流转 / last_error）"""
        await self.connect()
        fields = {**fields, "updated_at": datetime.now()}
        if self._use_mongo:
            result = await self._mongo.diagnosis_tasks.update_one(
                {"task_id": task_id}, {"$set": fields},
            )
            return result.matched_count > 0
        for task in self._diagnosis_tasks:
            if task.get("task_id") == task_id:
                task.update(fields)
                return True
        return False
