"""方向2 开放场景集校验测试。

验证 agent_eval_scenarios_open.json 相对主集 agent_eval_scenarios.json 是『真正未见』：
1. schema 完整（与 agent_eval.py 消费字段对齐）＋ 方向2 扩展字段存在
2. scenario_id 用 SC-OPEN-* 前缀，不与主集任何 id 冲突
3. user_input 为全新事件文本（不为主集任一输入的复读）
4. expected_root_cause 不得命中主集任何根因关键词 —— 保证『组合/未见』而非『检索复读』
5. closed_counterparts 引用的主集 id 必须真实存在
"""

import json
from pathlib import Path

import pytest

DATA_DIR = Path(__file__).resolve().parent.parent / "evaluation" / "data"


@pytest.fixture(scope="module")
def _sets():
    closed = _load("agent_eval_scenarios.json")
    open_ = _load("agent_eval_scenarios_open.json")
    return {"closed": closed, "open": open_}


def _load(name):
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


def _all_root_cause_keywords(scenarios):
    kws = set()
    for s in scenarios:
        kws.update(s["expected_root_cause"])
    return kws


# ========== schema 校验 ==========

def test_open_scenarios_schema(_sets):
    """每个开放场景具备 agent_eval.py 消费的全部字段＋方向2 标注"""
    closed, open_ = list(_sets.values())
    required = {
        "scenario_id", "title", "user_input", "ground_truth_incident",
        "required_tools", "expected_root_cause", "expected_evidence",
        "expected_doc_types",
    }
    direction2 = {"is_open", "novelty", "closed_counterparts"}
    for s in open_["scenarios"]:
        missing = required - set(s.keys())
        assert not missing, f"{s['scenario_id']} 缺少字段: {missing}"
        missing2 = direction2 - set(s.keys())
        assert not missing2, f"{s['scenario_id']} 缺少方向2标注字段: {missing2}"
        # evidence 三通道齐全
        for ch in ("metrics", "logs", "knowledge"):
            assert ch in s["expected_evidence"], (
                f"{s['scenario_id']} expected_evidence 缺 {ch} 通道")
        assert s["is_open"] is True, f"{s['scenario_id']} is_open 应为 true"


def test_open_scenario_prefix_unique(_sets):
    """SC-OPEN-* 前缀，且不与主集任何 scenario_id 冲突"""
    closed, open_ = list(_sets.values())
    closed_ids = {s["scenario_id"] for s in closed["scenarios"]}
    seen = set()
    for s in open_["scenarios"]:
        assert s["scenario_id"].startswith("SC-OPEN-"), (
            f"{s['scenario_id']} 未使用 SC-OPEN- 前缀——开放集需与主集命名空间隔离")
        assert s["scenario_id"] not in closed_ids, f"与主集 id 冲突: {s['scenario_id']}"
        assert s["scenario_id"] not in seen, f"开放集内部重复: {s['scenario_id']}"
        seen.add(s["scenario_id"])


def test_open_user_input_novel(_sets):
    """开放集 user_input 不得是主集任一输入的复读/子串"""
    closed, open_ = list(_sets.values())
    closed_inputs = [s["user_input"].lower() for s in closed["scenarios"]]
    for s in open_["scenarios"]:
        q = s["user_input"].lower()
        for ci in closed_inputs:
            assert q != ci, f"{s['scenario_id']} user_input 与主集重复"
            assert len(q) < max(len(q), len(ci)) or ci not in q, (
                f"{s['scenario_id']} user_input 是主集输入的子串——未真正新增事件")


def test_open_root_cause_novel(_sets):
    """开放集每条根因关键词都不得命中主集任何根因 → 保证『未见/组合』而非『复读』"""
    closed, open_ = list(_sets.values())
    closed_kws = _all_root_cause_keywords(closed["scenarios"])
    for s in open_["scenarios"]:
        hit = set(s["expected_root_cause"]) & closed_kws
        assert not hit, (
            f"{s['scenario_id']} 的根因关键词 {hit} 直接命中主集——不算开放未见场景。"
            "请改为组合性新根因（组合主集多个单一故障、但不命中任一既有关键词）。")


def test_closed_counterparts_exist(_sets):
    """closed_counterparts 引用的主集 id 必须真实存在"""
    closed, open_ = list(_sets.values())
    closed_ids = {s["scenario_id"] for s in closed["scenarios"]}
    for s in open_["scenarios"]:
        missing = [cid for cid in s.get("closed_counterparts", []) if cid not in closed_ids]
        assert not missing, f"{s['scenario_id']} closed_counterparts 引用不存在: {missing}"


def test_open_has_required_tools(_sets):
    """开放场景 required_tools 须含监控优先（query_metrics 前置于检索）以维持评测口径一致"""
    closed, open_ = list(_sets.values())
    monitoring = set(closed.get("monitoring_tools") or [])
    kb = closed.get("kb_tool", "search_knowledge")
    for s in open_["scenarios"]:
        tools = s["required_tools"]
        assert set(tools) & monitoring, f"{s['scenario_id']} 缺监控工具"
        assert kb in tools, f"{s['scenario_id']} 缺知识库工具 {kb}"
        assert tools.index(kb) > 0, f"{s['scenario_id']} 知识库工具不应排在监控之前"