"""Database ContentMixin 领域 Mixin（T3-B 从 database.py 拆分）。

方法体与迁移前 verbatim 一致，通过 Database 组合后仍通过 self 访问 _mongo/_mem/_use_mongo 等核心状态。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Dict, List, Optional

from app.core.db_utils import clean_mongo_doc, clean_mongo_docs


class ContentMixin:
    async def create_topic(self, topic_data: dict) -> str:
        """创建学习主题"""
        await self.connect()
        topic_data["created_at"] = datetime.now()
        topic_data["updated_at"] = datetime.now()
        if self._use_mongo:
            await self._mongo.topics.insert_one(topic_data)
        else:
            self._topics.append(topic_data)
        return topic_data.get("topic_id")
    async def get_topic(self, topic_id: str) -> Optional[dict]:
        """获取主题"""
        await self.connect()
        if self._use_mongo:
            doc = await self._mongo.topics.find_one({"topic_id": topic_id}, {"_id": 0})
            return clean_mongo_doc(doc)
        for t in self._topics:
            if t.get("topic_id") == topic_id:
                return t
        return None
    async def get_user_topics(
        self,
        user_id: str,
        status: Optional[str] = None,
        limit: int = 100
    ) -> List[dict]:
        """获取用户的所有主题"""
        await self.connect()
        if self._use_mongo:
            query = {"user_id": user_id}
            if status:
                query["status"] = status
            cursor = self._mongo.topics.find(query, {"_id": 0}).sort("updated_at", -1).limit(limit)
            docs = await cursor.to_list(limit)
            return clean_mongo_docs(docs)

        result = [t for t in self._topics if t.get("user_id") == user_id]
        if status:
            result = [t for t in result if t.get("status") == status]
        return sorted(
            result,
            key=lambda x: x.get("updated_at", ""),
            reverse=True
        )[:limit]
    async def update_topic(self, topic_id: str, update_data: dict) -> bool:
        """更新主题"""
        await self.connect()
        update_data["updated_at"] = datetime.now()
        if self._use_mongo:
            result = await self._mongo.topics.update_one(
                {"topic_id": topic_id},
                {"$set": update_data}
            )
            return result.modified_count > 0
        for t in self._topics:
            if t.get("topic_id") == topic_id:
                t.update(update_data)
                return True
        return False
    async def delete_topic(self, topic_id: str) -> bool:
        """删除主题"""
        await self.connect()
        if self._use_mongo:
            result = await self._mongo.topics.delete_one({"topic_id": topic_id})
            return result.deleted_count > 0
        original_len = len(self._topics)
        self._topics = [t for t in self._topics if t.get("topic_id") != topic_id]
        return len(self._topics) < original_len
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
    async def create_study_plan(self, plan_data: dict) -> str:
        """创建学习计划"""
        await self.connect()
        plan_data["created_at"] = datetime.now()
        plan_data["updated_at"] = datetime.now()
        if self._use_mongo:
            await self._mongo.study_plans.insert_one(plan_data)
        else:
            self._study_plans.append(plan_data)
        return plan_data.get("plan_id")
    async def get_study_plan(self, plan_id: str) -> Optional[dict]:
        """获取学习计划"""
        await self.connect()
        if self._use_mongo:
            doc = await self._mongo.study_plans.find_one({"plan_id": plan_id}, {"_id": 0})
            return clean_mongo_doc(doc)
        for p in self._study_plans:
            if p.get("plan_id") == plan_id:
                return p
        return None
    async def get_topic_study_plans(
        self,
        topic_id: str,
        status: Optional[str] = None,
        limit: int = 100
    ) -> List[dict]:
        """获取主题下的学习计划"""
        await self.connect()
        if self._use_mongo:
            query = {"topic_id": topic_id}
            if status:
                query["status"] = status
            cursor = self._mongo.study_plans.find(query, {"_id": 0}).sort("updated_at", -1).limit(limit)
            docs = await cursor.to_list(limit)
            return clean_mongo_docs(docs)

        result = [p for p in self._study_plans if p.get("topic_id") == topic_id]
        if status:
            result = [p for p in result if p.get("status") == status]
        return sorted(
            result,
            key=lambda x: x.get("updated_at", ""),
            reverse=True
        )[:limit]
    async def update_study_plan(self, plan_id: str, update_data: dict) -> bool:
        """更新学习计划"""
        await self.connect()
        update_data["updated_at"] = datetime.now()
        if self._use_mongo:
            result = await self._mongo.study_plans.update_one(
                {"plan_id": plan_id},
                {"$set": update_data}
            )
            return result.modified_count > 0
        for p in self._study_plans:
            if p.get("plan_id") == plan_id:
                p.update(update_data)
                return True
        return False
    async def delete_study_plan(self, plan_id: str) -> bool:
        """删除学习计划"""
        await self.connect()
        if self._use_mongo:
            result = await self._mongo.study_plans.delete_one({"plan_id": plan_id})
            return result.deleted_count > 0
        original_len = len(self._study_plans)
        self._study_plans = [p for p in self._study_plans if p.get("plan_id") != plan_id]
        return len(self._study_plans) < original_len
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
