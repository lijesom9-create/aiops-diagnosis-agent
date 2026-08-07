"""
运维监控 MCP Server（模拟 Prometheus + Loki）

通过 MCP 协议向 Agent 暴露两个监控查询工具：
- query_metrics: 查询 Prometheus 指标（错误率/连接池/QPS/延迟）
- query_logs: 查询 Loki 日志（错误日志/慢SQL/异常堆栈）

数据为模拟数据，与知识库 incident 文档（INC-2026-001/005/008）对应，
用于演示"Agent 查监控 → 发现连接池耗尽 → 查知识库历史事故 → 给方案"全链路。

传输方式：stdio（本地子进程，由 LangGraphAgent 通过 langchain-mcp-adapters 加载）

用法（独立测试）：
    python mcp_servers/ops_monitoring_server.py
    # 或由 agent 通过 stdio 自动拉起
"""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("OpsMonitoring")


# ========== 模拟指标数据（与 incident 文档对应） ==========
# 故障期 payment-service 指标（INC-2026-001 数据）
_METRICS_FAULT = {
    "payment-service": {
        "error_rate": {
            "value": 0.38,
            "unit": "ratio",
            "baseline": 0.001,
            "description": "HTTP 500 错误率 = 500请求/总请求，基线 0.1%，当前 38%",
            "promql": 'rate(http_requests_total{service="payment-service",code="500"}[5m]) / rate(http_requests_total{service="payment-service"}[5m])',
        },
        "connection_pool_usage": {
            "value": 1.0,
            "unit": "ratio",
            "max_size": 20,
            "active": 20,
            "description": "HikariCP 连接池使用率 = active/max，当前 20/20 = 100%（已打满）",
            "promql": 'hikaricp_connections_active{pool="HikariPool-1"} / hikaricp_connections_max{pool="HikariPool-1"}',
        },
        "pending_connections": {
            "value": 87,
            "unit": "count",
            "description": "等待获取连接的请求数，正常应为 0，当前 87 个请求堆积",
            "promql": 'hikaricp_connections_pending{pool="HikariPool-1"}',
        },
        "qps": {
            "value": 4200,
            "unit": "req/s",
            "baseline": 1200,
            "description": "当前 QPS 4200，基线 1200，营销活动带来 3.5 倍流量",
            "promql": 'rate(http_requests_total{service="payment-service"}[5m])',
        },
        "latency_p99": {
            "value": 30500,
            "unit": "ms",
            "baseline": 180,
            "description": "P99 延迟 30.5s（基线 180ms），因连接获取超时 30s 导致",
            "promql": 'histogram_quantile(0.99, rate(http_request_duration_seconds_bucket{service="payment-service"}[5m])) * 1000',
        },
    },
    # 其他服务返回正常基线（用于对比，排除下游服务问题）
    "order-service": {
        "error_rate": {"value": 0.0012, "unit": "ratio", "baseline": 0.001, "description": "正常基线 0.12%"},
        "connection_pool_usage": {"value": 0.42, "unit": "ratio", "description": "连接池使用率 42%（正常）"},
        "qps": {"value": 1800, "unit": "req/s", "baseline": 1500, "description": "QPS 正常"},
        "latency_p99": {"value": 210, "unit": "ms", "baseline": 200, "description": "P99 延迟正常"},
    },
    "mysql": {
        "connection_pool_usage": {"value": 0.95, "unit": "ratio", "description": "MySQL 连接数 95%（偏高，与 payment-service 连接泄漏相关）"},
        "slow_queries": {"value": 14, "unit": "count", "description": "慢查询数 14（>1s），正常应 <3，存在 1 条 8.7s 慢 SQL"},
    },
}

# 正常状态指标（非故障服务或非故障时段）
_METRICS_NORMAL = {
    "payment-service": {
        "error_rate": {"value": 0.001, "unit": "ratio", "baseline": 0.001, "description": "正常基线 0.1%"},
        "connection_pool_usage": {"value": 0.35, "unit": "ratio", "description": "连接池使用率 35%（正常）"},
        "qps": {"value": 1200, "unit": "req/s", "baseline": 1200, "description": "QPS 正常"},
        "latency_p99": {"value": 180, "unit": "ms", "baseline": 180, "description": "P99 延迟正常"},
    },
}


# ========== 模拟日志数据（与 incident 文档对应） ==========
_LOGS_FAULT = {
    "payment-service": {
        "error": [
            "2026-01-15 10:20:12 ERROR [payment-service] HikariPool-1 - Connection is not available, request timed out after 30000ms",
            "2026-01-15 10:20:13 ERROR [payment-service] c.e.p.s.PayServiceImpl - createPayment failed, msg=Could not get JDBC connection",
            "2026-01-15 10:20:14 ERROR [payment-service] c.e.p.s.PayServiceImpl - queryPayment failed, msg=Could not get JDBC connection",
            "2026-01-15 10:20:15 ERROR [payment-service] c.e.p.s.RefundServiceImpl - applyRefund failed, msg=Could not get JDBC connection",
            "2026-01-15 10:20:16 WARN  [payment-service] o.h.e.j.spi.SqlExceptionHelper - SQL Error: 0, SQLState: null",
        ],
        "HikariPool": [
            "2026-01-15 10:20:12 ERROR [payment-service] HikariPool-1 - Connection is not available, request timed out after 30000ms",
            "2026-01-15 10:20:10 WARN  [payment-service] HikariPool-1 - Pool stats (total=20, active=20, idle=0, waiting=87)",
            "2026-01-15 10:20:11 WARN  [payment-service] HikariPool-1 - Pool stats (total=20, active=20, idle=0, waiting=124)",
        ],
        "slow_query": [
            "2026-01-15 10:19:55 WARN  [mysql] slow_query (3200ms): SELECT * FROM payment_order WHERE user_id=8821 AND status IN ('PAID','PENDING') ORDER BY id DESC LIMIT 50",
            "2026-01-15 10:20:05 WARN  [mysql] slow_query (8700ms): SELECT * FROM refund_order WHERE status='PROCESSING' AND created_at > '2026-01-01' ORDER BY id DESC LIMIT 100",
            "2026-01-15 10:20:08 WARN  [mysql] slow_query (5400ms): SELECT COUNT(*) FROM payment_order WHERE created_at > '2026-01-14' AND status='PENDING'",
        ],
        "timeout": [
            "2026-01-15 10:20:12 ERROR [payment-service] HikariPool-1 - Connection is not available, request timed out after 30000ms",
            "2026-01-15 10:20:20 ERROR [payment-service] o.s.t.i.TransactionInterceptor - Transaction timed out: applyRefund",
        ],
    },
}


@mcp.tool()
def query_metrics(service: str, metric: str = "all", time_range: str = "5m") -> str:
    """查询 Prometheus 监控指标

    运维诊断时用于获取服务的实时/历史监控指标，辅助根因定位。

    支持的 metric 类型：
    - error_rate: HTTP 500 错误率（故障诊断首要指标）
    - connection_pool_usage: 数据库连接池使用率（连接池耗尽诊断）
    - pending_connections: 等待获取连接的请求数
    - qps: 每秒请求数（流量评估）
    - latency_p99: P99 延迟（性能指标）
    - slow_queries: 慢查询数（仅 mysql 服务）
    - all: 返回该服务所有指标（推荐诊断时用 all）

    诊断建议：
    - 故障诊断先查 error_rate 确认故障范围
    - 再查 connection_pool_usage + pending_connections 判断是否连接池耗尽
    - 对比 qps 与 baseline 判断是否有流量突增
    - 查 mysql 的 slow_queries 判断是否有慢 SQL 拖垮连接池

    Args:
        service: 服务名，如 payment-service / order-service / mysql
        metric: 指标名（error_rate/connection_pool_usage/pending_connections/qps/latency_p99/slow_queries/all）
        time_range: 时间范围（如 5m/10m/1h），默认 5m

    Returns:
        str: 指标查询结果（JSON 格式，含 value/baseline/promql 等）
    """
    import json

    # payment-service 当前处于故障期，返回故障数据；其他服务返回正常/对应数据
    svc_metrics = _METRICS_FAULT.get(service) or _METRICS_NORMAL.get(service)
    if not svc_metrics:
        return json.dumps({
            "service": service,
            "error": f"未找到服务 {service} 的监控数据，支持的服务: {list(_METRICS_FAULT.keys())}",
        }, ensure_ascii=False)

    if metric == "all":
        return json.dumps({
            "service": service,
            "time_range": time_range,
            "metrics": svc_metrics,
        }, ensure_ascii=False, indent=2)

    if metric not in svc_metrics:
        return json.dumps({
            "service": service,
            "error": f"未找到指标 {metric}，支持的指标: {list(svc_metrics.keys())}",
        }, ensure_ascii=False)

    return json.dumps({
        "service": service,
        "metric": metric,
        "time_range": time_range,
        **svc_metrics[metric],
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def query_logs(service: str, keyword: str = "error", time_range: str = "10m", limit: int = 20) -> str:
    """查询 Loki 应用日志

    运维诊断时用于检索错误日志、慢查询日志、异常堆栈，辅助根因定位。

    支持的 keyword 类型：
    - error: 错误级别日志（ERROR/异常堆栈）
    - HikariPool: 数据库连接池相关日志（连接获取失败/连接池打满）
    - slow_query: 慢查询日志（SQL 执行慢，连接占用久）
    - timeout: 超时相关日志
    - all: 返回所有类型日志

    诊断建议：
    - 连接池耗尽诊断：查 HikariPool 关键词，看 "Connection is not available" / "Pool stats ... waiting=N"
    - 慢 SQL 诊断：查 slow_query 关键词，看慢 SQL 的执行时长和 SQL 文本
    - 故障范围确认：查 error 关键词，看哪些接口报错

    Args:
        service: 服务名，如 payment-service / mysql
        keyword: 日志关键词过滤（error/HikariPool/slow_query/timeout/all）
        time_range: 时间范围（如 10m/30m/1h），默认 10m
        limit: 返回日志条数上限，默认 20

    Returns:
        str: 日志查询结果（每行一条日志，含时间戳/级别/服务/内容）
    """
    import json

    svc_logs = _LOGS_FAULT.get(service, {})
    if not svc_logs:
        return json.dumps({
            "service": service,
            "keyword": keyword,
            "logs": [],
            "message": f"服务 {service} 在 {time_range} 内无匹配日志（可能正常或未采集）",
        }, ensure_ascii=False)

    if keyword == "all":
        # 合并所有类型的日志，按时间排序去重
        seen = set()
        all_logs = []
        for kw_logs in svc_logs.values():
            for line in kw_logs:
                if line not in seen:
                    seen.add(line)
                    all_logs.append(line)
        all_logs.sort()
        return json.dumps({
            "service": service,
            "keyword": "all",
            "time_range": time_range,
            "count": len(all_logs[:limit]),
            "logs": all_logs[:limit],
        }, ensure_ascii=False, indent=2)

    if keyword not in svc_logs:
        return json.dumps({
            "service": service,
            "keyword": keyword,
            "logs": [],
            "message": f"未找到关键词 {keyword} 的日志，支持的关键词: {list(svc_logs.keys())}",
        }, ensure_ascii=False)

    logs = svc_logs[keyword][:limit]
    return json.dumps({
        "service": service,
        "keyword": keyword,
        "time_range": time_range,
        "count": len(logs),
        "logs": logs,
    }, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    # stdio 传输：由 langchain-mcp-adapters 通过子进程拉起
    mcp.run(transport="stdio")
