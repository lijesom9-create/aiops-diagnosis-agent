"""方向1 主动感知/预判风险（predictor + infer_status）测试。

覆盖：线性斜率/破阈时间纯函数、p99/连接池分析器定级、缺失数据跳过、
run_predictions 写 ops_risk_* 指标（注入取数，不依赖真实 Prometheus）。
"""

import pytest

from app.prediction import PredictionConfig, PredPoint, robust_slope
from app.prediction.predictor import (
    analyze_all,
    analyze_p99,
    analyze_pool,
    predict_breach_horizon_min,
)


def _pts(values, step_min=1.0):
    """按 step 分钟均匀生成时序点（首点 t=0）"""
    return [PredPoint(ts=i * step_min * 60.0, value=v) for i, v in enumerate(values)]


# ========== 纯数学 ==========

def test_robust_slope_linear():
    pts = _pts([0, 1, 2, 3, 4])          # 每分钟 +1 → 每秒 +1/60
    slope, r = robust_slope(pts)
    assert abs(slope - (1.0 / 60.0)) < 1e-6
    assert abs(r - 1.0) < 1e-6


def test_robust_slope_flat():
    slope, r = robust_slope(_pts([5, 5, 5]))
    assert abs(slope) < 1e-9


def test_robust_slope_too_few():
    assert robust_slope([]) == (0.0, 0.0)
    assert robust_slope(_pts([1])) == (0.0, 0.0)


def test_breach_horizon():
    # current=0，斜率 1/分钟，阈值 10 → 10 分钟
    h = predict_breach_horizon_min(0.0, 1.0, 10.0, 30)
    assert h == pytest.approx(10.0)
    assert predict_breach_horizon_min(12.0, 1.0, 10.0, 30) == 0.0   # 已破阈
    assert predict_breach_horizon_min(0.0, -1.0, 10.0, 30) is None  # 下行
    assert predict_breach_horizon_min(0.0, 0.1, 10.0, 30) is None   # 超 horizon


# ========== 分析器 ==========

def test_p99_flat_no_risk():
    cfg = PredictionConfig(p99_threshold_ms=800.0)
    sig = analyze_p99(_pts([0.1, 0.1, 0.1]), cfg)     # 单位秒
    assert sig.level == 0


def test_p99_rising_breaches():
    cfg = PredictionConfig(p99_threshold_ms=800.0)    # 0.8 s
    sig = analyze_p99(_pts([0.1, 0.3, 0.5]), cfg)
    assert sig.level >= 2
    assert sig.horizon_min is not None


def test_p99_already_high():
    cfg = PredictionConfig(p99_threshold_ms=800.0)
    sig = analyze_p99(_pts([0.9, 0.95, 1.0]), cfg)
    assert sig.level == 3                            # 已超阈值
    assert sig.horizon_min == 0.0


def test_pool_already_saturated():
    cfg = PredictionConfig(pool_limit=5, pool_saturation_ratio=0.8)
    sig = analyze_pool(_pts([4.0, 4.2, 4.3]), cfg)   # 4 > 5*0.8=4
    assert sig.level == 3
    assert sig.horizon_min == 0.0


def test_pool_rising_to_saturation():
    cfg = PredictionConfig(pool_limit=5, pool_saturation_ratio=0.8, horizon_min=30)
    sig = analyze_pool(_pts([1.0, 2.0, 3.0]), cfg)
    assert sig.level >= 2


def test_pool_low_flat_no_risk():
    cfg = PredictionConfig(pool_limit=5, pool_saturation_ratio=0.8)
    sig = analyze_pool(_pts([1.0, 1.0, 1.0]), cfg)
    assert sig.level == 0


def test_analyze_all_skips_missing_data():
    cfg = PredictionConfig()
    # p99 有数据，pool 无数据（<2 点）→ 只产出 p99
    signals = analyze_all({"p99": _pts([0.2, 0.3]), "pool": []}, cfg)
    assert [s.analyzer for s in signals] == ["p99"]


# ========== run_predictions 写指标 ==========

def test_run_predictions_writes_gauges(monkeypatch):
    import app.prediction.infer_status as mod
    from app.observability.metrics import get_metrics

    def _fake_fetch(**kw):
        return {
            "p99": _pts([0.1, 0.5]),      # 上升显著
            "pool": _pts([4.5, 5.0]),     # 已饱和
        }
    monkeypatch.setattr(mod, "fetch_series", _fake_fetch)

    signals = mod.run_predictions()
    assert len(signals) == 2
    m = get_metrics()
    for sig in signals:
        level = m.get_metric("ops_risk_level", {"analyzer": sig.analyzer})
        assert level is not None and level["value"] == float(sig.level)
    assert m.get_metric("ops_risk_max_level")["value"] == 3.0


def test_run_predictions_empty_series_ok(monkeypatch):
    import app.prediction.infer_status as mod
    from app.observability.metrics import get_metrics

    monkeypatch.setattr(mod, "fetch_series", lambda **kw: {"p99": [], "pool": []})
    signals = mod.run_predictions()
    assert signals == []
    assert get_metrics().get_metric("ops_risk_max_level")["value"] == 0.0