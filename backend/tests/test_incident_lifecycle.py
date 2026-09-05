"""
Incident 生命周期测试（初诊 → 重诊 → 恢复摘要）

覆盖：
1. 服务名提取（labels 优先级 / instance 解析 / 静态映射）
2. severity 门槛（零成本噪声拦截）
3. 告警路由到事故：新建 / 归入升级 / 心跳 / 抖动复用 / 安静期复燃
4. 诊断资格护栏：重诊上限 / 最小间隔 / 高置信心跳跳过
5. 端到端 mock：同批告警同事故只诊断一次 / 诊断历史落库 / 重诊卡片标记
6. 恢复摘要：全部 resolved → 安静期 → 摘要生成 + 闭案；安静期复燃不生成
7. 增量重诊 / 摘要 prompt 的关键内容

全部确定性运行：mock Agent + 飞书客户端 + 内存模式 Database。
"""

import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app.core.config import settings
from app.core.database import Database

# ============================================================
# 测试基础设施
# ============================================================

def _memory_db() -> Database:
    """强制内存模式的 Database（本地 Mongo 可用时 connect() 会切到 Mongo 分支）"""
    db = Database()

    async def _no_connect():
        return None

    db.connect = _no_connect
    db._use_mongo = False
    return db


def _firing_alert(fp: str = "fp-001", service: str = "payment-service",
                  alertname: str = "HighCpuUsage", severity: str = "warning",
                  instance: str = "payment-service-1:8080") -> dict:
    return {
        "status": "firing",
        "labels": {"alertname": alertname, "severity": severity,
                   "instance": instance, "service": service},
        "annotations": {"summary": "指标异常", "description": "持续恶化"},
        "startsAt": "2026-08-31T00:00:00Z",
        "fingerprint": fp,
    }


class FakeAgent:
    """确定性 Agent mock：返回结构化诊断报告，记录调用"""

    def __init__(self, confidence_level="medium", root_cause="连接池耗尽"):
        self.calls = []
        self.confidence_level = confidence_level
        self.root_cause = root_cause

    async def run(self, user_input="", session_id=None, context=None, use_web_search=False):
        self.calls.append({"user_input": user_input, "session_id": session_id})
        # 摘要请求含"事故已恢复"→ 返回带行动项段的复盘结构（避免触发 B7 定向重试）
        if "事故已恢复" in user_input:
            content = (
                f"### 事故时间线\n恢复\n### 影响\n轻微\n"
                f"### 最可能根因\n{self.root_cause}\n### 处置回顾\n止血\n"
                f"### 行动项\n"
                f"- 【检测】增加告警覆盖（负责人: 待定，期限: 2026-09-10）\n"
                f"- 【预防】扩容阈值（负责人: 待定，期限: 2026-09-15）\n"
                f"### 经验教训\n顺利"
            )
        else:
            content = (
                f"### 现象\nx\n### 根因分析\n{self.root_cause}\n"
                f"### 处置方案\ny\n### 置信度\n中"
            )
        return {
            "content": content,
            "tools_used": ["query_metrics"],
            "diagnosis_report": {
                "root_cause": self.root_cause,
                "solution": "扩容",
                "confidence": "中",
                "confidence_level": self.confidence_level,
            },
        }


class FakeFeishu:
    def __init__(self):
        self.cards = []

    def send_card(self, open_id, card):
        self.cards.append(card)
        return True


@pytest.fixture
def incident_env(monkeypatch):
    """隔离的事故测试环境：内存 db + mock agent/feishu + 放大间隔避免重诊"""
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
    monkeypatch.setattr(settings, "DIAG_MAX_REDIAG_PER_INCIDENT", 3)
    monkeypatch.setattr(lg, "get_agent", lambda: agent)

    return {"alerts": alerts_mod, "db": mem_db, "agent": agent, "feishu": feishu}


# ============================================================
# 1. 服务名提取 & severity 门槛
# ============================================================

class TestServiceExtraction:

    def test_service_label_first(self):
        from app.api.alerts import _extract_service
        alert = {"labels": {"service": "payment-service", "job": "other", "instance": "x:1"}}
        assert _extract_service(alert) == "payment-service"

    def test_job_fallback(self):
        from app.api.alerts import _extract_service
        alert = {"labels": {"job": "order-service", "instance": "10.0.0.1:9100"}}
        assert _extract_service(alert) == "order-service"

    def test_instance_host_parse(self):
        """instance 主机名剥离端口与序号"""
        from app.api.alerts import _extract_service
        alert = {"labels": {"instance": "payment-service-1:8080"}}
        assert _extract_service(alert) == "payment-service"

    def test_service_map_override(self, monkeypatch):
        """静态映射优先于原始候选值"""
        from app.api.alerts import _extract_service
        monkeypatch.setattr(settings, "ALERT_SERVICE_MAP",
                            '{"node-exporter": "host-infra"}')
        alert = {"labels": {"job": "node-exporter", "instance": "node-1:9100"}}
        assert _extract_service(alert) == "host-infra"

    def test_empty_returns_empty(self):
        from app.api.alerts import _extract_service
        assert _extract_service({"labels": {}}) == ""


class TestSeverityGate:

    def test_warning_passes_default_threshold(self):
        from app.api.alerts import _severity_ok
        assert _severity_ok(_firing_alert(severity="warning")) is True
        assert _severity_ok(_firing_alert(severity="critical")) is True

    def test_info_blocked_by_default_threshold(self):
        from app.api.alerts import _severity_ok
        assert _severity_ok(_firing_alert(severity="info")) is False

    def test_critical_threshold_blocks_warning(self, monkeypatch):
        from app.api.alerts import _severity_ok
        monkeypatch.setattr(settings, "ALERT_MIN_SEVERITY", "critical")
        assert _severity_ok(_firing_alert(severity="warning")) is False


# ============================================================
# 2. 告警路由到事故
# ============================================================

class TestIncidentRouting:

    @pytest.mark.asyncio
    async def test_new_fingerprint_creates_incident(self, incident_env):
        alerts_mod = incident_env["alerts"]
        incident, trigger = await alerts_mod._route_alert_to_incident(_firing_alert())
        assert trigger == "initial"
        assert incident["status"] == "active"
        assert incident["service"] == "payment-service"
        assert incident["diag_count"] == 0

    @pytest.mark.asyncio
    async def test_same_service_new_alert_joins_as_escalation(self, incident_env):
        """同服务关联窗口内的新告警归入活跃事故（升级信号）"""
        alerts_mod, db = incident_env["alerts"], incident_env["db"]
        inc1, trig1 = await alerts_mod._route_alert_to_incident(_firing_alert(fp="fp-001"))
        inc2, trig2 = await alerts_mod._route_alert_to_incident(
            _firing_alert(fp="fp-002", alertname="HighErrorRate", instance="payment-service-2:8080"),
        )
        assert trig1 == "initial"
        assert trig2 == "escalation"
        assert inc2["incident_id"] == inc1["incident_id"]
        refreshed = await db.get_incident(inc1["incident_id"])
        assert set(refreshed["fingerprints"]) == {"fp:fp-001", "fp:fp-002"}
        assert "HighErrorRate" in refreshed["alertnames"]

    @pytest.mark.asyncio
    async def test_same_fingerprint_again_is_repeat(self, incident_env):
        alerts_mod = incident_env["alerts"]
        await alerts_mod._route_alert_to_incident(_firing_alert(fp="fp-001"))
        _, trigger = await alerts_mod._route_alert_to_incident(_firing_alert(fp="fp-001"))
        assert trigger == "repeat"

    @pytest.mark.asyncio
    async def test_resolved_then_refire_reopens_within_flapping_window(self, incident_env, monkeypatch):
        """resolved 后窗口内复燃 → 重新打开原事故（不新建）"""
        alerts_mod, db = incident_env["alerts"], incident_env["db"]
        inc, _ = await alerts_mod._route_alert_to_incident(_firing_alert(fp="fp-001"))
        await db.update_incident_fields(inc["incident_id"], {
            "status": "resolved", "resolved_at": datetime.now(),
            "resolved_fps": ["fp:fp-001"],
        })
        incident, trigger = await alerts_mod._route_alert_to_incident(_firing_alert(fp="fp-001"))
        assert trigger == "repeat"
        assert incident["status"] == "active"
        assert incident["resolved_fps"] == []  # 复燃后清空已恢复标记

    @pytest.mark.asyncio
    async def test_resolving_incident_refire_reactivates(self, incident_env):
        """安静期事故（resolving）复燃 → 回到 active"""
        alerts_mod = incident_env["alerts"]
        inc, _ = await alerts_mod._route_alert_to_incident(_firing_alert(fp="fp-001"))
        await incident_env["db"].update_incident_fields(inc["incident_id"], {
            "status": "resolving", "resolved_at": datetime.now(),
        })
        incident, trigger = await alerts_mod._route_alert_to_incident(_firing_alert(fp="fp-001"))
        assert trigger == "repeat"
        assert incident["status"] == "active"


# ============================================================
# 3. 诊断资格护栏
# ============================================================

class TestDiagnosisGuards:

    def _incident(self, **overrides):
        base = {
            "incident_id": "INC-TEST", "diag_count": 0,
            "last_diag_at": None, "last_confidence_level": "unknown",
        }
        base.update(overrides)
        return base

    def test_initial_always_passes_within_cap(self):
        from app.api.alerts import _should_diagnose
        ok, _ = _should_diagnose(self._incident(), "initial")
        assert ok is True

    def test_repeat_high_confidence_skipped(self):
        from app.api.alerts import _should_diagnose
        ok, reason = _should_diagnose(
            self._incident(last_confidence_level="high"), "repeat")
        assert ok is False
        assert "高置信" in reason

    def test_repeat_within_interval_skipped(self):
        from app.api.alerts import _should_diagnose
        ok, reason = _should_diagnose(
            self._incident(last_diag_at=datetime.now() - timedelta(seconds=60)),
            "repeat")
        assert ok is False
        assert "冷却" in reason

    def test_escalation_ignores_confidence_but_respects_interval(self):
        from app.api.alerts import _should_diagnose
        # 升级不看置信度：高置信也允许（新症状值得再看）
        ok, _ = _should_diagnose(
            self._incident(last_confidence_level="high",
                           last_diag_at=datetime.now() - timedelta(seconds=3600)),
            "escalation")
        assert ok is True
        # 但受最小间隔约束
        ok, reason = _should_diagnose(
            self._incident(last_diag_at=datetime.now() - timedelta(seconds=60)),
            "escalation")
        assert ok is False

    def test_rediag_cap_hard_limit(self):
        from app.api.alerts import _should_diagnose
        # diag_count = 1(初诊) + 3(重诊上限) = 4 → 拒绝
        ok, reason = _should_diagnose(self._incident(diag_count=4), "escalation")
        assert ok is False
        assert "上限" in reason


# ============================================================
# 4. 端到端 mock 流程
# ============================================================

class TestIncidentDiagnosisFlow:

    @pytest.mark.asyncio
    async def test_batch_same_service_diagnosed_once(self, incident_env):
        """同批多条同服务告警 → 1 个事故、1 个任务、1 次诊断（聚合省成本的核心断言）"""
        alerts_mod, agent, feishu = (incident_env["alerts"], incident_env["agent"],
                                     incident_env["feishu"])
        batch = [
            _firing_alert(fp="fp-001"),
            _firing_alert(fp="fp-002", alertname="HighErrorRate", instance="payment-service-2:8080"),
        ]
        created = await alerts_mod.enqueue_diagnosis_tasks(batch)
        assert created == 1, "同批同服务告警只应入队一个诊断任务"
        await alerts_mod._drain_pending_tasks()

        assert len(agent.calls) == 1, "同批同服务告警只应触发一次诊断"
        assert agent.calls[0]["session_id"].startswith("incident_INC-AUTO-")
        assert len(feishu.cards) == 1
        assert feishu.cards[0]["header"]["template"] == "orange"

        incidents = [i for i in incident_env["db"]._incidents]
        assert len(incidents) == 1
        assert set(incidents[0]["fingerprints"]) == {"fp:fp-001", "fp:fp-002"}
        assert incidents[0]["diag_count"] == 1
        assert incidents[0]["diagnosis_history"][0]["trigger"] == "initial"

    @pytest.mark.asyncio
    async def test_escalation_uses_incremental_prompt_and_marks_card(self, incident_env):
        """升级触发重诊：增量 prompt + 卡片带更新标记"""
        alerts_mod, agent, feishu = (incident_env["alerts"], incident_env["agent"],
                                     incident_env["feishu"])
        await alerts_mod.enqueue_diagnosis_tasks([_firing_alert(fp="fp-001")])
        await alerts_mod._drain_pending_tasks()
        assert len(agent.calls) == 1

        # 模拟时间流逝满足最小间隔
        inc = incident_env["db"]._incidents[0]
        inc["last_diag_at"] = datetime.now() - timedelta(seconds=3600)

        await alerts_mod.enqueue_diagnosis_tasks(
            [_firing_alert(fp="fp-003", alertname="OOMKilled")])
        await alerts_mod._drain_pending_tasks()

        assert len(agent.calls) == 2
        prompt = agent.calls[1]["user_input"]
        assert "上次诊断" in prompt and "确认" in prompt and "推翻" in prompt
        assert "OOMKilled" in prompt  # 新证据包含在内
        assert feishu.cards[1]["header"]["template"] == "orange"
        assert "诊断更新" in feishu.cards[1]["header"]["title"]["content"]

        # 诊断历史落库
        assert inc["diag_count"] == 2
        assert inc["diagnosis_history"][1]["trigger"] == "escalation"

    @pytest.mark.asyncio
    async def test_low_severity_alert_never_reaches_agent(self, incident_env):
        alerts_mod, agent = incident_env["alerts"], incident_env["agent"]
        created = await alerts_mod.enqueue_diagnosis_tasks([_firing_alert(severity="info")])
        assert created == 0
        await alerts_mod._drain_pending_tasks()
        assert len(agent.calls) == 0

    @pytest.mark.asyncio
    async def test_disabled_skips_everything(self, incident_env, monkeypatch):
        monkeypatch.setattr(settings, "ALERT_AUTO_DIAGNOSIS_ENABLED", False)
        created = await incident_env["alerts"].enqueue_diagnosis_tasks([_firing_alert()])
        assert created == 0
        assert len(incident_env["agent"].calls) == 0


# ============================================================
# 5. 恢复摘要
# ============================================================

class TestResolvedFlow:

    @pytest.mark.asyncio
    async def test_full_resolved_generates_summary_and_closes(self, incident_env, monkeypatch):
        """全部恢复 → 安静期 → 摘要任务入队 → 摘要卡片（绿色）+ 事故闭案 resolved"""
        alerts_mod, agent, feishu, db = (incident_env["alerts"], incident_env["agent"],
                                         incident_env["feishu"], incident_env["db"])
        monkeypatch.setattr(settings, "INCIDENT_RESOLVE_QUIET_PERIOD", 0)

        await alerts_mod.enqueue_diagnosis_tasks([_firing_alert(fp="fp-001")])
        await alerts_mod._drain_pending_tasks()
        await alerts_mod.handle_resolved_alerts([
            {**_firing_alert(fp="fp-001"), "status": "resolved"},
        ])
        await alerts_mod._drain_pending_tasks()  # 消费摘要任务

        assert len(agent.calls) == 2  # 初诊 + 摘要
        assert "事故已恢复" in agent.calls[1]["user_input"]
        assert "时间线" in agent.calls[1]["user_input"]
        assert agent.calls[1]["session_id"] == agent.calls[0]["session_id"], "摘要与诊断共享会话记忆"

        assert len(feishu.cards) == 2
        assert feishu.cards[1]["header"]["template"] == "green"
        assert "恢复摘要" in feishu.cards[1]["header"]["title"]["content"]

        inc = db._incidents[0]
        assert inc["status"] == "resolved"
        assert inc["summary"], "摘要应落库"
        assert inc["diagnosis_history"][-1]["trigger"] == "summary"

    @pytest.mark.asyncio
    async def test_quiet_period_refire_cancels_summary(self, incident_env, monkeypatch):
        """安静期长、期间复燃 → 摘要取消（复燃改回 active 后不再生成）"""
        alerts_mod, agent, feishu, db = (incident_env["alerts"], incident_env["agent"],
                                         incident_env["feishu"], incident_env["db"])
        monkeypatch.setattr(settings, "INCIDENT_RESOLVE_QUIET_PERIOD", 30)

        await alerts_mod.enqueue_diagnosis_tasks([_firing_alert(fp="fp-001")])
        await alerts_mod._drain_pending_tasks()
        inc = db._incidents[0]

        # 模拟：resolved 处理进入 sleep 前，先手动标记 resolving；
        # 再用路由复燃改回 active；然后 resolved 处理醒来发现状态不符 → 不生成摘要
        await db.update_incident_fields(inc["incident_id"], {
            "status": "resolving", "resolved_at": datetime.now(),
            "resolved_fps": ["fp:fp-001"],
        })
        import asyncio as _asyncio

        task = _asyncio.create_task(alerts_mod.handle_resolved_alerts(
            [{**_firing_alert(fp="fp-001"), "status": "resolved"}]))
        await _asyncio.sleep(0.01)  # 让 resolved 处理先走到 sleep
        # 复燃：路由逻辑把事故改回 active
        await alerts_mod._route_alert_to_incident(_firing_alert(fp="fp-001"))
        await task

        assert len(agent.calls) == 1, "复燃后不应生成摘要"
        assert feishu.cards[-1]["header"]["template"] == "orange"  # 只有诊断卡片
        inc_refreshed = await db.get_incident(inc["incident_id"])
        assert inc_refreshed["status"] == "active"


# ============================================================
# 6. Prompt 内容
# ============================================================

class TestPrompts:

    def test_summary_prompt_contains_timeline_and_history(self, incident_env):
        from app.api.alerts import _build_summary_prompt

        incident = {
            "incident_id": "INC-TEST", "alertnames": ["HighCPU", "OOM"],
            "max_severity": "critical",
            "first_seen_at": datetime.now(), "resolved_at": datetime.now(),
            "diagnosis_history": [
                {"trigger": "initial", "root_cause": "发版引发", "at": datetime.now()},
                {"trigger": "escalation", "root_cause": "连接池耗尽", "at": datetime.now()},
            ],
        }
        prompt = _build_summary_prompt(incident)
        assert "INC-TEST" in prompt and "时间线" in prompt
        assert "发版引发" in prompt and "连接池耗尽" in prompt
        assert "复盘" in prompt

    def test_rediagnosis_prompt_requires_three_way_choice(self):
        from app.api.alerts import _build_rediagnosis_prompt
        incident = {
            "incident_id": "INC-T", "alertnames": ["A"],
            "max_severity": "warning",
            "diagnosis_history": [{"trigger": "initial", "root_cause": "X",
                                   "confidence_level": "medium"}],
        }
        prompt = _build_rediagnosis_prompt(incident, [_firing_alert()])
        assert "上次诊断" in prompt and "X" in prompt
        assert "确认" in prompt and "修正" in prompt and "推翻" in prompt


# ============================================================
# 7. M1 增强：路由指标 / runbook 附链 / 唯一索引
# ============================================================

class TestRouteMetrics:
    """B1：路由级别可观测（误路由可测量性）"""

    @pytest.mark.asyncio
    async def test_route_level_counters(self, incident_env, monkeypatch):
        from app.services import alert_service as alerts_mod

        levels = []
        monkeypatch.setattr(alerts_mod, "_route_metric",
                            lambda level: levels.append(level))

        # 新建 → new；已知指纹 → fingerprint
        await alerts_mod._route_alert_to_incident(_firing_alert(fp="fp-m1-1"))
        await alerts_mod._route_alert_to_incident(_firing_alert(fp="fp-m1-1"))
        assert levels == ["new", "fingerprint"]

        # 同服务新指纹 + 窗口内 → join
        await alerts_mod._route_alert_to_incident(
            _firing_alert(fp="fp-m1-2", alertname="OOMKilled",
                          instance="payment-service-2:8080"))
        assert levels[-1] == "join"


class TestRunbookAttach:
    """A3：诊断卡片自动附 runbook/SOP"""

    def test_lookup_runbook_hits(self, monkeypatch):
        """知识库命中 SOP → 返回标题与 doc_id"""
        import app.shared_services as ss
        from app.api.alerts import _lookup_runbook

        class FakeStore:
            def hybrid_search_parent_child(self, query, **kw):
                return [{"title": "payment-sim 连接池 SOP",
                         "score": 0.82,
                         "metadata": {"document_id": "doc_sop_1"}}]

        monkeypatch.setattr(ss, "_knowledge_store", FakeStore())
        rb = _lookup_runbook("payment-sim")
        assert rb["title"] == "payment-sim 连接池 SOP"
        assert rb["doc_id"] == "doc_sop_1"

    def test_lookup_runbook_none_when_no_store(self, monkeypatch):
        import app.shared_services as ss
        from app.api.alerts import _lookup_runbook

        monkeypatch.setattr(ss, "_knowledge_store", None)
        assert _lookup_runbook("payment-sim") is None

    def test_card_contains_runbook(self):
        """卡片含推荐 SOP 元素"""
        from app.notify.feishu import FeishuClient
        result = {
            "content": "### 根因分析\nx",
            "tools_used": [],
            "diagnosis_report": {"root_cause": "x", "solution": "y",
                                 "confidence": "高", "confidence_level": "high"},
        }
        card = FeishuClient.build_diagnosis_card(
            {"labels": {"alertname": "A"}}, result,
            runbook={"title": "连接池 SOP", "doc_id": "d1"})
        body = json.dumps(card["elements"], ensure_ascii=False)
        assert "连接池 SOP" in body and "推荐 SOP" in body

    def test_card_without_runbook_no_element(self):
        from app.notify.feishu import FeishuClient
        result = {"content": "### 根因分析\nx", "tools_used": [],
                  "diagnosis_report": {"root_cause": "x", "solution": "y",
                                       "confidence": "高", "confidence_level": "high"}}
        card = FeishuClient.build_diagnosis_card({"labels": {"alertname": "A"}}, result)
        body = json.dumps(card["elements"], ensure_ascii=False)
        assert "推荐 SOP" not in body


class TestUniqueIndex:
    """B2：active 事故唯一索引——并发创建同服务事故被拒绝并转归入"""

    @pytest.mark.asyncio
    async def test_memory_emulation_duplicate_rejected(self, incident_env, monkeypatch):
        """内存模拟唯一索引：同服务第二个 active insert 抛 DuplicateKeyError"""
        alerts_mod, db = incident_env["alerts"], incident_env["db"]
        monkeypatch.setattr(alerts_mod, "_get_feishu_client", lambda: None)

        await alerts_mod._route_alert_to_incident(_firing_alert(fp="fp-u1"))
        # 绕过路由直接 save（模拟并发窗口内的第二个创建）
        from pymongo.errors import DuplicateKeyError
        dup = dict(db._incidents[0])
        dup["incident_id"] = "INC-AUTO-DUP0001"
        dup["fingerprints"] = ["fp:fp-u2"]
        with pytest.raises(DuplicateKeyError):
            await db.save_incident(dup)

    @pytest.mark.asyncio
    async def test_route_falls_back_to_join_on_duplicate(self, incident_env, monkeypatch):
        """创建撞唯一索引 → 转为归入该事故（escalation）

        模拟竞态：第一次 find_active_incident_by_service 返回 None（对方尚未提交），
        save_incident 抛 DuplicateKeyError（唯一索引拒绝）→ 兜底重新查询 → 归入。
        """
        alerts_mod, db = incident_env["alerts"], incident_env["db"]
        monkeypatch.setattr(alerts_mod, "_get_feishu_client", lambda: None)

        # 先建一个事故（fp-u1）
        incident, trigger = await alerts_mod._route_alert_to_incident(
            _firing_alert(fp="fp-u1"))
        assert trigger == "initial"

        # 竞态模拟：join 查询第一次返回 None（并发窗口），save 撞唯一索引
        from pymongo.errors import DuplicateKeyError
        calls = {"find": 0}

        async def flaky_find(service, within_seconds=1800):
            calls["find"] += 1
            if calls["find"] == 1:
                return None  # 竞态窗口：查不到（对方事务未提交）
            return await type(db).find_active_incident_by_service(db, service, within_seconds)

        async def dup_save(doc):
            raise DuplicateKeyError("E11000 duplicate key: uniq_active_service")

        monkeypatch.setattr(db, "find_active_incident_by_service", flaky_find)
        monkeypatch.setattr(db, "save_incident", dup_save)

        incident2, trigger2 = await alerts_mod._route_alert_to_incident(
            _firing_alert(fp="fp-u3", alertname="OOMKilled",
                          instance="payment-service-2:8080"))
        assert trigger2 == "escalation"
        assert incident2["incident_id"] == incident["incident_id"], "兜底应归入原事故"
        assert len(db._incidents) == 1, "不应产生第二个事故"


# ============================================================
# 8. B6 事故卡死保护：超时自动闭案 + 强制闭案
# ============================================================

class TestStaleIncidentSweep:
    """B6：卡死事故自动闭案扫描"""

    @pytest.mark.asyncio
    async def test_find_stale_active_incidents(self, incident_env):
        """last_seen_at 过旧的 active 事故被查出；新鲜的不会"""
        db = incident_env["db"]
        old_inc = {
            "incident_id": "INC-STALE-01", "status": "active", "service": "payment-sim",
            "fingerprints": ["fp:stale-1"], "resolved_fps": [],
            "alertnames": ["HighLatency"], "max_severity": "warning",
            "first_seen_at": datetime.now() - timedelta(hours=10),
            "last_seen_at": datetime.now() - timedelta(hours=10),
            "resolved_at": None, "diag_count": 0, "last_diag_at": None,
            "last_confidence_level": "unknown", "diagnosis_history": [], "summary": None,
        }
        fresh_inc = {
            "incident_id": "INC-FRESH-01", "status": "active", "service": "other-svc",
            "fingerprints": ["fp:fresh-1"], "resolved_fps": [],
            "alertnames": ["HighCpu"], "max_severity": "warning",
            "first_seen_at": datetime.now() - timedelta(minutes=5),
            "last_seen_at": datetime.now() - timedelta(minutes=5),
            "resolved_at": None, "diag_count": 0, "last_diag_at": None,
            "last_confidence_level": "unknown", "diagnosis_history": [], "summary": None,
        }
        await db.save_incident(old_inc)
        await db.save_incident(fresh_inc)

        stale = await db.find_stale_active_incidents(older_than_seconds=3600)  # 1h
        stale_ids = [i["incident_id"] for i in stale]
        assert "INC-STALE-01" in stale_ids
        assert "INC-FRESH-01" not in stale_ids

    @pytest.mark.asyncio
    async def test_force_resolve_auto_marks_auto_resolved(self, incident_env):
        """worker 自动闭案标记 auto_resolved=True"""
        db = incident_env["db"]
        inc = {
            "incident_id": "INC-STALE-02", "status": "active", "service": "payment-sim",
            "fingerprints": ["fp:stale-2"], "resolved_fps": [],
            "alertnames": ["HighLatency"], "max_severity": "warning",
            "first_seen_at": datetime.now() - timedelta(hours=10),
            "last_seen_at": datetime.now() - timedelta(hours=10),
            "resolved_at": None, "diag_count": 0, "last_diag_at": None,
            "last_confidence_level": "unknown", "diagnosis_history": [], "summary": None,
        }
        await db.save_incident(inc)

        result = await db.force_resolve_incident(
            "INC-STALE-02", by_user="stale-sweep", auto=True)
        assert result["status"] == "resolved"
        assert result["auto_resolved"] is True
        assert result["force_resolved_by"] == "stale-sweep"
        assert result["resolved_at"] is not None

    @pytest.mark.asyncio
    async def test_force_resolve_nonexistent_returns_none(self, incident_env):
        db = incident_env["db"]
        assert await db.force_resolve_incident("INC-NOPE", by_user="x") is None


# ============================================================
# 9. B7 行动项解析鲁棒性：prompt 强化 + 定向重试
# ============================================================

class TestActionItemsRobustness:
    """B7：摘要行动项解析鲁棒性"""

    def test_summary_prompt_has_forced_action_items_section(self):
        """prompt 含独立强制行动项段 + 示例"""
        from app.api.alerts import _build_summary_prompt
        incident = {
            "incident_id": "INC-T", "alertnames": ["A"],
            "max_severity": "warning",
            "first_seen_at": datetime.now(), "resolved_at": datetime.now(),
            "diagnosis_history": [{"trigger": "initial", "root_cause": "x",
                                   "at": datetime.now()}],
        }
        prompt = _build_summary_prompt(incident)
        assert "强制要求" in prompt, "应有强制要求字样"
        assert "### 行动项" in prompt
        assert "【检测】" in prompt and "【预防】" in prompt and "【缓解】" in prompt
        assert "负责人" in prompt

    def test_parse_action_items_strict_format(self):
        """严格格式（【类别】描述（负责人: X，期限: Y））正确解析"""
        from app.api.alerts import _parse_action_items
        content = (
            "### 事故时间线\n...\n"
            "### 影响\n...\n"
            "### 行动项\n"
            "- 【检测】为支付接口增加 P95 延迟告警（负责人: 张三，期限: 2026-09-10）\n"
            "- 【预防】给连接池加入提前扩容阈值（负责人: 待定，期限: 2026-09-15）\n"
            "- 【缓解】准备连接池耗尽止血 runbook（负责人: 李四，期限: 2026-09-08）\n"
            "### 经验教训\n..."
        )
        items = _parse_action_items(content)
        assert len(items) == 3
        assert items[0]["category"] == "检测"
        assert "P95" in items[0]["item"]
        assert items[0]["owner"] == "张三"
        assert items[0]["deadline"] == "2026-09-10"
        assert items[1]["owner"] == "待定"
        assert items[2]["category"] == "缓解"

    def test_parse_action_items_empty_when_no_section(self):
        from app.api.alerts import _parse_action_items
        assert _parse_action_items("### 时间线\n无行动项章节") == []
        assert _parse_action_items("") == []

    @pytest.mark.asyncio
    async def test_summary_retries_when_action_items_empty(self, incident_env, monkeypatch):
        """摘要未解析到行动项 → 定向重试；重试返回有效行动项 → 落库"""
        alerts_mod, db = (incident_env["alerts"], incident_env["db"])
        monkeypatch.setattr(settings, "INCIDENT_RESOLVE_QUIET_PERIOD", 0)

        # 首次摘要不含行动项章节，重试只返回行动项
        call_count = {"n": 0}

        class RetryAgent:
            async def run(self, user_input="", session_id=None, context=None,
                          use_web_search=False):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    return {
                        "content": "### 时间线\n恢复\n### 影响\n轻微\n### 根因\nx",
                        "tools_used": [],
                        "diagnosis_report": {"root_cause": "x",
                                             "confidence_level": "high"},
                    }
                return {
                    "content": (
                        "### 行动项\n"
                        "- 【检测】增加告警（负责人: 张三，期限: 2026-09-10）\n"
                        "- 【预防】扩容阈值（负责人: 待定，期限: 2026-09-15）\n"
                    ),
                    "tools_used": [],
                    "diagnosis_report": {"root_cause": "x",
                                         "confidence_level": "high"},
                }

        import app.api.langgraph as lg
        monkeypatch.setattr(lg, "get_agent", lambda: RetryAgent())

        await alerts_mod.enqueue_diagnosis_tasks([_firing_alert(fp="fp-b7-1")])
        await alerts_mod._drain_pending_tasks()
        await alerts_mod.handle_resolved_alerts(
            [{**_firing_alert(fp="fp-b7-1"), "status": "resolved"}])
        await alerts_mod._drain_pending_tasks()  # 摘要任务

        assert call_count["n"] == 2, "应触发一次定向重试"
        inc = db._incidents[0]
        assert inc["status"] == "resolved"
        assert len(inc["action_items"]) == 2, "重试后应解析到行动项"
        assert inc["action_items"][0]["category"] == "检测"
