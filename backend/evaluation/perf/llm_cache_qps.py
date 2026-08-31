"""LLM 缓存命中下的并发 QPS 测试"""
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

API_BASE = "http://localhost:8000"

def login(u, p):
    r = requests.post(f"{API_BASE}/api/auth/login", json={"username": u, "password": p}, timeout=10)
    return r.json()["access_token"]

def do_chat(token, q):
    h = {"Authorization": f"Bearer {token}"}
    t0 = time.perf_counter()
    try:
        r = requests.post(f"{API_BASE}/api/langgraph/chat", json={"message": q}, headers=h, timeout=120)
        lat = time.perf_counter() - t0
        if r.status_code == 429:
            return lat, "rate_limited"
        r.raise_for_status()
        return lat, None
    except Exception as e:
        return time.perf_counter() - t0, str(e)[:60]

def run(token, queries, conc, label):
    lats, errs = [], {}
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=conc) as pool:
        fs = {pool.submit(do_chat, token, q): q for q in queries}
        for f in as_completed(fs):
            lat, e = f.result()
            if e: errs[e] = errs.get(e, 0) + 1
            else: lats.append(lat)
    total = time.perf_counter() - t0
    if not lats:
        print(f"  {label}: 无成功 | {errs}"); return
    qps = len(lats) / total
    lats.sort()
    p50 = lats[len(lats)//2]
    p90 = lats[int(len(lats)*0.9)] if len(lats)>1 else lats[-1]
    print(f"  {label} | 并发={conc} 成功={len(lats)} 失败={sum(errs.values())}")
    print(f"    QPS={qps:.2f} avg={statistics.mean(lats):.3f}s p50={p50:.3f}s p90={p90:.3f}s 总={total:.2f}s")
    if errs: print(f"    错误: {errs}")

# 注册/登录
try:
    requests.post(f"{API_BASE}/api/auth/register", json={"username":"cachetest","password":"cache123456","email":"c@t.com","role":"teacher","org_name":"CacheOrg"}, timeout=10)
except: pass
token = login("cachetest", "cache123456")
print("登录成功")

q = "如何备份MySQL数据库？"

# 预热：首次提问填 LLM 缓存
print("\n预热（首次提问填 LLM 缓存）...")
lat, err = do_chat(token, q)
print(f"  预热: {lat:.2f}s err={err}")

# 场景1: 并发8 相同query（LLM缓存命中）
run(token, [q]*8, 8, "场景1: 并发8 相同query(LLM缓存命中)")

# 场景2: 并发16 相同query
run(token, [q]*16, 16, "场景2: 并发16 相同query(LLM缓存命中)")

# 场景3: 并发8 不同query（LLM缓存未命中，对比）
diff_q = ["API网关的作用是什么？","什么是微服务架构？","如何排查CPU使用率过高？","Redis持久化有哪几种方式？","什么是CI/CD流水线？","如何配置Nginx反向代理？","Docker镜像如何优化体积？","什么是上下文工程？"]
run(token, diff_q, 8, "场景3: 并发8 不同query(无LLM缓存)")

print("\n压测完成")
