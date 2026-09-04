"""
数据库连接和操作模块
支持MongoDB和内存存储（开发测试用）
"""

import copy
import re
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from loguru import logger

from .config import settings


def escape_regex(pattern: str) -> str:
    """转义正则表达式特殊字符，防止注入"""
    return re.escape(pattern)


def clean_mongo_doc(doc: Optional[Dict]) -> Optional[Dict]:
    """清理MongoDB文档，移除ObjectId，转换datetime为字符串（不修改原始文档）"""
    if doc is None:
        return None
    result = copy.deepcopy(doc)
    if "_id" in result:
        del result["_id"]
    # 转换datetime对象为字符串
    for key, value in result.items():
        if isinstance(value, datetime):
            result[key] = value.isoformat()
    return result


def clean_mongo_docs(docs: List[Dict]) -> List[Dict]:
    """清理MongoDB文档列表"""
    return [clean_mongo_doc(doc) for doc in docs]


class Database:
    """数据库管理类"""

    def __init__(self):
        self._users: List[Dict] = []
        self._sessions: List[Dict] = []
        self._progress: List[Dict] = []
        self._questions: List[Dict] = []
        self._knowledge: List[Dict] = []
        self._quiz_records: List[Dict] = []
        self._analytics: Dict[str, Dict] = {}
        self._learning_states: List[Dict] = []
        self._student_states: List[Dict] = []
        self._tool_audit_logs: List[Dict] = []
        self._incidents: List[Dict] = []
        self._diagnosis_tasks: List[Dict] = []
        self._session_memories: List[Dict] = []
        self._long_term_memories: List[Dict] = []
        self._user_profiles: List[Dict] = []
        self._user_knowledge_bases: List[Dict] = []
        self._organizations: List[Dict] = []
        self._courses: List[Dict] = []
        self._knowledge_points: List[Dict] = []
        self._teaching_experiences: List[Dict] = []
        self._user_documents: List[Dict] = []
        self._evaluation_records: List[Dict] = []
        self._topics: List[Dict] = []
        self._documents: List[Dict] = []
        self._study_plans: List[Dict] = []
        self._connected = False
        self._use_mongo = False
        self._mongo = None
        self._client = None  # 保存 Motor client 引用，用于关闭连接

    async def connect(self):
        """连接数据库"""
        if self._connected:
            # 测试环境下 event loop 可能在多个 TestClient 之间变化，
            # 如果缓存的 client 绑定的 loop 已关闭，需要重新连接。
            if self._client is not None:
                try:
                    if self._client.get_io_loop().is_closed():
                        self._client = None
                        self._mongo = None
                        self._connected = False
                    else:
                        return
                except Exception:
                    return
            else:
                return

        try:
            from motor.motor_asyncio import AsyncIOMotorClient
            self._client = AsyncIOMotorClient(settings.MONGODB_URL, serverSelectionTimeoutMS=5000)
            await self._client.admin.command('ping')
            self._mongo = self._client[settings.MONGODB_DB_NAME]
            self._use_mongo = True
            logger.info(f"MongoDB连接成功: {settings.MONGODB_DB_NAME}")
        except Exception as e:
            logger.warning(f"MongoDB连接失败，使用内存存储: {e}")
            self._use_mongo = False
            self._client = None
            # 创建测试用户（密码哈希在运行时生成，避免bcrypt导入问题）
            try:
                from .auth import get_password_hash
                hashed_password = get_password_hash("123456")
            except Exception:
                # 如果bcrypt有问题，使用一个预计算的哈希值
                hashed_password = "$2b$12$LJ3m4ys3Lz0YBNOURq0Y3OjCfKJmKPOJYqDTPVCKzBXlqJKWzqDZK"
            self._users.append({
                "user_id": "test_user_001",
                "username": "test",
                "email": "test@example.com",
                "role": "student",
                "org_id": "org_test",
                "hashed_password": hashed_password
            })
            self._organizations.append({
                "org_id": "org_test",
                "name": "test-org",
                "owner_id": "test_user_001",
                "created_at": datetime.now(),
                "updated_at": datetime.now(),
            })
            logger.info("已创建测试用户: test/123456")

        self._connected = True

    async def disconnect(self):
        """关闭数据库连接"""
        if self._client:
            self._client.close()
            self._client = None
            self._mongo = None
            self._connected = False
            logger.info("MongoDB连接已关闭")
        else:
            logger.info("内存数据库已清除")

    # ========== 用户操作 ==========

    async def create_user(self, user_data: dict) -> str:
        await self.connect()
        user_data["created_at"] = datetime.now()
        user_data["updated_at"] = datetime.now()
        user_data.setdefault("token_version", 0)  # C1 JWT 吊销：版本号，logout/改密/降权时递增
        if self._use_mongo:
            await self._mongo.users.insert_one(user_data)
        else:
            self._users.append(user_data)
        return user_data.get("user_id")

    async def get_user(self, user_id: str) -> Optional[dict]:
        await self.connect()
        if self._use_mongo:
            return await self._mongo.users.find_one({"user_id": user_id})
        for u in self._users:
            if u.get("user_id") == user_id:
                return u
        return None

    async def increment_token_version(self, user_id: str) -> int:
        """递增 token_version（C1 JWT 服务端吊销）

        logout / 改密 / 降权时调用——使该用户所有旧 token 立即失效
        （get_current_user 校验 token.ver != db.token_version → 401）。
        老用户无 token_version 字段时 $inc 自动建为 1（即首次递增后旧 token 失效）。
        """
        await self.connect()
        if self._use_mongo:
            result = await self._mongo.users.find_one_and_update(
                {"user_id": user_id},
                {"$inc": {"token_version": 1}, "$set": {"updated_at": datetime.now()}},
                return_document=True,
            )
            return (result or {}).get("token_version", 1)
        for u in self._users:
            if u.get("user_id") == user_id:
                u["token_version"] = (u.get("token_version") or 0) + 1
                u["updated_at"] = datetime.now()
                return u["token_version"]
        return 0

    async def get_user_by_username(self, username: str) -> Optional[dict]:
        await self.connect()
        if self._use_mongo:
            return await self._mongo.users.find_one({"username": username})
        for u in self._users:
            if u.get("username") == username:
                return u
        return None

    async def update_user(self, user_id: str, update_data: dict) -> bool:
        await self.connect()
        update_data["updated_at"] = datetime.now()
        if self._use_mongo:
            result = await self._mongo.users.update_one({"user_id": user_id}, {"$set": update_data})
            return result.modified_count > 0
        for u in self._users:
            if u.get("user_id") == user_id:
                u.update(update_data)
                return True
        return False

    async def get_all_users(self, page: int = 1, page_size: int = 20) -> tuple:
        """分页列出所有用户（管理员视角，过滤 hashed_password）

        Returns:
            (users, total): 用户列表（无敏感字段）+ 总数
        """
        await self.connect()
        if self._use_mongo:
            total = await self._mongo.users.count_documents({})
            skip = (page - 1) * page_size
            cursor = self._mongo.users.find({}, {"_id": 0, "hashed_password": 0}).sort("created_at", -1).skip(skip).limit(page_size)
            users = await cursor.to_list(page_size)
            return clean_mongo_docs(users), total
        total = len(self._users)
        start = (page - 1) * page_size
        sorted_users = sorted(self._users, key=lambda x: x.get("created_at", ""), reverse=True)
        users = [{k: v for k, v in u.items() if k != "hashed_password"} for u in sorted_users[start:start + page_size]]
        return users, total

    async def count_users(self) -> int:
        """用户总数"""
        await self.connect()
        if self._use_mongo:
            return await self._mongo.users.count_documents({})
        return len(self._users)

    # ========== 主题操作 ==========

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

    # ========== 文档操作 ==========

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

    # ========== 学习计划操作 ==========

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

    # ========== 组织操作 ==========

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

    # ========== 会话操作 ==========

    async def create_session(self, user_id: str, title: str = "新对话") -> str:
        await self.connect()
        session_id = f"session_{uuid.uuid4().hex[:12]}"
        session = {
            "session_id": session_id,
            "user_id": user_id,
            "title": title,
            "messages": [],
            "created_at": datetime.now(),
            "updated_at": datetime.now()
        }
        if self._use_mongo:
            await self._mongo.chat_sessions.insert_one(session)
        else:
            self._sessions.append(session)
        return session_id

    async def get_session(self, session_id: str) -> Optional[dict]:
        """获取会话（不含消息）"""
        await self.connect()
        if self._use_mongo:
            doc = await self._mongo.chat_sessions.find_one(
                {"session_id": session_id},
                {"_id": 0, "messages": 0}
            )
            return clean_mongo_doc(doc)
        for s in self._sessions:
            if s.get("session_id") == session_id:
                # 统一转换 datetime → ISO 字符串（与 get_user_sessions 一致）
                result = {k: v for k, v in s.items() if k != "messages"}
                for key in ("created_at", "updated_at"):
                    if isinstance(result.get(key), datetime):
                        result[key] = result[key].isoformat()
                return result
        return None

    async def update_session_title(self, session_id: str, title: str) -> None:
        """更新会话标题"""
        await self.connect()
        if self._use_mongo:
            await self._mongo.chat_sessions.update_one(
                {"session_id": session_id},
                {"$set": {"title": title, "updated_at": datetime.now()}}
            )
        else:
            for s in self._sessions:
                if s.get("session_id") == session_id:
                    s["title"] = title
                    s["updated_at"] = datetime.now()
                    break

    async def add_message(self, session_id: str, message: dict):
        await self.connect()
        message["timestamp"] = datetime.now()
        if self._use_mongo:
            await self._mongo.chat_sessions.update_one(
                {"session_id": session_id},
                {"$push": {"messages": message}, "$set": {"updated_at": datetime.now()}}
            )
        else:
            for s in self._sessions:
                if s.get("session_id") == session_id:
                    s["messages"].append(message)
                    s["updated_at"] = datetime.now()
                    break

    async def get_session_messages(self, session_id: str, limit: int = 50) -> List[dict]:
        await self.connect()
        if self._use_mongo:
            session = await self._mongo.chat_sessions.find_one(
                {"session_id": session_id},
                {"_id": 0}
            )
            if session and "messages" in session:
                # 转换 datetime 对象为字符串
                messages = session["messages"][-limit:]
                for msg in messages:
                    if isinstance(msg.get("timestamp"), datetime):
                        msg["timestamp"] = msg["timestamp"].isoformat()
                return messages
            return []
        for s in self._sessions:
            if s.get("session_id") == session_id:
                messages = s.get("messages", [])[-limit:]
                for msg in messages:
                    if isinstance(msg.get("timestamp"), datetime):
                        msg["timestamp"] = msg["timestamp"].isoformat()
                return messages
        return []

    async def get_user_sessions(self, user_id: str, limit: int = 20) -> List[dict]:
        """获取用户的所有会话（含 message_count，不含消息体）

        Args:
            user_id: 用户 ID
            limit: 返回数量上限

        Returns:
            List[dict]: 会话列表，按 updated_at 降序，每项含 message_count
        """
        await self.connect()
        if self._use_mongo:
            # 用 aggregation 计算消息数量，避免拉取整个 messages 数组
            cursor = self._mongo.chat_sessions.aggregate([
                {"$match": {"user_id": user_id}},
                {"$project": {
                    "_id": 0,
                    "session_id": 1,
                    "user_id": 1,
                    "title": 1,
                    "created_at": 1,
                    "updated_at": 1,
                    "message_count": {"$size": {"$ifNull": ["$messages", []]}},
                }},
                {"$sort": {"updated_at": -1}},
                {"$limit": limit},
            ])
            sessions = await cursor.to_list(limit)
        else:
            sessions = []
            for s in self._sessions:
                if s.get("user_id") != user_id:
                    continue
                sessions.append({
                    "session_id": s.get("session_id"),
                    "user_id": s.get("user_id"),
                    "title": s.get("title", "新对话"),
                    "created_at": s.get("created_at"),
                    "updated_at": s.get("updated_at"),
                    "message_count": len(s.get("messages", [])),
                })

        # 清理 datetime → ISO 字符串（统一响应格式）
        for s in sessions:
            if isinstance(s.get("updated_at"), datetime):
                s["updated_at"] = s["updated_at"].isoformat()
            if isinstance(s.get("created_at"), datetime):
                s["created_at"] = s["created_at"].isoformat()

        # 内存模式已排序，MongoDB 模式由 aggregation 排序；这里统一兜底排序
        def _sort_key(x):
            val = x.get("updated_at", "")
            return val if isinstance(val, str) else str(val)

        return sorted(sessions, key=_sort_key, reverse=True)[:limit]

    async def get_session_message_count(self, session_id: str) -> int:
        """获取会话消息数量"""
        await self.connect()
        if self._use_mongo:
            session = await self._mongo.chat_sessions.find_one(
                {"session_id": session_id},
                {"_id": 0, "messages": 1}
            )
            if session and "messages" in session:
                return len(session["messages"])
            return 0
        for s in self._sessions:
            if s.get("session_id") == session_id:
                return len(s.get("messages", []))
        return 0

    async def delete_session(self, session_id: str) -> bool:
        """删除会话（含全部消息）

        Args:
            session_id: 会话 ID

        Returns:
            bool: 是否删除成功
        """
        await self.connect()
        if self._use_mongo:
            result = await self._mongo.chat_sessions.delete_one(
                {"session_id": session_id}
            )
            return result.deleted_count > 0
        original_len = len(self._sessions)
        self._sessions = [
            s for s in self._sessions if s.get("session_id") != session_id
        ]
        return len(self._sessions) < original_len

    # ========== 学习进度操作 ==========

    async def update_progress(self, user_id: str, course_id: str, topic: str, score: int):
        await self.connect()
        progress = {
            "user_id": user_id,
            "course_id": course_id,
            "topic": topic,
            "score": score,
            "updated_at": datetime.now()
        }
        if self._use_mongo:
            await self._mongo.learning_progress.update_one(
                {"user_id": user_id, "course_id": course_id, "topic": topic},
                {"$set": progress}, upsert=True
            )
        else:
            for p in self._progress:
                if p.get("user_id") == user_id and p.get("course_id") == course_id and p.get("topic") == topic:
                    p.update(progress)
                    return
            self._progress.append(progress)

    async def get_user_progress(self, user_id: str, course_id: Optional[str] = None) -> List[dict]:
        await self.connect()
        if self._use_mongo:
            query = {"user_id": user_id}
            if course_id:
                query["course_id"] = course_id
            cursor = self._mongo.learning_progress.find(query, {"_id": 0})
            return await cursor.to_list(100)
        result = [p for p in self._progress if p.get("user_id") == user_id]
        if course_id:
            result = [p for p in result if p.get("course_id") == course_id]
        return result

    # ========== 题目操作 ==========

    async def save_question(self, question_data: dict) -> str:
        await self.connect()
        question_data["created_at"] = datetime.now()
        if self._use_mongo:
            # 复制一份避免 MongoDB 修改原始数据
            data_to_save = question_data.copy()
            await self._mongo.questions.insert_one(data_to_save)
        else:
            self._questions.append(question_data)
        return question_data.get("question_id")

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

    # ========== Incident 注册表（告警自动诊断的事故生命周期） ==========

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

    # ========== D1 根因模式统计（问题管理入口） ==========
    # ITIL 问题管理触发条件"事件反复出现"——按 root_cause 聚合统计频次，
    # 回答"本月哪个根因反复出现"，作为主动消除高频根因的决策依据。
    #
    # 统计口径：
    # - 时间窗：first_seen_at 在最近 N 天内（按事故首次出现时间过滤）
    # - 去重：同一 incident 多次诊断（初诊/重诊）的同一 root_cause 只计一次
    #   ——否则重诊次数多的事故会虚增该根因的频次
    # - 排除：trigger=summary 的摘要条目（摘要与诊断描述同一根因，避免重复计数）

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

    # ========== D2 诊断质量分层统计（验证"高充分度 → 高采纳率"假设） ==========
    #
    # 产品价值假设链：H1 诊断命中 → H2 响应者采纳。
    # D2 按诊断充分度分层统计 ack 采纳率——若"高充分度 → 高采纳率"不成立，
    # 说明诊断质量（检索/推理）与响应者信任之间存在断点，需排查检索召回或
    # 诊断表达问题。
    #
    # 分层依据：每个事故取**最近一次非摘要诊断**的 sufficiency_level
    # （初诊/重诊的充分度，而非摘要——摘要是事后总结，不代表诊断时刻的质量）。

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

    # ========== 诊断任务表（持久化 + 原子认领 + 重启恢复） ==========
    # 解决两个问题：
    # 1. BackgroundTasks 进程内排队，重启丢任务 → 任务落 Mongo，启动时捞回
    # 2. 多副本重复诊断 → find_one_and_update 原子认领，全集群只有一个实例抢到

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

    async def get_question(self, question_id: str) -> Optional[dict]:
        """获取题目"""
        await self.connect()
        if self._use_mongo:
            doc = await self._mongo.questions.find_one({"question_id": question_id})
            return clean_mongo_doc(doc)
        for q in self._questions:
            if q.get("question_id") == question_id:
                return q
        return None

    async def get_questions_by_topic(
        self,
        topic: str,
        difficulty: Optional[str] = None,
        limit: int = 10,
        fuzzy: bool = False
    ) -> List[dict]:
        """
        按主题获取题目

        Args:
            topic: 主题
            difficulty: 难度
            limit: 数量限制
            fuzzy: 是否模糊匹配
        """
        await self.connect()
        if self._use_mongo:
            if fuzzy:
                # 模糊匹配：topic 包含搜索词（转义特殊字符防止正则注入）
                query = {"topic": {"$regex": escape_regex(topic), "$options": "i"}}
            else:
                query = {"topic": topic}
            if difficulty:
                query["difficulty"] = difficulty
            cursor = self._mongo.questions.find(query)
            docs = await cursor.to_list(limit)
            return clean_mongo_docs(docs)

        # 内存模式
        result = []
        for q in self._questions:
            q_topic = q.get("topic", "")
            if fuzzy:
                if topic in q_topic or q_topic in topic:
                    result.append(q)
            else:
                if q_topic == topic:
                    result.append(q)
        if difficulty:
            result = [q for q in result if q.get("difficulty") == difficulty]
        return result[:limit]

    # ========== 知识库操作 ==========

    async def save_knowledge(self, knowledge_data: dict) -> str:
        """保存知识点"""
        await self.connect()
        knowledge_data["created_at"] = datetime.now()
        knowledge_data["updated_at"] = datetime.now()
        if self._use_mongo:
            await self._mongo.knowledge_base.insert_one(knowledge_data)
        else:
            if not hasattr(self, '_knowledge'):
                self._knowledge = []
            self._knowledge.append(knowledge_data)
        return knowledge_data.get("knowledge_id")

    async def get_knowledge(self, knowledge_id: str) -> Optional[dict]:
        """获取知识点"""
        await self.connect()
        if self._use_mongo:
            doc = await self._mongo.knowledge_base.find_one({"knowledge_id": knowledge_id})
            return clean_mongo_doc(doc)
        if not hasattr(self, '_knowledge'):
            self._knowledge = []
        for k in self._knowledge:
            if k.get("knowledge_id") == knowledge_id:
                return k
        return None

    async def get_knowledge_by_topic(self, topic: str) -> Optional[dict]:
        """根据主题获取知识点"""
        await self.connect()
        if self._use_mongo:
            doc = await self._mongo.knowledge_base.find_one({"topic": topic})
            return clean_mongo_doc(doc)
        if not hasattr(self, '_knowledge'):
            self._knowledge = []
        for k in self._knowledge:
            if k.get("topic") == topic:
                return k
        return None

    async def search_knowledge(self, keyword: str, course_id: Optional[str] = None) -> List[dict]:
        """搜索知识点"""
        await self.connect()
        if self._use_mongo:
            escaped_keyword = escape_regex(keyword)
            query = {
                "$or": [
                    {"topic": {"$regex": escaped_keyword, "$options": "i"}},
                    {"content": {"$regex": escaped_keyword, "$options": "i"}},
                    {"key_points": {"$regex": escaped_keyword, "$options": "i"}}
                ]
            }
            if course_id:
                query["course_id"] = course_id
            cursor = self._mongo.knowledge_base.find(query)
            docs = await cursor.to_list(50)
            return clean_mongo_docs(docs)
        if not hasattr(self, '_knowledge'):
            self._knowledge = []
        result = []
        keyword_lower = keyword.lower()
        for k in self._knowledge:
            if course_id and k.get("course_id") != course_id:
                continue
            if (keyword_lower in k.get("topic", "").lower() or
                keyword_lower in k.get("content", "").lower() or
                any(keyword_lower in kp.lower() for kp in k.get("key_points", []))):
                result.append(k)
        return result

    async def get_knowledge_by_course(self, course_id: str) -> List[dict]:
        """获取课程下的所有知识点"""
        await self.connect()
        if self._use_mongo:
            cursor = self._mongo.knowledge_base.find({"course_id": course_id})
            docs = await cursor.to_list(100)
            return clean_mongo_docs(docs)
        if not hasattr(self, '_knowledge'):
            self._knowledge = []
        return [k for k in self._knowledge if k.get("course_id") == course_id]

    async def get_courses(self) -> List[dict]:
        """获取所有课程"""
        await self.connect()
        if self._use_mongo:
            pipeline = [
                {"$group": {
                    "_id": "$course_id",
                    "course_name": {"$first": "$course_name"},
                    "topic_count": {"$sum": 1}
                }},
                {"$project": {
                    "course_id": "$_id",
                    "course_name": 1,
                    "topic_count": 1,
                    "_id": 0
                }}
            ]
            cursor = self._mongo.knowledge_base.aggregate(pipeline)
            return await cursor.to_list(50)
        if not hasattr(self, '_knowledge'):
            self._knowledge = []
        courses = {}
        for k in self._knowledge:
            course_id = k.get("course_id")
            if course_id not in courses:
                courses[course_id] = {
                    "course_id": course_id,
                    "course_name": k.get("course_name", ""),
                    "topic_count": 0
                }
            courses[course_id]["topic_count"] += 1
        return list(courses.values())

    # ========== 答题记录操作 ==========

    async def save_quiz_record(self, record_data: dict) -> str:
        """保存答题记录"""
        await self.connect()
        record_data["answered_at"] = datetime.now()
        if self._use_mongo:
            await self._mongo.quiz_records.insert_one(record_data)
        else:
            if not hasattr(self, '_quiz_records'):
                self._quiz_records = []
            self._quiz_records.append(record_data)
        return record_data.get("record_id")

    async def get_user_quiz_records(self, user_id: str, course_id: Optional[str] = None,
                                     limit: int = 100) -> List[dict]:
        """获取用户答题记录"""
        await self.connect()
        if self._use_mongo:
            query = {"user_id": user_id}
            if course_id:
                query["course_id"] = course_id
            cursor = self._mongo.quiz_records.find(query).sort("answered_at", -1).limit(limit)
            return await cursor.to_list(limit)
        if not hasattr(self, '_quiz_records'):
            self._quiz_records = []
        result = [r for r in self._quiz_records if r.get("user_id") == user_id]
        if course_id:
            result = [r for r in result if r.get("course_id") == course_id]
        return sorted(result, key=lambda x: x.get("answered_at", ""), reverse=True)[:limit]

    # ========== 用户统计操作 ==========

    async def get_user_analytics(self, user_id: str) -> Optional[dict]:
        """获取用户统计数据"""
        await self.connect()
        if self._use_mongo:
            return await self._mongo.user_analytics.find_one({"user_id": user_id})
        if not hasattr(self, '_analytics'):
            self._analytics = {}
        return self._analytics.get(user_id)

    async def save_user_analytics(self, user_id: str, analytics: dict) -> None:
        """保存用户统计数据"""
        await self.connect()
        if self._use_mongo:
            await self._mongo.user_analytics.update_one(
                {"user_id": user_id},
                {"$set": analytics},
                upsert=True
            )
        else:
            if not hasattr(self, '_analytics'):
                self._analytics = {}
            self._analytics[user_id] = analytics

    # ========== 学习状态管理 ==========

    async def save_learning_state(self, user_id: str, state: dict) -> bool:
        """保存学习状态"""
        await self.connect()
        if self._use_mongo:
            await self._mongo.learning_states.update_one(
                {"user_id": user_id},
                {"$set": state},
                upsert=True
            )
            return True

        # 内存模式
        if not hasattr(self, '_learning_states'):
            self._learning_states: List[dict] = []

        # 查找并更新
        for i, s in enumerate(self._learning_states):
            if s.get("user_id") == user_id:
                self._learning_states[i] = state
                return True

        # 不存在则添加
        self._learning_states.append(state)
        return True

    async def get_learning_state(self, user_id: str) -> Optional[dict]:
        """获取学习状态"""
        await self.connect()
        if self._use_mongo:
            return await self._mongo.learning_states.find_one({"user_id": user_id})

        # 内存模式
        if not hasattr(self, '_learning_states'):
            self._learning_states: List[dict] = []
            return None

        for state in self._learning_states:
            if state.get("user_id") == user_id:
                return state
        return None

    # ========== Student State 操作 ==========

    async def get_student_state(self, user_id: str) -> Optional[Dict]:
        """获取学生状态"""
        await self.connect()

        if self._use_mongo:
            doc = await self._mongo.student_states.find_one(
                {"user_id": user_id},
                {"_id": 0}
            )
            return clean_mongo_doc(doc)

        # 内存模式
        if not hasattr(self, '_student_states'):
            self._student_states: List[dict] = []

        for state in self._student_states:
            if state.get("user_id") == user_id:
                return state
        return None

    async def save_student_state(self, user_id: str, state_data: Dict) -> None:
        """保存学生状态"""
        await self.connect()

        state_data["user_id"] = user_id
        state_data["updated_at"] = datetime.now().isoformat()

        if self._use_mongo:
            await self._mongo.student_states.update_one(
                {"user_id": user_id},
                {"$set": state_data},
                upsert=True
            )
        else:
            # 内存模式
            if not hasattr(self, '_student_states'):
                self._student_states: List[dict] = []

            # 查找并更新
            for i, state in enumerate(self._student_states):
                if state.get("user_id") == user_id:
                    self._student_states[i] = state_data
                    return

            # 不存在则添加
            self._student_states.append(state_data)

    # ========== 会话记忆操作 ==========

    async def get_session_memory(self, session_id: str) -> Optional[Dict]:
        """获取会话记忆"""
        await self.connect()

        if self._use_mongo:
            doc = await self._mongo.session_memories.find_one(
                {"session_id": session_id},
                {"_id": 0}
            )
            return clean_mongo_doc(doc)

        # 内存模式
        if not hasattr(self, '_session_memories'):
            self._session_memories: List[dict] = []

        for mem in self._session_memories:
            if mem.get("session_id") == session_id:
                return mem
        return None

    async def save_session_memory(self, session_id: str, memory_data: Dict) -> None:
        """保存会话记忆"""
        await self.connect()

        memory_data["session_id"] = session_id
        memory_data["updated_at"] = datetime.now().isoformat()

        if self._use_mongo:
            await self._mongo.session_memories.update_one(
                {"session_id": session_id},
                {"$set": memory_data},
                upsert=True
            )
        else:
            # 内存模式
            if not hasattr(self, '_session_memories'):
                self._session_memories: List[dict] = []

            # 查找并更新
            for i, mem in enumerate(self._session_memories):
                if mem.get("session_id") == session_id:
                    self._session_memories[i] = memory_data
                    return

            # 不存在则添加
            self._session_memories.append(memory_data)

    # ========== 长期记忆操作 ==========

    async def get_long_term_memory(self, user_id: str) -> Optional[Dict]:
        """获取长期记忆"""
        await self.connect()

        if self._use_mongo:
            doc = await self._mongo.long_term_memories.find_one(
                {"user_id": user_id},
                {"_id": 0}
            )
            return clean_mongo_doc(doc)

        # 内存模式
        if not hasattr(self, '_long_term_memories'):
            self._long_term_memories: List[dict] = []

        for mem in self._long_term_memories:
            if mem.get("user_id") == user_id:
                return mem
        return None

    async def save_long_term_memory(self, user_id: str, memory_data: Dict) -> None:
        """保存长期记忆"""
        await self.connect()

        memory_data["user_id"] = user_id
        memory_data["updated_at"] = datetime.now().isoformat()

        if self._use_mongo:
            await self._mongo.long_term_memories.update_one(
                {"user_id": user_id},
                {"$set": memory_data},
                upsert=True
            )
        else:
            # 内存模式
            if not hasattr(self, '_long_term_memories'):
                self._long_term_memories: List[dict] = []

            # 查找并更新
            for i, mem in enumerate(self._long_term_memories):
                if mem.get("user_id") == user_id:
                    self._long_term_memories[i] = memory_data
                    return

            # 不存在则添加
            self._long_term_memories.append(memory_data)

    # ========== 用户画像操作 ==========

    async def get_user_profile(self, user_id: str) -> Optional[Dict]:
        """获取用户画像"""
        await self.connect()

        if self._use_mongo:
            doc = await self._mongo.user_profiles.find_one(
                {"user_id": user_id},
                {"_id": 0}
            )
            return clean_mongo_doc(doc)

        # 内存模式
        if not hasattr(self, '_user_profiles'):
            self._user_profiles: List[dict] = []

        for profile in self._user_profiles:
            if profile.get("user_id") == user_id:
                return profile
        return None

    async def save_user_profile(self, user_id: str, profile_data: Dict) -> None:
        """保存用户画像"""
        await self.connect()

        profile_data["user_id"] = user_id
        profile_data["updated_at"] = datetime.now().isoformat()

        if self._use_mongo:
            await self._mongo.user_profiles.update_one(
                {"user_id": user_id},
                {"$set": profile_data},
                upsert=True
            )
        else:
            # 内存模式
            if not hasattr(self, '_user_profiles'):
                self._user_profiles: List[dict] = []

            # 查找并更新
            for i, profile in enumerate(self._user_profiles):
                if profile.get("user_id") == user_id:
                    self._user_profiles[i] = profile_data
                    return

            # 不存在则添加
            self._user_profiles.append(profile_data)

    # ========== 用户知识库操作 ==========

    async def get_knowledge_base(self, user_id: str) -> Optional[Dict]:
        """获取用户知识库"""
        await self.connect()

        if self._use_mongo:
            doc = await self._mongo.user_knowledge_bases.find_one(
                {"user_id": user_id},
                {"_id": 0}
            )
            return clean_mongo_doc(doc)

        # 内存模式
        if not hasattr(self, '_user_knowledge_bases'):
            self._user_knowledge_bases: List[dict] = []

        for kb in self._user_knowledge_bases:
            if kb.get("user_id") == user_id:
                return kb
        return None

    async def save_knowledge_base(self, user_id: str, kb_data: Dict) -> None:
        """保存用户知识库"""
        await self.connect()

        kb_data["user_id"] = user_id
        kb_data["updated_at"] = datetime.now().isoformat()

        if self._use_mongo:
            await self._mongo.user_knowledge_bases.update_one(
                {"user_id": user_id},
                {"$set": kb_data},
                upsert=True
            )
        else:
            # 内存模式
            if not hasattr(self, '_user_knowledge_bases'):
                self._user_knowledge_bases: List[dict] = []

            # 查找并更新
            for i, kb in enumerate(self._user_knowledge_bases):
                if kb.get("user_id") == user_id:
                    self._user_knowledge_bases[i] = kb_data
                    return

            # 不存在则添加
            self._user_knowledge_bases.append(kb_data)

    # ========== 课程知识操作 ==========

    async def get_all_courses(self) -> List[Dict]:
        """获取所有课程"""
        await self.connect()

        if self._use_mongo:
            cursor = self._mongo.courses.find({}, {"_id": 0})
            return await cursor.to_list(100)

        # 内存模式
        if not hasattr(self, '_courses'):
            self._courses: List[dict] = []
        return self._courses

    async def save_course(self, course_data: Dict) -> None:
        """保存课程"""
        await self.connect()

        course_id = course_data.get("course_id")

        if self._use_mongo:
            await self._mongo.courses.update_one(
                {"course_id": course_id},
                {"$set": course_data},
                upsert=True
            )
        else:
            if not hasattr(self, '_courses'):
                self._courses: List[dict] = []

            for i, course in enumerate(self._courses):
                if course.get("course_id") == course_id:
                    self._courses[i] = course_data
                    return
            self._courses.append(course_data)

    async def get_all_knowledge_points(self) -> List[Dict]:
        """获取所有知识点"""
        await self.connect()

        if self._use_mongo:
            cursor = self._mongo.knowledge_points.find({}, {"_id": 0})
            return await cursor.to_list(1000)

        # 内存模式
        if not hasattr(self, '_knowledge_points'):
            self._knowledge_points: List[dict] = []
        return self._knowledge_points

    async def save_knowledge_point(self, kp_data: Dict) -> None:
        """保存知识点"""
        await self.connect()

        knowledge_id = kp_data.get("knowledge_id")

        if self._use_mongo:
            await self._mongo.knowledge_points.update_one(
                {"knowledge_id": knowledge_id},
                {"$set": kp_data},
                upsert=True
            )
        else:
            if not hasattr(self, '_knowledge_points'):
                self._knowledge_points: List[dict] = []

            for i, kp in enumerate(self._knowledge_points):
                if kp.get("knowledge_id") == knowledge_id:
                    self._knowledge_points[i] = kp_data
                    return
            self._knowledge_points.append(kp_data)

    # ========== 教学知识操作 ==========

    async def get_teaching_experiences(self) -> List[Dict]:
        """获取教学经验"""
        await self.connect()

        if self._use_mongo:
            cursor = self._mongo.teaching_experiences.find({}, {"_id": 0})
            return await cursor.to_list(1000)

        # 内存模式
        if not hasattr(self, '_teaching_experiences'):
            self._teaching_experiences: List[dict] = []
        return self._teaching_experiences

    async def save_teaching_experience(self, exp_data: Dict) -> None:
        """保存教学经验"""
        await self.connect()

        experience_id = exp_data.get("experience_id")

        if self._use_mongo:
            await self._mongo.teaching_experiences.update_one(
                {"experience_id": experience_id},
                {"$set": exp_data},
                upsert=True
            )
        else:
            if not hasattr(self, '_teaching_experiences'):
                self._teaching_experiences: List[dict] = []

            for i, exp in enumerate(self._teaching_experiences):
                if exp.get("experience_id") == experience_id:
                    self._teaching_experiences[i] = exp_data
                    return
            self._teaching_experiences.append(exp_data)

    # ========== 用户知识操作 ==========

    async def get_user_knowledge_documents(self, user_id: str) -> List[Dict]:
        """获取用户知识库文档（用于 UserKnowledge 模块）"""
        await self.connect()

        if self._use_mongo:
            cursor = self._mongo.documents.find(
                {"user_id": user_id},
                {"_id": 0}
            )
            return await cursor.to_list(100)

        # 内存模式
        if not hasattr(self, '_user_documents'):
            self._user_documents: List[dict] = []
        return [doc for doc in self._user_documents if doc.get("user_id") == user_id]

    async def save_user_document(self, doc_data: Dict) -> None:
        """保存用户文档"""
        await self.connect()

        document_id = doc_data.get("document_id")

        if self._use_mongo:
            await self._mongo.user_documents.update_one(
                {"document_id": document_id},
                {"$set": doc_data},
                upsert=True
            )
        else:
            if not hasattr(self, '_user_documents'):
                self._user_documents: List[dict] = []

            for i, doc in enumerate(self._user_documents):
                if doc.get("document_id") == document_id:
                    self._user_documents[i] = doc_data
                    return
            self._user_documents.append(doc_data)

    async def delete_user_document(self, document_id: str) -> None:
        """删除用户文档"""
        await self.connect()

        if self._use_mongo:
            await self._mongo.user_documents.delete_one(
                {"document_id": document_id}
            )
        else:
            if not hasattr(self, '_user_documents'):
                self._user_documents: List[dict] = []
            self._user_documents = [
                doc for doc in self._user_documents
                if doc.get("document_id") != document_id
            ]

    # ========== 评估记录操作 ==========

    async def save_evaluation_record(self, record_data: Dict) -> None:
        """保存评估记录"""
        await self.connect()

        if self._use_mongo:
            await self._mongo.evaluation_records.insert_one(record_data)
        else:
            self._evaluation_records.append(record_data)

    async def get_evaluation_records(
        self, user_id: str, limit: int = 100
    ) -> List[Dict]:
        """获取用户评估记录"""
        await self.connect()

        if self._use_mongo:
            cursor = self._mongo.evaluation_records.find(
                {"user_id": user_id}, {"_id": 0}
            ).sort("created_at", -1).limit(limit)
            return await cursor.to_list(limit)

        records = [
            r for r in self._evaluation_records
            if r.get("user_id") == user_id
        ]
        return sorted(
            records,
            key=lambda x: x.get("created_at", ""),
            reverse=True,
        )[:limit]

    async def save_observability_trace(self, trace_data: Dict) -> None:
        """保存可观测性 trace 日志"""
        await self.connect()

        if self._use_mongo:
            await self._mongo.observability_traces.insert_one(trace_data)
        else:
            if not hasattr(self, "_observability_traces"):
                self._observability_traces = []
            self._observability_traces.append(trace_data)
            # 保留最近 1000 条
            if len(self._observability_traces) > 1000:
                self._observability_traces = self._observability_traces[-1000:]

    # ========== 工作流状态管理 ==========

    async def update_workflow_state(self, workflow_id: str, data: Dict) -> None:
        """更新工作流状态（upsert）"""
        await self.connect()

        if self._use_mongo:
            await self._mongo.workflow_states.update_one(
                {"workflow_id": workflow_id},
                {"$set": data},
                upsert=True,
            )
        else:
            if not hasattr(self, "_workflow_states"):
                self._workflow_states = {}

            self._workflow_states[workflow_id] = data

    async def get_workflow_state(self, workflow_id: str) -> Optional[Dict]:
        """获取工作流状态"""
        await self.connect()

        if self._use_mongo:
            doc = await self._mongo.workflow_states.find_one(
                {"workflow_id": workflow_id}
            )
            return clean_mongo_doc(doc)
        else:
            if not hasattr(self, "_workflow_states"):
                self._workflow_states = {}
            return self._workflow_states.get(workflow_id)

    async def list_workflow_states(
        self,
        user_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict]:
        """列出工作流状态"""
        await self.connect()

        if self._use_mongo:
            query = {}
            if user_id:
                query["user_id"] = user_id
            if status:
                query["status"] = status

            cursor = self._mongo.workflow_states.find(query).sort(
                "updated_at", -1
            ).limit(limit)
            return clean_mongo_docs(await cursor.to_list(limit))
        else:
            if not hasattr(self, "_workflow_states"):
                self._workflow_states = {}

            states = list(self._workflow_states.values())

            # 过滤
            if user_id:
                states = [s for s in states if s.get("user_id") == user_id]
            if status:
                states = [s for s in states if s.get("status") == status]

            # 排序
            states.sort(key=lambda x: x.get("updated_at", ""), reverse=True)

            return states[:limit]

    async def delete_workflow_state(self, workflow_id: str) -> None:
        """删除工作流状态"""
        await self.connect()

        if self._use_mongo:
            await self._mongo.workflow_states.delete_one(
                {"workflow_id": workflow_id}
            )
        else:
            if not hasattr(self, "_workflow_states"):
                self._workflow_states = {}
            self._workflow_states.pop(workflow_id, None)


# 全局数据库实例（向后兼容）
db = Database()


def get_db() -> Database:
    """
    FastAPI 依赖注入函数

    返回全局 db 实例；生产环境可替换为连接池或 request-scoped 实例。
    """
    return db
