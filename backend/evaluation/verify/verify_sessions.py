"""
会话管理端到端验证

测试链路：
1. 数据库层：create/get/list/update/delete session + message_count 字段
2. _ensure_session 辅助函数：未传 session_id 自动创建；传入非法 ID 报错；越权报错
3. _auto_generate_title：mock LLM，验证仅首轮对话触发、失败静默降级
4. API 端点契约：用 FastAPI TestClient 测试 5 个会话管理端点

用法：
    cd backend
    python evaluation/verify_sessions.py
"""
import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
# 强制使用内存数据库（不连 MongoDB）
os.environ.setdefault("MONGODB_URL", "mongodb://invalid:27017")
sys.path.insert(0, str(Path(__file__).parent.parent))

print("=" * 70)
print("会话管理端到端验证")
print("=" * 70)

passed = 0
failed = 0


def check(name: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name} {detail}")


# ============================================================
# 1. 数据库层：会话 CRUD + message_count
# ============================================================
print("\n【测试 1】数据库层会话 CRUD + message_count")
print("-" * 70)

from app.core.database import Database


async def test_db_session_crud():
    db = Database()
    await db.connect()  # 内存模式

    user_id = "test_user_sessions"

    # 1.1 创建会话
    sid1 = await db.create_session(user_id, title="会话1")
    check("create_session 返回 session_id", sid1.startswith("session_"), f"实际: {sid1}")

    # 1.2 获取会话元数据（不含 messages）
    s1 = await db.get_session(sid1)
    check("get_session 返回元数据", s1 is not None and s1.get("title") == "会话1")
    check("get_session 不含 messages", "messages" not in s1, "应排除 messages 字段")

    # 1.3 创建第二个会话
    sid2 = await db.create_session(user_id, title="会话2")

    # 1.4 列出用户会话（含 message_count）
    sessions = await db.get_user_sessions(user_id)
    check("get_user_sessions 返回 2 条", len(sessions) == 2, f"实际: {len(sessions)}")
    check(
        "会话列表含 message_count 字段",
        all("message_count" in s for s in sessions),
        "每项应有 message_count",
    )
    check(
        "新建会话 message_count=0",
        sessions[0].get("message_count") == 0,
        f"实际: {sessions[0].get('message_count')}",
    )

    # 1.5 添加消息后 message_count 应更新
    await db.add_message(sid1, {"role": "user", "content": "你好"})
    await db.add_message(sid1, {"role": "assistant", "content": "你好！有什么可以帮你的？"})
    sessions = await db.get_user_sessions(user_id)
    s1_updated = next(s for s in sessions if s["session_id"] == sid1)
    check(
        "添加 2 条消息后 message_count=2",
        s1_updated["message_count"] == 2,
        f"实际: {s1_updated['message_count']}",
    )

    # 1.6 获取消息历史
    msgs = await db.get_session_messages(sid1)
    check("get_session_messages 返回 2 条", len(msgs) == 2, f"实际: {len(msgs)}")
    check(
        "消息顺序正确（user 在前）",
        msgs[0]["role"] == "user" and msgs[1]["role"] == "assistant",
    )
    check(
        "消息含 timestamp",
        all("timestamp" in m for m in msgs),
        "每条消息应有 timestamp",
    )

    # 1.7 获取消息数量
    count = await db.get_session_message_count(sid1)
    check("get_session_message_count=2", count == 2, f"实际: {count}")

    # 1.8 更新会话标题
    await db.update_session_title(sid1, "FastAPI 学习")
    s1_renamed = await db.get_session(sid1)
    check(
        "update_session_title 生效",
        s1_renamed.get("title") == "FastAPI 学习",
        f"实际: {s1_renamed.get('title')}",
    )

    # 1.9 删除会话
    deleted = await db.delete_session(sid2)
    check("delete_session 返回 True", deleted is True)
    check("删除后 get_session 返回 None", await db.get_session(sid2) is None)
    sessions_after = await db.get_user_sessions(user_id)
    check(
        "删除后只剩 1 条会话",
        len(sessions_after) == 1,
        f"实际: {len(sessions_after)}",
    )

    # 1.10 删除不存在的会话返回 False
    deleted_again = await db.delete_session("session_not_exist")
    check("删除不存在的会话返回 False", deleted_again is False)


asyncio.run(test_db_session_crud())


# ============================================================
# 2. _ensure_session 辅助函数
# ============================================================
print("\n【测试 2】_ensure_session 辅助函数")
print("-" * 70)

from fastapi import HTTPException

from app.api.langgraph import _ensure_session


async def test_ensure_session():
    db = Database()
    await db.connect()
    user_id = "test_user_ensure"
    other_user = "test_other_user"

    # 2.1 未传 session_id → 自动创建新会话
    sid = await _ensure_session(db, user_id, None)
    check(
        "未传 session_id 自动创建",
        sid.startswith("session_"),
        f"实际: {sid}",
    )
    # 确认会话归属正确
    session = await db.get_session(sid)
    check("新会话归属当前用户", session.get("user_id") == user_id)

    # 2.2 传入有效 session_id → 直接使用
    sid2 = await _ensure_session(db, user_id, sid)
    check("传入有效 session_id 返回相同 ID", sid2 == sid)

    # 2.3 传入不存在的 session_id → 404
    try:
        await _ensure_session(db, user_id, "session_not_exist")
        check("传入不存在的 session_id 应报 404", False, "未抛异常")
    except HTTPException as e:
        check(
            "传入不存在的 session_id 报 404",
            e.status_code == 404,
            f"实际 status: {e.status_code}",
        )

    # 2.4 越权访问他人会话 → 403
    other_sid = await db.create_session(other_user, title="他人会话")
    try:
        await _ensure_session(db, user_id, other_sid)
        check("越权访问他人会话应报 403", False, "未抛异常")
    except HTTPException as e:
        check(
            "越权访问他人会话报 403",
            e.status_code == 403,
            f"实际 status: {e.status_code}",
        )


asyncio.run(test_ensure_session())


# ============================================================
# 3. _auto_generate_title 自动标题生成
# ============================================================
print("\n【测试 3】_auto_generate_title 自动标题生成")
print("-" * 70)

from app.api.langgraph import _auto_generate_title


async def test_auto_title():
    db = Database()
    await db.connect()
    user_id = "test_user_title"
    sid = await db.create_session(user_id, title="新对话")

    # Mock Agent（只需要 .llm 属性）
    mock_agent = MagicMock()
    mock_llm = MagicMock()
    mock_response = MagicMock()
    mock_response.content = "FastAPI 路由与路径参数"
    mock_llm.with_kwargs = MagicMock(return_value=mock_llm)
    mock_llm.ainvoke = AsyncMock(return_value=mock_response)
    mock_agent.llm = mock_llm

    # 3.1 首轮对话（2 条消息）触发标题生成
    await db.add_message(sid, {"role": "user", "content": "FastAPI 路由怎么用？"})
    await db.add_message(sid, {"role": "assistant", "content": "FastAPI 路由..."})
    await _auto_generate_title(db, sid, "FastAPI 路由怎么用？", mock_agent)

    session = await db.get_session(sid)
    check(
        "首轮对话后生成标题",
        session.get("title") == "FastAPI 路由与路径参数",
        f"实际: {session.get('title')}",
    )
    check("LLM 被调用一次", mock_llm.ainvoke.call_count == 1)

    # 3.2 非首轮对话（4 条消息）不触发标题生成
    mock_llm.ainvoke.reset_mock()
    await db.add_message(sid, {"role": "user", "content": "再问一次"})
    await db.add_message(sid, {"role": "assistant", "content": "回答"})
    await _auto_generate_title(db, sid, "再问一次", mock_agent)
    check("非首轮对话不触发 LLM 调用", mock_llm.ainvoke.call_count == 0)

    # 3.3 LLM 调用失败时静默降级（不抛异常，标题保持不变）
    mock_llm_fail = MagicMock()
    mock_llm_fail.with_kwargs = MagicMock(return_value=mock_llm_fail)
    mock_llm_fail.ainvoke = AsyncMock(side_effect=Exception("LLM 不可用"))
    mock_agent_fail = MagicMock()
    mock_agent_fail.llm = mock_llm_fail

    sid2 = await db.create_session(user_id, title="新对话")
    await db.add_message(sid2, {"role": "user", "content": "测试降级"})
    await db.add_message(sid2, {"role": "assistant", "content": "回答"})
    # 不应抛异常
    try:
        await _auto_generate_title(db, sid2, "测试降级", mock_agent_fail)
        check("LLM 失败时静默降级不抛异常", True)
    except Exception as e:
        check("LLM 失败时静默降级不抛异常", False, f"抛了异常: {e}")
    # 标题保持"新对话"
    session2 = await db.get_session(sid2)
    check(
        "LLM 失败后标题保持不变",
        session2.get("title") == "新对话",
        f"实际: {session2.get('title')}",
    )


asyncio.run(test_auto_title())


# ============================================================
# 4. API 端点契约（用 FastAPI TestClient）
# ============================================================
print("\n【测试 4】API 端点契约（FastAPI TestClient）")
print("-" * 70)

# 用 patch 避免 Agent 真实初始化（依赖知识库/LLM）
with patch("app.api.langgraph.get_agent"), \
     patch("app.shared_services.get_knowledge_store"):
    from fastapi.testclient import TestClient

    from main import app

    # Mock 认证：所有请求都当作 test_user_api
    TEST_USER = {
        "user_id": "test_user_api",
        "username": "tester",
        "email": "tester@example.com",
        "role": "student",
    }

    # 备份并替换 get_current_user
    from app.api import langgraph as lg_module
    original_auth = lg_module.get_current_user

    async def mock_get_current_user():
        from app.core.auth import UserResponse
        return UserResponse(**TEST_USER)

    # 用 dependency_overrides 覆盖认证
    from app.core.auth import get_current_user
    app.dependency_overrides[get_current_user] = mock_get_current_user

    client = TestClient(app)

    # 4.1 创建会话
    resp = client.post("/api/langgraph/sessions", json={"title": "测试会话"})
    check("POST /sessions 状态码 200", resp.status_code == 200, f"实际: {resp.status_code}")
    body = resp.json()
    check("POST /sessions 返回 session_id", "session_id" in body, f"响应: {body}")
    check("POST /sessions 返回 title", body.get("title") == "测试会话")
    check("POST /sessions 返回 message_count=0", body.get("message_count") == 0)
    sid = body["session_id"]

    # 4.2 创建第二个会话
    resp2 = client.post("/api/langgraph/sessions", json={"title": "会话2"})
    sid2 = resp2.json()["session_id"]

    # 4.3 列出会话
    resp = client.get("/api/langgraph/sessions")
    check("GET /sessions 状态码 200", resp.status_code == 200)
    body = resp.json()
    check("GET /sessions 返回 2 条会话", body.get("total") == 2, f"实际: {body.get('total')}")
    check(
        "会话列表项含 message_count",
        all("message_count" in s for s in body.get("sessions", [])),
    )

    # 4.4 获取会话消息（初始为空）
    resp = client.get(f"/api/langgraph/sessions/{sid}/messages")
    check("GET /sessions/{id}/messages 状态码 200", resp.status_code == 200)
    check("空会话消息数为 0", resp.json().get("total") == 0)

    # 4.5 重命名会话
    resp = client.patch(f"/api/langgraph/sessions/{sid}", json={"title": "重命名后的会话"})
    check("PATCH /sessions/{id} 状态码 200", resp.status_code == 200)
    check(
        "重命名生效",
        resp.json().get("title") == "重命名后的会话",
        f"实际: {resp.json().get('title')}",
    )

    # 4.6 越权访问他人会话 → 403
    # 用全局 db 实例（TestClient 用的同一个）创建他人会话
    from app.core.database import db as global_db

    async def _create_other_session():
        await global_db.connect()
        return await global_db.create_session("other_user_xxx", title="他人会话")

    other_sid = asyncio.run(_create_other_session())

    resp = client.get(f"/api/langgraph/sessions/{other_sid}/messages")
    check(
        "越权访问他人会话报 403",
        resp.status_code == 403,
        f"实际: {resp.status_code}",
    )

    # 4.7 删除会话
    resp = client.delete(f"/api/langgraph/sessions/{sid2}")
    check("DELETE /sessions/{id} 状态码 200", resp.status_code == 200)
    # 确认已删除
    resp = client.get("/api/langgraph/sessions")
    check("删除后会话数减 1", resp.json().get("total") == 1, f"实际: {resp.json().get('total')}")

    # 4.8 删除不存在的会话 → 404
    resp = client.delete("/api/langgraph/sessions/session_not_exist")
    check(
        "删除不存在的会话报 404",
        resp.status_code == 404,
        f"实际: {resp.status_code}",
    )

    # 清理 dependency overrides
    app.dependency_overrides.clear()


# ============================================================
# 汇总
# ============================================================
print("\n" + "=" * 70)
print(f"验证结果：{passed} 通过，{failed} 失败")
print("=" * 70)
sys.exit(0 if failed == 0 else 1)
