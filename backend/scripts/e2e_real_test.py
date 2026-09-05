"""端到端测试 - 真实 LLM + 真实 Mongo（不使用 mock）

测试场景（对齐现行 /api/langgraph API 面，P0-C 重写；旧版引用已移除的 /api/teaching）：
1. 用户注册（真实 MongoDB：docker compose 的 education-agent-mongodb-1）
2. 创建会话
3. 真实 LLM 问答（断言响应结构与内容，不做关键词断言——模型多变）
4. SSE 流式问答（断言事件序列 start → ... → done，无 error 事件）
5. 文档列表 + 就绪探针

运行方式：
    cd backend
    python scripts/e2e_real_test.py

前置：MongoDB（docker compose up -d mongodb）；backend/.env 配置 AI_API_KEY。
未配置 AI_API_KEY 时打印提示并以退出码 0 跳过（模拟模式无真实链路可验）。

注意：此测试会真实调用 LLM 并消耗 API 额度。
"""
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from fastapi.testclient import TestClient

from app.core.config import settings
from main import app


def main():
    if not settings.AI_API_KEY:
        print("[SKIP] 未配置 AI_API_KEY，真实 LLM 链路无从验证（模拟模式），跳过 e2e")
        return

    with TestClient(app) as client:
        # 1. 注册用户（真实 Mongo 写入）
        username = f"real_student_{uuid.uuid4().hex[:8]}"
        print(f"[1/5] 注册用户: {username}")
        resp = client.post("/api/auth/register", json={
            "username": username,
            "password": "test123456",
            "email": f"{username}@test.com",
            "role": "student",
            "org_name": f"org_{uuid.uuid4().hex[:8]}",
        })
        assert resp.status_code == 200, f"注册失败: {resp.status_code} {resp.text[:200]}"
        token = resp.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        print("  注册成功")

        # 2. 创建会话
        print("[2/5] 创建会话")
        resp = client.post("/api/langgraph/sessions", json={"title": "运维问答"}, headers=headers)
        assert resp.status_code == 200, f"建会话失败: {resp.status_code} {resp.text[:200]}"
        session_id = resp.json()["session_id"]
        print(f"  会话ID: {session_id}")

        # 3. 真实 LLM 问答（message 带 uuid 后缀避开 10 分钟响应缓存）
        question = f"什么是故障诊断中的证据链？（e2e-{uuid.uuid4().hex[:6]}）"
        print(f"[3/5] 真实 LLM 问答: {question}")
        resp = client.post("/api/langgraph/chat", json={
            "message": question,
            "session_id": session_id,
        }, headers=headers, timeout=300)
        assert resp.status_code == 200, f"对话失败: {resp.status_code} {resp.text[:300]}"
        data = resp.json()
        # 响应结构契约（结构化诊断面）
        for key in ("content", "tools_used", "citations", "step_count", "session_id"):
            assert key in data, f"响应缺少字段 {key}: {list(data.keys())}"
        content = data["content"] or ""
        # LLM 真实生成的硬性证据：不允许是 agent 层的兜底话术（LLM 挂掉时会命中）
        assert "内部错误" not in content, f"LLM 调用失败，命中降级话术: {content[:120]}"
        assert len(content.strip()) >= 10, f"回复内容异常为空: {content[:80]}"
        print(f"  tools_used={data['tools_used']} steps={data['step_count']} "
              f"citations={len(data['citations'])}")
        safe_text = content[:100].replace("\n", " ")
        print(f"  回复片段: {safe_text}")
        assert data["session_id"] == session_id, "会话ID未回传"

        # 4. SSE 流式问答：事件序列 start → (tool_calls/token...) → done，无 error
        stream_q = f"一句话说明什么是 RAG？（e2e-{uuid.uuid4().hex[:6]}）"
        print(f"[4/5] SSE 流式问答: {stream_q}")
        event_types = []
        with client.stream("POST", "/api/langgraph/chat/stream",
                           json={"message": stream_q, "session_id": session_id},
                           headers=headers, timeout=300) as resp:
            assert resp.status_code == 200, f"流式失败: {resp.status_code}"
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    try:
                        payload = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    event_types.append(payload.get("type"))
        assert event_types, "未收到任何 SSE 事件"
        assert event_types[0] == "start", f"首个事件应为 start，实际: {event_types[:3]}"
        assert "error" not in event_types, f"流式中出现 error 事件: {event_types}"
        assert "done" in event_types, f"缺少 done 事件: {event_types}"
        token_or_tool = [t for t in event_types if t in ("token", "tool_calls", "tools")]
        print(f"  事件序列: {event_types[:8]}{'...' if len(event_types) > 8 else ''} "
              f"(共 {len(event_types)} 个)")
        assert token_or_tool, "流式中无 token/tool 事件（LLM 未实际生成）"

        # 5. 文档列表（T10 薄 handler 面）+ 就绪探针
        print("[5/5] 文档列表 + 就绪探针")
        resp = client.get("/api/documents/", headers=headers)
        assert resp.status_code == 200, f"文档列表失败: {resp.status_code} {resp.text[:200]}"
        print(f"  文档总数: {resp.json().get('total')}")
        resp = client.get("/api/health/ready")
        assert resp.status_code == 200, f"就绪探针失败: {resp.status_code} {resp.text[:200]}"
        print(f"  就绪状态: {resp.json()}")

        print("\n[PASS] 真实 LLM 端到端测试全部通过！")


if __name__ == "__main__":
    main()
