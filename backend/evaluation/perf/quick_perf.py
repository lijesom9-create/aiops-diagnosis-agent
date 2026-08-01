"""
轻量 API 压测脚本

测试内容：
1. 健康检查基线延迟（P50/P95/P99）
2. 阶梯并发测试（1→5→10→20）
3. 限流验证（快速发 35 个请求，验证 429）

用法：
    cd backend
    python evaluation/perf/quick_perf.py
"""

import time
import statistics
import concurrent.futures
import requests
from collections import Counter

BASE_URL = "http://localhost:8000"


def measure_latency(url, n=20):
    """测量单接口延迟分布"""
    latencies = []
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            r = requests.get(url, timeout=5)
            if r.status_code == 200:
                latencies.append((time.perf_counter() - t0) * 1000)
        except Exception:
            pass
    return latencies


def percentile(data, p):
    """计算百分位数"""
    if not data:
        return 0
    data_sorted = sorted(data)
    k = (len(data_sorted) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(data_sorted) - 1)
    return data_sorted[f] + (data_sorted[c] - data_sorted[f]) * (k - f)


def test_baseline():
    """1. 健康检查基线延迟"""
    print("\n" + "=" * 50)
    print("1. 健康检查基线延迟（20 次）")
    print("=" * 50)
    latencies = measure_latency(f"{BASE_URL}/api/health", n=20)
    if latencies:
        print(f"  成功: {len(latencies)}/20")
        print(f"  P50:  {percentile(latencies, 50):.1f}ms")
        print(f"  P95:  {percentile(latencies, 95):.1f}ms")
        print(f"  P99:  {percentile(latencies, 99):.1f}ms")
        print(f"  Min:  {min(latencies):.1f}ms")
        print(f"  Max:  {max(latencies):.1f}ms")
    else:
        print("  全部失败，后端可能未启动")


def test_concurrency():
    """2. 阶梯并发测试"""
    print("\n" + "=" * 50)
    print("2. 阶梯并发测试（健康检查接口）")
    print("=" * 50)

    for concurrency in [1, 5, 10, 20]:
        latencies = []
        errors = 0

        def hit():
            t0 = time.perf_counter()
            try:
                r = requests.get(f"{BASE_URL}/api/health", timeout=10)
                if r.status_code == 200:
                    return (time.perf_counter() - t0) * 1000
            except Exception:
                pass
            return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            results = list(pool.map(lambda _: hit(), range(concurrency * 3)))

        latencies = [r for r in results if r is not None]
        errors = len(results) - len(latencies)

        if latencies:
            print(f"  并发 {concurrency:2d}: P50={percentile(latencies, 50):.0f}ms  "
                  f"P95={percentile(latencies, 95):.0f}ms  "
                  f"成功率={len(latencies)}/{len(results)}", end="")
            if errors:
                print(f"  (失败{errors})")
            else:
                print()


def test_rate_limit():
    """3. 限流验证：快速发 35 个请求，验证 429"""
    print("\n" + "=" * 50)
    print("3. 限流验证（RATE_LIMIT_RPM=30，发 35 个请求）")
    print("=" * 50)

    # 用 chat 端点测试限流（即使返回 401 也会计入限流计数）
    url = f"{BASE_URL}/api/langgraph/chat"
    status_codes = []

    def hit():
        try:
            r = requests.post(url, json={"message": "test"}, timeout=5)
            return r.status_code
        except Exception:
            return 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=35) as pool:
        status_codes = list(pool.map(lambda _: hit(), range(35)))

    counter = Counter(status_codes)
    print(f"  状态码分布: {dict(counter)}")
    print(f"  200/401: {counter.get(200, 0) + counter.get(401, 0)} 次（正常处理）")
    print(f"  429:      {counter.get(429, 0)} 次（被限流）")
    if counter.get(429, 0) > 0:
        print("  ✓ 限流器正常工作")
    else:
        print("  ✗ 限流器未触发（可能 RATE_LIMIT_RPM 设置过高或端点未接入限流）")


def test_memory_trend():
    """4. 内存趋势：连续 60 个请求，观察是否有泄漏"""
    print("\n" + "=" * 50)
    print("4. 内存趋势（连续 60 请求，观察延迟变化）")
    print("=" * 50)

    latencies = measure_latency(f"{BASE_URL}/api/health", n=60)
    if len(latencies) >= 60:
        first_20 = statistics.mean(latencies[:20])
        last_20 = statistics.mean(latencies[40:])
        print(f"  前 20 次平均: {first_20:.1f}ms")
        print(f"  后 20 次平均: {last_20:.1f}ms")
        ratio = last_20 / first_20 if first_20 > 0 else 0
        if ratio > 1.5:
            print(f"  ⚠ 延迟增长 {ratio:.1f}x，可能存在内存泄漏或资源未释放")
        else:
            print(f"  ✓ 延迟稳定（增长 {ratio:.1f}x）")
    else:
        print(f"  仅成功 {len(latencies)}/60，无法分析趋势")


if __name__ == "__main__":
    print("=" * 50)
    print("  轻量 API 压测")
    print("  目标: 发现瓶颈，不是追求精确 QPS")
    print("=" * 50)

    test_baseline()
    test_concurrency()
    test_rate_limit()
    test_memory_trend()

    print("\n" + "=" * 50)
    print("  压测完成")
    print("=" * 50)
