"""
故障注入注册表（控制面）

设计原则：
- 注入状态只存在于进程内存 + /_faults 控制端点，**不进入 Prometheus 指标**——
  避免诊断 Agent 通过 demo_fault_active 直接读到"答案"（它只应看到有机症状）
- 每类故障的生效点写在业务代码里（通过 is_active/param 查询），本模块只管状态与副作用

五类故障：
1. slow_query        慢查询：业务处理额外阻塞 delay 秒（默认 2s）→ P95 延迟告警 + 连接池压力
2. error_storm       错误风暴：/pay 全部 500 → 错误率告警
3. pool_exhaustion   连接池耗尽：占满全部池连接 → 后续请求 3s 超时 500（池饱和度 Gauge 先行）
4. memory_leak       内存泄漏：每秒增长 step MB（容器 mem_limit 触顶后 OOM 重启）
5. dependency_timeout 依赖超时：/orders 阻塞 delay 秒（模拟下游风控服务挂）
"""

import threading

from loguru import logger

AVAILABLE = ["slow_query", "error_storm", "pool_exhaustion", "memory_leak", "dependency_timeout"]

_lock = threading.Lock()
_state: dict = {}          # name -> params dict
_held_conns: list = []     # pool_exhaustion 持有的真实连接
_leak_stop = threading.Event()
_leak_thread: threading.Thread | None = None


def inject(name: str, params: dict):
    if name not in AVAILABLE:
        raise KeyError(name)
    params = params or {}
    with _lock:
        if name in _state:
            logger.warning("故障已处于注入状态，忽略重复注入: {name}", name=name)
            return
        _state[name] = params

    if name == "pool_exhaustion":
        _hold_pool_connections()
    elif name == "memory_leak":
        _start_leak(params.get("step_mb", 8))
    logger.warning("【故障注入】{name} params={params}", name=name, params=params)


def clear(name: str):
    with _lock:
        _state.pop(name, None)
    if name == "pool_exhaustion":
        _release_pool_connections()
    elif name == "memory_leak":
        _leak_stop.set()
    logger.warning("【故障解除】{name}", name=name)


def clear_all():
    for name in list(active_names()):
        clear(name)


def is_active(name: str) -> bool:
    return name in _state


def param(name: str, key: str, default):
    return _state.get(name, {}).get(key, default)


def active_names() -> list:
    return list(_state.keys())


# ---- pool_exhaustion：真实占满 SQLAlchemy 池 ----

def _hold_pool_connections():
    import db

    def _hold():
        for _ in range(db.POOL_LIMIT):
            try:
                conn = db.engine.connect()
                _held_conns.append(conn)
                logger.warning("池连接已占用 {n}/{limit}", n=len(_held_conns), limit=db.POOL_LIMIT)
            except Exception as e:
                logger.error("占用池连接失败（可能已耗尽）: {err}", err=e)
                break

    threading.Thread(target=_hold, daemon=True).start()


def _release_pool_connections():
    for conn in list(_held_conns):
        try:
            conn.close()
        except Exception:
            pass
    _held_conns.clear()
    logger.warning("池连接已全部释放")


# ---- memory_leak：后台线程持续吃内存 ----

def _start_leak(step_mb: float):
    global _leak_thread
    _leak_stop.clear()
    leak_bucket: list = []

    def _leak():
        block = b"x" * int(step_mb * 1024 * 1024)
        while not _leak_stop.is_set():
            leak_bucket.append(block)
            logger.warning("内存泄漏注入中：已增长 {n} MB", n=len(leak_bucket) * int(step_mb))
            _leak_stop.wait(2.0)

    _leak_thread = threading.Thread(target=_leak, daemon=True)
    _leak_thread.start()
