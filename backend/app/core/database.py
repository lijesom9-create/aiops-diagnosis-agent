"""
数据库连接和操作模块
支持MongoDB和内存存储（开发测试用）
"""

from typing import Optional, List, Dict, Any
from datetime import datetime
from loguru import logger
import re
import uuid
import copy

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
