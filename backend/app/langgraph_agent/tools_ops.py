"""运维诊断 mock 工具与评测场景开关（T9 从 tools.py 拆出）。

包含 AIOps 诊断链路的 6 个 mock 工具（query_metrics / query_logs / analyze_chart /
get_recent_changes / create_incident_ticket / get_service_dependencies），
以及开放场景评测（agent_eval）的场景差异化 mock 数据与开关。

注意：评测开关不用 ContextVar——工具是同步函数，LangGraph 的 ToolNode 会在
独立线程里执行，ContextVar 跨线程不保证传播；改用评测进程级全局开关。
"""

from typing import Dict, Optional

from langchain_core.tools import tool

# ================= 开放场景评测 Mock：按场景差异化监控信号 =================
# 修"测评污染①"：默认 query_metrics/query_logs/get_recent_changes 对所有 service /
# 场景返回同一套"连接池耗尽 + v2.3.1 发版"固定信号，导致 SC-OPEN-* 场景的 Agent 全被
# 锚定在连接池根因，测不出区分性组合推理。agent_eval 逐场景调用 set_eval_scenario(id)，
# 本组工具读取该开关返回该场景专属信号；未设置（日常对话/真实诊断链路）时保持原 mock。
#
# 注意：不用 ContextVar。工具是同步函数，LangGraph 的 ToolNode 会在独立线程里执行，
# ContextVar 跨线程不保证传播。改用评测进程级全局开关——agent_eval 是单进程串行跑
# 场景，且真实诊断链路从不调用 set_eval_scenario()，因此该开关不会污染生产路径。
_eval_scenario_override: Optional[str] = None


def set_eval_scenario(scenario_id: Optional[str]) -> None:
    """设置/清除评测场景开关（仅供 agent_eval 调用）。"""
    global _eval_scenario_override
    _eval_scenario_override = scenario_id


def _eval_scenario() -> Optional[str]:
    return _eval_scenario_override


# 各开放场景的差异化信号（key = 场景 ID）：
#   metrics：现场应呈现的关键指标（value/baseline/unit/status，query_metrics 拼接）
#   logs：    keyword -> 该场景定向日志（Agent 会按需传 keyword 查证）
#   changes：故障前附近的变更事件（get_recent_changes 返回）
_EVAL_SCENARIO_MOCKS: Dict[str, dict] = {
    "SC-OPEN-001": {  # 对账定时任务凌晨抢连接：池高占用但未耗尽、业务高峰前偶发卡顿
        "title": "对账定时任务抢占连接",
        "metrics": {
            "connection_pool_usage": {"value": 0.82, "baseline": 0.4, "unit": "%", "status": "warning"},
            "pending_connections": {"value": 12, "baseline": 2, "unit": "count", "status": "warning"},
            "checked_out_rows": {"value": 9, "baseline": 3, "unit": "count", "status": "warning"},
            "batch_job_active": {"value": 1, "baseline": 0, "unit": "count", "status": "info"},
            "error_rate": {"value": 0.003, "baseline": 0.005, "unit": "%", "status": "normal"},
            "p99_latency": {"value": 452, "baseline": 120, "unit": "ms", "status": "warning"},
        },
        "logs": {
            "checkout": [
                "[WARN] mysql - checkout blocked 12px 未达池上限（max=20）",
                "[INFO] payment-service - payout 对账子任务占用 6 个 DB 连接",
            ],
            "batch": [
                "[INFO] payout-reconcile - 凌晨 02:00 对账任务启动，预取 6 个 DB 连接",
                "[WARN] payout-reconcile - UPDATE reconcile_status 持锁行进中",
            ],
            "timeout": ["[INFO] payment-service - 无 SQLTransientConnectionException，连接均按时获取"],
        },
        "changes": [{"change_id": "CHG-2026-0915", "type": "schedule", "service": "payment-service",
                     "time": "2026-09-02T00:00:00Z",
                     "description": "新增每日凌晨定时对账任务（重事务，持锁长）"}],
    },
    "SC-OPEN-002": {  # 支付结果回调消费组反复重平衡
        "title": "支付结果回调消费组反复重平衡",
        "metrics": {
            "consumer_lag": {"value": 48200, "baseline": 800, "unit": "msg", "status": "critical"},
            "rebalance_events": {"value": 7, "baseline": 0, "unit": "count", "status": "warning"},
            "coordinator_rebalance": {"value": 1, "baseline": 0, "unit": "count", "status": "warning"},
            "callback_success_rate": {"value": 0.64, "baseline": 1.0, "unit": "%", "status": "warning"},
            "error_rate": {"value": 0.09, "baseline": 0.01, "unit": "%", "status": "warning"},
        },
        "logs": {
            "rebalance": [
                "[WARN] payment-callback-consumer - ConsumerRebalanceStarted: JoinGroup",
                "[WARN] payment-callback-consumer - Stop consuming during rebalance",
            ],
            "lag": ["[WARN] payment-callback-consumer - lag 48k，积压回调触发后续补偿扫描"],
            "callback": [
                "[WARN] payment-callback-consumer - 回调处理阻塞在长事务，超 max.poll.interval.ms",
                "[INFO] payment-callback-consumer - 长事务后 beginOffset 发生跳跃",
            ],
        },
        "changes": [],
    },
    "SC-OPEN-003": {  # 报表查询把读打到主库（读未走从库）
        "title": "报表查询读打到主库",
        "metrics": {
            "master_cpu": {"value": 0.93, "baseline": 0.35, "unit": "%", "status": "critical"},
            "master_read_io": {"value": 0.9, "baseline": 0.2, "unit": "%", "status": "critical"},
            "replica_cpu": {"value": 0.08, "baseline": 0.2, "unit": "%", "status": "normal"},
            "active_conn_master": {"value": 180, "baseline": 40, "unit": "count", "status": "warning"},
            "p99_latency": {"value": 2100, "baseline": 80, "unit": "ms", "status": "critical"},
        },
        "logs": {
            "slow": [
                "[WARN] mysql master - slow_query: SELECT SUM(amount),COUNT(*) FROM orders WHERE status=pending (耗时 18s) 主库执行",
            ],
            "read_only": [
                "[INFO] mysql - 主库出现大量 SELECT 报表查询（read_only=off）",
                "[WARN] mysql - 报表 SQL 未路由到从库，堆在主库",
            ],
            "route": ["[WARN] payment-service - 本应走从库的 SELECT 报表查询落到主库"],
        },
        "changes": [{"change_id": "CHG-2026-0905", "type": "config_change", "service": "mysql",
                     "time": "2026-08-30T15:00:00Z",
                     "description": "新增报表读写分离路由规则（含 order 汇总查询）"}],
    },
    "SC-OPEN-004": {  # 单热点 Key 击穿回源
        "title": "秒杀商品详情热点 Key 击穿回源",
        "metrics": {
            "cache_hit_ratio": {"value": 0.31, "baseline": 0.95, "unit": "%", "status": "critical"},
            "cache_miss_backend": {"value": 82000, "baseline": 2000, "unit": "req", "status": "critical"},
            "hot_key_qps": {"value": 96000, "baseline": 3000, "unit": "req/s", "status": "critical"},
            "db_qps": {"value": 15000, "baseline": 800, "unit": "req/s", "status": "critical"},
            "p99_latency": {"value": 3400, "baseline": 90, "unit": "ms", "status": "critical"},
        },
        "logs": {
            "miss": ["[WARN] redis - 热点 Key 'product:SKU888' 分支大量 cache miss 直接回源 DB"],
            "hotkey": ["[WARN] redis - Key 'product:SKU888' QPS 9.6w，命中率骤降至 31% (50ms 流失效)"],
            "backend": ["[WARN] mysql - 回源码打到 DB，连接/查询队列入秒杀"],
        },
        "changes": [],
    },
    "SC-OPEN-005": {  # 灰度实例签名密钥与服务端不一致产生 401
        "title": "灰度实例签名密钥漂移产生 401",
        "metrics": {
            "signature_401_rate": {"value": 0.33, "baseline": 0.0, "unit": "%", "status": "critical"},
            "http_401_count": {"value": 41200, "baseline": 1200, "unit": "count", "status": "critical"},
            "gray_instance_errors": {"value": 0.38, "baseline": 0.02, "unit": "%", "status": "warning"},
            "stable_instance_errors": {"value": 0.01, "baseline": 0.02, "unit": "%", "status": "normal"},
            "p99_latency": {"value": 140, "baseline": 80, "unit": "ms", "status": "normal"},
        },
        "logs": {
            "401": [
                "[WARN] payment-service - HTTP 401: signature verify failed, invalid sign version",
                "[WARN] payment-service-gray - 请求命中灰度实例，按 v2.4.0 密钥校签",
            ],
            "signature": [
                "[ERROR] payment-service - 401: 客户端按 v2.3.0 密钥签名，服务端灰度实例按 v2.4.0 校签",
                "[INFO] api-gateway - 灰度标签 zone=gray 命中约 1/3 流量",
            ],
            "auth": ["[WARN] payment-service - 签名校验失败集中在灰度实例（稳定实例正常）"],
        },
        "changes": [{"change_id": "CHG-2026-0920", "type": "deploy", "service": "payment-service",
                     "time": "2026-09-03T22:00:00Z",
                     "description": "灰度发布 v2.4.0：签名密钥升级（灰度路由 zone=gray）"}],
    },
}


@tool
def query_metrics(service: str, metric: str = "all", time_range: str = "1h") -> str:
    """查询服务的监控指标（AIOps 故障诊断首选工具），支持指定时间窗。

    返回服务的关键监控指标，用于故障诊断的"现场取证"。
    拿到指标后应根据异常方向再调 query_logs 定向查日志。
    诊断回顾性故障时务必指定故障发生的时间窗（如告警描述"30 分钟前开始"→ time_range="30m"）。

    Args:
        service: 服务名，如 "payment-service"、"order-service"、"mysql"
        metric: 指标名，默认 "all" 一次拿全。也可指定具体指标名（以服务实际暴露的指标为准）
        time_range: 查询时间窗，默认 "1h"，可选 "5m"/"30m"/"2h"/"6h"/"24h"。
            短窗口（≤2h）返回故障时刻的瞬时值；长窗口（6h/24h）返回窗口均值——
            若长窗口指标正常但短窗口异常，说明故障是近期突发的

    Returns:
        JSON 格式的监控指标数据
    """
    import json

    sid = _eval_scenario()
    if sid and sid in _EVAL_SCENARIO_MOCKS:
        _sm = _EVAL_SCENARIO_MOCKS[sid]
        if metric != "all" and metric in _sm["metrics"]:
            return json.dumps({"service": service, "metric": metric, "time_range": time_range,
                               "scenario": sid, **_sm["metrics"][metric]}, ensure_ascii=False)
        return json.dumps({"service": service, "time_range": time_range, "scenario": sid,
                           "title": _sm["title"], "metrics": _sm["metrics"]}, ensure_ascii=False)

    # 故障时刻的瞬时值（模拟 payment-service 连接池耗尽场景）
    incident_metrics = {
        "error_rate": {"value": 0.38, "baseline": 0.01, "unit": "%", "status": "critical"},
        "connection_pool_usage": {"value": 1.0, "baseline": 0.3, "unit": "%", "status": "critical"},
        "pending_connections": {"value": 87, "baseline": 2, "unit": "count", "status": "critical"},
        "qps": {"value": 4200, "baseline": 1500, "unit": "req/s", "status": "warning"},
        "p99_latency": {"value": 3200, "baseline": 80, "unit": "ms", "status": "critical"},
    }
    # 长窗口均值：故障时段被正常时段稀释，指标回落但仍有残留异常
    averaged_metrics = {
        "error_rate": {"value": 0.06, "baseline": 0.01, "unit": "%", "status": "warning"},
        "connection_pool_usage": {"value": 0.52, "baseline": 0.3, "unit": "%", "status": "warning"},
        "pending_connections": {"value": 9, "baseline": 2, "unit": "count", "status": "warning"},
        "qps": {"value": 1800, "baseline": 1500, "unit": "req/s", "status": "normal"},
        "p99_latency": {"value": 310, "baseline": 80, "unit": "ms", "status": "warning"},
    }

    short_windows = {"5m", "30m", "1h", "2h"}
    metrics = incident_metrics if time_range in short_windows else averaged_metrics

    if metric != "all" and metric in metrics:
        return json.dumps({
            "service": service, "metric": metric, "time_range": time_range,
            **metrics[metric],
        }, ensure_ascii=False)
    return json.dumps({
        "service": service, "time_range": time_range, "metrics": metrics,
    }, ensure_ascii=False)


@tool
def query_logs(service: str, keyword: str, time_range: str = "1h") -> str:
    """查询服务日志，按关键词过滤。

    根据 query_metrics 的异常方向定向查日志找具体异常。
    如资源饱和度高 → keyword="connection" 看连接相关报错。

    Args:
        service: 服务名，如 "payment-service"、"mysql"
        keyword: 日志关键词，如 "connection"、"error"、"slow"、"timeout"
        time_range: 时间范围，默认 "1h"，可选 "5m"/"30m"/"2h"/"24h"

    Returns:
        JSON 格式的日志数据
    """
    import json

    sid = _eval_scenario()
    if sid and sid in _EVAL_SCENARIO_MOCKS:
        _sl = _EVAL_SCENARIO_MOCKS[sid]["logs"]
        matched = _sl.get(keyword)
        if matched is None:
            # 场景下查了不在预设里的关键词：返回场景一致的"无命中"，而非默认连接池日志
            matched = [f"[INFO] {service} - no logs matched keyword '{keyword}'（场景 {sid}）"]
        return json.dumps({"service": service, "keyword": keyword, "time_range": time_range,
                           "scenario": sid, "count": len(matched), "logs": matched},
                          ensure_ascii=False)

    # Mock 日志：根据关键词返回不同的模拟日志
    log_templates = {
        "HikariPool": [
            "[ERROR] 2026-08-02 14:55:23 HikariPool-1 - Connection is not available, timeout 30000ms",
            "[WARN]  2026-08-02 14:55:24 HikariPool-1 - Pool stats: active=10, idle=0, waiting=87",
            "[ERROR] 2026-08-02 14:55:25 HikariPool-1 - Connection pool exhausted (max=10)",
        ],
        "error": [
            "[ERROR] 2026-08-02 14:55:23 payment-service - HTTP 500: upstream connect timed out",
            "[ERROR] 2026-08-02 14:55:26 payment-service - java.sql.SQLTransientConnectionException",
            "[ERROR] 2026-08-02 14:55:28 payment-service - HikariPool-1 - Connection is not available",
        ],
        "slow_query": [
            "[WARN] 2026-08-02 14:54:00 mysql - slow_query detected: SELECT * FROM orders WHERE status='pending' (耗时 12.3s)",
            "[WARN] 2026-08-02 14:55:00 mysql - slow_query detected: UPDATE inventory SET stock=stock-1 (耗时 8.7s)",
        ],
    }

    logs = log_templates.get(keyword, [
        f"[INFO] 2026-08-02 14:55:00 {service} - no logs matched keyword '{keyword}'",
    ])

    return json.dumps({
        "service": service,
        "keyword": keyword,
        "time_range": time_range,
        "count": len(logs),
        "logs": logs,
    }, ensure_ascii=False)


@tool
def analyze_chart(service: str, chart_type: str = "overview") -> str:
    """分析服务监控图表（Grafana 截图），提取图表中的异常模式。

    通过视觉语言模型（VLM）理解监控图表截图，识别曲线异常、跨指标关联，
    输出结构化分析结果。用于故障诊断的"看图取证"，比纯文本指标更直观。

    Args:
        service: 服务名，如 "payment-service"、"order-service"
        chart_type: 图表类型，默认 "overview"。可选："overview"全览 / "connection_pool"连接池 / "latency"延迟

    Returns:
        JSON 格式的图表分析结果（metrics + anomalies + insights）
    """
    import json

    # Mock 数据：模拟 VLM 分析 Grafana 截图后的输出
    # 与 query_metrics 数据一致，但增加 VLM 特有的 anomalies/insights（看图才能发现的形态级信息）
    mock_analysis = {
        "service": service,
        "chart_type": chart_type,
        "source": "grafana_screenshot",
        "metrics": {
            "error_rate": {"value": 0.38, "baseline": 0.01, "unit": "ratio", "status": "critical"},
            "connection_pool_usage": {"value": 1.0, "baseline": 0.3, "unit": "ratio", "status": "critical"},
            "pending_connections": {"value": 87, "baseline": 2, "unit": "count", "status": "critical"},
        },
        "anomalies": [
            {"type": "spike", "description": "error_rate 在 14:30 出现陡升尖峰，从 0.01 飙至 0.38", "severity": "critical"},
            {"type": "saturation", "description": "connection_pool_usage 曲线触顶 100% 并持续横盘，连接池饱和", "severity": "critical"},
            {"type": "correlation", "description": "pending_connections 与 error_rate 同步上升，强相关", "severity": "high"},
        ],
        "insights": "图表显示连接池打满（100%）与错误率飙升（38%）强相关，尖峰始于 14:30，符合连接池耗尽特征",
    }
    return json.dumps(mock_analysis, ensure_ascii=False)


@tool
def get_recent_changes(service: str, hours: int = 24) -> str:
    """查询服务最近 N 小时内的变更事件（发布/配置修改/扩缩容/基础设施操作）。

    变更是生产故障的第一大根因。诊断时必查：若故障时间点附近存在变更，
    应优先沿"变更 → 影响"的因果链定位，而不是只按症状匹配历史经验。

    Args:
        service: 服务名，如 "payment-service"、"order-service"
        hours: 回溯小时数，默认 24。建议与故障时间窗匹配（故障发生在 1 小时内则 hours=1~2）

    Returns:
        JSON 格式的变更事件列表（type: deploy/config_change/scale/infra，change_id，时间，描述）
    """
    import json

    sid = _eval_scenario()
    if sid and sid in _EVAL_SCENARIO_MOCKS:
        _ce = _EVAL_SCENARIO_MOCKS[sid]["changes"]
        return json.dumps({"service": service, "hours": hours, "scenario": sid,
                           "count": len(_ce), "changes": _ce}, ensure_ascii=False)

    # Mock 变更事件：与种子事故对齐——payment-service 事发前 40 分钟有一次发版
    events_by_service = {
        "payment-service": [
            {
                "change_id": "CHG-2026-0812",
                "type": "deploy",
                "service": "payment-service",
                "time": "2026-08-02T14:20:00Z",
                "description": "v2.3.1 发版：新增大额支付风控查询（新增 2 个 DB 查询/笔）",
                "operator": "ci-cd",
            },
            {
                "change_id": "CHG-2026-0805",
                "type": "scale",
                "service": "payment-service",
                "time": "2026-07-30T10:00:00Z",
                "description": "连接池 max-size 保持 10 未调整（上季度容量评估遗留项）",
                "operator": "ops",
            },
        ],
        "order-service": [
            {
                "change_id": "CHG-2026-0809",
                "type": "config_change",
                "service": "order-service",
                "time": "2026-08-01T16:00:00Z",
                "description": "Redis maxmemory 从 4gb 调整为 2gb（成本优化变更）",
                "operator": "ops",
            },
        ],
    }
    events = events_by_service.get(service, [])
    return json.dumps({
        "service": service,
        "hours": hours,
        "count": len(events),
        "changes": events,
    }, ensure_ascii=False)


@tool
def create_incident_ticket(service: str, title: str, root_cause: str,
                           severity: str = "P2", priority: str = "high") -> str:
    """创建故障处理工单，用于诊断结论的落地跟进（诊断 → 行动闭环）。

    使用纪律：
    - 仅在 P1/P2 级故障诊断完成、或用户明确要求创建工单时调用
    - 工单内容应基于已确认的诊断结论，不要在诊断中途调用

    Args:
        service: 受影响的服务名
        title: 工单标题，如 "payment-service 连接池耗尽 - 扩容与慢查询治理"
        root_cause: 诊断出的根因摘要
        severity: 故障级别，P1/P2/P3
        priority: 工单优先级，默认 high

    Returns:
        JSON 格式的创建结果（ticket_id + 状态）
    """
    import json
    import uuid
    from datetime import datetime

    ticket_id = f"TK-{uuid.uuid4().hex[:6].upper()}"
    now_str = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    return json.dumps({
        "ticket_id": ticket_id,
        "status": "created",
        "service": service,
        "title": title,
        "severity": severity,
        "priority": priority,
        "root_cause": root_cause[:200],
        "created_at": now_str,
        "note": "工单已记录（当前为演示环境，未接入真实工单系统）；请人工跟进处理进度",
    }, ensure_ascii=False)


@tool
def get_service_dependencies(service: str, direction: str = "all") -> str:
    """查询服务的依赖拓扑：下游依赖（本服务调用了谁）与上游调用方（谁调用了本服务）。

    跨服务诊断的关键工具：本服务指标无法解释现象、或怀疑问题出在依赖时，
    用本工具锁定可疑依赖服务，再对该服务补充取证（query_metrics/query_logs 换成该服务名）。

    Args:
        service: 服务名，如 "payment-service"、"mysql"
        direction: 方向，默认 "all"。可选："downstream"只看下游依赖 / "upstream"只看上游调用方 / "all"

    Returns:
        JSON 格式的依赖拓扑（depends_on / called_by 列表）
    """
    import json
    import os

    # 拓扑数据文件化：真实落地时替换 data/service_topology.json
    # （APM 服务地图 / K8s 服务发现自动生成），工具与 prompt 不用改
    topo_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "data", "service_topology.json",
    )
    topology = {}
    try:
        with open(topo_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        topology = {k: v for k, v in raw.items() if not k.startswith("_")}
    except Exception:
        # 文件缺失/损坏时的兜底拓扑（与种子事故对齐）
        topology = {
            "payment-service": {
                "depends_on": ["mysql", "redis", "order-service"],
                "called_by": ["api-gateway"],
            },
            "order-service": {
                "depends_on": ["mysql", "inventory-service", "redis"],
                "called_by": ["payment-service", "api-gateway"],
            },
            "mysql": {"depends_on": [], "called_by": ["payment-service", "order-service"]},
            "redis": {"depends_on": [], "called_by": ["payment-service", "order-service"]},
        }

    entry = topology.get(service)
    if not entry:
        return json.dumps({
            "service": service, "direction": direction,
            "depends_on": [], "called_by": [],
            "note": f"拓扑中无 {service} 的记录（可能是基础设施组件或未登记服务），无法跨服务排查",
        }, ensure_ascii=False)

    depends_on = entry.get("depends_on", [])
    called_by = entry.get("called_by", [])
    result = {"service": service, "direction": direction}
    if direction in ("all", "downstream"):
        result["depends_on"] = depends_on
        result["downstream_note"] = "下游依赖故障可能传导到本服务（对可疑依赖补充取证）"
    if direction in ("all", "upstream"):
        result["called_by"] = called_by
        result["upstream_note"] = "本服务故障会向上游调用方传导（影响面评估）"
    return json.dumps(result, ensure_ascii=False)
