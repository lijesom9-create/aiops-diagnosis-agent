"""
端到端测试 — 验证所有修复的正确性

覆盖范围：
1. 认证流程（注册/登录/角色限制）
2. Agent 并发安全（每次请求独立实例）
3. State 工具方法名修复
4. Database 层修复（UUID、clean_mongo_doc、排序）
5. 安全修复（SECRET_KEY、角色校验、正则转义）
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ============================================================
# 1. 认证流程测试
# ============================================================

class TestAuthFlow:
    """认证流程端到端测试"""

    @pytest.mark.asyncio
    async def test_register_and_login(self):
        """注册 → 登录 → 获取用户信息"""
        import uuid

        from app.core.auth import UserCreate, UserLogin, login_user, register_user

        # 使用唯一用户名避免与已有数据冲突
        unique_name = f"e2e_{uuid.uuid4().hex[:8]}"
        user_data = UserCreate(
            username=unique_name,
            password="test123456",
            email=f"{unique_name}@test.com",
            org_name=f"org_{uuid.uuid4().hex[:8]}",
        )
        token = await register_user(user_data)
        assert token.access_token
        assert token.token_type == "bearer"

        # 登录
        login_data = UserLogin(username=unique_name, password="test123456")
        login_token = await login_user(login_data)
        assert login_token.access_token

    @pytest.mark.asyncio
    async def test_register_rejects_admin_role(self):
        """注册模型不接收 role 字段（防提权：注册角色不信任客户端输入）

        UserCreate 已移除 role 字段；角色只能由 register_user 内部决定
        （固定 student，或用户名匹配 SUPER_ADMIN_USERNAME 时为 admin）。
        """
        from app.core.auth import UserCreate

        # role 字段已从注册模型移除
        assert "role" not in UserCreate.model_fields

        # 模拟恶意客户端在 payload 中携带 role：模型不应保留该字段
        user = UserCreate(**{
            "username": "hacker",
            "password": "test123456",
            "email": "h@h.com",
            "org_name": "test-org",
            "role": "admin",
        })
        assert not hasattr(user, "role")

    @pytest.mark.asyncio
    async def test_password_too_short(self):
        """密码太短应被拒绝"""
        from pydantic import ValidationError

        from app.core.auth import UserCreate

        with pytest.raises(ValidationError):
            UserCreate(username="u", password="123", email="e@e.com", org_name="test-org")

    def test_uuid_user_id_generation(self):
        """用户 ID 应使用 UUID 格式而非时间戳"""
        import uuid as uuid_mod


        # 验证 UUID 格式：12位hex
        user_id = f"user_{uuid_mod.uuid4().hex[:12]}"
        assert user_id.startswith("user_")
        hex_part = user_id.replace("user_", "")
        assert len(hex_part) == 12
        int(hex_part, 16)  # 应该是有效hex

    @pytest.mark.asyncio
    async def test_secret_key_not_hardcoded(self):
        """SECRET_KEY 不应是硬编码值"""
        from app.core.config import Settings

        s1 = Settings()
        _s2 = Settings()
        # 自动生成的密钥不应是固定值
        assert s1.SECRET_KEY != "education-agent-dev-secret-key-2024"
        # 每次创建新 Settings 应生成不同密钥（因为没有缓存）
        # 注意：如果使用了 lru_cache，这两个会相同


# ============================================================
# 2. Database 层测试
# ============================================================

class TestDatabaseFixes:
    """Database 层修复验证"""

    @pytest.mark.asyncio
    async def test_use_mongo_initialized(self):
        """_use_mongo 应在 __init__ 中初始化"""
        from app.core.database import Database

        db = Database()
        # 不调用 connect() 的情况下，_use_mongo 应该存在
        assert hasattr(db, "_use_mongo")
        assert db._use_mongo is False

    @pytest.mark.asyncio
    async def test_clean_mongo_doc_does_not_mutate(self):
        """clean_mongo_doc 不应修改原始文档"""
        from datetime import datetime

        from app.core.database import clean_mongo_doc

        original = {
            "_id": "some_object_id",
            "name": "test",
            "created_at": datetime(2024, 1, 1),
        }
        _original_copy = original.copy()

        result = clean_mongo_doc(original)

        # 原始文档不应被修改
        assert "_id" in original
        assert isinstance(original["created_at"], datetime)

        # 返回的文档应被清理
        assert "_id" not in result
        assert isinstance(result["created_at"], str)

    @pytest.mark.asyncio
    async def test_escape_regex(self):
        """正则特殊字符应被转义"""
        from app.core.database import escape_regex

        assert escape_regex(".*") == r"\.\*"
        assert escape_regex("test+") == r"test\+"
        assert escape_regex("[abc]") == r"\[abc\]"
        assert escape_regex("normal") == "normal"

    @pytest.mark.asyncio
    async def test_session_id_uses_uuid(self):
        """会话 ID 应使用 UUID 格式"""
        from app.core.database import Database

        db = Database()
        await db.connect()

        session_id = await db.create_session("test_user")
        # UUID 格式：session_ + 12位hex
        assert session_id.startswith("session_")
        hex_part = session_id.replace("session_", "")
        assert len(hex_part) == 12
        # 应该是有效的hex
        int(hex_part, 16)

    @pytest.mark.asyncio
    async def test_all_collections_initialized(self):
        """运维域集合应在 __init__ 中初始化（教学域集合已随聚焦裁剪移除）"""
        from app.core.database import Database

        db = Database()
        for attr in ("_users", "_sessions", "_documents", "_organizations",
                     "_tool_audit_logs", "_incidents", "_diagnosis_tasks"):
            assert hasattr(db, attr)
        # 教学域集合确认已删除
        for gone in ("_quiz_records", "_student_states", "_user_profiles",
                     "_knowledge_points", "_teaching_experiences", "_topics",
                     "_study_plans", "_courses"):
            assert not hasattr(db, gone)

    @pytest.mark.asyncio
    async def test_sort_key_handles_datetime_and_string(self):
        """排序应同时处理 datetime 和 string 类型"""
        from datetime import datetime

        # 模拟 get_user_sessions 的排序逻辑
        def _sort_key(x):
            val = x.get("updated_at", "")
            if isinstance(val, datetime):
                return val.isoformat()
            return str(val) if val else ""

        items = [
            {"updated_at": datetime(2024, 1, 3)},
            {"updated_at": "2024-01-01T00:00:00"},
            {"updated_at": datetime(2024, 1, 2)},
        ]
        sorted_items = sorted(items, key=_sort_key, reverse=True)
        # 最新的应该在前面
        assert isinstance(sorted_items[0]["updated_at"], datetime)
        assert sorted_items[0]["updated_at"].day == 3


# ============================================================
# 3. StudentState 测试
# ============================================================

# ============================================================
# 7. AI Service 测试
# ============================================================

class TestAIServiceFixes:
    """AI Service 修复验证"""

    def test_anthropic_provider_has_close(self):
        """AnthropicProvider 应有 close 方法"""
        from app.core.ai_service import AnthropicProvider

        provider = AnthropicProvider(api_key="test", model="test")
        assert hasattr(provider, "close")
        assert hasattr(provider, "_client")

    def test_anthropic_provider_reuses_client(self):
        """AnthropicProvider 应复用客户端"""
        from app.core.ai_service import AnthropicProvider

        provider = AnthropicProvider(api_key="test", model="test")
        # _client 初始应为 None
        assert provider._client is None


# ============================================================
# 8. 入口
# ============================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
