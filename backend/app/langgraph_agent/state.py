"""
Agent 状态定义

定义 LangGraph Agent 的状态结构。
"""

from typing import List, Dict, Any, Optional, Annotated
from dataclasses import dataclass, field
from langgraph.graph import MessagesState


class AgentState(MessagesState):
    """
    Agent 状态

    继承 MessagesState，添加自定义字段。
    """
    # 工具调用记录
    tools_used: List[str] = []
    tool_results: Dict[str, Any] = {}

    # 任务信息
    task_type: str = ""  # 问答、总结、生成
    task_context: Dict[str, Any] = {}

    # 查询意图（P1-1 查询路由）
    # "diagnosis": 线上故障诊断（走完整诊断工作流 + 反思）
    # "qa": 通用知识/流程/经验问答（标准 ReAct，无反思，简洁回答）
    # "unknown": 路由未确定（默认走 qa 链路）
    intent: str = "unknown"

    # 一次性组装的记忆上下文（P1-2 上下文一次性组装）
    # run() 入口组装，_build_system_prompt 直接读取，避免每步重复检索
    memory_context: str = ""

    # RAG 检索结果（知识库证据）
    retrieved_docs: List[Dict] = []
    citations: List[Dict] = []

    # 监控证据（MCP 工具 query_metrics/query_logs 返回的结构化提取）
    # 每条: {type:"metrics"/"logs", service, summary, details}
    monitoring_evidence: List[Dict] = []

    # 结构化诊断报告（运维故障诊断 Agent 专用）
    # 从 LLM 的 Markdown 输出中解析：symptom/evidence/root_cause/solution/confidence
    diagnosis_report: Optional[Dict] = None

    # 执行信息
    step_count: int = 0
    max_steps: int = 8
