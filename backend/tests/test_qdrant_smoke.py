"""
Qdrant 后端冒烟测试

验证 QdrantVectorStore 与 ChromaDBVectorStore 接口一致性，能完成基本 CRUD + 搜索。
"""
import os
import sys
import tempfile
import shutil

# 添加 backend 到 path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.retrieval.embeddings import TFIDFModel, create_embedding_model
from app.retrieval.qdrant_store import QdrantVectorStore


def main():
    print("=" * 60)
    print("Qdrant 后端冒烟测试")
    print("=" * 60)

    tmpdir = tempfile.mkdtemp(prefix="qdrant_smoke_")
    print(f"临时目录: {tmpdir}")

    try:
        # 使用 TF-IDF（够快，避免加载 sentence-transformer）
        emb = TFIDFModel(max_features=100)
        # 先 fit
        docs_for_fit = [
            "Python 装饰器 decorator 用法",
            "FastAPI 路由 routing 教程",
            "Redis 缓存 cache 配置",
            "SQLAlchemy ORM 模型定义",
            "Celery 异步任务 broker 配置",
        ]
        emb.fit(docs_for_fit)
        print(f"TF-IDF fit 完成, dim={emb.dimension}")

        # 创建 Qdrant store
        store = QdrantVectorStore(
            embedding_model=emb,
            collection_name="smoke_test",
            persist_directory=tmpdir,
            hnsw_preset="small",
        )
        print(f"Qdrant 创建成功, size={store.size()}")

        # add_batch
        doc_ids = [f"doc_{i}" for i in range(5)]
        contents = docs_for_fit
        metadatas = [
            {"source": "test", "chunk_type": "child", "document_id": doc_ids[i]}
            for i in range(5)
        ]
        store.add_batch(doc_ids, contents, metadatas)
        print(f"add_batch 完成, size={store.size()}")

        # search（带 filter）
        results = store.search(
            query="装饰器怎么用",
            top_k=3,
            filters={"chunk_type": "child"},
        )
        print(f"\nsearch 结果 (query='装饰器怎么用', filter chunk_type=child):")
        for doc_id, score, meta in results:
            print(f"  - id={doc_id}, score={score:.4f}, content={meta.get('content', '')[:50]}")

        # search（无 filter）
        results2 = store.search(
            query="Redis 缓存",
            top_k=2,
            filters=None,
        )
        print(f"\nsearch 结果 (query='Redis 缓存', no filter):")
        for doc_id, score, meta in results2:
            print(f"  - id={doc_id}, score={score:.4f}")

        # get_by_ids
        records = store.get_by_ids(["doc_0", "doc_2"])
        print(f"\nget_by_ids 结果:")
        for r in records:
            print(f"  - id={r['id']}, content={r['content'][:40]}, meta={r['metadata']}")

        # get_by_document
        chunks = store.get_by_document("doc_1")
        print(f"\nget_by_document('doc_1') 结果: {len(chunks)} 条")

        # filter with $and
        results3 = store.search(
            query="FastAPI",
            top_k=3,
            filters={"$and": [{"chunk_type": "child"}, {"source": "test"}]},
        )
        print(f"\nsearch 结果 ($and filter): {len(results3)} 条")

        # size & clear
        print(f"\nfinal size: {store.size()}")
        store.clear()
        print(f"after clear: {store.size()}")

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
