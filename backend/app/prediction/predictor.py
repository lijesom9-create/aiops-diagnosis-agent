"""主动感知/预判风险（方向1）——纯逻辑部分，可在无 Prometheus 时单测。

产品形态：**仅风险提示**（写入 ops_risk_* 指标供 Grafana 面板，不发告警、
不进事故闭环）——符合"辅助不替代、人兜底"的安全边界。

纯逻辑与副作用分离：
- 本模块：数学（线性斜率/外推/破阈时间）、分析器（p99 / 连接池）、RiskSignal
- 副作用（时序查询 + 写指标）在 run_predictions() 侧，通过注入取数以便测试。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass
class PredPoint:
    """一个时序数据点（epoch 秒, 值）"""
    ts: float
    value: float


@dataclass
class RiskSignal:
    """一条风险信号"""
    analyzer: str          # p99 / pool
    service: str
    metric: str
    current: float
    slope_per_min: float
    level: int             # 0=无 1=低 2=中 3=高
    horizon_min: Optional[float] = None   # 预测破阈剩余分钟（None=不破阈）
    note: str = ""


@dataclass
class PredictionConfig:
    horizon_min: int = 30                 # 只看未来 N 分钟内的风险
    p99_threshold_ms: float = 800.0
    pool_limit: int = 5
    pool_saturation_ratio: float = 0.8
    rising_ratio: float = 0.2             # 斜率>当前值*比例才视为显著上行


def robust_slope(points: Sequence[PredPoint]) -> Tuple[float, float]:
    """最小二乘线性回归，返回 (slope/秒, 相关系数 r)。

    - 点数 < 2 → (0.0, 0.0)
    - 时间跨度约 0 → (0.0, 0.0) 防除零
    """
    n = len(points)
    if n < 2:
        return 0.0, 0.0
    # 以首点时间作为原点，避免大数差值精度问题
    t0 = points[0].ts
    sx = sy = sxx = sxy = syy = 0.0
    for p in points:
        x = p.ts - t0
        sx += x
        sy += p.value
        sxx += x * x
        sxy += x * p.value
        syy += p.value * p.value
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-12:
        return 0.0, 0.0
    slope = (n * sxy - sx * sy) / denom          # /秒
    denom_r = ((n * sxx - sx * sx) * (n * syy - sy * sy)) ** 0.5
    r = (n * sxy - sx * sy) / denom_r if denom_r > 1e-12 else 0.0
    return slope, r


def predict_breach_horizon_min(
    current: float,
    slope_per_min: float,
    threshold: float,
    max_horizon_min: float,
) -> Optional[float]:
    """沿斜线上行外推达到 threshold 还需多少分钟。

    - slope <= 0（不上升）→ None
    - 当前已超 threshold → 0.0
    - 超过 max_horizon_min 视为不破阈 → None（只报近期风险）
    假定上行单调。
    """
    if current >= threshold:
        return 0.0
    if slope_per_min <= 0:
        return None
    minutes = (threshold - current) / slope_per_min
    if minutes > max_horizon_min:
        return None
    return minutes


def _is_significant_rise(slope_per_min: float, current: float, rising_ratio: float) -> bool:
    """斜率是否构成"显著上行"：相对当前量级至少上升 rising_ratio/分钟"""
    if slope_per_min <= 0:
        return False
    return current <= 0 or slope_per_min >= abs(current) * rising_ratio


def _level_for_rising(horizon: Optional[float]) -> int:
    """上行风险定级：已破阈=高，近期将破阈=中，仅显著上行未近期破阈=低"""
    if horizon is None:
        return 1
    if horizon <= 0:
        return 3
    return 2


def analyze_p99(points: Sequence[PredPoint], cfg: PredictionConfig) -> RiskSignal:
    """p99 上行斜率 → 是否会在 horizon 内破 SLO 阈值（demo 直方图值为秒）"""
    current = points[-1].value if points else 0.0
    slope, _r = robust_slope(points)
    slope_min = slope * 60.0  # /秒 → /分钟
    threshold_sec = cfg.p99_threshold_ms / 1000.0
    # 当前已破阈值 → 无条件报高（无论斜率方向与显著性）
    if current >= threshold_sec:
        return RiskSignal(
            "p99", "demo", "demo_http_request_duration_seconds", current, slope_min, 3, 0.0,
            f"p99 已超阈值 {threshold_sec:.2f}s（当前 {current:.2f}s）",
        )
    if not _is_significant_rise(slope_min, current, cfg.rising_ratio):
        return RiskSignal("p99", "demo", "demo_http_request_duration_seconds",
                          current, slope_min, 0, None)
    horizon = predict_breach_horizon_min(current, slope_min, threshold_sec, cfg.horizon_min)
    level = _level_for_rising(horizon)
    note = (
        f"p99 上行斜率显著，预计 {horizon:.0f} 分钟内破 {threshold_sec:.0f}s 阈值"
        if level >= 2 and horizon is not None
        else "p99 上行斜率显著，未在近期破裂阈值"
        if level == 1
        else ""
    )
    return RiskSignal("p99", "demo", "demo_http_request_duration_seconds",
                      current, slope_min, level, horizon, note)


def analyze_pool(points: Sequence[PredPoint], cfg: PredictionConfig) -> RiskSignal:
    """连接池饱和度容量外推 → 是否会在 horizon 内逼近耗尽"""
    current = points[-1].value if points else 0.0
    slope, _r = robust_slope(points)
    slope_min = slope * 60.0
    sat_point = cfg.pool_limit * cfg.pool_saturation_ratio
    if current >= sat_point:
        return RiskSignal(
            "pool", "demo", "demo_db_pool_checked_out", current, slope_min, 3, 0.0,
            f"连接池已到 {current:.1f}/{cfg.pool_limit}（{cfg.pool_saturation_ratio:.0%} 饱和线），"
            f"持续可能触发第 {cfg.pool_limit + 1} 个请求超时",
        )
    if _is_significant_rise(slope_min, current, cfg.rising_ratio):
        horizon = predict_breach_horizon_min(current, slope_min, sat_point, cfg.horizon_min)
        level = _level_for_rising(horizon)
        note = (f"连接池上行斜率显著，预计 {horizon:.0f} 分钟逼近 {sat_point:.1f} 饱和线"
                if horizon is not None else "")
        return RiskSignal("pool", "demo", "demo_db_pool_checked_out",
                          current, slope_min, level, horizon, note)
    return RiskSignal("pool", "demo", "demo_db_pool_checked_out",
                      current, slope_min, 0, None)


# 分析器注册表
ANALYZERS = {
    "p99": analyze_p99,
    "pool": analyze_pool,
}


def analyze_all(
    series: Dict[str, List[PredPoint]],
    cfg: PredictionConfig,
) -> List[RiskSignal]:
    """对每个有数据的分析器计算信号（数据不足→跳过，不产出风险）"""
    signals = []
    for name, fn in ANALYZERS.items():
        pts = series.get(name) or []
        if len(pts) >= 2:
            signals.append(fn(pts, cfg))
    return signals