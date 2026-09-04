"""预判风险副作用层：查 Prometheus → 跑分析器 → 写 ops_risk_* 指标 + 后台循环。

产品形态：仅风险提示（Grafana 面板消费 ops_risk_*，不发告警、不进事故闭环）。
任何外部依赖失败（Prometheus 不可达/无数据）都优雅降级：不抛错炸循环。
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Dict, List

import requests

from app.prediction.predictor import (
    PredictionConfig,
    PredPoint,
    RiskSignal,
    analyze_all,
)

logger = None


def _log():
    global logger
    if logger is None:
        from loguru import logger as _lg
        logger = _lg
    return logger


def _promql_p99(window_min: int) -> str:
    return (
        f"histogram_quantile(0.99, "
        f"sum by (le) (rate(demo_http_request_duration_seconds_bucket[{window_min}m])))"
    )


def _fetch_range(promql: str, minutes: int, step: str) -> List[PredPoint]:
    """查询 Prometheus query_range，返回 [{ts,value}...]；失败抛异常由调用方兜底。"""
    from app.core.config import settings

    url = (settings.PROMETHEUS_URL or "http://prometheus:9090").rstrip("/")
    end = datetime.now()
    start = end - timedelta(minutes=minutes)
    resp = requests.get(
        f"{url}/api/v1/query_range",
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
        raise RuntimeError(f"Prometheus 非成功状态: {data.get('error', data)}")
    points = []
    for item in data["data"]["result"]:
        for ts, val in item.get("values", []):
            try:
                points.append(PredPoint(float(ts), float(val)))
            except (TypeError, ValueError):
                continue
    return points


def _config() -> PredictionConfig:
    from app.core.config import settings
    return PredictionConfig(
        horizon_min=int(getattr(settings, "PREDICTION_HORIZON_MIN", 30)),
        p99_threshold_ms=float(getattr(settings, "PREDICTION_P99_THRESHOLD_MS", 800.0)),
        pool_limit=int(getattr(settings, "PREDICTION_POOL_LIMIT", 5)),
        pool_saturation_ratio=float(getattr(settings, "PREDICTION_POOL_SATURATION_RATIO", 0.8)),
        rising_ratio=float(getattr(settings, "PREDICTION_RISING_RATIO", 0.2)),
    )


def fetch_series(minutes: int = 20, step: str = "60s") -> Dict[str, List[PredPoint]]:
    """并行拉取各分析器所需时序。任一失败 → 该项空列表（analyze_all 会跳过）。"""
    queries = {
        "p99": _promql_p99(minutes),
        "pool": "demo_db_pool_checked_out",
    }

    def _one(kv):
        key, q = kv
        try:
            return key, _fetch_range(q, minutes, step)
        except Exception as e:
            _log().debug("预判取数失败 %s: %s", key, e)
            return key, []

    with ThreadPoolExecutor(max_workers=2) as ex:
        return dict(list(ex.map(_one, queries.items())))


def run_predictions() -> List[RiskSignal]:
    """同步执行一次完整预判：取数 → 分析 → 写 ops_risk_* 指标。返回信号列表。"""
    from app.observability.metrics import get_metrics

    log = _log()
    cfg = _config()
    series = fetch_series()
    signals = analyze_all(series, cfg)

    metrics = get_metrics()
    max_level = 0
    for sig in signals:
        metrics.set_gauge("ops_risk_level", float(sig.level), labels={"analyzer": sig.analyzer})
        metrics.set_gauge(
            "ops_risk_horizon_min",
            sig.horizon_min if sig.horizon_min is not None else -1.0,
            labels={"analyzer": sig.analyzer},
        )
        if sig.level > max_level:
            max_level = sig.level
        if sig.level >= 2 and sig.note:
            log.warning("[预判风险][{}] level={} {}", sig.analyzer, sig.level, sig.note)
    metrics.set_gauge("ops_risk_max_level", float(max_level))
    return signals


async def risk_prediction_loop():
    """后台预判循环（lifespan 启动）。独立 sleep，不与其他 worker 耦合。"""
    from app.core.config import settings

    interval = int(getattr(settings, "PREDICTION_INTERVAL_SECONDS", 60))
    while True:
        try:
            await asyncio.to_thread(run_predictions)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _log().warning("预判循环异常（继续运行）: {}", e)
        await asyncio.sleep(interval)