"""
Agent 级评估：故障诊断 Agent 端到端质量评估（RAG + Agent Evaluation）

指标：
1. Root Cause Accuracy    根因准确性：诊断报告根因与期望根因关键词的命中率（可选 LLM Judge）
2. Evidence Recall       证据召回率：期望证据（监控指标/日志/知识库引用）在 Agent 输出中的覆盖率
3. Tool Call Accuracy    工具调用准确性：期望工具覆盖率 + 监控优先顺序正确率
4. Diagnosis Success Rate 诊断成功率：输出结构化诊断报告且置信度合格的占比

用法：
    cd backend
    python evaluation/eval/agent_eval.py [--scenarios 1-12] [--top-k 8] [--llm-judge]

场景集：evaluation/data/agent_eval_scenarios.json（12 个，基于真实 incident 设计）
依赖：复用 agent.run() 返回的 tools_used / monitoring_evidence / diagnosis_report / citations，
      不修改任何核心代码（零侵入）。
输出：evaluation/results/agent_eval_report.json
"""
import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

from loguru import logger

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BACKEND_DIR))

# 环境修复：REQUESTS_CA_BUNDLE 可能指向不存在的路径，导致本地 BGE 模型 TLS 校验失败
try:
    import certifi
    if not os.path.exists(os.environ.get("REQUESTS_CA_BUNDLE", "")):
        os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
        os.environ["SSL_CERT_FILE"] = certifi.where()
except Exception:
    pass

DATA_DIR = BACKEND_DIR / "evaluation" / "data"
RESULTS_DIR = BACKEND_DIR / "evaluation" / "results"

SCENARIOS_PATH = DATA_DIR / "agent_eval_scenarios.json"


# ========== 指标计算 ==========

def keyword_hit_rate(text: str, keywords: List[str]) -> float:
    """关键词命中率（0~1），text 为空或 keywords 为空时返回 0"""
    if not keywords or not text:
        return 0.0
    tl = text.lower()
    return sum(1 for kw in keywords if kw.lower() in tl) / len(keywords)


def tool_call_accuracy(tools_used: List[str], scenario: Dict, dataset: Dict) -> Dict[str, float]:
    """工具调用准确性：覆盖率 + 顺序正确率"""
    required = set(scenario.get("required_tools") or [])
    monitoring = set(dataset.get("monitoring_tools") or [])
    kb_tool = dataset.get("kb_tool", "search_knowledge")
    tools = set(tools_used)

    coverage = len(required & tools) / len(required) if required else 1.0

    mono_idx = next((i for i, t in enumerate(tools_used) if t in monitoring), None)
    kb_idx = next((i for i, t in enumerate(tools_used) if t == kb_tool), None)
    order_ok = mono_idx is not None and kb_idx is not None and mono_idx < kb_idx

    accuracy = 0.5 * coverage + (0.5 if order_ok else 0.0)
    return {
        "coverage": round(coverage, 3),
        "order_ok": float(order_ok),
        "accuracy": round(accuracy, 3),
    }


def evidence_recall(result: Dict, scenario: Dict) -> Dict[str, float]:
    """证据召回率：三通道（指标/日志/知识库）期望证据在 Agent 输出中的覆盖率"""
    expected = scenario.get("expected_evidence") or {}
    # 收集 Agent 输出中的全部证据文本
    evidence_texts: List[str] = []
    for ev in result.get("monitoring_evidence") or []:
        evidence_texts.append(str(ev.get("summary", "")))
        evidence_texts.append(str(ev.get("details", "")))
    for c in result.get("citations") or []:
        evidence_texts.append(str(c.get("title", "")))
        evidence_texts.append(str(c.get("content", ""))[:500])
    diag = result.get("diagnosis_report") or {}
    evidence_texts.append(str(diag.get("evidence", "")))
    evidence_texts.append(str(result.get("content", ""))[:2000])
    blob = "\n".join(evidence_texts)

    per_channel = {}
    for channel in ("metrics", "logs", "knowledge"):
        kws = expected.get(channel) or []
        per_channel[channel] = round(keyword_hit_rate(blob, kws), 3)
    recall = round(statistics.mean(per_channel.values()) if per_channel else 0.0, 3)
    return {"per_channel": per_channel, "recall": recall}


def root_cause_accuracy(result: Dict, scenario: Dict, llm_judge_result: Any = None) -> Dict[str, Any]:
    """根因准确性：诊断报告根因 vs 期望根因关键词命中率（recall）；可选 LLM Judge 语义判定（异步执行后传入）"""
    diag = result.get("diagnosis_report") or {}
    rc_text = str(diag.get("root_cause", "")) or ""
    keywords = scenario.get("expected_root_cause") or []
    recall = round(keyword_hit_rate(rc_text, keywords), 3)

    out: Dict[str, Any] = {
        "recall": recall,
        "root_cause_extracted": rc_text[:120],
    }
    if llm_judge_result is not None:
        out["llm_judge"] = llm_judge_result
    return out


async def llm_root_cause_judge(root_cause: str, keywords: List[str]) -> float:
    """LLM-as-Judge（异步）：判定诊断根因与期望根因语义是否一致（返回 0/1）"""
    try:
        from app.core.ai_service import ai_service
    except Exception:
        return 0.0
    prompt = (
        f"诊断报告的根因描述：{root_cause or '（空）'}\n"
        f"期望根因要点：{'、'.join(keywords)}\n"
        "请判定诊断根因是否覆盖期望根因的核心语义。只回答 1（一致）或 0（不一致）。"
    )
    try:
        resp = await ai_service.chat(messages=[
            {"role": "system", "content": "你是故障根因评估裁判，只输出 1 或 0。"},
            {"role": "user", "content": prompt},
        ])
        answer = (resp.get("content") or "").strip()
        return 1.0 if answer.startswith("1") else 0.0
    except Exception:
        return 0.0


def diagnosis_success(result: Dict) -> Dict[str, Any]:
    """诊断成功率：是否输出结构化报告 + 置信度是否合格"""
    diag = result.get("diagnosis_report")
    report_ok = bool(diag)
    confidence_ok = False
    confidence_level = ""
    if diag:
        # agent.py 解析的结构：confidence 为原文字符串，confidence_level 为顶层字段
        # {"confidence": "...", "confidence_level": "high|medium|low|unknown"}
        confidence_level = str(diag.get("confidence_level", "") or "")
        confidence_ok = confidence_level in ("high", "medium")
    success = report_ok and confidence_ok
    return {
        "report_ok": float(report_ok),
        "confidence_ok": float(confidence_ok),
        "confidence_level": confidence_level,
        "success": float(success),
    }


# ========== 主评估 ==========

async def run_scenario(agent, scenario: Dict) -> Dict[str, Any]:
    """对单个场景执行诊断并采集中间状态"""
    query = scenario["user_input"]
    session_id = f"agent_eval_{scenario['scenario_id']}_{int(time.time())}"
    start = time.perf_counter()
    result = await agent.run(
        user_input=query,
        session_id=session_id,
        context={"user_id": ""},
        use_web_search=False,
    )
    latency = time.perf_counter() - start
    return {
        "scenario_id": scenario["scenario_id"],
        "query": query,
        "tools_used": result.get("tools_used", []),
        "step_count": result.get("step_count", 0),
        "monitoring_evidence_count": len(result.get("monitoring_evidence", [])),
        "citations_count": len(result.get("citations", [])),
        "diagnosis_report": result.get("diagnosis_report"),
        "content_preview": (result.get("content") or "")[:300],
        "latency_s": round(latency, 1),
        "raw": result,
    }


async def main():
    parser = argparse.ArgumentParser(description="Agent 级故障诊断评估")
    parser.add_argument("--scenarios", type=str, default="", help="逗号分隔的场景 ID，如 SC-001,SC-002；空=全部")
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--llm-judge", action="store_true", help="启用 LLM 根因语义判定（需要 LLM 可用）")
    args = parser.parse_args()

    if not SCENARIOS_PATH.exists():
        logger.error(f"场景集不存在: {SCENARIOS_PATH}")
        return
    dataset = json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))
    scenarios = dataset["scenarios"]
    if args.scenarios:
        want = {s.strip() for s in args.scenarios.split(",") if s.strip()}
        scenarios = [s for s in scenarios if s["scenario_id"] in want]
    logger.info(f"加载 {len(scenarios)} 个诊断场景")

    # 初始化知识库 + Agent + MCP 监控工具（参考 verify_agent_diagnosis.py）
    from app.shared_services import init_knowledge_store
    from app.langgraph_agent.agent import LangGraphAgent
    from app.core.config import settings

    store = init_knowledge_store()
    agent = LangGraphAgent(
        llm_model=settings.AI_MODEL.split("/")[-1] if "/" in settings.AI_MODEL else settings.AI_MODEL,
        llm_base_url=settings.AI_BASE_URL or "https://api.deepseek.com",
        llm_api_key=settings.AI_API_KEY or "dummy",
        knowledge_store=store,
        checkpoint_path=str(BACKEND_DIR / "data" / "langgraph_checkpoints.db"),
    )
    mcp_count = await agent.init_mcp_tools(LangGraphAgent._build_default_mcp_config())
    logger.info(f"MCP 监控工具加载: {mcp_count} 个（0 = 纯知识库模式降级）")

    # 逐场景执行
    per_scenario: List[Dict[str, Any]] = []
    for sc in scenarios:
        logger.info(f"执行场景 {sc['scenario_id']}: {sc['title']}")
        try:
            raw = await run_scenario(agent, sc)
            tool = tool_call_accuracy(raw["tools_used"], sc, dataset)
            evidence = evidence_recall(raw, sc)
            rc_text = str((raw.get("diagnosis_report") or {}).get("root_cause", "")) or ""
            llm_judge_val = None
            if args.llm_judge:
                llm_judge_val = await llm_root_cause_judge(
                    rc_text, sc.get("expected_root_cause") or [])
            rc = root_cause_accuracy(raw, sc, llm_judge_val)
            success = diagnosis_success(raw)
            per_scenario.append({
                "scenario_id": sc["scenario_id"],
                "title": sc["title"],
                "ground_truth_incident": sc["ground_truth_incident"],
                "tools_used": raw["tools_used"],
                "step_count": raw["step_count"],
                "tool_call": tool,
                "evidence": evidence,
                "root_cause": rc,
                "diagnosis": success,
                "latency_s": raw["latency_s"],
            })
        except Exception as e:
            logger.error(f"场景 {sc['scenario_id']} 执行失败: {e}")
            per_scenario.append({
                "scenario_id": sc["scenario_id"],
                "title": sc["title"],
                "error": str(e),
            })

    # 汇总
    def _avg(key, sub=None, default=0.0):
        vals = [s[key][sub] if sub else s[key] for s in per_scenario
                if key in s and (sub is None or sub in s[key])]
        return round(statistics.mean(vals), 3) if vals else default

    # 证据分通道均值（metrics/logs/knowledge）
    def _avg_channel(channel: str) -> float:
        vals = [s["evidence"]["per_channel"].get(channel, 0.0) for s in per_scenario
                if "evidence" in s]
        return round(statistics.mean(vals), 3) if vals else 0.0

    summary = {
        "scenario_count": len(scenarios),
        "completed": sum(1 for s in per_scenario if "tool_call" in s),
        "failed": sum(1 for s in per_scenario if "error" in s),
        "mcp_tools_loaded": mcp_count,
        "tool_call_accuracy": _avg("tool_call", "accuracy"),
        "tool_coverage": _avg("tool_call", "coverage"),
        "tool_order_ok_rate": _avg("tool_call", "order_ok"),
        "evidence_recall": _avg("evidence", "recall"),
        "evidence_metrics_channel": _avg_channel("metrics"),
        "evidence_logs_channel": _avg_channel("logs"),
        "evidence_knowledge_channel": _avg_channel("knowledge"),
        "root_cause_accuracy": _avg("root_cause", "recall"),
        "report_rate": _avg("diagnosis", "report_ok"),
        "confidence_ok_rate": _avg("diagnosis", "confidence_ok"),
        "diagnosis_success_rate": _avg("diagnosis", "success"),
        "avg_latency_s": _avg("latency_s"),
    }

    report = {
        "dataset": "agent_eval_scenarios",
        "top_k": args.top_k,
        "llm_judge": args.llm_judge,
        "summary": summary,
        "per_scenario": per_scenario,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / "agent_eval_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"评估报告已保存: {out}")

    # 控制台摘要
    print("\n" + "=" * 70)
    print("Agent 级故障诊断评估结果")
    print("=" * 70)
    print(f"场景数: {summary['scenario_count']}  完成: {summary['completed']}  失败: {summary['failed']}  MCP工具: {mcp_count}")
    print("-" * 70)
    print(f"Tool Call Accuracy : {summary['tool_call_accuracy']}  (覆盖率 {summary['tool_coverage']} / 顺序正确率 {summary['tool_order_ok_rate']})")
    print(f"Evidence Recall   : {summary['evidence_recall']}")
    print(f"Root Cause Accuracy: {summary['root_cause_accuracy']}")
    print(f"Diagnosis Success : {summary['diagnosis_success_rate']}  (报告率 {summary['report_rate']} / 置信度合格 {summary['confidence_ok_rate']})")
    print(f"平均诊断耗时       : {summary['avg_latency_s']}s")
    print("-" * 70)
    for s in per_scenario:
        if "tool_call" in s:
            print(f"  {s['scenario_id']} [{s['ground_truth_incident']}] "
                  f"tool={s['tool_call']['accuracy']} evid={s['evidence']['recall']} "
                  f"root={s['root_cause']['recall']} diag={s['diagnosis']['success']} "
                  f"steps={s['step_count']} tools={','.join(s['tools_used'][:4])}")
        else:
            print(f"  {s['scenario_id']} ✗ 失败: {s.get('error')}")


if __name__ == "__main__":
    asyncio.run(main())
