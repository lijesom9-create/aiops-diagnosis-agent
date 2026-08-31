"""
Metrics - 指标收集器（prometheus_client 后端）

设计：
- 真实 Prometheus 指标：Counter/Gauge/Histogram 注册到全局 REGISTRY，
  由 /metrics 端点（main.py 挂载 make_asgi_app）暴露，Prometheus 采集
- 快照视图：保留 JSON 快照能力（/api/health/metrics 使用），只存最新值——
  旧实现的无界 history 列表是长运行进程的内存泄漏，已改为有界 deque
- API 兼容：increment/observe/set_gauge/get_all_metrics 签名不变，
  unified_store 等既有埋点零改动

线程安全：prometheus_client 原生线程安全；快照 dict 有锁保护。
"""

import re
import threading
from collections import deque
from typing import Any, Dict, List, Optional

from loguru import logger

try:
    from prometheus_client import Counter, Gauge, Histogram
    HAS_PROMETHEUS = True
except ImportError:  # prometheus-client 未安装时降级为纯快照模式（不阻塞启动）
    HAS_PROMETHEUS = False

# Prometheus 指标名/标签名合法字符（其余替换为下划线）
_NAME_RE = re.compile(r"[^a-zA-Z0-9_]")


def _safe_name(name: str) -> str:
    return _NAME_RE.sub("_", name)


class Metrics:
    """指标收集器：prometheus_client 真实指标 + 有界 JSON 快照"""

    # 快照历史有界（旧实现无界 list 是内存泄漏）
    _HISTORY_MAX = 500

    def __init__(self):
        self._lock = threading.Lock()
        # 快照：name{labels} -> 最新值（供 /api/health/metrics JSON 视图）
        self.metrics: Dict[str, Dict[str, Any]] = {}
        self.history = deque(maxlen=self._HISTORY_MAX)
        self._prom_objects: Dict[str, Any] = {}

    def _get_prom(self, name: str, metric_type: str, label_keys: tuple):
        """获取或创建 prometheus_client 指标对象（按名字+类型+标签键缓存）"""
        if not HAS_PROMETHEUS:
            return None
        safe = _safe_name(name)
        cache_key = f"{metric_type}:{safe}:{','.join(sorted(label_keys))}"
        if cache_key in self._prom_objects:
            return self._prom_objects[cache_key]
        try:
            if metric_type == "counter":
                obj = Counter(safe, f"metric {safe}", list(label_keys) if label_keys else [])
            elif metric_type == "gauge":
                obj = Gauge(safe, f"metric {safe}", list(label_keys) if label_keys else [])
            elif metric_type == "histogram":
                obj = Histogram(safe, f"metric {safe}", list(label_keys) if label_keys else [])
            else:
                return None
        except Exception as e:
            # 名称冲突等注册异常只告警一次，快照视图继续可用
            logger.debug(f"Prometheus 指标注册失败（仅快照模式）: {name}: {e}")
            obj = None
        self._prom_objects[cache_key] = obj
        return obj

    def _get_key(self, name: str, labels: Dict = None) -> str:
        if not labels:
            return name
        label_str = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return f"{name}{{{label_str}}}"

    def increment(self, name: str, value: float = 1.0, labels: Dict = None):
        """增加计数器"""
        labels = labels or {}
        prom = self._get_prom(name, "counter", tuple(labels.keys()))
        if prom is not None:
            try:
                prom.labels(**labels).inc(value) if labels else prom.inc(value)
            except Exception as e:
                logger.debug(f"prometheus inc 失败: {name}: {e}")
        key = self._get_key(name, labels)
        with self._lock:
            entry = self.metrics.setdefault(key, {
                "name": name, "type": "counter", "value": 0.0, "labels": labels,
            })
            entry["value"] += value
            self.history.append({"name": name, "type": "counter", "value": value, "labels": labels})

    def set_gauge(self, name: str, value: float, labels: Dict = None):
        """设置仪表盘"""
        labels = labels or {}
        prom = self._get_prom(name, "gauge", tuple(labels.keys()))
        if prom is not None:
            try:
                prom.labels(**labels).set(value) if labels else prom.set(value)
            except Exception as e:
                logger.debug(f"prometheus set 失败: {name}: {e}")
        key = self._get_key(name, labels)
        with self._lock:
            self.metrics[key] = {
                "name": name, "type": "gauge", "value": value, "labels": labels,
            }
            self.history.append({"name": name, "type": "gauge", "value": value, "labels": labels})

    def observe(self, name: str, value: float, labels: Dict = None):
        """记录直方图观测值（真实分桶，不再只记录最新值）"""
        labels = labels or {}
        prom = self._get_prom(name, "histogram", tuple(labels.keys()))
        if prom is not None:
            try:
                prom.labels(**labels).observe(value) if labels else prom.observe(value)
            except Exception as e:
                logger.debug(f"prometheus observe 失败: {name}: {e}")
        key = self._get_key(name, labels)
        with self._lock:
            self.metrics[key] = {
                "name": name, "type": "histogram", "value": value, "labels": labels,
            }
            self.history.append({"name": name, "type": "histogram", "value": value, "labels": labels})

    def get_metric(self, name: str, labels: Dict = None) -> Optional[Dict]:
        """获取指标快照"""
        with self._lock:
            entry = self.metrics.get(self._get_key(name, labels))
            return dict(entry) if entry else None

    def get_all_metrics(self) -> List[Dict]:
        """获取所有指标快照"""
        with self._lock:
            return [
                {"name": e["name"], "type": e["type"], "value": e["value"], "labels": e["labels"]}
                for e in self.metrics.values()
            ]

    def get_history(self, name: str = None, limit: int = 100) -> List[Dict]:
        """获取近期观测历史（有界）"""
        with self._lock:
            items = list(self.history)
        if name:
            items = [m for m in items if m["name"] == name]
        return items[-limit:]

    def reset(self):
        """重置快照视图（prometheus 指标不支持反注册，跨测试隔离只影响快照）"""
        with self._lock:
            self.metrics.clear()
            self.history.clear()


# 全局指标实例
_metrics: Optional[Metrics] = None
_metrics_lock = threading.Lock()


def get_metrics() -> Metrics:
    """获取全局指标（线程安全单例）"""
    global _metrics
    with _metrics_lock:
        if _metrics is None:
            _metrics = Metrics()
            if not HAS_PROMETHEUS:
                logger.warning("prometheus-client 未安装，指标仅快照模式（无 /metrics 暴露）")
        return _metrics
