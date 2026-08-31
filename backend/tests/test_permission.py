"""
权限隔离集成测试

测试核心安全特性：用户只能检索到公共文档 + 本人私有文档

测试覆盖：
- 用户 A 检索不到用户 B 的私有文档
- 用户 B 检索不到用户 A 的私有文档
- 不带 user_id 检索返回所有文档
- 公共文档（无 user_id）所有用户可见
- 组织隔离（org_id）
"""

import pytest

from app.knowledge.unified_store import KnowledgeItem, UnifiedKnowledgeStore
from app.retrieval.embeddings import TFIDFModel


@pytest.fixture
def knowledge_store(tmp_path):
    """创建临时知识库实例（用 TFIDFModel 轻量 embedding + chroma 临时目录）"""
    store = UnifiedKnowledgeStore(
        embedding_model=TFIDFModel(max_features=100),
        collection_name="test_permission",
        persist_directory=str(tmp_path / "chroma"),
        vector_store_backend="chroma",
        separate_parent_child=False,  # 简化测试：父子同库
    )
    yield store


def _make_item(item_id, title, content, user_id=None, org_id=None):
    """构造 KnowledgeItem，user_id 放 metadata"""
    metadata = {}
    if user_id:
        metadata["user_id"] = user_id
    return KnowledgeItem(
        id=item_id,
        title=title,
        content=content,
        source="user_document",
        metadata=metadata,
        org_id=org_id or "",
    )


class TestUserIsolation:
    """用户隔离：私有文档不可被其他用户检索"""

    def test_user_cannot_see_others_private_docs(self, knowledge_store):
        """用户 A 检索不到用户 B 的私有文档"""
        # 用户 A 的私有文档
        knowledge_store.add(_make_item(
            "doc_a_1", "A的文档", "FastAPI 路由定义使用 @app.get 装饰器",
            user_id="userA",
        ))
        # 用户 B 的私有文档
        knowledge_store.add(_make_item(
            "doc_b_1", "B的文档", "数据库备份使用 mysqldump 命令导出",
            user_id="userB",
        ))

        # 用户 A 检索，只能看到自己的文档
        results_a = knowledge_store.search("FastAPI", user_id="userA", min_score=0.0)
        user_ids = {r["metadata"].get("user_id") for r in results_a}
        assert "userB" not in user_ids, "用户 A 不应看到用户 B 的文档"
        assert "userA" in user_ids or None in user_ids, "用户 A 应看到自己的文档"

    def test_user_b_cannot_see_user_a_docs(self, knowledge_store):
        """用户 B 检索不到用户 A 的私有文档"""
        knowledge_store.add(_make_item(
            "doc_a_1", "A的文档", "FastAPI 路由定义使用装饰器",
            user_id="userA",
        ))
        knowledge_store.add(_make_item(
            "doc_b_1", "B的文档", "数据库备份使用 mysqldump 命令",
            user_id="userB",
        ))

        results_b = knowledge_store.search("数据库", user_id="userB", min_score=0.0)
        user_ids = {r["metadata"].get("user_id") for r in results_b}
        assert "userA" not in user_ids, "用户 B 不应看到用户 A 的文档"

    def test_no_user_id_returns_all(self, knowledge_store):
        """不带 user_id 检索返回所有文档（管理员视角）"""
        knowledge_store.add(_make_item(
            "doc_a_1", "A的文档", "FastAPI 路由定义",
            user_id="userA",
        ))
        knowledge_store.add(_make_item(
            "doc_b_1", "B的文档", "数据库备份命令",
            user_id="userB",
        ))

        results = knowledge_store.search("文档", min_score=0.0)
        user_ids = {r["metadata"].get("user_id") for r in results}
        assert "userA" in user_ids, "无 user_id 时应返回 A 的文档"
        assert "userB" in user_ids, "无 user_id 时应返回 B 的文档"


class TestPublicDocumentVisibility:
    """公共文档（无 user_id）对所有用户可见"""

    def test_public_doc_visible_to_all_users(self, knowledge_store):
        """公共文档对所有用户可见"""
        # 公共文档（无 user_id）
        knowledge_store.add(_make_item(
            "doc_public", "公共手册", "公司技术规范手册 FastAPI 开发指南",
        ))
        # 用户 A 的私有文档
        knowledge_store.add(_make_item(
            "doc_a_1", "A的笔记", "个人学习笔记 FastAPI",
            user_id="userA",
        ))

        # 用户 A 检索：应看到公共 + 自己的
        results_a = knowledge_store.search("FastAPI", user_id="userA", min_score=0.0)
        doc_ids = {r["id"] for r in results_a}
        assert "doc_public" in doc_ids, "用户 A 应看到公共文档"

        # 用户 B 检索：应看到公共，但看不到 A 的私有
        results_b = knowledge_store.search("FastAPI", user_id="userB", min_score=0.0)
        doc_ids_b = {r["id"] for r in results_b}
        assert "doc_public" in doc_ids_b, "用户 B 应看到公共文档"
        assert "doc_a_1" not in doc_ids_b, "用户 B 不应看到 A 的私有文档"


class TestOrgIsolation:
    """组织隔离"""

    def test_org_isolation(self, knowledge_store):
        """不同组织的数据互相隔离"""
        knowledge_store.add(_make_item(
            "doc_org1", "组织1文档", "FastAPI 开发指南",
            org_id="org1",
        ))
        knowledge_store.add(_make_item(
            "doc_org2", "组织2文档", "FastAPI 部署手册",
            org_id="org2",
        ))

        # 组织1 检索，不应看到组织2 的文档
        results = knowledge_store.search("FastAPI", org_id="org1", min_score=0.0)
        org_ids = {r["metadata"].get("org_id") for r in results}
        assert "org2" not in org_ids, "组织1 不应看到组织2 的文档"
        assert "org1" in org_ids, "组织1 应看到自己的文档"
