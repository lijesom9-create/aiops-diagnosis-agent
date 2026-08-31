"""
端到端测试 - 真实 LLM 测试（不使用 mock）

测试场景：
1. 用户注册
2. 创建会话
3. 真实 LLM 概念讲解
4. 真实 LLM 出题练习
5. 获取个性化学习路径

运行方式：
    cd backend
    python tests/e2e_real_test.py

注意：此测试会真实调用 LLM 并消耗 API 额度。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import uuid
from fastapi.testclient import TestClient
from main import app


def main():
    with TestClient(app) as client:
        # 1. 注册学生用户
        username = f"real_student_{uuid.uuid4().hex[:8]}"
        print(f"[1/5] 注册用户: {username}")
        resp = client.post("/api/auth/register", json={
            "username": username,
            "password": "test123456",
            "email": f"{username}@test.com",
            "role": "student",
            "org_name": f"org_{uuid.uuid4().hex[:8]}",
        })
        assert resp.status_code == 200, resp.text
        token = resp.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        print(f"  注册成功")

        # 2. 创建会话
        print("[2/5] 创建会话")
        resp = client.post("/api/teaching/sessions", json={"title": "递归学习"}, headers=headers)
        assert resp.status_code == 200, resp.text
        session_id = resp.json()["session_id"]
        print(f"  会话ID: {session_id}")

        # 3. 请求概念讲解（真实 LLM）
        print("[3/5] 请求概念讲解: 什么是递归？")
        resp = client.post("/api/teaching/chat", json={
            "message": "什么是递归？",
            "session_id": session_id,
        }, headers=headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        print(f"  使用技能: {data['skill_used']}")
        safe_text = data['response'][:120].encode('ascii', 'ignore').decode('ascii')
        print(f"  回复摘要: {safe_text}...")
        assert "递归" in data["response"]

        # 4. 请求出题（真实 LLM）
        print("[4/5] 请求出题: 给我出道递归题")
        resp = client.post("/api/teaching/chat", json={
            "message": "给我出道递归题",
            "session_id": session_id,
        }, headers=headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        print(f"  使用技能: {data['skill_used']}")
        safe_text = data['response'][:120].encode('ascii', 'ignore').decode('ascii')
        print(f"  回复摘要: {safe_text}...")
        assert "题目" in data["response"] or "A" in data["response"]

        # 5. 获取学习路径
        print("[5/5] 获取学习路径: 递归")
        resp = client.post("/api/teaching/learning-path", json={"target_topic": "递归"}, headers=headers)
        assert resp.status_code == 200, resp.text
        path = resp.json()
        print(f"  目标: {path['target_topic']}")
        print(f"  节点数: {len(path['nodes'])}")
        print(f"  推荐下一步: {path['next_recommended']['topic'] if path['next_recommended'] else None}")
        assert len(path["nodes"]) > 0

        print("\n[PASS] 真实 LLM 端到端测试全部通过！")


if __name__ == "__main__":
    main()
