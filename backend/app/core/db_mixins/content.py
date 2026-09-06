"""Database ContentMixin 领域 Mixin（T3-B 从 database.py 拆分）。

方法体与迁移前 verbatim 一致，通过 Database 组合后仍通过 self 访问 _mongo/_mem/_use_mongo 等核心状态。"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.core.db_utils import clean_mongo_doc, clean_mongo_docs


class ContentMixin:
    # 属性由 Database 组合后在其 __init__ 赋值（Mixin 自身不初始化）；
    # 类级声明让 mypy 识别跨 Mixin 共享状态（P1-C 棘轮：新增代码零错误）。
    _mongo: Any
    _use_mongo: bool
    _documents: List[Dict]
    _organizations: List[Dict]

    async def create_document(self, doc_data: dict) -> str:
        """创建文档记录"""
        await self.connect()
        doc_data["created_at"] = datetime.now()
        doc_data["updated_at"] = datetime.now()
        if self._use_mongo:
            await self._mongo.documents.insert_one(doc_data)
        else:
            self._documents.append(doc_data)
        return doc_data.get("document_id")

    async def get_document(self, document_id: str) -> Optional[dict]:
        """获取文档记录"""
        await self.connect()
        if self._use_mongo:
            doc = await self._mongo.documents.find_one({"document_id": document_id}, {"_id": 0})
            return clean_mongo_doc(doc)
        for d in self._documents:
            if d.get("document_id") == document_id:
                return d
        return None
    async def get_topic_documents(
        self,
        topic_id: str,
        status: Optional[str] = None,
        limit: int = 100
    ) -> List[dict]:
        """获取主题下的所有文档"""
        await self.connect()
        if self._use_mongo:
            query = {"topic_id": topic_id}
            if status:
                query["status"] = status
            cursor = self._mongo.documents.find(query, {"_id": 0}).sort("created_at", -1).limit(limit)
            docs = await cursor.to_list(limit)
            return clean_mongo_docs(docs)

        result = [d for d in self._documents if d.get("topic_id") == topic_id]
        if status:
            result = [d for d in result if d.get("status") == status]
        return sorted(
            result,
            key=lambda x: x.get("created_at", ""),
            reverse=True
        )[:limit]
    async def get_user_documents(
        self,
        user_id: str,
        status: Optional[str] = None,
        limit: int = 100
    ) -> List[dict]:
        """获取用户的所有文档（包括公共文档）"""
        await self.connect()
        if self._use_mongo:
            query = {"$or": [{"user_id": user_id}, {"is_public": True}]}
            if status:
                query = {"$and": [query, {"status": status}]}
            cursor = self._mongo.documents.find(query, {"_id": 0}).sort("created_at", -1).limit(limit)
            docs = await cursor.to_list(limit)
            return clean_mongo_docs(docs)

        result = [d for d in self._documents if d.get("user_id") == user_id or d.get("is_public")]
        if status:
            result = [d for d in result if d.get("status") == status]
        return sorted(
            result,
            key=lambda x: x.get("created_at", ""),
            reverse=True
        )[:limit]
    async def get_all_documents(
        self,
        status: Optional[str] = None,
        limit: int = 1000,
    ) -> List[dict]:
        """获取所有文档（管理员视角，跨用户）"""
        await self.connect()
        if self._use_mongo:
            query: dict = {}
            if status:
                query["status"] = status
            cursor = self._mongo.documents.find(query, {"_id": 0}).sort("created_at", -1).limit(limit)
            docs = await cursor.to_list(limit)
            return clean_mongo_docs(docs)
        result = list(self._documents)
        if status:
            result = [d for d in result if d.get("status") == status]
        return sorted(
            result,
            key=lambda x: x.get("created_at", ""),
            reverse=True
        )[:limit]
    async def update_document(self, document_id: str, update_data: dict) -> bool:
        """更新文档记录"""
        await self.connect()
        update_data["updated_at"] = datetime.now()
        if self._use_mongo:
            result = await self._mongo.documents.update_one(
                {"document_id": document_id},
                {"$set": update_data}
            )
            return result.modified_count > 0
        for d in self._documents:
            if d.get("document_id") == document_id:
                d.update(update_data)
                return True
        return False
    async def delete_document(self, document_id: str) -> bool:
        """删除文档记录"""
        await self.connect()
        if self._use_mongo:
            result = await self._mongo.documents.delete_one({"document_id": document_id})
            return result.deleted_count > 0
        original_len = len(self._documents)
        self._documents = [d for d in self._documents if d.get("document_id") != document_id]
        return len(self._documents) < original_len

    async def find_stale_documents(self, stale_seconds: int, statuses=None, limit: int = 100) -> List[dict]:
        """查询卡死的文档（指定状态超 threshold 未推进）

        供启动捞回与周期扫描使用（与诊断域 find_stale_active_incidents 同模式）。
        卡死判定锚点 updated_at 由 create/update_document 自动维护。
        """
        if statuses is None:
            statuses = ["pending", "processing"]
        cutoff = datetime.now() - timedelta(seconds=stale_seconds)
        await self.connect()
        if self._use_mongo:
            cursor = self._mongo.documents.find(
                {"status": {"$in": list(statuses)}, "updated_at": {"$lt": cutoff}},
                {"_id": 0},
            ).limit(limit)
            return [clean_mongo_doc(d) async for d in cursor]
        return [
            dict(d) for d in self._documents
            if d.get("status") in statuses
            and isinstance(d.get("updated_at"), datetime)
            and d["updated_at"] < cutoff
        ][:limit]

    async def find_documents_by_status(self, status: str, limit: int = 100) -> List[dict]:
        """按状态查询文档（如 deleting 挂起清理的 finalize 扫描）"""
        await self.connect()
        if self._use_mongo:
            cursor = self._mongo.documents.find(
                {"status": status},
                {"_id": 0},
            ).limit(limit)
            return [clean_mongo_doc(d) async for d in cursor]
        return [dict(d) for d in self._documents if d.get("status") == status][:limit]

    async def ensure_document_indexes(self) -> None:
        """documents 集合索引（幂等：同名 create_index no-op）。

        - uniq_document_id: document_id 唯一（上传并发的最后防线）
        - idx_status_updated: 卡死扫描（find_stale_documents）与状态过滤
        - idx_user_public: 列表查询（get_user_documents 的 $or 前缀）
        """
        await self.connect()
        if not self._use_mongo:
            return
        await self._mongo.documents.create_index(
            [("document_id", 1)], unique=True, name="uniq_document_id"
        )
        await self._mongo.documents.create_index(
            [("status", 1), ("updated_at", 1)], name="idx_status_updated"
        )
        await self._mongo.documents.create_index(
            [("user_id", 1), ("is_public", 1)], name="idx_user_public"
        )

    async def create_org(self, name: str, owner_id: str) -> str:
        """创建组织"""
        await self.connect()
        org_id = f"org_{uuid.uuid4().hex[:12]}"
        org = {
            "org_id": org_id,
            "name": name,
            "owner_id": owner_id,
            "created_at": datetime.now(),
            "updated_at": datetime.now(),
        }
        if self._use_mongo:
            await self._mongo.organizations.insert_one(org)
        else:
            self._organizations.append(org)
        return org_id
    async def get_org(self, org_id: str) -> Optional[Dict]:
        """获取组织"""
        await self.connect()
        if self._use_mongo:
            return await self._mongo.organizations.find_one({"org_id": org_id}, {"_id": 0})
        for org in self._organizations:
            if org.get("org_id") == org_id:
                return org
        return None
    async def get_org_by_name(self, name: str) -> Optional[Dict]:
        """按名称获取组织"""
        await self.connect()
        if self._use_mongo:
            return await self._mongo.organizations.find_one({"name": name}, {"_id": 0})
        for org in self._organizations:
            if org.get("name") == name:
                return org
        return None
    async def get_user_orgs(self, user_id: str) -> List[Dict]:
        """获取用户创建的组织"""
        await self.connect()
        if self._use_mongo:
            cursor = self._mongo.organizations.find({"owner_id": user_id}, {"_id": 0})
            return await cursor.to_list(100)
        return [org for org in self._organizations if org.get("owner_id") == user_id]
