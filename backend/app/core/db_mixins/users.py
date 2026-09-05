"""Database UsersMixin 领域 Mixin（T3-B 从 database.py 拆分）。

方法体与迁移前 verbatim 一致，通过 Database 组合后仍通过 self 访问 _mongo/_mem/_use_mongo 等核心状态。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from app.core.db_utils import clean_mongo_doc, clean_mongo_docs


class UsersMixin:
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
