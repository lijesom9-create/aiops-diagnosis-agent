"""
本机系统监控 MCP Server（基于 psutil，查真实系统指标）

通过 MCP 协议向 Agent 暴露本机真实的系统监控工具：
- query_system_metrics: CPU/内存/磁盘/网络 实时指标（真实数据）
- query_top_processes: 按 CPU/内存排序的 Top N 进程
- query_disk_usage: 各分区磁盘使用情况

数据来源：psutil 直接读取本机内核/操作系统指标，非模拟。
适用于：把 Agent 接到自己的电脑上，让它看到真实的系统负载并做诊断。

传输方式：stdio（本地子进程，由 LangGraphAgent 通过 langchain-mcp-adapters 加载）

用法（独立测试）：
    python mcp_servers/system_monitoring_server.py
    # 或由 agent 通过 stdio 自动拉起
"""
import json
import platform
import socket
import time

import psutil
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("SystemMonitoring")

# 进程信息缓存（避免每次调用都遍历全部 pid 的开销）
# psutil.process_iter 在进程数多时较慢，加短缓存提升连续调用性能
_proc_cache: dict = {"ts": 0.0, "data": []}
_PROC_CACHE_TTL = 2.0  # 2 秒内复用


def _get_processes() -> list:
    """获取所有进程信息（带 2s 缓存）"""
    now = time.time()
    if now - _proc_cache["ts"] < _PROC_CACHE_TTL and _proc_cache["data"]:
        return _proc_cache["data"]

    procs = []
    for p in psutil.process_iter(["pid", "name", "username", "cpu_percent",
                                   "memory_percent", "memory_info", "status",
                                   "create_time", "cmdline"]):
        try:
            info = p.info
            procs.append({
                "pid": info["pid"],
                "name": info["name"] or "",
                "username": info["username"] or "",
                "cpu_percent": round(info["cpu_percent"] or 0.0, 2),
                "memory_percent": round(info["memory_percent"] or 0.0, 2),
                "memory_rss_mb": round((info["memory_info"].rss if info["memory_info"] else 0) / 1024 / 1024, 1),
                "status": info["status"] or "",
                "create_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(info["create_time"])) if info["create_time"] else "",
                "cmdline": " ".join(info["cmdline"] or [])[:200],
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    _proc_cache["ts"] = now
    _proc_cache["data"] = procs
    return procs


@mcp.tool()
def query_system_metrics(category: str = "all") -> str:
    """查询本机实时系统监控指标（真实数据，来自 psutil）。

    用于让 Agent 看到当前电脑的真实负载情况：CPU/内存/磁盘/网络。
    拿到指标后应根据异常方向再调 query_top_processes 定位占用资源的进程。

    Args:
        category: 指标类别，默认 "all" 返回全部。可选：
            - cpu: CPU 使用率、负载、核心数、上下文切换
            - memory: 内存/交换分区使用率
            - disk: 各分区使用率
            - network: 各网卡收发字节数、连接状态统计
            - all: 返回以上全部（推荐首次诊断用 all）

    Returns:
        JSON 格式的系统指标数据
    """
    data = {"hostname": socket.gethostname(), "platform": platform.platform(),
            "category": category, "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}

    if category in ("all", "cpu"):
        # cpu_percent 首次调用返回 0，预热一次再取
        psutil.cpu_percent(interval=None)
        data["cpu"] = {
            "percent": psutil.cpu_percent(interval=0.1),
            "per_cpu_percent": psutil.cpu_percent(interval=0.1, percpu=True),
            "logical_cores": psutil.cpu_count(logical=True),
            "physical_cores": psutil.cpu_count(logical=False),
            "load_avg": list(psutil.getloadavg()) if hasattr(psutil, "getloadavg") else None,
            "ctx_switches": psutil.cpu_stats().ctx_switches,
            "interrupts": psutil.cpu_stats().interrupts,
        }

    if category in ("all", "memory"):
        vm = psutil.virtual_memory()
        sm = psutil.swap_memory()
        data["memory"] = {
            "total_gb": round(vm.total / 1024**3, 2),
            "available_gb": round(vm.available / 1024**3, 2),
            "used_gb": round(vm.used / 1024**3, 2),
            "percent": vm.percent,
            "swap_total_gb": round(sm.total / 1024**3, 2),
            "swap_used_gb": round(sm.used / 1024**3, 2),
            "swap_percent": sm.percent,
        }

    if category in ("all", "disk"):
        partitions = []
        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
                partitions.append({
                    "device": part.device,
                    "mountpoint": part.mountpoint,
                    "fstype": part.fstype,
                    "total_gb": round(usage.total / 1024**3, 2),
                    "used_gb": round(usage.used / 1024**3, 2),
                    "free_gb": round(usage.free / 1024**3, 2),
                    "percent": usage.percent,
                })
            except (PermissionError, OSError):
                continue
        data["disk"] = {"partitions": partitions}

    if category in ("all", "network"):
        io = psutil.net_io_counters()
        nics = {}
        for name, stats in psutil.net_io_counters(pernic=True).items():
            nics[name] = {
                "bytes_sent": stats.bytes_sent,
                "bytes_recv": stats.bytes_recv,
                "packets_sent": stats.packets_sent,
                "packets_recv": stats.packets_recv,
                "errin": stats.errin,
                "errout": stats.errout,
            }
        # 连接状态统计
        conn_status = {}
        try:
            for c in psutil.net_connections(kind="inet"):
                st = c.status
                conn_status[st] = conn_status.get(st, 0) + 1
        except (psutil.AccessDenied, PermissionError):
            pass
        data["network"] = {
            "total_bytes_sent": io.bytes_sent,
            "total_bytes_recv": io.bytes_recv,
            "nics": nics,
            "connections_by_status": conn_status,
        }

    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
def query_top_processes(sort_by: str = "cpu", limit: int = 10) -> str:
    """查询占用资源最多的 Top N 进程（真实数据，来自 psutil）。

    根据 query_system_metrics 的异常方向定位具体进程：
    - CPU 占用高 → sort_by="cpu" 找吃 CPU 的进程
    - 内存占用高 → sort_by="memory" 找吃内存的进程

    Args:
        sort_by: 排序方式，"cpu"（默认）或 "memory"
        limit: 返回进程数，默认 10

    Returns:
        JSON 格式的进程列表
    """
    if sort_by not in ("cpu", "memory"):
        sort_by = "cpu"
    key = "cpu_percent" if sort_by == "cpu" else "memory_percent"

    procs = _get_processes()
    # cpu_percent 首次调用返回 0，这里 process_iter 已采集过一次，
    # 排序前再采一次让数值更准（process_iter 内部已做差分）
    sorted_procs = sorted(procs, key=lambda p: p.get(key, 0), reverse=True)[:limit]

    return json.dumps({
        "sort_by": sort_by,
        "limit": limit,
        "count": len(sorted_procs),
        "processes": sorted_procs,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def query_disk_usage() -> str:
    """查询各磁盘分区使用情况（真实数据，来自 psutil）。

    用于磁盘空间不足或 IO 性能问题的诊断。

    Returns:
        JSON 格式的分区使用情况列表
    """
    partitions = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
            partitions.append({
                "device": part.device,
                "mountpoint": part.mountpoint,
                "fstype": part.fstype,
                "total_gb": round(usage.total / 1024**3, 2),
                "used_gb": round(usage.used / 1024**3, 2),
                "free_gb": round(usage.free / 1024**3, 2),
                "percent": usage.percent,
            })
        except (PermissionError, OSError):
            continue

    # IO 计数器（自开机累计）
    io = psutil.disk_io_counters()
    io_data = {
        "total_read_bytes": io.read_bytes if io else 0,
        "total_write_bytes": io.write_bytes if io else 0,
        "read_count": io.read_count if io else 0,
        "write_count": io.write_count if io else 0,
    }

    return json.dumps({
        "partitions": partitions,
        "io_counters": io_data,
    }, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    # stdio 传输：由 langchain-mcp-adapters 通过子进程拉起
    mcp.run(transport="stdio")
