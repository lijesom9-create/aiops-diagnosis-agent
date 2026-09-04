#!/usr/bin/env python3
"""
R5 故障注入验证矩阵（最终验收标准）

每类故障：注入 -> 等待告警触发 -> 自动诊断执行 -> 结构化判定
-> 产出"真实故障验证报告"（evaluation/results/real_fault_matrix.json）

前置条件（docker compose 全栈运行中）：
  - demo-service (port 8001)
  - prometheus (port 9090)
  - alertmanager (port 9093)
  - backend (port 8000)

用法：
  python verify_matrix.py                              # 默认跑 slow_query + error_storm + pool_exhaustion
  python verify_matrix.py --faults slow_query,error_storm  # 指定故障类型
  python verify_matrix.py --all                          # 跑全部五类（含 memory_leak，可能 OOM）
  python verify_matrix.py --backend-url http://localhost:8000 --demo-url http://localhost:8001

验收标准：>=3 类故障全链路（注入->告警->诊断->根因命中）通过
"""

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone

import requests


# --- 配置 ---

FAULT_CONFIG = {
    "slow_query": {
        "params": {"delay": 4.0},
        "expected_alerts": ["DemoServiceHighLatency"],
        "alert_wait": 420,
        "root_cause_keywords": ["慢", "slow", "数据库", "database", "延迟", "latency", "query"],
    },
    "error_storm": {
        "params": {},
        "expected_alerts": ["DemoServiceHighErrorRate"],
        "alert_wait": 300,
        "root_cause_keywords": ["错误率", "500", "5xx", "error", "错误风暴", "失败"],
    },
    "pool_exhaustion": {
        "params": {},
        "expected_alerts": ["DemoServicePoolSaturation", "DemoServiceHighErrorRate"],
        "alert_wait": 300,
        "root_cause_keywords": ["连接池", "pool", "exhaust", "耗尽", "饱和", "连接"],
    },
    "memory_leak": {
        "params": {"step_mb": 8},
        "expected_alerts": ["DemoServiceMemoryHigh"],
        "alert_wait": 420,
        "root_cause_keywords": ["内存", "memory", "OOM", "leak", "泄漏"],
    },
    "dependency_timeout": {
        "params": {"delay": 5.0},
        "expected_alerts": ["DemoServiceHighLatency"],
        "alert_wait": 420,
        "root_cause_keywords": ["超时", "timeout", "依赖", "depend", "下游", "延迟"],
    },
}

DEFAULT_FAULTS = ["slow_query", "error_storm", "pool_exhaustion"]
ALL_FAULTS = list(FAULT_CONFIG.keys())
REAL_METRIC_PREFIX = "demo_"

# ============ 复合（多故障叠加）场景 ============
# 开放场景的本质：多个独立故障在真实链路上同时发生，Agent 须组合多路真实信号
# 各自归因并合并根因（不能只蒙对其中一个）。faults.py 是 dict 状态，天然支持多故障同注。
# 判定：诊断根因须对每个构成故障的关键词组**至少命中一个**，才算真正识别了全部成因。
COMPOSITE_CONFIG = {
    "C1_double_latency": {
        "title": "双重延迟叠加：下游依赖超时 + 数据库慢查询",
        "faults": [
            {"fault": "dependency_timeout", "params": {"delay": 5.0},
             "keywords": ["超时", "timeout", "依赖", "depend", "下游", "delay"]},
            {"fault": "slow_query", "params": {"delay": 3.0},
             "keywords": ["慢", "slow", "数据库", "database", "查询", "连接池"]},
        ],
        "expected_alerts": ["DemoServiceHighLatency"],
        "alert_wait": 420,
    },
    "C2_latency_and_errors": {
        "title": "慢+错并发：延迟爬升且错误率同时抬升",
        "faults": [
            {"fault": "slow_query", "params": {"delay": 3.0},
             "keywords": ["慢", "slow", "数据库", "database", "延迟", "latency"]},
            {"fault": "error_storm", "params": {},
             "keywords": ["错误率", "500", "5xx", "error", "错误风暴", "失败"]},
        ],
        "expected_alerts": ["DemoServiceHighErrorRate", "DemoServiceHighLatency"],
        "alert_wait": 420,
    },
}


class APIClient:
    """HTTP API 客户端：封装 backend / demo-service / prometheus 交互"""

    def __init__(self, backend_url, demo_url, prometheus_url):
        self.backend = backend_url.rstrip("/")
        self.demo = demo_url.rstrip("/")
        self.prometheus = prometheus_url.rstrip("/")
        self.token = None
        self.session = requests.Session()

    def register_temp_user(self):
        """注册临时用户并设置认证头"""
        username = f"verify_{uuid.uuid4().hex[:8]}"
        resp = self.session.post(
            f"{self.backend}/api/auth/register",
            json={
                "username": username,
                "password": "test123456",
                "email": f"{username}@verify.test",
                "org_name": f"verify_org_{uuid.uuid4().hex[:4]}",
            },
            timeout=10,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"注册失败: {resp.status_code} {resp.text}")
        self.token = resp.json()["access_token"]
        self.session.headers["Authorization"] = f"Bearer {self.token}"

    def login(self, username, password):
        """使用已有账号登录"""
        resp = self.session.post(
            f"{self.backend}/api/auth/login",
            json={"username": username, "password": password},
            timeout=10,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"登录失败: {resp.status_code} {resp.text}")
        self.token = resp.json()["access_token"]
        self.session.headers["Authorization"] = f"Bearer {self.token}"

    # -- demo-service 故障注入 --

    def inject_fault(self, fault_name, params):
        resp = self.session.post(
            f"{self.demo}/_faults/{fault_name}/on",
            json=params, timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def clear_fault(self, fault_name):
        try:
            resp = self.session.post(
                f"{self.demo}/_faults/{fault_name}/off", timeout=10,
            )
            resp.raise_for_status()
        except Exception as e:
            print(f"  [warn] 清除故障 {fault_name} 失败: {e}")

    def clear_all_faults(self):
        try:
            resp = self.session.get(f"{self.demo}/_faults", timeout=10)
            if resp.status_code == 200:
                for name in resp.json().get("active", []):
                    self.clear_fault(name)
        except Exception:
            pass

    def get_active_faults(self):
        try:
            resp = self.session.get(f"{self.demo}/_faults", timeout=10)
            if resp.status_code == 200:
                return resp.json().get("active", [])
        except Exception:
            pass
        return []

    # -- Prometheus 告警查询 --

    def get_firing_alerts(self):
        try:
            resp = self.session.get(
                f"{self.prometheus}/api/v1/alerts", timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") != "success":
                return []
            alerts = data.get("data", {}).get("alerts", [])
            return [a for a in alerts if a.get("state") == "firing"]
        except Exception as e:
            print(f"  [warn] 查询 Prometheus 告警失败: {e}")
            return []

    def get_firing_alert_names(self):
        alerts = self.get_firing_alerts()
        names = set()
        for a in alerts:
            name = a.get("labels", {}).get("alertname", "")
            if name:
                names.add(name)
        return names

    # -- backend 事故查询 --

    def list_incidents(self, service=None, status=None):
        params = {}
        if service:
            params["service"] = service
        if status:
            params["status"] = status
        resp = self.session.get(
            f"{self.backend}/api/incidents", params=params, timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def get_incident_detail(self, incident_id):
        resp = self.session.get(
            f"{self.backend}/api/incidents/{incident_id}", timeout=10,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def force_resolve_incident(self, incident_id):
        """需要管理员权限；普通用户 403"""
        resp = self.session.post(
            f"{self.backend}/api/incidents/{incident_id}/force-resolve", timeout=10,
        )
        return resp.status_code == 200


def wait_for_condition(description, check_fn, timeout, interval=10):
    """轮询等待条件满足"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = check_fn()
        if result:
            return result
        time.sleep(interval)
    print(f"  [timeout] {description}（{timeout}s）")
    return None


class TrafficGenerator:
    def __init__(self, demo_url, interval=2.0, num_threads=5):
        self.demo_url = demo_url.rstrip("/")
        self.interval = interval
        self.num_threads = num_threads
        self._stop = False
        self._threads = []
        self.session = requests.Session()
        self._sent = 0
        self._errors = 0

    def start(self):
        import threading
        self._stop = False
        for _ in range(self.num_threads):
            t = threading.Thread(target=self._run, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop = True
        for t in self._threads:
            t.join(timeout=5)
        self._threads = []

    def _run(self):
        import random
        while not self._stop:
            try:
                # 1. 下单
                resp = self.session.post(
                    f"{self.demo_url}/orders",
                    json={"item": "verify_item", "amount": random.randint(1, 100)},
                    timeout=30,
                )
                self._sent += 1
                # 2. 支付（error_storm 在 /pay 返回 500，触发 5xx 告警）
                if resp.status_code == 200:
                    order_id = resp.json().get("order_id", "")
                    if order_id:
                        self.session.post(
                            f"{self.demo_url}/pay/{order_id}",
                            timeout=30,
                        )
            except Exception:
                self._errors += 1
            time.sleep(self.interval)

    @property
    def stats(self):
        return {"sent": self._sent, "errors": self._errors}


def verify_single_fault(client, fault_name, config, diag_wait=300):
    """验证单个故障的完整链路：注入 -> 告警 -> 诊断 -> 根因判定"""
    result = {
        "fault": fault_name,
        "params": config["params"],
        "expected_alerts": config["expected_alerts"],
        "timestamp_start": datetime.now(timezone.utc).isoformat(),
        "steps": {},
    }

    # 0. 清理已有故障
    print(f"\n{'='*60}")
    print(f"[{fault_name}] 清理已有故障...")
    client.clear_all_faults()
    traffic = TrafficGenerator(client.demo, interval=1.0)
    traffic.start()
    print(f"[{fault_name}] traffic generator started")
    time.sleep(5)

    # 1. 注入故障
    print(f"[{fault_name}] 注入故障: {config['params']}")
    try:
        inject_resp = client.inject_fault(fault_name, config["params"])
        result["steps"]["inject"] = {"status": "ok", "response": inject_resp}
    except Exception as e:
        result["steps"]["inject"] = {"status": "failed", "error": str(e)}
        result["overall"] = "failed"
        return result

    # 2. 等待告警触发
    expected = set(config["expected_alerts"])
    print(f"[{fault_name}] 等待告警触发: {expected}（最多 {config['alert_wait']}s）")

    def check_alert():
        firing = client.get_firing_alert_names()
        if expected & firing:
            return firing
        return None

    firing_alerts = wait_for_condition(
        "告警触发", check_alert, timeout=config["alert_wait"], interval=15,
    )
    result["steps"]["alert_fired"] = {
        "status": "ok" if firing_alerts else "timeout",
        "firing_alerts": list(firing_alerts) if firing_alerts else [],
    }
    if not firing_alerts:
        result["overall"] = "failed"
        client.clear_fault(fault_name)
        return result

    # 3. 等待事故创建（Alertmanager -> webhook -> incident）
    print(f"[{fault_name}] 等待事故创建...")
    existing_ids = {
        i["incident_id"] for i in client.list_incidents(service="payment-sim")
    }

    def check_incident():
        incidents = client.list_incidents(service="payment-sim", status="active")
        new_incidents = [
            i for i in incidents if i["incident_id"] not in existing_ids
        ]
        return new_incidents[0] if new_incidents else None

    incident = wait_for_condition(
        "事故创建", check_incident, timeout=180, interval=10,
    )
    if not incident:
        # 退而求其次：使用最近的 active 事故（可能被归入已有事故）
        incidents = client.list_incidents(service="payment-sim", status="active")
        if incidents:
            incident = incidents[0]
            print(
                f"[{fault_name}] 未发现新事故，"
                f"使用最近的 active 事故: {incident['incident_id']}"
            )
        else:
            result["steps"]["incident_created"] = {"status": "timeout"}
            result["overall"] = "failed"
            client.clear_fault(fault_name)
            return result

    result["steps"]["incident_created"] = {
        "status": "ok",
        "incident_id": incident["incident_id"],
    }

    # 4. 等待诊断完成
    print(f"[{fault_name}] 等待诊断完成（最多 {diag_wait}s）...")

    def check_diagnosis():
        detail = client.get_incident_detail(incident["incident_id"])
        if detail and detail.get("diag_count", 0) > 0:
            return detail
        return None

    detail = wait_for_condition(
        "诊断完成", check_diagnosis, timeout=diag_wait, interval=15,
    )
    if not detail:
        result["steps"]["diagnosis"] = {"status": "timeout"}
        result["overall"] = "failed"
        client.clear_fault(fault_name)
        return result

    diag_history = detail.get("diagnosis_history") or []
    last_diag = diag_history[-1] if diag_history else {}
    root_cause = last_diag.get("root_cause", "")
    confidence = last_diag.get("confidence_level", "unknown")
    sufficiency = last_diag.get("sufficiency_level", "unknown")
    # 诊断结果存储在 content 字段（Agent 内部 evidence 字段未落库到 diagnosis_history）
    evidence = last_diag.get("content", "") or last_diag.get("evidence", "")

    result["steps"]["diagnosis"] = {
        "status": "ok",
        "incident_id": incident["incident_id"],
        "diag_count": detail.get("diag_count", 0),
        "root_cause": root_cause,
        "confidence_level": confidence,
        "sufficiency_level": sufficiency,
    }

    # 5. 根因命中判定
    keywords = config["root_cause_keywords"]
    root_cause_lower = root_cause.lower()
    hit_keywords = [kw for kw in keywords if kw.lower() in root_cause_lower]
    root_cause_hit = len(hit_keywords) > 0

    result["steps"]["root_cause_judgment"] = {
        "expected_keywords": keywords,
        "hit_keywords": hit_keywords,
        "hit": root_cause_hit,
    }

    # 6. 证据引用真实指标判定
    evidence_lower = evidence.lower() if evidence else ""
    has_real_metrics = REAL_METRIC_PREFIX in evidence_lower
    result["steps"]["evidence_check"] = {
        "has_real_metrics": has_real_metrics,
        "evidence_preview": evidence[:500] if evidence else "",
    }

    # 7. 总体判定
    if root_cause_hit and has_real_metrics:
        result["overall"] = "passed"
    elif not root_cause_hit:
        result["overall"] = "root_cause_missed"
    else:
        result["overall"] = "partial"

    # 8. 清理
    print(f"[{fault_name}] 清除故障 + 强制闭案...")
    client.clear_fault(fault_name)
    traffic.stop()
    print(f"[{fault_name}] traffic generator stopped")
    time.sleep(10)
    client.force_resolve_incident(incident["incident_id"])

    result["timestamp_end"] = datetime.now(timezone.utc).isoformat()
    return result


def verify_composite(client, scenario_id, scenario_cfg, diag_wait):
    """验证复合场景完整链路：同时注入多个故障 -> 合并告警 -> 诊断 -> 多因根因判定。

    与单故障链路的差异：注入 N 个故障、合并期望告警、要求诊断根因对每个
    构成故障的关键词组都命中至少一个（证明 Agent 识别了全部并存成因）。
    """
    result = {
        "scenario": scenario_id,
        "title": scenario_cfg.get("title", scenario_id),
        "faults": [f["fault"] for f in scenario_cfg["faults"]],
        "timestamp_start": datetime.now(timezone.utc).isoformat(),
        "steps": {},
    }

    # 0. 清理 + 开流量
    print(f"\n{'='*60}")
    print(f"[{scenario_id}] {scenario_cfg.get('title','')}")
    print(f"[{scenario_id}] 清理已有故障...")
    client.clear_all_faults()
    traffic = TrafficGenerator(client.demo, interval=1.0)
    traffic.start()
    time.sleep(5)

    # 1. 注入全部故障（叠加）
    print(f"[{scenario_id}] 注入 {len(scenario_cfg['faults'])} 个故障...")
    inject_ok = []
    for f in scenario_cfg["faults"]:
        try:
            client.inject_fault(f["fault"], f["params"])
            inject_ok.append(f["fault"])
        except Exception as e:
            print(f"  [warn] 注入 {f['fault']} 失败: {e}")
    if not inject_ok or any(
        f["fault"] not in client.get_active_faults() for f in scenario_cfg["faults"]
    ):
        result["steps"]["inject"] = {"status": "failed", "faults": scenario_cfg["faults"]}
        result["overall"] = "failed"
        client.clear_all_faults()
        traffic.stop()
        return result
    active = client.get_active_faults()
    result["steps"]["inject"] = {"status": "ok", "injected": active}
    print(f"[{scenario_id}] 当前激活故障: {active}")

    # 2. 等待合并告警触发
    expected = set(scenario_cfg["expected_alerts"])
    print(f"[{scenario_id}] 等待告警: {expected}（最多 {scenario_cfg['alert_wait']}s）")

    def check_alert():
        firing = client.get_firing_alert_names()
        if expected & firing:
            return firing
        return None

    firing_alerts = wait_for_condition(
        "告警触发", check_alert, timeout=scenario_cfg["alert_wait"], interval=15,
    )
    result["steps"]["alert_fired"] = {
        "status": "ok" if firing_alerts else "timeout",
        "firing_alerts": list(firing_alerts) if firing_alerts else [],
    }
    if not firing_alerts:
        result["overall"] = "failed"
        client.clear_all_faults()
        traffic.stop()
        return result

    # 3. 等待事故创建
    print(f"[{scenario_id}] 等待事故创建...")
    existing_ids = {
        i["incident_id"] for i in client.list_incidents(service="payment-sim")
    }

    def check_incident():
        incidents = client.list_incidents(service="payment-sim", status="active")
        new_incidents = [
            i for i in incidents if i["incident_id"] not in existing_ids
        ]
        return new_incidents[0] if new_incidents else None

    incident = wait_for_condition("事故创建", check_incident, timeout=180, interval=10)
    if not incident:
        incidents = client.list_incidents(service="payment-sim", status="active")
        incident = incidents[0] if incidents else None
        if not incident:
            result["steps"]["incident_created"] = {"status": "timeout"}
            result["overall"] = "failed"
            client.clear_all_faults()
            traffic.stop()
            return result
    result["steps"]["incident_created"] = {
        "status": "ok",
        "incident_id": incident["incident_id"],
    }

    # 4. 等待诊断完成
    print(f"[{scenario_id}] 等待诊断完成（最多 {diag_wait}s）...")

    def check_diagnosis():
        detail = client.get_incident_detail(incident["incident_id"])
        if detail and detail.get("diag_count", 0) > 0:
            return detail
        return None

    detail = wait_for_condition("诊断完成", check_diagnosis, timeout=diag_wait, interval=15)
    if not detail:
        result["steps"]["diagnosis"] = {"status": "timeout"}
        result["overall"] = "failed"
        client.clear_all_faults()
        traffic.stop()
        return result

    diag_history = detail.get("diagnosis_history") or []
    last_diag = diag_history[-1] if diag_history else {}
    root_cause = last_diag.get("root_cause", "")
    confidence = last_diag.get("confidence_level", "unknown")
    evidence = last_diag.get("content", "") or last_diag.get("evidence", "")

    result["steps"]["diagnosis"] = {
        "status": "ok",
        "incident_id": incident["incident_id"],
        "diag_count": detail.get("diag_count", 0),
        "root_cause": root_cause,
        "confidence_level": confidence,
    }

    # 5. 多因根因判定：每个构成故障的关键词组都须命中至少一个
    rcl = root_cause.lower()
    per_fault_hits = []
    for f in scenario_cfg["faults"]:
        hits = [kw for kw in f["keywords"] if kw.lower() in rcl]
        per_fault_hits.append({"fault": f["fault"], "hits": hits, "hit": len(hits) > 0})
    all_faults_hit = all(p["hit"] for p in per_fault_hits)
    result["steps"]["root_cause_judgment"] = {
        "per_fault": per_fault_hits,
        "all_faults_identified": all_faults_hit,
    }

    # 6. 证据引用真实指标
    evl = evidence.lower() if evidence else ""
    has_real_metrics = REAL_METRIC_PREFIX in evl
    result["steps"]["evidence_check"] = {
        "has_real_metrics": has_real_metrics,
        "evidence_preview": evidence[:500] if evidence else "",
    }

    # 7. 总体判定
    if all_faults_hit and has_real_metrics:
        result["overall"] = "passed"
    elif not all_faults_hit:
        result["overall"] = "root_cause_missed"
    else:
        result["overall"] = "partial"

    # 8. 清理 + 闭案
    print(f"[{scenario_id}] 清除故障 + 强制闭案...")
    client.clear_all_faults()
    traffic.stop()
    time.sleep(10)
    client.force_resolve_incident(incident["incident_id"])
    result["timestamp_end"] = datetime.now(timezone.utc).isoformat()
    return result


def main():
    parser = argparse.ArgumentParser(description="R5 故障注入验证矩阵")
    parser.add_argument("--backend-url", default="http://localhost:8000")
    parser.add_argument("--demo-url", default="http://localhost:8001")
    parser.add_argument("--prometheus-url", default="http://localhost:9090")
    parser.add_argument(
        "--faults",
        default=None,
        help=f"逗号分隔的故障类型（可选: {','.join(ALL_FAULTS)}）；不指定且未跑复合时不跑单体",
    )
    parser.add_argument(
        "--all", action="store_true", help="跑全部五类故障（含 memory_leak）"
    )
    parser.add_argument(
        "--diag-wait", type=int, default=300, help="等待诊断完成的最大秒数"
    )
    parser.add_argument("--output", default=None, help="输出文件路径")
    parser.add_argument(
        "--composites",
        default="",
        help="跑复合（多故障叠加）场景，逗号分隔（可选: " + ",".join(COMPOSITE_CONFIG) + "）",
    )
    args = parser.parse_args()

    composite_names = [c.strip() for c in args.composites.split(",") if c.strip()]

    fault_names = ALL_FAULTS if args.all else (args.faults.split(",") if args.faults else [])
    for name in fault_names:
        if name not in FAULT_CONFIG:
            print(f"未知故障类型: {name}；可选: {','.join(ALL_FAULTS)}")
            sys.exit(1)
    for name in composite_names:
        if name not in COMPOSITE_CONFIG:
            print(f"未知复合场景: {name}；可选: {','.join(COMPOSITE_CONFIG)}")
            sys.exit(1)

    if not fault_names and not composite_names:
        fault_names = DEFAULT_FAULTS

    output_path = args.output
    if not output_path:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        output_path = os.path.join(
            script_dir, "..", "backend", "evaluation", "results",
            "real_fault_matrix.json",
        )
    output_path = os.path.abspath(output_path)

    print(f"\nR5 故障注入验证矩阵")
    print(f"故障类型: {','.join(fault_names)}")
    print(f"Backend:  {args.backend_url}")
    print(f"Demo:     {args.demo_url}")
    print(f"Prom:     {args.prometheus_url}")
    print(f"输出:     {output_path}\n")

    client = APIClient(args.backend_url, args.demo_url, args.prometheus_url)

    # 优先使用环境变量中的管理员凭据（force-resolve 需要管理员权限）
    env_user = os.environ.get("VERIFY_USER")
    env_pass = os.environ.get("VERIFY_PASSWORD")
    if env_user and env_pass:
        print(f"使用环境变量凭据登录: {env_user}")
        client.login(env_user, env_pass)
        print("  登录成功")
    else:
        print("注册临时用户...")
        try:
            client.register_temp_user()
            print("  临时用户注册成功")
        except Exception as e:
            print(f"  注册失败: {e}")
            username = os.environ.get("VERIFY_USER", "admin_root")
            password = os.environ.get("VERIFY_PASSWORD", "")
            if not password:
                print("  请设置 VERIFY_USER / VERIFY_PASSWORD 环境变量")
            sys.exit(1)
            client.login(username, password)

    # 前置检查
    print("\n前置检查...")
    active = client.get_active_faults()
    if active:
        print(f"  发现有未清除的故障: {active}，先清理...")
        client.clear_all_faults()
        time.sleep(10)

    firing = client.get_firing_alert_names()
    if firing:
        print(f"  发现有正在触发的告警: {firing}，等待恢复...")
        wait_for_condition(
            "告警恢复",
            lambda: not client.get_firing_alert_names(),
            120, 15,
        )

    # 执行验证矩阵（单故障 + 复合场景）
    results = []
    for fault_name in fault_names:
        config = FAULT_CONFIG[fault_name]
        result = verify_single_fault(
            client, fault_name, config, args.diag_wait,
        )
        results.append(result)
        print(f"\n[{fault_name}] 结果: {result.get('overall', 'unknown')}")

    for scene in composite_names:
        cfg = COMPOSITE_CONFIG[scene]
        result = verify_composite(client, scene, cfg, args.diag_wait)
        results.append(result)
        print(f"\n[{scene}] 结果: {result.get('overall', 'unknown')}")

    # 汇总
    passed = sum(1 for r in results if r.get("overall") == "passed")
    partial = sum(1 for r in results if r.get("overall") == "partial")
    failed = sum(
        1 for r in results
        if r.get("overall") in ("failed", "root_cause_missed")
    )

    summary = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "total": len(results),
        "passed": passed,
        "partial": partial,
        "failed": failed,
        "acceptance_criteria": ">=3 类通过（或仅跑复合/少量项时需全部通过）",
        "acceptance_met": (passed >= 3 and len(results) >= 3) or (passed == len(results) and len(results) < 3),
        "results": results,
    }

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"\n验证矩阵完成")
    print(f"总计: {len(results)}  通过: {passed}  部分: {partial}  失败: {failed}")
    acceptance = "达标" if summary["acceptance_met"] else "未达标"
    print(f"达标判定: {acceptance}")
    print(f"报告: {output_path}\n")

    for r in results:
        label = r.get("scenario") or r.get("fault", "?")
        overall = r.get("overall", "unknown")
        root_cause = (
            r.get("steps", {})
            .get("diagnosis", {})
            .get("root_cause", "N/A")
        )
        print(f"  {label:25s} -> {overall:15s} | 根因: {root_cause[:60]}")

    return 0 if summary["acceptance_met"] else 1


if __name__ == "__main__":
    sys.exit(main())
