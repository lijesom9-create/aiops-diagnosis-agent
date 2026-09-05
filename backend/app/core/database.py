"""
数据库连接和操作模块
支持MongoDB和内存存储（开发测试用）
"""

from datetime import datetime
from typing import Dict, List

from loguru import logger

from app.core.db_mixins import ContentMixin, DiagnosisMixin, IncidentsMixin, KnowledgeMixin, UsersMixin
from app.core.db_utils import clean_mongo_doc, clean_mongo_docs, escape_regex  # noqa: F401 (re-export)

from .config import settings


class Database(UsersMixin, ContentMixin, IncidentsMixin, DiagnosisMixin, KnowledgeMixin):
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








    # ========== 主题操作 ==========






    # ========== 文档操作 ==========








    # ========== 学习计划操作 ==========






    # ========== 组织操作 ==========





    # ========== 会话操作 ==========









    # ========== 学习进度操作 ==========



    # ========== 题目操作 ==========





    # ========== Incident 注册表（告警自动诊断的事故生命周期） ==========













    # ========== D1 根因模式统计（问题管理入口） ==========
    # ITIL 问题管理触发条件"事件反复出现"——按 root_cause 聚合统计频次，
    # 回答"本月哪个根因反复出现"，作为主动消除高频根因的决策依据。
    #
    # 统计口径：
    # - 时间窗：first_seen_at 在最近 N 天内（按事故首次出现时间过滤）
    # - 去重：同一 incident 多次诊断（初诊/重诊）的同一 root_cause 只计一次
    #   ——否则重诊次数多的事故会虚增该根因的频次
    # - 排除：trigger=summary 的摘要条目（摘要与诊断描述同一根因，避免重复计数）


    # ========== D2 诊断质量分层统计（验证"高充分度 → 高采纳率"假设） ==========
    #
    # 产品价值假设链：H1 诊断命中 → H2 响应者采纳。
    # D2 按诊断充分度分层统计 ack 采纳率——若"高充分度 → 高采纳率"不成立，
    # 说明诊断质量（检索/推理）与响应者信任之间存在断点，需排查检索召回或
    # 诊断表达问题。
    #
    # 分层依据：每个事故取**最近一次非摘要诊断**的 sufficiency_level
    # （初诊/重诊的充分度，而非摘要——摘要是事后总结，不代表诊断时刻的质量）。


    # ========== 诊断任务表（持久化 + 原子认领 + 重启恢复） ==========
    # 解决两个问题：
    # 1. BackgroundTasks 进程内排队，重启丢任务 → 任务落 Mongo，启动时捞回
    # 2. 多副本重复诊断 → find_one_and_update 原子认领，全集群只有一个实例抢到







    # ========== 知识库操作 ==========







    # ========== 答题记录操作 ==========



    # ========== 用户统计操作 ==========



    # ========== 学习状态管理 ==========



    # ========== Student State 操作 ==========



    # ========== 会话记忆操作 ==========



    # ========== 长期记忆操作 ==========



    # ========== 用户画像操作 ==========



    # ========== 用户知识库操作 ==========



    # ========== 课程知识操作 ==========





    # ========== 教学知识操作 ==========



    # ========== 用户知识操作 ==========




    # ========== 评估记录操作 ==========




    # ========== 工作流状态管理 ==========






# 全局数据库实例（向后兼容）
db = Database()


def get_db() -> Database:
    """
    FastAPI 依赖注入函数

    返回全局 db 实例；生产环境可替换为连接池或 request-scoped 实例。
    """
    return db
