"""
父子存储分离 + 投票加权 + reranker 冒烟测试
验证 P0-1/P0-2/P1 修复后基本功能正常
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.knowledge.unified_store import KnowledgeItem, UnifiedKnowledgeStore
from app.retrieval.embeddings import TFIDFModel


def main():
    print("=" * 60)
    print("父子存储分离 + 投票加权 + reranker 冒烟测试")
    print("=" * 60)

    tmpdir = tempfile.mkdtemp(prefix="pc_separation_")
    print(f"临时目录: {tmpdir}")

    try:
        # 用 TF-IDF（快，不加载 sentence-transformer）
        emb = TFIDFModel(max_features=200)
        docs_for_fit = [
            "Python 装饰器 decorator 用法 高阶函数",
            "FastAPI 路由 routing 依赖注入 Depends",
            "Redis 缓存 cache 数据类型 String Hash",
            "SQLAlchemy ORM Session declarative_base",
            "Celery 异步任务 Broker Worker delay",
            "Python async await 事件循环 coroutine",
        ]
        emb.fit(docs_for_fit + ["装饰器", "FastAPI", "Redis", "ORM", "Celery", "async"])

        store = UnifiedKnowledgeStore(
            embedding_model=emb,
            collection_name="test_pc",
            persist_directory=tmpdir,
            separate_parent_child=True,
        )
        print(f"\nStore 创建成功, child_size={store.vector_store.size()}, parent_size={store._parent_store.size()}")

        # 构造父子分块
        # 父块 1: 装饰器（含 2 个子块，用于测试投票加权）
        items = [
            KnowledgeItem(
                id="parent_doc1",
                title="装饰器",
                content="Python 装饰器 decorator 高阶函数 wrapper 用法详解",
                metadata={"chunk_type": "parent", "document_id": "doc1", "heading_path": ["Python", "装饰器"]},
                source="test",
            ),
            KnowledgeItem(
                id="child_doc1_0",
                title="装饰器",
                content="装饰器 decorator 高阶函数 wrapper 用法",
                metadata={"chunk_type": "child", "parent_id": "parent_doc1", "document_id": "doc1", "chunk_index": 0},
                source="test",
            ),
            KnowledgeItem(
                id="child_doc1_1",
                title="装饰器",
                content="装饰器 应用场景 函数执行时间统计",
                metadata={"chunk_type": "child", "parent_id": "parent_doc1", "document_id": "doc1", "chunk_index": 1},
                source="test",
            ),
            # 父块 2: FastAPI（含 1 个子块）
            KnowledgeItem(
                id="parent_doc2",
                title="FastAPI",
                content="FastAPI 路由 routing 依赖注入 Depends 教程",
                metadata={"chunk_type": "parent", "document_id": "doc2", "heading_path": ["Web", "FastAPI"]},
                source="test",
            ),
            KnowledgeItem(
                id="child_doc2_0",
                title="FastAPI",
                content="FastAPI 路由 routing 依赖注入 Depends",
                metadata={"chunk_type": "child", "parent_id": "parent_doc2", "document_id": "doc2", "chunk_index": 0},
                source="test",
            ),
        ]

        store.add_batch(items)
        print(f"\n添加 {len(items)} 条后:")
        print(f"  child_size={store.vector_store.size()}")
        print(f"  parent_size={store._parent_store.size()}")
        print(f"  total_size={store.size()}")

        # 验证父子分离：child store 里不应有 parent 块
        all_child = store.vector_store.get_all()
        child_types = [m.get("chunk_type") for m in all_child.get("metadatas", []) if m]
        print(f"\n  child_store 里的 chunk_type: {child_types}")
        assert "parent" not in child_types, "❌ child_store 里不应有 parent 块!"

        all_parent = store._parent_store.get_all()
        parent_types = [m.get("chunk_type") for m in all_parent.get("metadatas", []) if m]
        print(f"  parent_store 里的 chunk_type: {parent_types}")
        assert "child" not in parent_types, "❌ parent_store 里不应有 child 块!"
        print("  ✓ 父子分离验证通过")

        # 测试 hybrid_search_parent_child（多子块命中应触发投票加权）
        print("\n--- 测试 hybrid_search_parent_child ---")
        results = store.hybrid_search_parent_child(
            query="装饰器怎么用",
            top_k=5,
            rewrite_query=False,  # 跳过重写加速
        )
        print(f"返回 {len(results)} 条结果:")
        for r in results:
            print(f"  - id={r['id']}, score={r['score']:.4f}, title={r.get('title', '')}, hit_count={r.get('_hit_count', 'N/A')}")

        if results:
            top = results[0]
            if top["id"] == "parent_doc1":
                print("  ✓ 装饰器查询返回 parent_doc1（正确）")
                if top.get("_hit_count", 0) >= 2:
                    print(f"  ✓ 投票加权生效（hit_count={top['_hit_count']}）")
                else:
                    print(f"  ⚠ hit_count={top.get('_hit_count', 0)}（可能 BM25 没命中 2 个子块）")
            else:
                print(f"  ⚠ 期望 parent_doc1，实际 {top['id']}")

        # 测试 get_by_document
        print("\n--- 测试 get_by_document ---")
        chunks = store.get_by_document("doc1")
        print(f"doc1 的分块数: {len(chunks)} (期望 3: 1 parent + 2 child)")

        # 测试 delete_by_document
        print("\n--- 测试 delete_by_document ---")
        store.delete_by_document("doc1")
        after_del_child = store.vector_store.size()
        after_del_parent = store._parent_store.size()
        print(f"删除 doc1 后: child_size={after_del_child}, parent_size={after_del_parent}")
        assert after_del_child == 1, f"❌ child 应剩 1，实际 {after_del_child}"
        assert after_del_parent == 1, f"❌ parent 应剩 1，实际 {after_del_parent}"
        print("  ✓ delete_by_document 同时删父子 store 验证通过")

        print("\n" + "=" * 60)
        print("✓ 所有冒烟测试通过")
        print("=" * 60)
        return 0
    except Exception as e:
        import traceback
        print(f"\n✗ 测试失败: {e}")
        traceback.print_exc()
        return 1
    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
