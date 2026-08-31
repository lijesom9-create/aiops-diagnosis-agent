"""
Qdrant 迁移脚本：本地嵌入模式 → Server 模式
读取本地 qdrant_db 的所有向量，上传到 Qdrant Server
"""
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct

LOCAL_PATH = "/app/data/qdrant_db"
SERVER_HOST = "qdrant"
SERVER_PORT = 6333
BATCH_SIZE = 500

def migrate():
    local = QdrantClient(path=LOCAL_PATH)
    server = QdrantClient(host=SERVER_HOST, port=SERVER_PORT)

    local_cols = local.get_collections()
    print(f"本地集合: {[c.name for c in local_cols.collections]}")
    print(f"Server 集合: {[c.name for c in server.get_collections().collections]}")
    print()

    for col in local_cols.collections:
        name = col.name
        local_info = local.get_collection(name)
        count = local_info.points_count or 0
        print(f"=== 迁移集合: {name} ({count} points) ===")

        if count == 0:
            print("  跳过（空集合）")
            continue

        # 读取本地向量配置
        local_config = local_info.config
        vector_size = local_config.params.vectors.size
        distance = local_config.params.vectors.distance
        print(f"  维度: {vector_size}, 距离: {distance}")

        # 确保_server 有对应集合（backend 已创建，但确认一下）
        try:
            server.get_collection(name)
            print("  Server 集合已存在")
        except Exception:
            from qdrant_client.models import Distance, VectorParams
            server.create_collection(
                collection_name=name,
                vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
            )
            print("  Server 集合已创建")

        # 分批读取并上传
        offset = None
        total_migrated = 0
        while True:
            records, offset = local.scroll(
                collection_name=name,
                limit=BATCH_SIZE,
                offset=offset,
                with_payload=True,
                with_vectors=True,
            )
            if not records:
                break

            # 上传到 server（Record → PointStruct 转换）
            points = [
                PointStruct(id=r.id, vector=r.vector, payload=r.payload)
                for r in records
            ]
            server.upsert(
                collection_name=name,
                points=points,
            )
            total_migrated += len(records)
            print(f"  已迁移: {total_migrated}/{count}")

            if offset is None:
                break

        # 验证
        server_count = server.get_collection(name).points_count or 0
        print(f"  完成! Server 端: {server_count} points")
        assert server_count == count, f"数量不匹配: local={count} server={server_count}"
        print()

    print("=== 全部迁移完成 ===")

if __name__ == "__main__":
    migrate()
