"""
Loki 日志查询 MCP Server（查真实容器日志）

通过 MCP 协议向 Agent 暴露 Loki 日志查询工具：
- query_logs: 按容器名 + 关键词查询日志（高层封装，Agent 首选）
- query_loki: 直接执行 LogQL 查询（底层工具，复杂场景用）

数据来源：Loki HTTP API（http://loki:3100/loki/api/v1/query_range）
适用于：AIOps 故障诊断的"日志取证"环节，查真实容器日志找根因。

与 prometheus_monitoring_server.py 的分工：
- prometheus: 查时序指标（CPU/内存/QPS/延迟）
- loki:        查日志文本（ERROR/异常堆栈/慢 SQL）

传输方式：stdio（本地子进程，由 LangGraphAgent 通过 langchain-mcp-adapters 加载）

环境变量：
    LOKI_URL: Loki 地址（默认 http://loki:3100）

用法（独立测试）：
    set LOKI_URL=http://localhost:3100
    python mcp_servers/loki_query_server.py
"""
import json
import os
from datetime import datetime, timedelta
from typing import Optional

import requests
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("LokiLogging")

# Loki 地址：优先环境变量，默认容器内访问名
LOKI_URL = os.environ.get("LOKI_URL", "http://loki:3100").rstrip("/")


def _query_loki_range(logql: str, minutes: int = 10, limit: int = 50) -> dict:
    """执行 LogQL 范围查询（Loki HTTP API）

    GET /loki/api/v1/query_range?query=&start=&end=&limit=

    Args:
        logql: LogQL 查询表达式，如 '{container="backend"} |= "ERROR"'
        minutes: 查询时间范围（分钟），默认 10
        limit: 返回日志行数上限，默认 50
    """
    try:
        end = datetime.now()
        start = end - timedelta(minutes=minutes)
        resp = requests.get(
            f"{LOKI_URL}/loki/api/v1/query_range",
            params={
                "query": logql,
                "start": start.timestamp(),
                "end": end.timestamp(),
                "limit": limit,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "success":
            return {"error": f"Loki 返回非成功状态: {data}"}

        result = data.get("data", {}).get("result", [])
        lines = []
        for stream in result:
            labels = stream.get("stream", {})
            container = labels.get("container", "")
            _filename = labels.get("filename", "")
            for ts, line in stream.get("values", []):
                # ts 是纳秒级字符串时间戳，转为可读格式
                try:
                    ts_sec = float(ts) / 1e9
                    ts_str = datetime.fromtimestamp(ts_sec).strftime("%H:%M:%S")
                except (ValueError, OSError):
                    ts_str = ts[:8]
                lines.append({
                    "timestamp": ts_str,
                    "container": container,
                    "labels": labels,
                    "line": line,
                })

        return {
            "query": logql,
            "range_minutes": minutes,
            "limit": limit,
            "count": len(lines),
            "logs": lines,
        }

    except requests.exceptions.ConnectionError:
        return {"error": f"无法连接 Loki ({LOKI_URL})，请确认服务已启动"}
    except Exception as e:
        return {"error": f"日志查询失败: {type(e).__name__}: {e}"}


def _build_logql(container: Optional[str], keyword: Optional[str]) -> str:
    """构建 LogQL 查询表达式

    Args:
        container: 容器名（可选，空则查所有容器）
        keyword: 关键词过滤（可选，空则返回全部日志）

    Returns:
        LogQL 字符串，如 '{container="backend"} |= "error"'
    """
    # 选择器：按 container label 过滤
    if container:
        # Loki 的 container label 是容器名（如 "education-agent-backend-1"）
        # 支持模糊匹配：用 =~ 正则
        if container in ("backend", "prometheus", "qdrant", "redis", "mongodb"):
            # 常见简称 → 正则匹配 docker compose 容器名
            selector = f'{{container=~".*{container}.*"}}'
        else:
            selector = f'{{container=~".*{container}.*"}}'
    else:
        # 查所有容器（排除 promtail/loki 自身的日志噪声）
        selector = '{container!~"loki|promtail"}'

    # 过滤器：关键词
    if keyword:
        # 转义双引号
        keyword_escaped = keyword.replace('"', '\\"')
        return f'{selector} |= "{keyword_escaped}"'
    return selector


@mcp.tool()
def query_logs(
    container: str = "",
    keyword: str = "",
    time_range: str = "10m",
    limit: int = 30,
) -> str:
    """查询容器日志，按关键词过滤（真实日志，来自 Loki）。

    运维诊断时用于检索错误日志、异常堆栈、慢 SQL，辅助根因定位。
    拿到日志后应结合 query_prometheus 的指标异常方向交叉验证。

    使用场景：
    - 查 ERROR 日志：keyword="ERROR"，定位异常堆栈
    - 查连接池日志：keyword="HikariPool" / keyword="connection pool"
    - 查超时日志：keyword="timeout" / keyword="timed out"
    - 查慢 SQL：keyword="slow_query" / keyword="slow query"
    - 查特定容器：container="backend" 只查后端日志

    Args:
        container: 容器名（可选，支持模糊匹配，如 "backend" 匹配 education-agent-backend-1）。
                   留空则查所有容器日志（自动排除 loki/promtail 自身日志）。
        keyword: 日志关键词过滤（可选，大小写敏感，直接匹配日志原文）。
                 留空则返回该容器全部日志（可能较多，注意 limit）。
        time_range: 时间范围，默认 "10m"。可选 "5m"/"10m"/"30m"/"1h"/"2h"。
        limit: 返回日志行数上限，默认 30。

    Returns:
        JSON 格式的日志列表，每条含 timestamp/container/line 字段
    """
    # 解析时间范围（支持 5m/10m/30m/1h/2h 格式）
    minutes = 10
    if time_range:
        if time_range.endswith("m"):
            minutes = int(time_range[:-1])
        elif time_range.endswith("h"):
            minutes = int(time_range[:-1]) * 60
        minutes = max(1, min(minutes, 120))  # 限制 1-120 分钟

    logql = _build_logql(container if container else None, keyword if keyword else None)
    result = _query_loki_range(logql, minutes=minutes, limit=limit)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def query_loki(logql: str, time_range: str = "10m", limit: int = 30) -> str:
    """直接执行 LogQL 查询（Loki 原生查询语言，高级用法）。

    适用于 query_logs 无法满足的复杂场景，如：
    - 正则过滤：'{container="backend"} |~ "ERROR|WARN"'
    - JSON 解析：'{container="backend"} | json | line_format "{{.msg}}"'
    - 排除关键词：'{container="backend"} != "healthcheck"'
    - 多标签组合：'{container="backend",level="error"} |= "HikariPool"'

    LogQL 语法参考：
    - 选择器：{label="value"} 或 {label=~"regex"}
    - 包含过滤：|= "keyword"
    - 正则过滤：|~ "regex"
    - 排除过滤：!= "keyword" 或 !~ "regex"
    - JSON 解析：| json
    - 格式化：| line_format "{{.field}}"

    Args:
        logql: LogQL 查询表达式（完整语法）
        time_range: 时间范围，默认 "10m"
        limit: 返回日志行数上限，默认 30

    Returns:
        JSON 格式的查询结果
    """
    minutes = 10
    if time_range:
        if time_range.endswith("m"):
            minutes = int(time_range[:-1])
        elif time_range.endswith("h"):
            minutes = int(time_range[:-1]) * 60
        minutes = max(1, min(minutes, 120))

    result = _query_loki_range(logql, minutes=minutes, limit=limit)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def list_containers() -> str:
    """列出 Loki 中有日志的容器名（帮助 Agent 选择 query_logs 的 container 参数）。

    Agent 诊断时如果不确定容器名，先调这个工具看有哪些容器有日志，
    再按需调 query_logs 查特定容器的日志。

    Returns:
        JSON 格式的容器名列表
    """
    try:
        # 查 container label 的所有值
        resp = requests.get(
            f"{LOKI_URL}/loki/api/v1/label/container/values",
            params={"start": (datetime.now() - timedelta(hours=1)).timestamp()},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "success":
            return json.dumps({"error": f"Loki 返回非成功状态: {data}"}, ensure_ascii=False)

        containers = data.get("data", [])
        return json.dumps({
            "containers": containers,
            "count": len(containers),
            "note": "query_logs 的 container 参数传这些值，或传 'backend' 等简称模糊匹配",
        }, ensure_ascii=False, indent=2)

    except requests.exceptions.ConnectionError:
        return json.dumps({"error": f"无法连接 Loki ({LOKI_URL})，请确认服务已启动"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"查询容器列表失败: {type(e).__name__}: {e}"}, ensure_ascii=False)


if __name__ == "__main__":
    # stdio 传输：由 langchain-mcp-adapters 通过子进程拉起
    mcp.run(transport="stdio")
