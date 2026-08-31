"""
测试配置

核心职责：
1. 在导入 app 之前加载 .env.test 配置，确保测试用独立数据库
2. 提供测试 fixture（数据库自动清理、缓存重置、临时 Qdrant 目录）
3. 禁用 LLM 调用，避免单元测试依赖外部服务

使用方式：
    cd backend
    APP_ENV=test python -m pytest tests/    # 跑全部测试
    python -m pytest tests/test_sanitizer.py # 跑单个文件
    python -m pytest -k "test_phone"         # 按名称过滤
"""

import os
import sys

# 将 backend 目录加入 Python 路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ========== 在导入 app 之前设置测试环境 ==========
# 必须在 import app.* 之前执行，确保 Settings 单例读取的是测试配置
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENV", "test")
os.environ.setdefault("MONGODB_DB_NAME", "education_agent_test")
os.environ.setdefault("USE_LOCAL_EMBEDDING", "false")
os.environ.setdefault("INTENT_USE_LLM", "false")
os.environ.setdefault("USE_REACT", "false")
os.environ.setdefault("RERANKER_ENABLED", "false")
os.environ.setdefault("MULTIMODAL_ENABLED", "false")
os.environ.setdefault("RATE_LIMIT_RPM", "3")
# 注册角色不信任客户端输入；测试里 admin 统一通过该超管用户名注册获得
os.environ.setdefault("SUPER_ADMIN_USERNAME", "admin_root")
# 认证端点（登录/注册）按 IP 限流，测试内大量注册会触发，测试环境放大阈值
os.environ.setdefault("AUTH_RATE_LIMIT_PER_MIN", "100000")
# 禁用 Redis（优先级高于 .env）：避免每次限流/缓存操作先探测不存在的 Redis，
# 探测耗时会让时间敏感的窗口过期测试 flaky
os.environ.setdefault("REDIS_URL", "")

import asyncio

import pytest

from app.core.cache import reset_cache

# ========== 事件循环 fixture ==========

@pytest.fixture(scope="session")
def event_loop():
    """session 级别事件循环，避免 async fixture 跨 session 报错"""
    policy = asyncio.get_event_loop_policy()
    loop = policy.new_event_loop()
    yield loop
    loop.close()


# ========== 缓存重置 fixture ==========

@pytest.fixture(autouse=True)
def reset_cache_each_test():
    """每个测试前后重置缓存单例，确保测试隔离

    autouse=True 表示自动应用到所有测试，无需显式声明
    """
    reset_cache()
    yield
    reset_cache()


# ========== 测试数据库 fixture（集成测试用） ==========

@pytest.fixture
async def test_db():
    """提供测试数据库实例，测试后自动清空

    用法：
        async def test_something(test_db):
            await test_db.create_session("user1", "test")
            ...

    特点：
    - 使用 education_agent_test 数据库，不污染开发数据
    - 每个测试函数后自动 drop 整个测试数据库
    """
    from app.core.database import Database
    db = Database(
        url=os.environ.get("MONGODB_URL", "mongodb://localhost:27017"),
        db_name=os.environ.get("MONGODB_DB_NAME", "education_agent_test"),
    )
    await db.connect()
    yield db
    # 测试后清空测试数据库
    try:
        await db._mongo.client.drop_database("education_agent_test")
    except Exception:
        pass
    await db.close()
