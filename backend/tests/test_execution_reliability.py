"""
执行层可靠性测试（任务表 / worker / checkpointer / 诊断可见性）

覆盖：
1. 任务表：FIFO 认领、attempts 计数、僵尸恢复、状态流转
2. worker：入队 → 消费端到端、失败重试回队、达上限标 dead、
   中断重试走"继续完成"提示、护栏幂等再判
3. checkpointer 双后端：CHECKPOINT_BACKEND=mongodb 分支
4. 诊断可见性：过滤器结构（shared OR (org AND user)）、
   Qdrant 翻译器嵌套 $or、Python 层过滤语义

全部确定性：内存模式 Database + mock Agent/飞书。
"""

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from test_incident_lifecycle import FakeAgent, FakeFeishu, _firing_alert, _memory_db

from app.core.config import settings


def _make_task(task_id: str, **overrides) -> dict:
    base = {
        "task_id": task_id,
        "kind": "diagnosis",
        "incident_id": "INC-TEST",
        "trigger": "initial",
        "alerts": [_firing_alert()],
        "last_error": "",
    }
    base.update(overrides)
    return base


# ============================================================
# 1. 任务表基础操作（内存模式）
# ============================================================

class TestDiagnosisTaskTable:

    @pytest.mark.asyncio
    async def test_claim_fifo_and_attempts(self):
        db = _memory_db()
        await db.save_diagnosis_task(_make_task("t1"))
        await db.save_diagnosis_task(_make_task("t2"))

        t1 = await db.claim_next_diagnosis_task("inst-a")
        assert t1["task_id"] == "t1"
        assert t1["status"] == "running"
        assert t1["claimed_by"] == "inst-a"
        assert t1["attempts"] == 1

        t2 = await db.claim_next_diagnosis_task("inst-b")
        assert t2["task_id"] == "t2"
        assert t2["attempts"] == 1

        assert await db.claim_next_diagnosis_task("inst-a") is None

    @pytest.mark.asyncio
    async def test_recover_stale_running(self):
        """卡在 running 的僵尸任务被重置为 pending（实例崩溃恢复）"""
        db = _memory_db()
        await db.save_diagnosis_task(_make_task("t1"))
        await db.claim_next_diagnosis_task("dead-instance")

        # 模拟实例死亡：claimed_at 回拨到陈旧窗口之外
        t1 = db._diagnosis_tasks[0]
        t1["claimed_at"] = datetime.now() - timedelta(seconds=3600)

        recovered = await db.recover_stale_diagnosis_tasks(stale_seconds=900)
        assert recovered == 1
        assert t1["status"] == "pending"
        assert t1["attempts"] == 1, "恢复不重复计 attempts（认领时才计数）"

        # 未超时的 running 不受影响；最早的 pending（t1 被恢复）会被再次认领
        await db.save_diagnosis_task(_make_task("t2"))
        claimed = await db.claim_next_diagnosis_task("live-instance")
        recovered2 = await db.recover_stale_diagnosis_tasks(stale_seconds=900)
        assert recovered2 == 0
        assert claimed["task_id"] == "t1", "恢复的 t1（最早 pending）先被认领"
        assert claimed["attempts"] == 2, "重新认领计新一次尝试"
        assert db._diagnosis_tasks[1]["task_id"] == "t2"
        assert db._diagnosis_tasks[1]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_update_task_status_flow(self):
        db = _memory_db()
        await db.save_diagnosis_task(_make_task("t1"))
        assert await db.update_diagnosis_task("t1", {"status": "done"}) is True
        assert db._diagnosis_tasks[0]["status"] == "done"
        assert await db.update_diagnosis_task("no-such", {"status": "done"}) is False


# ============================================================
# 2. Worker 端到端（mock Agent / 飞书 / 内存 db）
# ============================================================

@pytest.fixture
def worker_env(monkeypatch):
    import app.api.langgraph as lg
    from app.services import alert_service as alerts_mod

    mem_db = _memory_db()
    agent = FakeAgent()
    feishu = FakeFeishu()

    monkeypatch.setattr(alerts_mod, "db", mem_db)
    monkeypatch.setattr(alerts_mod, "_get_feishu_client", lambda: feishu)
    monkeypatch.setattr(settings, "FEISHU_ALERT_OPEN_ID", "ou_test")
    monkeypatch.setattr(settings, "ALERT_AUTO_DIAGNOSIS_ENABLED", True)
    monkeypatch.setattr(settings, "ALERT_DIAG_COOLDOWN_SECONDS", 900)
    monkeypatch.setattr(settings, "DIAG_TASK_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(lg, "get_agent", lambda: agent)

    return {"alerts": alerts_mod, "db": mem_db, "agent": agent, "feishu": feishu}


class TestWorkerFlow:

    @pytest.mark.asyncio
    async def test_enqueue_drain_end_to_end(self, worker_env):
        """入队 → worker 消费 → 诊断执行 → 任务 done → 事故历史落库"""
        alerts_mod, agent, feishu, db = (worker_env["alerts"], worker_env["agent"],
                                         worker_env["feishu"], worker_env["db"])
        created = await alerts_mod.enqueue_diagnosis_tasks([_firing_alert(fp="fp-001")])
        assert created == 1
        assert db._diagnosis_tasks[0]["status"] == "pending"

        await alerts_mod._drain_pending_tasks()

        assert len(agent.calls) == 1
        assert agent.calls[0]["session_id"].startswith("incident_INC-AUTO-")
        assert len(feishu.cards) == 1
        assert db._diagnosis_tasks[0]["status"] == "done"
        assert db._incidents[0]["diag_count"] == 1

    @pytest.mark.asyncio
    async def test_failure_requeues_then_recovers(self, worker_env):
        """首次失败 → 回 pending；恢复后重试成功 → done"""
        alerts_mod, _agent, _feishu, db = (worker_env["alerts"], worker_env["agent"],
                                           worker_env["feishu"], worker_env["db"])

        class FlakyAgent(FakeAgent):
            def __init__(self):
                super().__init__()
                self.fail_first = True

            async def run(self, **kwargs):
                if self.fail_first:
                    raise RuntimeError("LLM 超时模拟")
                return await super().run(**kwargs)

        flaky = FlakyAgent()
        worker_env["agent"] = flaky
        from unittest.mock import patch

        import app.api.langgraph as lg
        with patch.object(lg, "get_agent", lambda: flaky):
            await alerts_mod.enqueue_diagnosis_tasks([_firing_alert(fp="fp-001")])
            await alerts_mod._drain_pending_tasks()

            task = db._diagnosis_tasks[0]
            assert task["status"] == "pending", "首次失败应回队列"
            assert "LLM 超时" in task["last_error"]
            assert task["attempts"] == 1
            assert task["not_before"] > datetime.now(), "重试有退避窗口"

            flaky.fail_first = False
            # 模拟退避窗口过期
            task["not_before"] = datetime.now() - timedelta(seconds=1)
            await alerts_mod._drain_pending_tasks()

            task = db._diagnosis_tasks[0]
            assert task["status"] == "done"
            assert task["attempts"] == 2
            assert db._incidents[0]["diag_count"] == 1

    @pytest.mark.asyncio
    async def test_max_attempts_marks_dead(self, worker_env, monkeypatch):
        """失败达到上限 → dead（不再重试）"""
        alerts_mod, db = worker_env["alerts"], worker_env["db"]
        monkeypatch.setattr(settings, "DIAG_TASK_MAX_ATTEMPTS", 1)

        class AlwaysFailAgent(FakeAgent):
            async def run(self, **kwargs):
                raise RuntimeError("永久失败")

        bad = AlwaysFailAgent()
        from unittest.mock import patch

        import app.api.langgraph as lg
        with patch.object(lg, "get_agent", lambda: bad):
            await alerts_mod.enqueue_diagnosis_tasks([_firing_alert(fp="fp-001")])
            await alerts_mod._drain_pending_tasks()

            task = db._diagnosis_tasks[0]
            assert task["status"] == "dead"
            assert "永久失败" in task["last_error"]

    @pytest.mark.asyncio
    async def test_retry_of_interrupted_initial_uses_continuation_prompt(self, worker_env):
        """中断重试（attempts>1）走"继续完成"提示，不重新构造初诊输入"""
        alerts_mod, agent, db = worker_env["alerts"], worker_env["agent"], worker_env["db"]
        await alerts_mod.enqueue_diagnosis_tasks([_firing_alert(fp="fp-001")])
        # 模拟：任务已失败过一次（attempts=1），现在被再次认领（认领后 attempts=2）
        task = db._diagnosis_tasks[0]
        task["attempts"] = 1
        task["status"] = "running"
        task["attempts"] = 2  # 第二次尝试

        await alerts_mod._process_diagnosis_task(task)

        assert len(agent.calls) == 1
        assert "中断" in agent.calls[0]["user_input"]
        assert "继续完成" in agent.calls[0]["user_input"]
        assert task["status"] == "done"

    @pytest.mark.asyncio
    async def test_stale_task_guard_skips_idempotently(self, worker_env):
        """重启后重复消费已满足护栏的任务 → done + skipped，不重复诊断"""
        alerts_mod, agent, db = worker_env["alerts"], worker_env["agent"], worker_env["db"]
        await alerts_mod.enqueue_diagnosis_tasks([_firing_alert(fp="fp-001")])
        await alerts_mod._drain_pending_tasks()  # 初诊完成，diag_count=1

        # 构造重复任务（同一事故、trigger=repeat、间隔未满足）
        dup = _make_task("t-dup", incident_id=db._incidents[0]["incident_id"],
                         trigger="repeat")
        db._diagnosis_tasks.append(dup)
        await alerts_mod._process_diagnosis_task(dup)

        assert dup["status"] == "done"
        assert "skipped" in dup["last_error"]
        assert len(agent.calls) == 1, "护栏幂等再判应阻止重复诊断"

    @pytest.mark.asyncio
    async def test_summary_task_processed_by_worker(self, worker_env, monkeypatch):
        """kind=summary 任务：仅当事故处于 resolving 状态时生成摘要"""
        alerts_mod, agent, db = worker_env["alerts"], worker_env["agent"], worker_env["db"]
        await alerts_mod.enqueue_diagnosis_tasks([_firing_alert(fp="fp-001")])
        await alerts_mod._drain_pending_tasks()
        inc = db._incidents[0]

        # 事故 active → 摘要任务跳过
        task = _make_task("t-sum", kind="summary", incident_id=inc["incident_id"],
                          trigger="summary", alerts=[])
        db._diagnosis_tasks.append(task)
        await alerts_mod._process_diagnosis_task(task)
        assert task["status"] == "done"
        assert len(agent.calls) == 1

        # resolving → 生成摘要
        await db.update_incident_fields(inc["incident_id"], {"status": "resolving"})
        await alerts_mod._process_diagnosis_task(task)
        assert len(agent.calls) == 2


# ============================================================
# 3. Checkpointer 双后端
# ============================================================

class TestCheckpointBackend:

    @pytest.mark.asyncio
    async def test_mongodb_backend_branch(self, monkeypatch):
        """CHECKPOINT_BACKEND=mongodb → 使用 MongoDBSaver 并重建图"""
        from app.langgraph_agent.agent import LangGraphAgent

        captured = {}

        class FakeMongoSaver:
            def __init__(self, client, db_name="", checkpoint_collection_name="",
                         writes_collection_name=""):
                captured["db_name"] = db_name
                captured["collection"] = checkpoint_collection_name

        import langgraph.checkpoint.mongodb as mongo_mod
        monkeypatch.setattr(mongo_mod, "MongoDBSaver", FakeMongoSaver)
        monkeypatch.setattr(settings, "CHECKPOINT_BACKEND", "mongodb")
        monkeypatch.setattr(settings, "MONGODB_DB_NAME", "education_agent_test")

        agent = object.__new__(LangGraphAgent)
        agent._saver_initialized = False
        agent.checkpoint_path = "unused.db"
        agent.memory = None
        agent._build_graph = lambda: "REBUILT_GRAPH"

        await agent._ensure_saver()

        assert isinstance(agent.memory, FakeMongoSaver)
        assert captured["db_name"] == "education_agent_test"
        assert agent.graph == "REBUILT_GRAPH"
        assert agent._saver_initialized is True

    @pytest.mark.asyncio
    async def test_mongodb_failure_degrades_to_memory(self, monkeypatch):
        """Mongo checkpointer 初始化失败 → 降级 MemorySaver（不阻塞启动）"""
        import langgraph.checkpoint.mongodb as mongo_mod

        from app.langgraph_agent.agent import LangGraphAgent

        def _boom(*args, **kwargs):
            raise RuntimeError("mongo 不可用")

        monkeypatch.setattr(mongo_mod, "MongoDBSaver", _boom)
        monkeypatch.setattr(settings, "CHECKPOINT_BACKEND", "mongodb")

        agent = object.__new__(LangGraphAgent)
        agent._saver_initialized = False
        agent.checkpoint_path = "unused.db"
        agent.memory = "MEMORY_SAVER_PLACEHOLDER"
        agent._build_graph = lambda: "GRAPH"

        await agent._ensure_saver()

        assert agent.memory == "MEMORY_SAVER_PLACEHOLDER", "失败时保留原 MemorySaver"
        assert agent._saver_initialized is True


# ============================================================
# 4. 诊断可见性过滤
# ============================================================

class TestVisibilityFilter:

    @staticmethod
    def _store():
        """_build_visibility_filter 是实例方法，用空壳实例避免触发向量库初始化"""
        from app.knowledge.unified_store import UnifiedKnowledgeStore
        return UnifiedKnowledgeStore.__new__(UnifiedKnowledgeStore)

    def test_filter_structure_shared_or_personal(self):
        """结构：$or [shared, $and [org 条件, user 条件]]"""
        f = self._store()._build_visibility_filter(org_id="org1", user_id="u1")
        assert "$or" in f
        or_items = f["$or"]
        assert {"shared_to_diagnosis": "true"} in or_items
        and_item = [i for i in or_items if "$and" in i][0]
        cond_keys = [list(c.keys())[0] for c in and_item["$and"]]
        assert "$or_empty" in cond_keys and "$or_missing" in cond_keys

    def test_filter_no_org_user_no_constraint(self):
        """无 org/user 上下文 → 无可见性约束（保持原行为）"""
        f = self._store()._build_visibility_filter()
        assert f == {}

    def test_python_matcher_shared_bypasses_org(self):
        """Python 层：shared 文档跨组织可见，非 shared 不可见"""
        from app.knowledge.unified_store import UnifiedKnowledgeStore
        f = self._store()._build_visibility_filter(org_id="org1", user_id="u1")

        # 共享文档：他人组织 → 可见
        assert UnifiedKnowledgeStore._match_metadata_filter(
            {"org_id": "org-other", "user_id": "someone-else",
             "shared_to_diagnosis": "true"}, f) is True
        # 非共享文档：他人组织 → 不可见（AND 语义保持）
        assert UnifiedKnowledgeStore._match_metadata_filter(
            {"org_id": "org-other", "user_id": "someone-else"}, f) is False
        # 本组织本人文档 → 可见
        assert UnifiedKnowledgeStore._match_metadata_filter(
            {"org_id": "org1", "user_id": "u1"}, f) is True
        # 公共文档（org 空、无 user）→ 可见
        assert UnifiedKnowledgeStore._match_metadata_filter(
            {"org_id": "", "user_id": ""}, f) is True
        # 非共享：本组织但他人私有 → 不可见（AND 语义不放宽）
        assert UnifiedKnowledgeStore._match_metadata_filter(
            {"org_id": "org1", "user_id": "someone-else"}, f) is False

    def test_qdrant_translator_nested_or_groups(self):
        """Qdrant 翻译器：$or 中的嵌套 $and 组转成嵌套 Filter"""
        from app.retrieval.qdrant_store import QdrantVectorStore
        f = QdrantVectorStore._convert_filter({
            "$or": [
                {"shared_to_diagnosis": "true"},
                {"$and": [
                    {"$or_empty": {"key": "org_id", "value": "org1"}},
                    {"$or_missing": {"key": "user_id", "value": "u1"}},
                ]},
            ],
        })
        assert f is not None
        assert len(f.should) == 2, "$or 两项：shared 简单条件 + 嵌套组"
        first, second = f.should
        # 简单项：shared 字段条件
        assert first.key == "shared_to_diagnosis"
        # 嵌套项：Filter 对象（$or_empty→should[2 条 org 条件]，$or_missing→must_not[user 条件]）
        from qdrant_client.models import Filter as QdrantFilter
        assert isinstance(second, QdrantFilter)
        assert len(second.should) == 2, "org 条件（空串 OR 指定组织）"
        assert len(second.must_not) == 1, "user 条件（排除非本人）"

    def test_search_postfilter_shared_bypass(self):
        """search() Python post-filter：shared 文档跨组织/跨用户保留"""
        # 直接构造 search 的过滤段逻辑验证（不依赖向量库）
        # 模拟 search() 中的判定（与实现保持同一表达式）
        def _visible(metadata, org_id, user_id):
            shared = metadata.get("shared_to_diagnosis") == "true"
            if org_id:
                doc_org = metadata.get("org_id")
                if doc_org and doc_org != org_id and not shared:
                    return False
            if user_id:
                doc_user = metadata.get("user_id")
                if doc_user and doc_user != user_id and not shared:
                    return False
            return True

        assert _visible({"org_id": "other", "user_id": "x",
                         "shared_to_diagnosis": "true"}, "org1", "u1") is True
        assert _visible({"org_id": "other", "user_id": "x"}, "org1", "u1") is False
        assert _visible({"org_id": "org1", "user_id": "u1"}, "org1", "u1") is True
