"""Agent 证据解析与组装工具。

从 LLM 工具返回的原始输出中提取/解析出结构化证据（监控指标、日志、图表、
告警、变更、知识库引用），并组装证据看板与诊断报告摘要。

这些函数都是纯函数：输入 messages/data 等，输出结构化 dict/字符串，
不依赖任何 Agent 实例状态，因此独立成模块便于复用与单测。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from langchain_core.messages import AIMessage, ToolMessage


def _collect_retrieved_docs(messages) -> List[Dict]:
    """从 messages 中的 ToolMessage 提取 artifact（备用方案）

    当前 LangGraph 1.1.x 的 ToolNode 调用 tool.invoke()，
    对 response_format="content_and_artifact" 只返回 content，artifact 丢失。
    此方法作为未来兼容的备用方案，主要数据来源是 pop_retrieval_buffer()。
    """
    retrieved: List[Dict] = []
    seen_ids: set = set()
    for msg in messages:
        if not isinstance(msg, ToolMessage):
            continue
        tc_id = getattr(msg, 'tool_call_id', None)
        if tc_id and tc_id in seen_ids:
            continue
        artifact = getattr(msg, 'artifact', None)
        if artifact and isinstance(artifact, list):
            if tc_id:
                seen_ids.add(tc_id)
            retrieved.extend(artifact)
    return retrieved


def _merge_retrieved_docs(docs: List[Dict]) -> List[Dict]:
    """合并检索结果并去重（按 doc_id + content 前 100 字符）"""
    seen: set = set()
    unique: List[Dict] = []
    for doc in docs:
        key = (doc.get("doc_id", ""), doc.get("content", "")[:100])
        if key in seen:
            continue
        seen.add(key)
        unique.append(doc)
    return unique


def _extract_monitoring_evidence(messages) -> List[Dict]:
    """从 messages 中的 ToolMessage 提取监控证据（query_metrics/query_logs 返回）

    与 _collect_retrieved_docs 类似，但专门处理 MCP 监控工具的结构化 JSON 数据。
    将 JSON 返回值解析为 {type, service, summary, details} 格式，供证据看板使用。

    为什么需要这个：监控工具返回的 JSON 散落在 ToolMessage 文本中，
    LLM 无法可靠回顾"我查了什么指标、值是多少"。结构化提取后注入到
    system prompt 的证据看板，LLM 始终能看到证据全貌。

    Returns:
        [{"type":"metrics"/"logs", "service":"...", "summary":"...", "details":{...}}]
    """

    # 1. 构建 tool_call_id → tool_name 映射（从 AIMessage.tool_calls）
    tool_call_names: Dict[str, str] = {}
    for msg in messages:
        if isinstance(msg, AIMessage) and hasattr(msg, 'tool_calls'):
            for tc in (msg.tool_calls or []):
                tc_id = tc.get('id')
                tc_name = tc.get('name')
                if tc_id and tc_name:
                    tool_call_names[tc_id] = tc_name

    # 2. 遍历 ToolMessage，提取监控工具结果
    evidence: List[Dict] = []
    seen_call_ids: set = set()

    for msg in messages:
        if not isinstance(msg, ToolMessage):
            continue
        tc_id = getattr(msg, 'tool_call_id', None)
        if tc_id and tc_id in seen_call_ids:
            continue

        # 工具名：优先从映射查（兼容旧版本 ToolMessage 无 name 字段）
        tool_name = tool_call_names.get(tc_id, '') or getattr(msg, 'name', '')
        # 兼容旧 mock 工具名 + 新 MCP 工具名：
        # 旧 mock: query_metrics / query_logs / analyze_chart
        # 新 MCP:  query_prometheus / query_prometheus_range / query_system_overview
        #          query_logs(Loki) / query_loki / list_containers
        #          query_alerts / query_alertmanager / query_silences
        _MONITORING_TOOL_NAMES = {
            'query_metrics', 'query_logs', 'analyze_chart',
            'get_recent_changes',
            'query_prometheus', 'query_prometheus_range', 'query_system_overview',
            'query_loki', 'list_containers',
            'query_alerts', 'query_alertmanager', 'query_silences',
        }
        if tool_name not in _MONITORING_TOOL_NAMES:
            continue

        if tc_id:
            seen_call_ids.add(tc_id)

        # 解析 JSON 内容
        # MCP 工具（langchain-mcp-adapters）返回 content 为 list[TextBlock] 格式：
        # [{'type': 'text', 'text': '{"service":"...",...}'}]
        # 普通工具返回 content 为 str
        raw_content = msg.content
        if isinstance(raw_content, list):
            # MCP TextBlock 格式：提取所有 text 块拼接
            text_parts = [block.get('text', '') for block in raw_content if isinstance(block, dict) and block.get('type') == 'text']
            raw_content = '\n'.join(text_parts)
        try:
            data = json.loads(raw_content) if isinstance(raw_content, str) else raw_content
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict) or data.get("error"):
            continue

        # 分发到对应解析器：
        # - 指标类（query_metrics / query_prometheus / query_system_overview）→ metrics 证据
        # - 日志类（query_logs mock / query_logs Loki / query_loki）→ logs 证据
        # - 告警类（query_alerts / query_alertmanager）→ alerts 证据
        # - 图表类（analyze_chart）→ chart 证据
        # - list_containers / query_silences → 跳过（辅助信息，不是诊断证据）
        if tool_name in ('query_metrics', 'query_prometheus',
                         'query_prometheus_range', 'query_system_overview'):
            ev = _parse_metrics_evidence(data)
        elif tool_name == 'analyze_chart':
            ev = _parse_chart_evidence(data)
        elif tool_name in ('query_logs', 'query_loki'):
            ev = _parse_logs_evidence(data)
        elif tool_name in ('query_alerts', 'query_alertmanager'):
            ev = _parse_alerts_evidence(data)
        elif tool_name == 'get_recent_changes':
            ev = _parse_changes_evidence(data)
        else:
            ev = None  # list_containers / query_silences 等辅助工具，不提取为证据
        if ev:
            evidence.append(ev)

    return evidence


def _merge_monitoring_evidence(evidence: List[Dict]) -> List[Dict]:
    """合并监控证据并去重（按 type + service + summary 前 80 字符）"""
    seen: set = set()
    unique: List[Dict] = []
    for ev in evidence:
        key = (ev.get("type", ""), ev.get("service", ""), ev.get("summary", "")[:80])
        if key in seen:
            continue
        seen.add(key)
        unique.append(ev)
    return unique


def _parse_metrics_evidence(data: Dict) -> Optional[Dict]:
    """解析监控指标工具返回的 JSON 为结构化监控证据

    兼容 5 种返回格式：
    1. 旧 mock query_metrics(all): {"service":"...", "metrics": {name: {value, baseline, unit}}}
    2. 旧 mock query_metrics(单): {"service":"...", "metric":"...", "value":..., "baseline":...}
    3. 新 MCP query_system_overview: {"timestamp":"...", "metrics": {name: {value, labels}}}
    4. 新 MCP query_prometheus: {"query":"...", "count":N, "values":[{labels, value}]}
    5. 新 MCP query_prometheus_range: {"query":"...", "series":[{labels, points}]}
    """

    def _fmt_val(v) -> str:
        """格式化数值：浮点保留 2 位，百分比类自动 *100"""
        if isinstance(v, (int, float)):
            return f"{v:.2f}" if abs(v) < 1e6 else f"{v:.2e}"
        return str(v)

    # 格式3: 新 query_system_overview（有 metrics，无 service/baseline）
    if "metrics" in data and "service" not in data:
        parts = []
        details = {}
        for name, m in data["metrics"].items():
            if not isinstance(m, dict) or "error" in m:
                continue
            if "value" in m and m["value"] is not None:
                v = m["value"]
                parts.append(f"{name}={_fmt_val(v)}")
                details[name] = {"value": v}
            elif "values" in m:
                # 多值指标（如各网卡流量），取汇总值
                vals = m["values"]
                total = sum(
                    v.get("value", 0) for v in vals
                    if isinstance(v, dict) and isinstance(v.get("value"), (int, float))
                )
                parts.append(f"{name}(sum)={_fmt_val(total)}")
                details[name] = {"count": len(vals), "total": total}
        return {
            "type": "metrics",
            "service": "system",
            "summary": ", ".join(parts) if parts else "无指标数据",
            "details": details,
        }

    # 格式1: 旧 mock query_metrics(all)（有 service + metrics）
    if "metrics" in data:
        service = data.get("service", "unknown")
        parts = []
        details = {}
        for metric_name, metric_data in data["metrics"].items():
            if not isinstance(metric_data, dict):
                continue
            value = metric_data.get("value")
            baseline = metric_data.get("baseline")
            unit = metric_data.get("unit", "")

            if unit == "ratio" and isinstance(value, (int, float)):
                val_str = f"{value * 100:.0f}%"
                base_str = f"{baseline * 100:.1f}%" if isinstance(baseline, (int, float)) else "N/A"
                parts.append(f"{metric_name}={val_str}(基线{base_str})")
            elif isinstance(baseline, (int, float)) and isinstance(value, (int, float)):
                parts.append(f"{metric_name}={value}(基线{baseline})")
            else:
                parts.append(f"{metric_name}={value}")

            details[metric_name] = {
                "value": value, "baseline": baseline, "unit": unit,
                "description": metric_data.get("description", ""),
            }
        return {
            "type": "metrics",
            "service": service,
            "summary": ", ".join(parts) if parts else "无指标数据",
            "details": details,
        }

    # 格式4: 新 query_prometheus（有 values 列表）
    if "values" in data:
        query = data.get("query", "unknown")
        values = data.get("values", [])
        parts = []
        details = {}
        for v in values[:5]:
            if not isinstance(v, dict):
                continue
            labels = v.get("labels", {})
            val = v.get("value")
            name = labels.get("__name__") or labels.get("device") or query[:30]
            parts.append(f"{name}={_fmt_val(val)}")
            details[name] = {"value": val, "labels": labels}
        return {
            "type": "metrics",
            "service": "prometheus",
            "summary": ", ".join(parts) if parts else f"查询: {query[:40]}（无数据）",
            "details": details,
        }

    # 格式5: 新 query_prometheus_range（有 series 时序数据）
    if "series" in data:
        query = data.get("query", "unknown")
        series = data.get("series", [])
        parts = []
        details = {}
        for s in series[:3]:
            if not isinstance(s, dict):
                continue
            labels = s.get("labels", {})
            points = s.get("points", [])
            name = labels.get("__name__") or query[:30]
            if points:
                last_val = points[-1].get("value", 0)
                parts.append(f"{name}[末值]={_fmt_val(last_val)}")
                details[name] = {"last_value": last_val, "points": len(points)}
        return {
            "type": "metrics",
            "service": "prometheus_range",
            "summary": ", ".join(parts) if parts else f"查询: {query[:40]}（无数据）",
            "details": details,
        }

    # 格式2: 旧 mock 单指标（fallback）
    service = data.get("service", "unknown")
    metric_name = data.get("metric", "unknown")
    value = data.get("value")
    baseline = data.get("baseline")
    unit = data.get("unit", "")
    if unit == "ratio" and isinstance(value, (int, float)):
        val_str = f"{value * 100:.0f}%"
        base_str = f"{baseline * 100:.1f}%" if isinstance(baseline, (int, float)) else "N/A"
        summary = f"{metric_name}={val_str}(基线{base_str})"
    else:
        summary = f"{metric_name}={value}"
    return {
        "type": "metrics",
        "service": service,
        "summary": summary,
        "details": {metric_name: {"value": value, "baseline": baseline, "unit": unit}},
    }


def _parse_logs_evidence(data: Dict) -> Optional[Dict]:
    """解析 query_logs 返回的 JSON 为结构化监控证据

    兼容两种返回格式：
    1. 旧 mock 格式：{"service":"...", "keyword":"...", "count":N, "logs":["text", ...]}
    2. 新 Loki 格式：{"query":"...", "range_minutes":N, "count":N,
                      "logs":[{"timestamp","container","line","labels"}, ...]}
    """
    # service：旧 mock 有 service 字段；Loki 用 container（取第一条日志的 container）
    service = data.get("service") or data.get("container") or "unknown"
    # keyword：旧 mock 有 keyword；Loki 用 query（LogQL）
    keyword = data.get("keyword") or data.get("query") or "unknown"
    logs = data.get("logs", [])
    count = data.get("count", len(logs))

    # 提取前 2 条日志的关键内容（截断避免过长）
    # 兼容字符串（旧 mock）和 dict（新 Loki）两种元素格式
    sample_logs = []
    for line in logs[:2]:
        if isinstance(line, dict):
            # Loki 格式：取 line 字段（真实日志文本）
            text = line.get("line", "")
            ts = line.get("timestamp", "")
            container = line.get("container", "")
            text = f"[{ts}]{container}: {text}" if ts else text
        elif isinstance(line, str):
            text = line
        else:
            text = str(line)
        sample_logs.append(text[:120])

    summary = f"{keyword}: {count}条日志"
    if sample_logs:
        summary += f", 关键: {' | '.join(sample_logs)}"

    return {
        "type": "logs",
        "service": service,
        "summary": summary,
        "details": {"keyword": keyword, "count": count, "sample_logs": logs[:5]},
    }


def _parse_chart_evidence(data: Dict) -> Optional[Dict]:
    """解析 analyze_chart 返回的 JSON 为结构化监控证据（VLM 看图结果）

    格式: {"service":"...", "chart_type":"...", "metrics": {...},
           "anomalies": [...], "insights": "..."}
    """
    service = data.get("service", "unknown")
    chart_type = data.get("chart_type", "overview")

    # 指标部分（复用 metrics 解析逻辑，支持 ratio 转百分比）
    metric_parts = []
    metrics = data.get("metrics", {})
    for name, m in metrics.items():
        if not isinstance(m, dict):
            continue
        value = m.get("value")
        baseline = m.get("baseline")
        if m.get("unit") == "ratio" and isinstance(value, (int, float)):
            base_str = f"{baseline * 100:.1f}%" if isinstance(baseline, (int, float)) else "N/A"
            metric_parts.append(f"{name}={value * 100:.0f}%(基线{base_str})")
        else:
            metric_parts.append(f"{name}={value}")
    metrics_str = ", ".join(metric_parts) if metric_parts else "无指标"

    # 异常模式部分（VLM 看图才能发现的形态级信息）
    anomalies = data.get("anomalies", [])
    anomaly_str = "; ".join(a.get("description", "") for a in anomalies[:3]) if anomalies else "无明显异常"

    # 洞察（跨指标关联推理）
    insights = data.get("insights", "")

    summary = f"图表分析({chart_type}): {metrics_str}"
    if anomalies:
        summary += f" | 异常: {anomaly_str}"
    if insights:
        summary += f" | 洞察: {insights[:100]}"

    return {
        "type": "chart",
        "service": service,
        "summary": summary,
        "details": {
            "chart_type": chart_type,
            "metrics": metrics,
            "anomalies": anomalies,
            "insights": insights,
        },
    }


def _parse_alerts_evidence(data: Dict) -> Optional[Dict]:
    """解析 alertmanager 工具返回的 JSON 为结构化监控证据

    兼容两种返回格式：
    1. query_alerts: {"count":N, "alerts":[{alert_name,severity,summary,...}]}
    2. query_alertmanager: {"endpoint":"alerts", "data":[原始告警列表]}
    """
    # query_alertmanager 格式：data 字段是原始 API 响应列表
    if "data" in data and isinstance(data["data"], list):
        alerts_raw = data["data"]
        alerts = []
        for a in alerts_raw:
            labels = a.get("labels", {}) if isinstance(a, dict) else {}
            annotations = a.get("annotations", {}) if isinstance(a, dict) else {}
            status = a.get("status", {}) if isinstance(a, dict) else {}
            alerts.append({
                "alert_name": labels.get("alertname", "unknown"),
                "severity": labels.get("severity", "unknown"),
                "state": status.get("state", "unknown"),
                "summary": annotations.get("summary", ""),
            })
    else:
        # query_alerts 格式：alerts 字段已格式化
        alerts = data.get("alerts", [])

    count = data.get("count", len(alerts))

    # 按严重级别统计 + 提取告警名
    severity_count = {"critical": 0, "warning": 0, "info": 0}
    alert_names = []
    for a in alerts:
        sev = a.get("severity", "unknown")
        severity_count[sev] = severity_count.get(sev, 0) + 1
        name = a.get("alert_name") or a.get("alertname") or "unknown"
        alert_names.append(name)

    # summary：告警数 + 严重级别分布 + 告警名
    parts = [f"{count}条告警"]
    for sev, n in severity_count.items():
        if n > 0:
            parts.append(f"{sev}:{n}")
    if alert_names:
        parts.append("[" + ", ".join(alert_names[:3]) + "]")
    summary = " ".join(parts)

    return {
        "type": "alerts",
        "service": "alertmanager",
        "summary": summary,
        "details": {
            "count": count,
            "severity_count": severity_count,
            "alerts": alerts[:5],  # 保留前 5 条详情
        },
    }


def _parse_changes_evidence(data: Dict) -> Optional[Dict]:
    """解析 get_recent_changes 返回的 JSON 为变更事件证据

    返回格式: {"service":"...", "hours":N, "count":N, "changes":[{change_id,type,time,description}]}
    变更是生产故障的第一大根因，单独作为一类证据（type="changes"）进入证据看板。
    """
    changes = data.get("changes", [])
    if not changes:
        return {
            "type": "changes",
            "service": data.get("service", "unknown"),
            "summary": f"最近 {data.get('hours', 24)}h 无变更事件",
            "details": {"count": 0},
        }

    type_mark = {
        "deploy": "发版", "config_change": "配置变更",
        "scale": "扩缩容", "infra": "基础设施",
    }
    parts = []
    for c in changes[:3]:
        t = type_mark.get(c.get("type", ""), c.get("type", "变更"))
        parts.append(f"[{t}] {c.get('time', '?')} {c.get('description', '')[:60]}")

    return {
        "type": "changes",
        "service": data.get("service", "unknown"),
        "summary": f"最近 {data.get('hours', 24)}h {len(changes)} 条变更: " + "；".join(parts),
        "details": {"count": len(changes), "changes": changes[:5]},
    }


def _build_citations(retrieved_docs: List[Dict]) -> List[Dict]:
    """从检索结果构建引用列表（去重 + 按分数排序 + 重新编号）

    Args:
        retrieved_docs: 从 ToolMessage artifact 收集的检索结果

    Returns:
        清理后的引用列表，每项包含 index/doc_id/title/heading_path/score/source/image_path
    """
    if not retrieved_docs:
        return []

    # 按 doc_id + content 前 100 字符去重（多次 search_knowledge 调用可能有重复）
    # 保留 LLM 可见的原始 index（工具返回时已编号），不做 sort+reindex，
    # 否则 LLM 文本中的 [1] 与最终引用列表的 [1] 会错位。
    seen: set = set()
    citations = []
    for doc in retrieved_docs:
        dedup_key = (doc.get("doc_id", ""), doc.get("content", "")[:100])
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        citations.append({
            "index": doc.get("index", 0),
            "doc_id": doc.get("doc_id", ""),
            "title": doc.get("title", ""),
            "heading_path": doc.get("heading_path", ""),
            "score": doc.get("score", 0),
            "source": doc.get("source", "knowledge_base"),
            "image_path": doc.get("image_path"),
            "doc_type": doc.get("doc_type", ""),
            "service": doc.get("service", ""),
            # 知识时效：检索层对过期文档打了 _expired 标记（valid_until 已过）
            "expired": bool((doc.get("metadata") or {}).get("_expired")),
        })
    return citations


def _parse_diagnosis_report(content: str) -> Optional[Dict]:
    """从 LLM 的 Markdown 输出中解析结构化诊断报告

    按 ### 标题提取各段：现象/证据/根因分析/处置方案/置信度。
    鲁棒性设计：解析失败返回 None，调用方降级为纯文本展示。

    Returns:
        {
            "symptom": "...",          # 现象
            "evidence": "...",         # 证据
            "root_cause": "...",       # 根因分析
            "solution": "...",         # 处置方案
            "confidence": "...",       # 置信度原文
            "confidence_level": "high" # high/medium/low（解析失败为 unknown）
        } 或 None（无 root_cause 且无 solution 时视为非诊断回答）
    """

    if not content:
        return None

    # 匹配 ### 标题 + 内容（直到下一个 ### 或文末）
    pattern = r'###\s+(.+?)\s*\n(.*?)(?=\n###\s+|\Z)'
    sections: Dict[str, str] = {}
    for match in re.finditer(pattern, content, re.DOTALL):
        title = match.group(1).strip()
        body = match.group(2).strip()
        sections[title] = body

    # 中文标题 → 英文字段映射
    field_map = {
        "现象": "symptom",
        "证据": "evidence",
        "根因分析": "root_cause",
        "根因": "root_cause",
        "处置方案": "solution",
        "解决方案": "solution",
        "置信度": "confidence",
    }
    report: Dict[str, str] = {}
    for cn_title, en_field in field_map.items():
        if cn_title in sections and en_field not in report:
            report[en_field] = sections[cn_title]

    # 至少要有 root_cause 或 solution 才算有效诊断报告
    if not report.get("root_cause") and not report.get("solution"):
        return None

    # 解析置信度等级（取置信度文本前 10 字符判断）
    conf_text = report.get("confidence", "")
    conf_level = "unknown"
    if conf_text:
        head = conf_text[:10]
        if "高" in head:
            conf_level = "high"
        elif "中" in head:
            conf_level = "medium"
        elif "低" in head:
            conf_level = "low"
    report["confidence_level"] = conf_level

    return report


def _build_evidence_summary(monitoring_evidence: List[Dict], retrieved_docs: List[Dict]) -> str:
    """构建证据看板：聚合监控证据 + 知识库证据，标注完整性

    这是双源融合推理的核心——将两类证据结构化呈现在一个看板中，
    让 LLM 在每一步都能看到"已收集了什么、还缺什么"，而不是靠记忆对话历史。

    为什么不用 LLM 自己回忆：工具结果散落在 ToolMessage 文本中，
    LLM 无法可靠回顾"我查了什么"，结构化看板直接注入 prompt 解决这个问题。

    Returns:
        证据看板文本（注入 system prompt）
    """
    if not monitoring_evidence and not retrieved_docs:
        return ""  # 无证据时不注入看板

    lines = ["\n## 已收集证据看板（双源融合）"]

    # === 监控证据（现场实时）===
    if monitoring_evidence:
        lines.append("\n### 监控证据（现场实时）")
        for ev in monitoring_evidence:
            type_label = "指标" if ev.get("type") == "metrics" else "日志"
            service = ev.get("service", "unknown")
            summary = ev.get("summary", "")
            lines.append(f"- [{type_label}] {service}: {summary}")
    else:
        lines.append("\n### 监控证据（现场实时）\n- （尚未查询监控指标/日志）")

    # === 知识库证据（历史经验）===
    if retrieved_docs:
        lines.append("\n### 知识库证据（历史经验）")
        # 按 doc_type 分组展示
        doc_type_labels = {
            "manual": "服务手册",
            "incident": "历史事故",
            "sop": "处置预案",
            "postmortem": "事故复盘",
        }
        by_type: Dict[str, List[Dict]] = {}
        for doc in retrieved_docs:
            dt = doc.get("doc_type", "") or "other"
            by_type.setdefault(dt, []).append(doc)

        for dt, docs in by_type.items():
            label = doc_type_labels.get(dt, dt)
            for doc in docs:
                idx = doc.get("index", "?")
                title = doc.get("title", "未知")[:50]
                snippet = doc.get("content", "")[:60].replace("\n", " ")
                lines.append(f"- [{label}] {title} [{idx}]: {snippet}...")
    else:
        lines.append("\n### 知识库证据（历史经验）\n- （尚未检索知识库）")

    # === 证据完整性检查 ===
    lines.append("\n### 证据完整性检查")
    has_metrics = any(ev.get("type") == "metrics" for ev in monitoring_evidence)
    has_logs = any(ev.get("type") == "logs" for ev in monitoring_evidence)
    doc_types_collected = set()
    for doc in retrieved_docs:
        dt = doc.get("doc_type", "")
        if dt:
            doc_types_collected.add(dt)

    checks = [
        ("监控指标(metrics)", has_metrics),
        ("监控日志(logs)", has_logs),
        ("服务手册(manual)", "manual" in doc_types_collected),
        ("历史事故(incident)", "incident" in doc_types_collected),
    ]
    for name, done in checks:
        mark = "✓ 已查" if done else "✗ 未查"
        lines.append(f"- {name}: {mark}")

    return "\n".join(lines)


def _compute_evidence_sufficiency(
    monitoring_evidence: List[Dict],
    citations: List[Dict],
    tools_used: List[str],
    diagnosis_report: Optional[Dict],
    mcp_degraded: bool = False,
) -> Dict[str, Any]:
    """规则计算的"证据充分度"（校准 LLM 自报置信度的过度自信）

    与 LLM 置信度的关系：置信度是模型对"我的结论对不对"的主观自评，
    充分度是"这次诊断拿到的客观证据够不够"的规则化度量——两者正交，
    展示时取较低者作为建议采信级别。

    因子（满分 100）：
    - 监控取证 30（metrics/logs/alerts/chart 各 15，封顶 30）
    - 知识库命中 25（≥2 条引用 25，1 条 12）
    - 变更检查 15（get_recent_changes 已执行且有事件或明确查过）
    - 跨服务拓扑 10（get_service_dependencies 已执行）
    - 报告完整性 20（根因+方案 20，仅有其一 10）
    - 监控源降级 → 总分封顶 50（对应 prompt 层"置信度最高中"的数值化）
    """
    factors: Dict[str, Any] = {}
    score = 0

    # 1. 监控取证（按证据类型计数，封顶 30）
    mon_types = {e.get("type") for e in (monitoring_evidence or []) if e.get("type")}
    mon_score = min(30, 15 * len(mon_types))
    score += mon_score
    factors["monitoring"] = {"score": mon_score, "types": sorted(mon_types)}

    # 2. 知识库命中
    cite_count = len(citations or [])
    kb_score = 25 if cite_count >= 2 else (12 if cite_count == 1 else 0)
    score += kb_score
    factors["knowledge"] = {"score": kb_score, "citations": cite_count}

    # 3. 变更检查
    tools = set(tools_used or [])
    change_score = 15 if "get_recent_changes" in tools else 0
    score += change_score
    factors["change_check"] = {"score": change_score}

    # 4. 跨服务拓扑
    topo_score = 10 if "get_service_dependencies" in tools else 0
    score += topo_score
    factors["topology"] = {"score": topo_score}

    # 5. 报告完整性
    if diagnosis_report:
        has_root = bool(diagnosis_report.get("root_cause"))
        has_solution = bool(diagnosis_report.get("solution"))
        report_score = 20 if (has_root and has_solution) else (10 if (has_root or has_solution) else 0)
    else:
        report_score = 0
    score += report_score
    factors["report_complete"] = {"score": report_score}

    # 6. 监控源降级封顶
    capped = False
    if mcp_degraded:
        if score > 50:
            score = 50
            capped = True
    factors["mcp_degraded"] = {"capped": capped, "degraded": bool(mcp_degraded)}

    level = "high" if score >= 70 else ("medium" if score >= 40 else "low")
    return {"score": score, "level": level, "factors": factors}