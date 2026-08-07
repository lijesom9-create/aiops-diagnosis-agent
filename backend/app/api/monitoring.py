"""
监控数据查询 API（给前端智能运维看板用）

提供 HTTP 端点让前端查询 Prometheus/Loki/Alertmanager 数据：
- GET /api/monitoring/overview  系统指标概览（CPU/内存/磁盘/网络）
- GET /api/monitoring/query     PromQL 瞬时查询
- GET /api/monitoring/range     PromQL 范围查询（时序趋势）
- GET /api/monitoring/alerts    当前告警列表
- GET /api/monitoring/logs       日志查询（Loki）
- GET /api/monitoring/health    监控服务健康状态

与 MCP server 的关系：
- MCP server 通过 stdio 给 Agent 用（工具调用）
- 本模块通过 HTTP 给前端用（看板展示）
- 查询逻辑相同，数据源相同（Prometheus/Loki/Alertmanager）

认证：复用全局 JWT 依赖（需登录），不要求 admin（只读查询）
"""
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
import requests

from ..core.config import settings
from ..core.auth import get_current_user

router = APIRouter(prefix="/api/monitoring", tags=["监控数据"])


# ========== Prometheus 查询 ==========
def _prometheus_get(path: str, params: Dict) -> Dict:
    """调用 Prometheus HTTP API（统一错误处理）"""
    url = f"{settings.PROMETHEUS_URL}{path}"
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "success":
            return {"error": f"Prometheus 返回非成功状态: {data.get('errorType', '')} - {data.get('error', '')}"}
        return data.get("data", {})
    except requests.exceptions.ConnectionError:
        return {"error": f"无法连接 Prometheus ({settings.PROMETHEUS_URL})"}
    except Exception as e:
        return {"error": f"查询失败: {type(e).__name__}: {e}"}


@router.get("/overview")
async def system_overview(current_user: str = Depends(get_current_user)):
    """系统指标概览（CPU/内存/磁盘/网络/负载，一次返回全部）

    前端 Dashboard 页首屏调用，快速展示系统全貌。
    """
    queries = {
        "cpu_usage_percent": '100 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100',
        "cpu_load1": "node_load1",
        "cpu_load5": "node_load5",
        "cpu_cores": 'count(count(node_cpu_seconds_total{mode="idle"}) by (cpu))',
        "memory_total_bytes": "node_memory_MemTotal_bytes",
        "memory_available_bytes": "node_memory_MemAvailable_bytes",
        "memory_usage_percent": '(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100',
        "swap_usage_percent": '(1 - node_memory_SwapFree_bytes / node_memory_SwapTotal_bytes) * 100',
        "disk_root_usage_percent": '(1 - node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay"} / node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs|overlay"}) * 100',
        "network_rx_bytes_rate": 'rate(node_network_receive_bytes_total{device!~"lo|veth.*|docker.*|br-.*"}[5m])',
        "network_tx_bytes_rate": 'rate(node_network_transmit_bytes_total{device!~"lo|veth.*|docker.*|br-.*"}[5m])',
    }

    overview = {
        "prometheus_url": settings.PROMETHEUS_URL,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "metrics": {},
    }
    for name, promql in queries.items():
        data = _prometheus_get("/api/v1/query", {"query": promql})
        if "error" in data:
            overview["metrics"][name] = {"error": data["error"]}
            continue
        result = data.get("result", [])
        if len(result) == 1:
            val = result[0].get("value", [None, "0"])[1]
            overview["metrics"][name] = {"value": float(val) if val else 0.0}
        elif len(result) > 1:
            values = []
            for item in result:
                v = item.get("value", [None, "0"])[1]
                values.append({"labels": item.get("metric", {}), "value": float(v) if v else 0.0})
            overview["metrics"][name] = {"values": values}
        else:
            overview["metrics"][name] = {"value": None, "note": "无数据"}
    return overview


@router.get("/query")
async def prometheus_query(
    query: str = Query(..., description="PromQL 查询表达式"),
    current_user: str = Depends(get_current_user),
):
    """PromQL 瞬时查询（当前值）

    前端系统指标页用，支持任意 PromQL 查询。
    """
    data = _prometheus_get("/api/v1/query", {"query": query})
    if "error" in data:
        raise HTTPException(status_code=502, detail=data["error"])
    result = data.get("result", [])
    values = []
    for item in result:
        metric = item.get("metric", {})
        val = item.get("value", [None, "0"])[1]
        values.append({"labels": metric, "value": float(val) if val else 0.0})
    return {"query": query, "count": len(values), "values": values,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


@router.get("/range")
async def prometheus_range(
    query: str = Query(..., description="PromQL 查询表达式"),
    minutes: int = Query(30, ge=1, le=1440, description="查询时间范围（分钟）"),
    step: str = Query("60s", description="采样步长"),
    current_user: str = Depends(get_current_user),
):
    """PromQL 范围查询（时序趋势数据）

    前端用于绘制趋势图：CPU/内存随时间变化曲线。
    """
    end = datetime.now()
    start = end - timedelta(minutes=minutes)
    data = _prometheus_get("/api/v1/query_range", {
        "query": query,
        "start": start.timestamp(),
        "end": end.timestamp(),
        "step": step,
    })
    if "error" in data:
        raise HTTPException(status_code=502, detail=data["error"])
    result = data.get("result", [])
    series = []
    for item in result:
        metric = item.get("metric", {})
        points = item.get("values", [])
        formatted = [{"time": datetime.fromtimestamp(t).strftime("%H:%M:%S"),
                      "value": float(v) if v else 0.0} for t, v in points]
        series.append({"labels": metric, "points": formatted})
    return {"query": query, "range_minutes": minutes, "step": step,
            "series_count": len(series), "series": series}


# ========== Alertmanager 告警查询 ==========
@router.get("/alerts")
async def list_alerts(
    state: Optional[str] = Query(None, description="按状态过滤：firing/pending/suppressed"),
    current_user: str = Depends(get_current_user),
):
    """查询当前告警列表（来自 Alertmanager）

    前端告警管理页用，展示当前正在触发/待处理/已静默的告警。
    """
    url = f"{settings.ALERTMANAGER_URL}/api/v2/alerts"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        alerts = resp.json()
    except requests.exceptions.ConnectionError:
        raise HTTPException(status_code=502, detail=f"无法连接 Alertmanager ({settings.ALERTMANAGER_URL})")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"查询告警失败: {e}")

    formatted = []
    for alert in alerts:
        labels = alert.get("labels", {})
        annotations = alert.get("annotations", {})
        status_obj = alert.get("status", {})
        alert_state = status_obj.get("state", "unknown")

        # 按状态过滤
        if state and alert_state != state:
            continue

        starts_at = alert.get("startsAt", "")
        ends_at = alert.get("endsAt", "")
        try:
            if starts_at:
                dt = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
                starts_at = dt.strftime("%Y-%m-%d %H:%M:%S")
            if ends_at and ends_at != "0001-01-01T00:00:00Z":
                dt = datetime.fromisoformat(ends_at.replace("Z", "+00:00"))
                ends_at = dt.strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            pass

        formatted.append({
            "alertname": labels.get("alertname", "unknown"),
            "severity": labels.get("severity", "unknown"),
            "category": labels.get("category", ""),
            "instance": labels.get("instance", ""),
            "state": alert_state,
            "summary": annotations.get("summary", ""),
            "description": annotations.get("description", ""),
            "starts_at": starts_at,
            "ends_at": ends_at,
            "fingerprint": alert.get("fingerprint", ""),
        })

    # 按严重级别排序：critical > warning > info
    severity_order = {"critical": 0, "warning": 1, "info": 2}
    formatted.sort(key=lambda a: (severity_order.get(a["severity"], 3), a["alertname"]))

    return {
        "count": len(formatted),
        "alerts": formatted,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ========== Loki 日志查询 ==========
@router.get("/logs")
async def query_logs(
    container: Optional[str] = Query(None, description="容器名过滤，如 backend"),
    keyword: Optional[str] = Query(None, description="关键词过滤，如 ERROR"),
    minutes: int = Query(30, ge=1, le=1440, description="查询时间范围（分钟）"),
    limit: int = Query(100, ge=1, le=1000, description="返回日志行数上限"),
    current_user: str = Depends(get_current_user),
):
    """日志查询（Loki LogQL）

    前端日志查询页用，按容器名 + 关键词查日志。
    """
    # 构建 LogQL（容器名支持模糊匹配，如 backend 匹配 education-agent-backend-1）
    if container:
        logql = f'{{container=~".*{container}.*"}}'
    else:
        logql = '{container=~".+"}'
    if keyword:
        logql += f' |= "{keyword}"'

    end = datetime.now()
    start = end - timedelta(minutes=minutes)
    url = f"{settings.LOKI_URL}/loki/api/v1/query_range"
    try:
        resp = requests.get(url, params={
            "query": logql,
            "start": start.timestamp(),
            "end": end.timestamp(),
            "limit": limit,
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "success":
            raise HTTPException(status_code=502, detail=f"Loki 返回非成功状态: {data}")
    except requests.exceptions.ConnectionError:
        raise HTTPException(status_code=502, detail=f"无法连接 Loki ({settings.LOKI_URL})")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"日志查询失败: {e}")

    result = data.get("data", {}).get("result", [])
    lines = []
    for stream in result:
        labels = stream.get("stream", {})
        cont = labels.get("container", "")
        for ts, line in stream.get("values", []):
            try:
                ts_sec = float(ts) / 1e9
                ts_str = datetime.fromtimestamp(ts_sec).strftime("%H:%M:%S")
            except (ValueError, OSError):
                ts_str = ts[:8]
            lines.append({
                "time": ts_str,
                "container": cont,
                "content": line,
            })

    # Loki 返回的是逆序（最新在前），反转为正序（旧→新）
    lines.reverse()
    return {
        "query": logql,
        "count": len(lines),
        "lines": lines,
        "range_minutes": minutes,
    }


# ========== 监控服务健康状态 ==========
@router.get("/health")
async def monitoring_health(current_user: str = Depends(get_current_user)):
    """监控服务健康状态（前端用于展示各服务是否在线）

    检查 Prometheus/Loki/Alertmanager 是否可达。
    """
    services = [
        {"name": "Prometheus", "url": settings.PROMETHEUS_URL, "check_path": "/-/healthy"},
        {"name": "Loki", "url": settings.LOKI_URL, "check_path": "/ready"},
        {"name": "Alertmanager", "url": settings.ALERTMANAGER_URL, "check_path": "/-/healthy"},
    ]
    result = []
    for svc in services:
        check_url = f"{svc['url']}{svc['check_path']}"
        try:
            resp = requests.get(check_url, timeout=5)
            healthy = resp.status_code == 200
        except Exception:
            healthy = False
        result.append({
            "name": svc["name"],
            "url": svc["url"],
            "healthy": healthy,
        })
    return {"services": result, "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
