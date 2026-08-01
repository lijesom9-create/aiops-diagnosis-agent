"""
Qdrant Vector Store - 基于 Qdrant 的向量存储

与 ChromaDBVectorStore 接口完全一致，支持作为后端替换。

特性：
- 支持 Local 模式（无需 Docker，单文件持久化）
- 支持 Server 模式（Qdrant 服务端）
- HNSW 索引配置（与 Chroma 同义）
- ChromaDB 风格 filter 兼容（自动转 Qdrant Filter）
- 内容哈希去重、批量处理
"""

import os
import hashlib
import uuid
import functools
import time
import sqlite3
from typing import List, Dict, Optional, Tuple, Set, Any

from loguru import logger

from .embeddings import EmbeddingModel
from .chroma_store import ChromaDBVectorStore  # 复用 clean_markdown


# ========== Qdrant 操作重试装饰器 ==========
# 只重试连接/锁相关异常（sqlite 锁冲突、网络断连），不重试逻辑错误
# 指数退避：0.5s → 1s → 2s，最多 3 次

_RETRYABLE_EXC = (
    sqlite3.OperationalError,  # local 模式：database is locked
    ConnectionError,           # server 模式：连接断开
    TimeoutError,              # 超时
    OSError,                   # 网络相关 IO 错误
)


def _retry_qdrant(max_retries: int = 3, base_delay: float = 0.5):
    """Qdrant 操作重试装饰器：只重试连接/锁相关异常"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except _RETRYABLE_EXC as e:
                    last_exc = e
                    if attempt < max_retries:
                        delay = base_delay * (2 ** attempt)
                        logger.warning(
                            f"Qdrant {func.__name__} 失败（第 {attempt + 1}/{max_retries} 次），"
                            f"{delay:.1f}s 后重试: {type(e).__name__}: {e}"
                        )
                        time.sleep(delay)
                    else:
                        logger.error(f"Qdrant {func.__name__} 重试 {max_retries} 次后仍失败: {e}")
                        raise
            raise last_exc  # type: ignore[misc]
        return wrapper
    return decorator


# ========== Local client 缓存 ==========
# Qdrant local 模式不允许两个 QdrantClient 实例访问同一目录。
# UnifiedKnowledgeStore 会创建子块和父块两个 store（同目录不同 collection），
# 因此需要复用同一 client 实例避免 "already accessed by another instance" 锁冲突。
_LOCAL_CLIENT_CACHE: Dict[str, Any] = {}


def _get_or_create_local_client(persist_dir: str) -> Any:
    """同一目录的 local client 复用，避免锁冲突"""
    from qdrant_client import QdrantClient

    abs_dir = os.path.abspath(persist_dir)
    if abs_dir not in _LOCAL_CLIENT_CACHE:
        os.makedirs(abs_dir, exist_ok=True)
        _LOCAL_CLIENT_CACHE[abs_dir] = QdrantClient(path=abs_dir)
        logger.debug(f"Qdrant local client 创建: {abs_dir}")
    return _LOCAL_CLIENT_CACHE[abs_dir]


class QdrantVectorStore:
    """
    Qdrant 向量存储

    与 ChromaDBVectorStore 接口对齐：
    - 同样的 add / add_batch / upsert / search / get_by_ids 等
    - 同样的 filters 风格（{"key": "value"} 或 {"$and": [...]}）
    - score 统一映射到 [0, 1] 区间（与 ChromaDB 一致）

    设计要点：
    - Qdrant 的 point id 必须是 UUID 或 uint64
      我们用 uuid5(NAMESPACE_DNS, doc_id) 把字符串 ID 转 UUID
    - payload 存全部 metadata + content + _original_id（原始 doc_id）
    - search 时返回的 metadata 不含 _original_id 与 content
    """

    # HNSW 配置预设（与 ChromaDBVectorStore 保持语义一致）
    HNSW_PRESETS = {
        "small": {"m": 16, "ef_construct": 100, "ef_search": 50},
        "medium": {"m": 32, "ef_construct": 200, "ef_search": 100},
        "large": {"m": 64, "ef_construct": 500, "ef_search": 200},
    }

    # 复用 ChromaDBVectorStore 的 Markdown 清理逻辑
    clean_markdown = staticmethod(ChromaDBVectorStore.clean_markdown)

    def __init__(
        self,
        embedding_model: EmbeddingModel,
        collection_name: str = "education_agent",
        persist_directory: Optional[str] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        hnsw_preset: str = "medium",
    ):
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, VectorParams

        self.embedding_model = embedding_model
        self.collection_name = collection_name
        self._dimension = embedding_model.dimension

        # 客户端配置
        if host and port:
            # 服务端 Qdrant
            self._client = QdrantClient(host=host, port=port)
            logger.info(f"连接远程 Qdrant: {host}:{port}")
        else:
            # 本地持久化 Qdrant（local mode，基于 sqlite + mmap）
            # 复用同一目录的 client 实例，避免父子 store 锁冲突
            persist_dir = persist_directory or os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                "data",
                "qdrant_db",
            )
            self._client = _get_or_create_local_client(persist_dir)
            logger.info(f"使用本地持久化 Qdrant: {persist_dir}")

        # HNSW 配置
        hnsw_config = self.HNSW_PRESETS.get(hnsw_preset, self.HNSW_PRESETS["medium"])

        # 获取或创建 collection
        try:
            self._client.get_collection(collection_name=collection_name)
            logger.info(f"Qdrant 集合已存在: {collection_name}")
        except Exception:
            # 集合不存在，创建
            self._client.create_collection(
                collection_name=collection_name,
                vectors_config=VectorParams(
                    size=self._dimension,
                    distance=Distance.COSINE,
                ),
                hnsw_config={
                    "m": hnsw_config["m"],
                    "ef_construct": hnsw_config["ef_construct"],
                    "full_scan_threshold": 10000,
                },
            )
            # 为常用过滤字段创建 payload 索引（加速过滤）
            self._ensure_payload_indexes(collection_name)
            logger.info(
                f"Qdrant 集合已创建: {collection_name}, dim={self._dimension}, HNSW: {hnsw_preset}"
            )

        logger.info(f"Qdrant 集合已就绪: {collection_name}, 当前记录数: {self.size()}")

    def _ensure_payload_indexes(self, collection_name: str) -> None:
        """为常用过滤字段创建 payload 索引"""
        from qdrant_client.models import PayloadSchemaType

        for field in ("chunk_type", "source", "user_id", "org_id",
                      "document_id", "topic_id", "parent_id"):
            try:
                self._client.create_payload_index(
                    collection_name=collection_name,
                    field_name=field,
                    field_schema=PayloadSchemaType.KEYWORD,
                )
            except Exception as e:
                logger.debug(f"创建 payload 索引 {field} 失败（可忽略）: {e}")

    # ========== ID 转换 ==========

    @staticmethod
    def _to_uuid(doc_id: str) -> str:
        """把字符串 doc_id 转换为 UUID 字符串"""
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, doc_id))

    # ========== Embedding ==========

    def _embed(self, content: str, clean_markdown: bool = True) -> List[float]:
        """生成文本向量（与 ChromaDBVectorStore._embed 行为一致）"""
        if clean_markdown:
            cleaned = self.clean_markdown(content)
        else:
            cleaned = content
        if not cleaned.strip():
            cleaned = content
        return self.embedding_model.embed(cleaned)

    # ========== 添加 ==========

    def add(
        self,
        doc_id: str,
        content: str,
        metadata: Optional[Dict] = None,
        clean_for_embedding: bool = True,
    ) -> None:
        """添加单条文档"""
        from qdrant_client.models import PointStruct

        meta = metadata or {}
        payload = {**meta, "content": content, "_original_id": doc_id}
        self._client.upsert(
            collection_name=self.collection_name,
            points=[
                PointStruct(
                    id=self._to_uuid(doc_id),
                    vector=self._embed(content, clean_for_embedding),
                    payload=payload,
                )
            ],
        )

    @_retry_qdrant()
    def add_batch(
        self,
        doc_ids: List[str],
        contents: List[str],
        metadatas: Optional[List[Dict]] = None,
        clean_for_embedding: bool = True,
    ) -> None:
        """批量添加"""
        from qdrant_client.models import PointStruct

        if not doc_ids:
            return

        metadatas = metadatas or [{}] * len(doc_ids)
        vectors = [self._embed(c, clean_for_embedding) for c in contents]
        points = []
        for doc_id, content, vector, meta in zip(doc_ids, contents, vectors, metadatas):
            payload = {**meta, "content": content, "_original_id": doc_id}
            points.append(
                PointStruct(
                    id=self._to_uuid(doc_id),
                    vector=vector,
                    payload=payload,
                )
            )
        self._client.upsert(
            collection_name=self.collection_name,
            points=points,
        )

    def add_with_vector(
        self,
        doc_id: str,
        vector: List[float],
        content: str,
        metadata: Optional[Dict] = None,
    ) -> None:
        """添加文档（使用预计算向量，不调用 embedding_model）

        用于多模态向量：CLIP 图像向量直接写入
        """
        from qdrant_client.models import PointStruct

        meta = metadata or {}
        payload = {**meta, "content": content, "_original_id": doc_id}
        self._client.upsert(
            collection_name=self.collection_name,
            points=[
                PointStruct(
                    id=self._to_uuid(doc_id),
                    vector=vector,
                    payload=payload,
                )
            ],
        )

    @_retry_qdrant()
    def search_by_vector(
        self,
        query_vector: List[float],
        top_k: int = 5,
        min_score: float = 0.0,
        filters: Optional[Dict] = None,
    ) -> List[Tuple[str, float, Dict]]:
        """用预计算向量做相似度搜索（不调用 embedding_model）

        用于多模态检索：CLIP 文本向量查 CLIP 图像向量库
        """
        qdrant_filter = self._convert_filter(filters)
        results = self._client.query_points(
            collection_name=self.collection_name,
            query=query_vector,
            limit=top_k,
            query_filter=qdrant_filter,
            with_payload=True,
            with_vectors=False,
        ).points

        output = []
        for p in results:
            payload = dict(p.payload or {})
            original_id = payload.pop("_original_id", "")
            content = payload.pop("content", "")
            similarity = (float(p.score) + 1.0) / 2.0  # 与 search() 一致
            if similarity >= min_score:
                output.append((
                    original_id,
                    similarity,
                    {**payload, "content": content},
                ))
        return output

    def upsert(
        self,
        doc_id: str,
        content: str,
        metadata: Optional[Dict] = None,
    ) -> None:
        """更新或插入（与 add 等价）"""
        self.add(doc_id, content, metadata, clean_for_embedding=False)

    def upsert_batch(
        self,
        doc_ids: List[str],
        contents: List[str],
        metadatas: Optional[List[Dict]] = None,
    ) -> None:
        """批量更新或插入"""
        self.add_batch(doc_ids, contents, metadatas, clean_for_embedding=False)

    # ========== 去重与查询 ==========

    @staticmethod
    def get_content_hash(content: str) -> str:
        return hashlib.md5(content.encode()).hexdigest()[:16]

    def generate_id_with_hash(self, document_id: str, chunk_index: int, content: str) -> str:
        content_hash = self.get_content_hash(content)
        return f"{document_id}_{chunk_index}_{content_hash}"

    def get_existing_ids(self, doc_ids: List[str]) -> Set[str]:
        """获取已存在的 ID 集合"""
        if not doc_ids:
            return set()
        from qdrant_client.models import PointIdsList

        existing = set()
        batch_size = 1000
        for i in range(0, len(doc_ids), batch_size):
            batch = doc_ids[i:i + batch_size]
            uuids = [self._to_uuid(d) for d in batch]
            try:
                # 仅取 id 字段，避免拉取 payload
                points, _ = self._client.scroll(
                    collection_name=self.collection_name,
                    scroll_filter=None,
                    limit=len(uuids),
                    with_payload=False,
                    with_vectors=False,
                )
                # local 模式 scroll 不能按 ids 直接过滤，改用 retrieve
                retrieved = self._client.retrieve(
                    collection_name=self.collection_name,
                    ids=uuids,
                    with_payload=False,
                    with_vectors=False,
                )
                # 用 _original_id 反查原始 doc_id
                # retrieve 返回的 point.payload 在 with_payload=False 时为 None
                # 我们需要在 scroll 时拿 _original_id，但 retrieve 不能用 ids+with_payload
                # 改用 client.scroll + must-filter
                existing_uuids = {str(p.id) for p in retrieved}
                # 把 uuid 反向映射回原始 doc_id
                for d in batch:
                    if self._to_uuid(d) in existing_uuids:
                        existing.add(d)
            except Exception as e:
                logger.warning(f"查询已存在 ID 失败: {e}")
                return set()
        return existing

    def _get_content_by_id(self, doc_id: str) -> Optional[str]:
        """根据 ID 获取内容"""
        try:
            points = self._client.retrieve(
                collection_name=self.collection_name,
                ids=[self._to_uuid(doc_id)],
                with_payload=True,
                with_vectors=False,
            )
            if points:
                return points[0].payload.get("content")
            return None
        except Exception:
            return None

    def get_by_ids(self, doc_ids: List[str]) -> List[Dict]:
        """批量根据 ID 获取记录

        返回 [{"id", "content", "metadata"}, ...]
        与 ChromaDBVectorStore.get_by_ids 完全一致
        """
        if not doc_ids:
            return []
        try:
            uuids = [self._to_uuid(d) for d in doc_ids]
            points = self._client.retrieve(
                collection_name=self.collection_name,
                ids=uuids,
                with_payload=True,
                with_vectors=False,
            )
            # Qdrant retrieve 不保证顺序，按 doc_ids 顺序重新排列
            uuid_to_point = {str(p.id): p for p in points}
            output = []
            for doc_id in doc_ids:
                p = uuid_to_point.get(self._to_uuid(doc_id))
                if p is None:
                    continue
                payload = dict(p.payload or {})
                content = payload.pop("content", "")
                payload.pop("_original_id", None)
                output.append({
                    "id": doc_id,
                    "content": content,
                    "metadata": payload,
                })
            return output
        except Exception as e:
            logger.error(f"批量获取记录失败: {e}")
            return []

    def get_by_document(self, document_id: str) -> List[Dict]:
        """获取文档的所有分块"""
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        try:
            points, _ = self._client.scroll(
                collection_name=self.collection_name,
                scroll_filter=Filter(
                    must=[
                        FieldCondition(
                            key="document_id",
                            match=MatchValue(value=document_id),
                        )
                    ]
                ),
                limit=10000,
                with_payload=True,
                with_vectors=False,
            )
            chunks = []
            for p in points:
                payload = dict(p.payload or {})
                content = payload.pop("content", "")
                original_id = payload.pop("_original_id", "")
                chunks.append({
                    "id": original_id,
                    "content": content,
                    "metadata": payload,
                })
            return chunks
        except Exception as e:
            logger.error(f"获取文档分块失败: {e}")
            return []

    def add_with_dedup(
        self,
        doc_ids: List[str],
        contents: List[str],
        metadatas: Optional[List[Dict]] = None,
        batch_size: int = 100,
        use_content_hash: bool = True,
    ) -> Dict[str, int]:
        """带去重的批量添加（与 ChromaDBVectorStore 接口一致）"""
        stats = {"added": 0, "skipped": 0, "failed": 0, "updated": 0}

        if not doc_ids:
            return stats

        if use_content_hash:
            doc_ids = [
                self.generate_id_with_hash(doc_id, i, content)
                for i, (doc_id, content) in enumerate(zip(doc_ids, contents))
            ]

        existing_ids = self.get_existing_ids(doc_ids)

        to_add_ids, to_add_contents, to_add_metas = [], [], []
        to_update_ids, to_update_contents, to_update_metas = [], [], []

        for i, (doc_id, content) in enumerate(zip(doc_ids, contents)):
            meta = metadatas[i] if metadatas else {}

            if doc_id in existing_ids:
                existing_content = self._get_content_by_id(doc_id)
                if existing_content != content:
                    to_update_ids.append(doc_id)
                    to_update_contents.append(content)
                    to_update_metas.append(meta)
                    stats["updated"] += 1
                else:
                    stats["skipped"] += 1
            else:
                to_add_ids.append(doc_id)
                to_add_contents.append(content)
                to_add_metas.append(meta)

        # 批量新增
        if to_add_ids:
            for i in range(0, len(to_add_ids), batch_size):
                b_ids = to_add_ids[i:i + batch_size]
                b_contents = to_add_contents[i:i + batch_size]
                b_metas = to_add_metas[i:i + batch_size]
                try:
                    vectors = self.embedding_model.embed_batch(b_contents)
                    valid_idx = [j for j, v in enumerate(vectors) if any(x != 0 for x in v)]
                    if valid_idx:
                        self._upsert_points(
                            [b_ids[j] for j in valid_idx],
                            [b_contents[j] for j in valid_idx],
                            [b_metas[j] for j in valid_idx],
                            [vectors[j] for j in valid_idx],
                        )
                        stats["added"] += len(valid_idx)
                    failed = len(b_ids) - len(valid_idx)
                    if failed > 0:
                        stats["failed"] += failed
                except Exception as e:
                    logger.error(f"批量添加失败: {e}")
                    stats["failed"] += len(b_ids)

        # 批量更新
        if to_update_ids:
            for i in range(0, len(to_update_ids), batch_size):
                b_ids = to_update_ids[i:i + batch_size]
                b_contents = to_update_contents[i:i + batch_size]
                b_metas = to_update_metas[i:i + batch_size]
                try:
                    vectors = self.embedding_model.embed_batch(b_contents)
                    self._upsert_points(b_ids, b_contents, b_metas, vectors)
                except Exception as e:
                    logger.error(f"批量更新失败: {e}")
                    stats["failed"] += len(b_ids)

        logger.info(f"Qdrant 批量添加完成: {stats}")
        return stats

    @_retry_qdrant()
    def _upsert_points(
        self,
        doc_ids: List[str],
        contents: List[str],
        metadatas: List[Dict],
        vectors: List[List[float]],
    ) -> None:
        """辅助：构造 PointStruct 批量 upsert"""
        from qdrant_client.models import PointStruct

        points = []
        for doc_id, content, vector, meta in zip(doc_ids, contents, vectors, metadatas):
            payload = {**meta, "content": content, "_original_id": doc_id}
            points.append(
                PointStruct(
                    id=self._to_uuid(doc_id),
                    vector=vector,
                    payload=payload,
                )
            )
        self._client.upsert(collection_name=self.collection_name, points=points)

    # ========== 检索 ==========

    @staticmethod
    def _convert_filter(filters: Optional[Dict]) -> Optional[Any]:
        """把 ChromaDB 风格 filter 转成 Qdrant Filter

        支持：
        - None -> None
        - {"key": "value"} -> Filter(must=[FieldCondition(key=key, match=MatchValue(value=value))])
        - {"$and": [{"key": "value"}, ...]} -> Filter(must=[...])
        """
        if not filters:
            return None
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        conditions = []

        def _parse(clause: Dict):
            for k, v in clause.items():
                if k == "$and":
                    for sub in v:
                        _parse(sub)
                elif k == "$or":
                    # 转为 should
                    pass  # 当前项目未使用 $or，留作扩展
                else:
                    conditions.append(
                        FieldCondition(key=k, match=MatchValue(value=v))
                    )

        _parse(filters)
        if not conditions:
            return None
        return Filter(must=conditions)

    @_retry_qdrant()
    def search(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.0,
        filters: Optional[Dict] = None,
    ) -> List[Tuple[str, float, Dict]]:
        """相似度搜索

        返回 [(doc_id, score, metadata_with_content), ...]
        与 ChromaDBVectorStore.search 完全一致

        score 在 [0, 1] 区间（与 ChromaDB 一致，cosine similarity 不做负值截断）
        """
        query_vector = self.embedding_model.embed(query)
        qdrant_filter = self._convert_filter(filters)

        results = self._client.query_points(
            collection_name=self.collection_name,
            query=query_vector,
            limit=top_k,
            query_filter=qdrant_filter,
            with_payload=True,
            with_vectors=False,
        ).points

        output = []
        for p in results:
            payload = dict(p.payload or {})
            original_id = payload.pop("_original_id", "")
            content = payload.pop("content", "")
            # Qdrant cosine similarity 直接是 score
            similarity = float(p.score)
            # 与 ChromaDB 保持一致：cosine similarity 映射到 [0, 1]
            # ChromaDB: similarity = 1 - distance/2, distance ∈ [0, 2]
            # 等价 cosine similarity ∈ [-1, 1] 映射到 [0, 1]
            similarity = (similarity + 1.0) / 2.0
            if similarity >= min_score:
                output.append((
                    original_id,
                    similarity,
                    {**payload, "content": content},
                ))
        return output

    # ========== 删除 ==========

    def delete_by_document(self, document_id: str) -> int:
        """删除文档的所有分块"""
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        try:
            self._client.delete(
                collection_name=self.collection_name,
                points_selector=Filter(
                    must=[
                        FieldCondition(
                            key="document_id",
                            match=MatchValue(value=document_id),
                        )
                    ]
                ),
            )
            logger.info(f"删除文档 {document_id} 的所有分块")
            return -1
        except Exception as e:
            logger.error(f"删除文档失败: {e}")
            return 0

    def delete(self, doc_id: Optional[str] = None, where: Optional[Dict] = None) -> bool:
        """删除文档

        支持按 doc_id 删除或按元数据过滤条件删除
        """
        from qdrant_client.models import PointIdsList, Filter

        try:
            if doc_id:
                self._client.delete(
                    collection_name=self.collection_name,
                    points_selector=PointIdsList(points=[self._to_uuid(doc_id)]),
                )
                return True
            if where:
                qdrant_filter = self._convert_filter(where)
                if qdrant_filter is None:
                    logger.warning("delete where 条件为空")
                    return False
                self._client.delete(
                    collection_name=self.collection_name,
                    points_selector=qdrant_filter,
                )
                return True
            logger.warning("delete 未提供 doc_id 或 where 条件")
            return False
        except Exception as e:
            logger.warning(f"删除 Qdrant 文档失败: {e}")
            return False

    def delete_by_filter(self, filters: Dict) -> int:
        """按过滤条件删除"""
        from qdrant_client.models import Filter

        try:
            qdrant_filter = self._convert_filter(filters)
            if qdrant_filter is None:
                return 0
            self._client.delete(
                collection_name=self.collection_name,
                points_selector=qdrant_filter,
            )
            return -1
        except Exception as e:
            logger.warning(f"按条件删除 Qdrant 文档失败: {e}")
            return 0

    def delete_batch(self, doc_ids: List[str]) -> int:
        """批量删除分块"""
        from qdrant_client.models import PointIdsList

        if not doc_ids:
            return 0
        try:
            self._client.delete(
                collection_name=self.collection_name,
                points_selector=PointIdsList(
                    points=[self._to_uuid(d) for d in doc_ids]
                ),
            )
            logger.info(f"批量删除 {len(doc_ids)} 个分块")
            return len(doc_ids)
        except Exception as e:
            logger.error(f"批量删除失败: {e}")
            return 0

    # ========== 全量查询 ==========

    def get_all(
        self,
        limit: int = 10000,
        include: Optional[List[str]] = None,
    ) -> Dict:
        """获取集合中的所有记录

        返回格式与 ChromaDBVectorStore.get_all 一致：
        {"ids": [...], "documents": [...], "metadatas": [...]}
        """
        try:
            include = include or ["metadatas", "documents"]
            points, _ = self._client.scroll(
                collection_name=self.collection_name,
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )
            ids, docs, metas = [], [], []
            for p in points:
                payload = dict(p.payload or {})
                original_id = payload.pop("_original_id", "")
                content = payload.pop("content", "")
                ids.append(original_id)
                docs.append(content if "documents" in include else None)
                metas.append(payload if "metadatas" in include else None)
            return {
                "ids": ids,
                "documents": docs,
                "metadatas": metas,
                "embeddings": [],
            }
        except Exception as e:
            logger.warning(f"获取 Qdrant 全部记录失败: {e}")
            return {"ids": [], "documents": [], "metadatas": [], "embeddings": []}

    def size(self) -> int:
        """记录数量"""
        try:
            return self._client.count(
                collection_name=self.collection_name,
                exact=True,
            ).count
        except Exception:
            return 0

    def clear(self) -> None:
        """清空集合"""
        try:
            self._client.delete_collection(self.collection_name)
            from qdrant_client.models import Distance, VectorParams
            self._client.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(
                    size=self._dimension,
                    distance=Distance.COSINE,
                ),
            )
            self._ensure_payload_indexes(self.collection_name)
        except Exception as e:
            logger.warning(f"清空 Qdrant 集合失败: {e}")

    # ========== 更新与增量 ==========

    def update_document(
        self,
        document_id: str,
        doc_ids: List[str],
        contents: List[str],
        metadatas: Optional[List[Dict]] = None,
        batch_size: int = 100,
    ) -> Dict[str, int]:
        """更新文档（先删除再添加）"""
        stats = {"deleted": 0, "added": 0, "failed": 0}

        self.delete_by_document(document_id)
        stats["deleted"] = -1

        if doc_ids:
            for i in range(0, len(doc_ids), batch_size):
                b_ids = doc_ids[i:i + batch_size]
                b_contents = contents[i:i + batch_size]
                b_metas = metadatas[i:i + batch_size] if metadatas else [{}] * len(b_ids)
                try:
                    vectors = self.embedding_model.embed_batch(b_contents)
                    valid_idx = [j for j, v in enumerate(vectors) if any(x != 0 for x in v)]
                    if valid_idx:
                        self._upsert_points(
                            [b_ids[j] for j in valid_idx],
                            [b_contents[j] for j in valid_idx],
                            [b_metas[j] for j in valid_idx],
                            [vectors[j] for j in valid_idx],
                        )
                        stats["added"] += len(valid_idx)
                    failed = len(b_ids) - len(valid_idx)
                    if failed > 0:
                        stats["failed"] += failed
                except Exception as e:
                    logger.error(f"批量添加失败: {e}")
                    stats["failed"] += len(b_ids)

        logger.info(f"文档更新完成: {stats}")
        return stats

    def incremental_update(
        self,
        document_id: str,
        new_chunks: List[Dict],
        batch_size: int = 100,
    ) -> Dict[str, int]:
        """增量更新文档（与 ChromaDBVectorStore 接口一致）"""
        stats = {"added": 0, "deleted": 0, "updated": 0, "unchanged": 0, "failed": 0}

        old_chunks = self.get_by_document(document_id)
        old_chunk_map = {}
        for chunk in old_chunks:
            chunk_index = chunk["metadata"].get("chunk_index")
            if chunk_index is not None:
                old_chunk_map[chunk_index] = chunk

        new_chunk_map = {}
        for i, chunk in enumerate(new_chunks):
            chunk_index = chunk.get("metadata", {}).get("chunk_index", i)
            new_chunk_map[chunk_index] = chunk

        to_add, to_update, to_delete = [], [], []

        for idx, new_chunk in new_chunk_map.items():
            if idx not in old_chunk_map:
                to_add.append(new_chunk)
            else:
                old_chunk = old_chunk_map[idx]
                old_hash = old_chunk["metadata"].get("content_hash", "")
                new_hash = self.get_content_hash(new_chunk.get("text", ""))
                if old_hash != new_hash:
                    to_delete.append(old_chunk["id"])
                    to_update.append(new_chunk)
                else:
                    stats["unchanged"] += 1

        for idx, old_chunk in old_chunk_map.items():
            if idx not in new_chunk_map:
                to_delete.append(old_chunk["id"])

        if to_delete:
            for i in range(0, len(to_delete), batch_size):
                batch = to_delete[i:i + batch_size]
                try:
                    self.delete_batch(batch)
                    stats["deleted"] += len(batch)
                except Exception as e:
                    logger.error(f"批量删除失败: {e}")
                    stats["failed"] += len(batch)

        all_to_add = to_add + to_update
        if all_to_add:
            for i in range(0, len(all_to_add), batch_size):
                batch = all_to_add[i:i + batch_size]
                batch_ids = [c.get("id", f"{document_id}_chunk_{i + j}") for j, c in enumerate(batch)]
                batch_contents = [c.get("text", "") for c in batch]
                batch_metas = [c.get("metadata", {}) for c in batch]
                for meta, content in zip(batch_metas, batch_contents):
                    meta["content_hash"] = self.get_content_hash(content)
                try:
                    vectors = self.embedding_model.embed_batch(batch_contents)
                    valid_idx = [j for j, v in enumerate(vectors) if any(x != 0 for x in v)]
                    if valid_idx:
                        self._upsert_points(
                            [batch_ids[j] for j in valid_idx],
                            [batch_contents[j] for j in valid_idx],
                            [batch_metas[j] for j in valid_idx],
                            [vectors[j] for j in valid_idx],
                        )
                        stats["added"] += len([to_add[j] for j in valid_idx if j < len(to_add)])
                        stats["updated"] += len([to_update[j] for j in valid_idx if j >= len(to_add)])
                    failed = len(batch) - len(valid_idx)
                    if failed > 0:
                        stats["failed"] += failed
                except Exception as e:
                    logger.error(f"批量添加失败: {e}")
                    stats["failed"] += len(batch)

        logger.info(f"Qdrant 增量更新完成: {stats}")
        return stats
