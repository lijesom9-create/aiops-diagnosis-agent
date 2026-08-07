"""
Alertmanager 告警查询 MCP Server（查真实活跃告警）

通过 MCP 协议向 Agent 暴露 Alertmanager 告警查询工具：
- query_alerts: 查询当前活跃告警（Agent 首选，高层封装）
- query_alertmanager: 直接查询 Alertmanager API（底层工具，复杂场景用）
- query_silences: 查询告警静默规则（哪些告警被人为屏蔽了）

数据来源：Alertmanager HTTP API（http://alertmanager:9093/api/v2/alerts）
适用于：AIOps 故障诊断的"告警确认"环节，让 Agent 知道当前什么告警在响。

与 prometheus/loki 的分工：
- prometheus: 查时序指标（CPU/内存/QPS/延迟）—— 量化数据
- loki:        查日志文本（ERROR/异常堆栈/慢 SQL）—— 文本证据
- alertmanager: 查告警状态（什么告警在响、何时触发、严重级别）—— 事件通知

诊断闭环：
  告警触发 → Agent 查告警确认故障 → 查指标定位异常 → 查日志找根因 → 给处置方案

传输方式：stdio（本地子进程，由 LangGraphAgent 通过 langchain-mcp-adapters 加载）

环境变量：
    ALERTMANAGER_URL: Alertmanager 地址（默认 http://alertmanager:9093）

用法（独立测试）：
    set ALERTMANAGER_URL=http://localhost:9093
    python mcp_servers/alertmanager_query_server.py
"""
import json
import os
from datetime import datetime
from typing import Optional

import requests
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Alertmanager")

# Alertmanager 地址：优先环境变量，默认容器内访问名
ALERTMANAGER_URL = os.environ.get("ALERTMANAGER_URL", "http://alertmanager:9093").rstrip("/")


def _format_alert(alert: dict) -> dict:
    """格式化单个告警为 Agent 友好的结构

    Alertmanager API v2 返回的告警格式：
    {
        "labels": {"alertname":"HighCpuUsage","severity":"warning",...},
        "annotations": {"summary":"...","description":"..."},
        "startsAt": "2026-08-07T02:14:00Z",
        "endsAt": "2026-08-07T02:20:00Z",
        "status": {"state":"firing","silencedBy":[],"inhibitedBy":[]},
        "fingerprint": "abc123..."
    }
    """
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    status = alert.get("status", {})

    # 时间格式化（ISO 8601 → 可读格式）
    starts_at = alert.get("startsAt", "")
    ends_at = alert.get("endsAt", "")
    try:
        if starts_at:
            dt = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
            starts_at = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        if ends_at and ends_at != "0001-01-01T00:00:00Z":
            dt = datetime.fromisoformat(ends_at.replace("Z", "+00:00"))
            ends_at = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, TypeError):
        pass

    return {
        "alert_name": labels.get("alertname", "unknown"),
        "severity": labels.get("severity", "unknown"),
        "category": labels.get("category", ""),
        "state": status.get("state", "unknown"),
        "summary": annotations.get("summary", ""),
        "description": annotations.get("description", ""),
        "starts_at": starts_at,
        "ends_at": ends_at,
        "labels": labels,
        "fingerprint": alert.get("fingerprint", ""),
    }


@mcp.tool()
def query_alerts(severity: str = "", state: str = "firing") -> str:
    """查询当前活跃告警（真实数据，来自 Alertmanager）。

    运维诊断时首先调这个工具了解"当前什么告警在响"，
    再按告警类别调 query_prometheus 查指标、query_logs 查日志深入分析。

    使用场景：
    - 查所有活跃告警：不传参数
    - 查 critical 告警：severity="critical"
    - 查 warning 告警：severity="warning"
    - 查已恢复告警：state="resolved"
    - 查所有状态告警：state="all"

    Args:
        severity: 按严重级别过滤，可选 "critical"/"warning"/""（空=不过滤）
        state: 按状态过滤，默认 "firing"（告警中）。
               可选 "firing"/"resolved"/"all"。

    Returns:
        JSON 格式的告警列表，每条含 alert_name/severity/summary/starts_at 等字段
    """
    try:
        # Alertmanager API v2: GET /api/v2/alerts
        # 参数：
        #   active=true: 只返回活跃告警（state=firing）
        #   silenced=false: 不返回静默告警
        #   inhibited=false: 不返回被抑制告警
        params = {"silenced": "false", "inhibited": "false"}
        if state == "firing":
            params["active"] = "true"
        elif state == "resolved":
            # Alertmanager API 不直接支持查 resolved，需查全部再过滤
            params["active"] = "false"
        else:
            # state="all" 查全部
            pass

        resp = requests.get(
            f"{ALERTMANAGER_URL}/api/v2/alerts",
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        alerts_raw = resp.json()

        # 格式化 + 过滤
        alerts = [_format_alert(a) for a in alerts_raw]

        # 按 severity 过滤
        if severity:
            alerts = [a for a in alerts if a.get("severity") == severity]

        # 按 state 过滤（API 层面已部分过滤，这里再精确过滤一次）
        if state != "all":
            alerts = [a for a in alerts if a.get("state") == state]

        # 按 severity 排序（critical > warning > 其他）
        severity_order = {"critical": 0, "warning": 1}
        alerts.sort(key=lambda a: severity_order.get(a.get("severity", ""), 99))

        return json.dumps({
            "count": len(alerts),
            "state_filter": state,
            "severity_filter": severity or "all",
            "alerts": alerts,
            "note": "按 severity 排序（critical 优先）；无告警时 count=0",
        }, ensure_ascii=False, indent=2)

    except requests.exceptions.ConnectionError:
        return json.dumps(
            {"error": f"无法连接 Alertmanager ({ALERTMANAGER_URL})，请确认服务已启动"},
            ensure_ascii=False
        )
    except Exception as e:
        return json.dumps(
            {"error": f"告警查询失败: {type(e).__name__}: {e}"},
            ensure_ascii=False
        )


@mcp.tool()
def query_alertmanager(endpoint: str = "alerts", params: str = "") -> str:
    """直接查询 Alertmanager API v2（底层工具，高级用法）。

    适用于 query_alerts 无法满足的场景，如：
    - 查询告警接收器状态：endpoint="receivers"
    - 查询告警静默规则：endpoint="silences"
    - 查询 Alertmanager 集群状态：endpoint="status"
    - 带复杂参数查询：params="active=true&silenced=true"

    Alertmanager API v2 端点：
    - alerts: 活跃告警列表
    - alerts/groups: 按组聚合的告警
    - silences: 静默规则列表
    - receivers: 接收器列表
    - status: Alertmanager 状态信息

    Args:
        endpoint: API 端点，默认 "alerts"。可选 "alerts"/"alerts/groups"/
                  "silences"/"receivers"/"status"
        params: 查询参数字符串，如 "active=true&silenced=false"（可选）

    Returns:
        JSON 格式的原始 API 响应
    """
    try:
        url = f"{ALERTMANAGER_URL}/api/v2/{endpoint.lstrip('/')}"
        query_params = {}
        if params:
            for pair in params.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    query_params[k] = v

        resp = requests.get(url, params=query_params, timeout=10)
        resp.raise_for_status()
        return json.dumps({
            "endpoint": endpoint,
            "params": params,
            "data": resp.json(),
        }, ensure_ascii=False, indent=2)

    except requests.exceptions.ConnectionError:
        return json.dumps(
            {"error": f"无法连接 Alertmanager ({ALERTMANAGER_URL})，请确认服务已启动"},
            ensure_ascii=False
        )
    except Exception as e:
        return json.dumps(
            {"error": f"查询失败: {type(e).__name__}: {e}"},
            ensure_ascii=False
        )


@mcp.tool()
def query_silences() -> str:
    """查询告警静默规则（哪些告警被人为屏蔽了）。

    静默规则用于临时屏蔽特定告警（如维护期间屏蔽 CPU 告警）。
    诊断时如果发现某个告警"应该响但没响"，可能是被静默了，
    调这个工具确认静默规则列表。

    Returns:
        JSON 格式的静默规则列表
    """
    try:
        resp = requests.get(
            f"{ALERTMANAGER_URL}/api/v2/silences",
            timeout=10,
        )
        resp.raise_for_status()
        silences = resp.json()

        # 格式化静默规则
        formatted = []
        for s in silences:
            formatted.append({
                "id": s.get("id", ""),
                "matchers": s.get("matchers", []),
                "starts_at": s.get("startsAt", ""),
                "ends_at": s.get("endsAt", ""),
                "created_by": s.get("createdBy", ""),
                "comment": s.get("comment", ""),
                "status": s.get("status", {}).get("state", "unknown"),
            })

        return json.dumps({
            "count": len(formatted),
            "silences": formatted,
            "note": "静默规则用于临时屏蔽告警；count=0 表示无静默规则",
        }, ensure_ascii=False, indent=2)

    except requests.exceptions.ConnectionError:
        return json.dumps(
            {"error": f"无法连接 Alertmanager ({ALERTMANAGER_URL})，请确认服务已启动"},
            ensure_ascii=False
        )
    except Exception as e:
        return json.dumps(
            {"error": f"静默规则查询失败: {type(e).__name__}: {e}"},
            ensure_ascii=False
        )


if __name__ == "__main__":
    # stdio 传输：由 langchain-mcp-adapters 通过子进程拉起
    mcp.run(transport="stdio")
