"""Database KnowledgeMixin 领域 Mixin（T3-B② 从 database.py 拆分）。"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from app.core.db_utils import clean_mongo_doc, clean_mongo_docs, escape_regex


class KnowledgeMixin:
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
