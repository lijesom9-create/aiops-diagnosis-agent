"""
Prometheus 监控 MCP Server（查真实时序指标）

通过 MCP 协议向 Agent 暴露 Prometheus 查询工具：
- query_prometheus: 执行 PromQL 查询（瞬时值）
- query_prometheus_range: 执行 PromQL 范围查询（时序数据）
- query_system_overview: 预置的系统指标概览（CPU/内存/磁盘/网络，封装常用 PromQL）

数据来源：Prometheus HTTP API（http://localhost:9090/api/v1/query）
适用于：接入真实运维监控，Agent 查 PromQL 获取时序指标做诊断。

与 system_monitoring_server.py 的区别：
- system_monitoring: psutil 查"此刻"瞬时值，无历史
- prometheus:    查时序库，能看"过去 N 分钟"趋势、同比环比

传输方式：stdio（本地子进程，由 LangGraphAgent 通过 langchain-mcp-adapters 加载）

环境变量：
    PROMETHEUS_URL: Prometheus 地址（默认 http://localhost:9090）

用法（独立测试）：
    set PROMETHEUS_URL=http://localhost:9090
    python mcp_servers/prometheus_monitoring_server.py
"""
import json
import os
from datetime import datetime, timedelta

import requests
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("PrometheusMonitoring")

# Prometheus 地址：优先环境变量，默认本地
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090").rstrip("/")


def _query_instant(promql: str) -> dict:
    """执行 PromQL 瞬时查询（当前值）

    Prometheus API: /api/v1/query?query=<PromQL>
    返回当前时刻各时间序列的值。
    """
    try:
        resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": promql},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "success":
            return {"error": f"Prometheus 返回非成功状态: {data}"}

        result = data.get("data", {}).get("result", [])
        # 格式化结果：提取 metric labels + value
        values = []
        for item in result:
            metric = item.get("metric", {})
            val = item.get("value", [None, "0"])[1]
            values.append({"labels": metric, "value": float(val) if val else 0.0})
        return {"query": promql, "count": len(values), "values": values,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    except requests.exceptions.ConnectionError:
        return {"error": f"无法连接 Prometheus ({PROMETHEUS_URL}),请确认服务已启动"}
    except Exception as e:
        return {"error": f"查询失败: {type(e).__name__}: {e}"}


def _query_range(promql: str, minutes: int = 30, step: str = "60s") -> dict:
    """执行 PromQL 范围查询（时序数据）

    Prometheus API: /api/v1/query_range?query=&start=&end=&step=
    返回指定时间范围内的时序数据点。
    """
    try:
        end = datetime.now()
        start = end - timedelta(minutes=minutes)
        resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query_range",
            params={
                "query": promql,
                "start": start.timestamp(),
                "end": end.timestamp(),
                "step": step,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "success":
            return {"error": f"Prometheus 返回非成功状态: {data}"}

        result = data.get("data", {}).get("result", [])
        series = []
        for item in result:
            metric = item.get("metric", {})
            points = item.get("values", [])
            # 只取值，时间戳转为可读格式
            formatted = [{"time": datetime.fromtimestamp(t).strftime("%H:%M:%S"),
                          "value": float(v) if v else 0.0} for t, v in points]
            series.append({"labels": metric, "points": formatted})
        return {"query": promql, "range_minutes": minutes, "step": step,
                "series_count": len(series), "series": series}
    except requests.exceptions.ConnectionError:
        return {"error": f"无法连接 Prometheus ({PROMETHEUS_URL}),请确认服务已启动"}
    except Exception as e:
        return {"error": f"范围查询失败: {type(e).__name__}: {e}"}


@mcp.tool()
def query_prometheus(query: str) -> str:
    """执行 PromQL 查询，获取当前时刻的监控指标值（真实时序数据，来自 Prometheus）。

    用于让 Agent 查询任意 Prometheus 指标。常用 PromQL 示例：
    - CPU 使用率: 100 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100
    - 内存使用率: (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100
    - 磁盘使用率: (1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}) * 100
    - 网络入流量: rate(node_network_receive_bytes_total{device="eth0"}[5m])
    - 负载: node_load1
    - 可用内存: node_memory_MemAvailable_bytes

    Args:
        query: PromQL 查询表达式

    Returns:
        JSON 格式的查询结果，含各时间序列的 labels 和 value
    """
    result = _query_instant(query)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def query_prometheus_range(query: str, minutes: int = 30) -> str:
    """执行 PromQL 范围查询，获取过去 N 分钟的时序数据（趋势分析）。

    用于趋势分析：看指标在过去 N 分钟的变化趋势，判断是突增/渐增/稳定。
    例如：查过去 30 分钟 CPU 使用率趋势，判断是否持续飙升。

    Args:
        query: PromQL 查询表达式
        minutes: 查询时间范围（分钟），默认 30

    Returns:
        JSON 格式的时序数据，含各序列的数据点列表
    """
    result = _query_range(query, minutes=minutes)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def query_system_overview() -> str:
    """查询系统监控指标概览（预置常用 PromQL，一次返回全部）。

    封装了 CPU/内存/磁盘/网络/负载 5 类核心指标的当前值，
    Agent 首次诊断时调这个工具快速了解系统全貌，再按异常方向深入查询。

    Returns:
        JSON 格式的系统指标概览
    """
    # 预置常用 PromQL（node_exporter 暴露的指标）
    queries = {
        "cpu_usage_percent": '100 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100',
        "cpu_load1": "node_load1",
        "cpu_load5": "node_load5",
        "cpu_cores": "count(count(node_cpu_seconds_total{mode=\"idle\"}) by (cpu))",
        "memory_total_bytes": "node_memory_MemTotal_bytes",
        "memory_available_bytes": "node_memory_MemAvailable_bytes",
        "memory_usage_percent": '(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100',
        "swap_usage_percent": '(1 - node_memory_SwapFree_bytes / node_memory_SwapTotal_bytes) * 100',
        "disk_root_usage_percent": '(1 - node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay"} / node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs|overlay"}) * 100',
        "network_rx_bytes_rate": 'rate(node_network_receive_bytes_total{device!~"lo|veth.*|docker.*|br-.*"}[5m])',
        "network_tx_bytes_rate": 'rate(node_network_transmit_bytes_total{device!~"lo|veth.*|docker.*|br-.*"}[5m])',
        "filesystem_full_percent": '(1 - node_filesystem_avail_bytes / node_filesystem_size_bytes) * 100',
    }

    overview = {"prometheus_url": PROMETHEUS_URL,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "metrics": {}}
    for name, promql in queries.items():
        result = _query_instant(promql)
        if "error" in result:
            overview["metrics"][name] = {"error": result["error"]}
        else:
            values = result.get("values", [])
            if len(values) == 1:
                # 单值指标
                overview["metrics"][name] = {"value": values[0]["value"],
                                              "labels": values[0]["labels"]}
            elif len(values) > 1:
                # 多值指标（如各 CPU 核、各网卡）
                overview["metrics"][name] = {"values": values}
            else:
                overview["metrics"][name] = {"value": None, "note": "无数据"}

    return json.dumps(overview, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    # stdio 传输：由 langchain-mcp-adapters 通过子进程拉起
    mcp.run(transport="stdio")
