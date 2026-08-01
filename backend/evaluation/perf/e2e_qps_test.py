"""
端到端 QPS 压测脚本（含 LLM 生成）

通过 API 并发调用 /api/langgraph/chat，测真实端到端 QPS。
使用不同 query 避免检索缓存命中，反映真实负载。

用法：
    cd backend
    $env:PYTHONPATH = "."; .\venv\Scripts\python.exe evaluation\perf\e2e_qps_test.py
"""

import os
import sys
import time
import statistics
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

API_BASE = "http://localhost:8000"

# 不同 query，避免缓存命中（每个 query 独立，测真实检索+LLM）
TEST_QUERIES = [
    "如何备份MySQL数据库？",
    "API网关的作用是什么？",
    "什么是微服务架构？",
    "如何排查CPU使用率过高？",
    "Redis持久化有哪几种方式？",
    "什么是CI/CD流水线？",
    "如何配置Nginx反向代理？",
    "Docker镜像如何优化体积？",
]


def login(username, password):
    """登录获取 token"""
    r = requests.post(f"{API_BASE}/api/auth/login", json={"username": username, "password": password}, timeout=10)
    r.raise_for_status()
    return r.json()["access_token"]


def do_chat(token, query, timeout=120):
    """单次 chat 请求，返回 (延迟, 错误)"""
    headers = {"Authorization": f"Bearer {token}"}
    t0 = time.perf_counter()
    try:
        r = requests.post(
            f"{API_BASE}/api/langgraph/chat",
            json={"message": query},
            headers=headers,
            timeout=timeout,
        )
        latency = time.perf_counter() - t0
        if r.status_code == 429:
            return latency, "rate_limited"
        r.raise_for_status()
        return latency, None
    except Exception as e:
        return time.perf_counter() - t0, str(e)[:80]


def run_concurrent(token, queries, concurrency, label):
    """并发压测"""
    print(f"\n{'='*60}")
    print(f"{label} | 并发={concurrency} | 请求数={len(queries)}")
    print(f"{'='*60}")

    latencies = []
    errors = {}
    t_start = time.perf_counter()

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(do_chat, token, q): q for q in queries}
        for fut in as_completed(futures):
            latency, err = fut.result()
            if err:
                errors[err] = errors.get(err, 0) + 1
            else:
                latencies.append(latency)
    t_total = time.perf_counter() - t_start

    if not latencies:
        print(f"  无成功请求 | 错误: {errors}")
        return 0, {}

    qps = len(latencies) / t_total
    latencies.sort()
    p50 = latencies[len(latencies) // 2]
    p90 = latencies[int(len(latencies) * 0.9)] if len(latencies) > 1 else latencies[-1]

    print(f"  成功: {len(latencies)} | 失败: {sum(errors.values())} | 错误类型: {errors}")
    print(f"  总耗时: {t_total:.2f}s")
    print(f"  QPS: {qps:.2f}")
    print(f"  延迟: avg={statistics.mean(latencies):.2f}s p50={p50:.2f}s p90={p90:.2f}s")
    return qps, errors


def main():
    print("="*60)
    print("端到端 QPS 压测（含 LLM 生成）")
    print("="*60)

    # 注册临时账号
    print("\n注册临时账号...")
    try:
        requests.post(
            f"{API_BASE}/api/auth/register",
            json={"username": "qpstest", "password": "qps123456", "email": "qps@test.com", "role": "teacher", "org_name": "QpsOrg"},
            timeout=10,
        )
    except Exception:
        pass  # 已存在则忽略

    token = login("qpstest", "qps123456")
    print(f"登录成功, token: {token[:20]}...")

    # 预热：发一个请求触发模型加载（避免冷启动影响压测）
    print("\n预热（1 个请求）...")
    t0 = time.perf_counter()
    lat, err = do_chat(token, "你好", timeout=120)
    print(f"  预热完成: {lat:.2f}s, err={err}")

    # 场景1：并发 3（低于 30 RPM 限流，3 个不同 query）
    queries_3 = TEST_QUERIES[:3]
    run_concurrent(token, queries_3, concurrency=3, label="场景1: 并发3（不同query）")

    # 场景2：并发 5
    queries_5 = TEST_QUERIES[:5]
    run_concurrent(token, queries_5, concurrency=5, label="场景2: 并发5（不同query）")

    # 场景3：串行基准（5 个请求串行，对比并发收益）
    print(f"\n{'='*60}")
    print(f"场景3: 串行基准（5 个请求）")
    print(f"{'='*60}")
    serial_times = []
    t_start = time.perf_counter()
    for q in TEST_QUERIES[:5]:
        lat, err = do_chat(token, q)
        if not err:
            serial_times.append(lat)
            print(f"  {q[:20]}... -> {lat:.2f}s")
    t_serial = time.perf_counter() - t_start
    print(f"  串行总耗时: {t_serial:.2f}s | 串行 QPS: {len(serial_times)/t_serial:.2f}")

    print(f"\n{'='*60}")
    print("压测完成")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
