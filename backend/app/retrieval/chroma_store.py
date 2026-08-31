"""
ChromaDB Vector Store - 基于 ChromaDB 的向量存储

替代自研内存 VectorStore，提供企业级向量检索能力。
"""

import hashlib
import os
import re
from typing import Dict, List, Optional, Set, Tuple

from loguru import logger

from .embeddings import EmbeddingModel


class ChromaDBVectorStore:
    """
    ChromaDB 向量存储

    特性：
    - 持久化存储（服务重启不丢失）
    - HNSW 近似最近邻索引
    - 支持元数据过滤
    - 支持内容哈希去重
    - 支持批量分块处理
    """

    # HNSW 配置预设
    HNSW_PRESETS = {
        "small": {  # < 10K 文档
            "hnsw:space": "cosine",
            "hnsw:M": 16,
            "hnsw:construction_ef": 100,
            "hnsw:search_ef": 50,
        },
        "medium": {  # 10K - 100K 文档
            "hnsw:space": "cosine",
            "hnsw:M": 32,
            "hnsw:construction_ef": 200,
            "hnsw:search_ef": 100,
        },
        "large": {  # > 100K 文档
            "hnsw:space": "cosine",
            "hnsw:M": 64,
            "hnsw:construction_ef": 500,
            "hnsw:search_ef": 200,
        },
    }

    def __init__(
        self,
        embedding_model: EmbeddingModel,
        collection_name: str = "education_agent",
        persist_directory: Optional[str] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        hnsw_preset: str = "medium",  # small, medium, large
    ):
        import chromadb
        from chromadb.config import Settings

        self.embedding_model = embedding_model
        self.collection_name = collection_name

        # 客户端配置
        if host and port:
            # 服务端 ChromaDB
            self._client = chromadb.HttpClient(host=host, port=port)
            logger.info(f"连接远程 ChromaDB: {host}:{port}")
        else:
            # 本地持久化 ChromaDB
            persist_dir = persist_directory or os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                "data",
                "chroma_db",
            )
            os.makedirs(persist_dir, exist_ok=True)
            self._client = chromadb.PersistentClient(
                path=persist_dir,
                settings=Settings(anonymized_telemetry=False),
            )
            logger.info(f"使用本地持久化 ChromaDB: {persist_dir}")

        # HNSW 配置
        hnsw_config = self.HNSW_PRESETS.get(hnsw_preset, self.HNSW_PRESETS["medium"])

        # 获取或创建集合
        self._collection = self._client.get_or_create_collection(
            name=collection_name,
            metadata=hnsw_config,
        )
        logger.info(f"ChromaDB 集合已就绪: {collection_name}, 当前记录数: {self._collection.count()}, HNSW: {hnsw_preset}")

    @staticmethod
    def clean_markdown(text: str) -> str:
        """
        清理 Markdown 符号，保留纯文本

        用于向量化前的文本清理，提高向量质量。

        Args:
            text: 包含 Markdown 符号的文本

        Returns:
            str: 清理后的纯文本
        """
        if not text:
            return ""

        # 1. 移除代码块
        text = re.sub(r'```[\s\S]*?```', '', text)
        text = re.sub(r'`[^`]+`', '', text)

        # 2. 移除标题符号（保留文本）
        text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)

        # 3. 移除粗体/斜体符号（保留文本）
        text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
        text = re.sub(r'\*([^*]+)\*', r'\1', text)
        text = re.sub(r'__([^_]+)__', r'\1', text)
        text = re.sub(r'_([^_]+)_', r'\1', text)

        # 4. 移除链接语法（保留文本）
        text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)

        # 5. 移除图片语法
        text = re.sub(r'!\[([^\]]*)\]\([^)]+\)', r'\1', text)

        # 6. 移除列表符号
        text = re.sub(r'^[\s]*[-*+]\s+', '', text, flags=re.MULTILINE)
        text = re.sub(r'^[\s]*\d+\.\s+', '', text, flags=re.MULTILINE)

        # 7. 移除引用符号
        text = re.sub(r'^>\s+', '', text, flags=re.MULTILINE)

        # 8. 移除水平线
        text = re.sub(r'^[-*_]{3,}\s*$', '', text, flags=re.MULTILINE)

        # 9. 移除 HTML 标签
        text = re.sub(r'<[^>]+>', '', text)

        # 10. 清理多余空白
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r'[ \t]+', ' ', text)
        text = text.strip()

        return text

    def add(
        self,
        doc_id: str,
        content: str,
        metadata: Optional[Dict] = None,
        clean_for_embedding: bool = True,
    ) -> None:
        """
        添加文档

        Args:
            doc_id: 文档 ID
            content: 文档内容（原始内容，保存到数据库）
            metadata: 元数据
            clean_for_embedding: 是否清理 Markdown 符号后再向量化
        """
        # 保存原始内容
        self._collection.add(
            ids=[doc_id],
            embeddings=[self._embed(content, clean_for_embedding)],
            documents=[content],
            metadatas=[metadata or {}],
        )

    def add_batch(
        self,
        doc_ids: List[str],
        contents: List[str],
        metadatas: Optional[List[Dict]] = None,
        clean_for_embedding: bool = True,
    ) -> None:
        """
        批量添加文档

        Args:
            doc_ids: 文档 ID 列表
            contents: 文档内容列表（原始内容）
            metadatas: 元数据列表
            clean_for_embedding: 是否清理 Markdown 符号后再向量化
        """
        if not doc_ids:
            return
        vectors = [self._embed(c, clean_for_embedding) for c in contents]
        self._collection.add(
            ids=doc_ids,
            embeddings=vectors,
            documents=contents,
            metadatas=metadatas or [{}] * len(doc_ids),
        )

    def add_with_vector(
        self,
        doc_id: str,
        vector: List[float],
        content: str,
        metadata: Optional[Dict] = None,
    ) -> None:
        """
        添加文档（使用预计算的向量，不调用 embedding_model）

        用于多模态向量：CLIP 图像向量直接写入，不经过文本 embedding
        """
        self._collection.add(
            ids=[doc_id],
            embeddings=[vector],
            documents=[content],
            metadatas=[metadata or {}],
        )

    def search_by_vector(
        self,
        query_vector: List[float],
        top_k: int = 5,
        min_score: float = 0.0,
        filters: Optional[Dict] = None,
    ) -> List[Tuple[str, float, Dict]]:
        """
        用预计算的向量做相似度搜索（不调用 embedding_model）

        用于多模态检索：CLIP 文本向量查 CLIP 图像向量库
        """
        results = self._collection.query(
            query_embeddings=[query_vector],
            n_results=top_k,
            where=filters,
            include=["metadatas", "documents", "distances"],
        )
        output = []
        if not results.get("ids") or not results["ids"][0]:
            return output
        for doc_id, distance, metadata, document in zip(
            results["ids"][0],
            results["distances"][0],
            results["metadatas"][0],
            results["documents"][0],
        ):
            similarity = 1.0 - distance / 2.0
            if similarity >= min_score:
                output.append((doc_id, similarity, {**metadata, "content": document}))
        return output

    def _embed(self, content: str, clean_markdown: bool = True) -> List[float]:
        """
        生成文本向量

        Args:
            content: 原始文本
            clean_markdown: 是否清理 Markdown 符号

        Returns:
            List[float]: 向量
        """
        if clean_markdown:
            cleaned = self.clean_markdown(content)
        else:
            cleaned = content

        # 如果清理后为空，使用原始内容
        if not cleaned.strip():
            cleaned = content

        return self.embedding_model.embed(cleaned)

    def upsert(
        self,
        doc_id: str,
        content: str,
        metadata: Optional[Dict] = None,
    ) -> None:
        """更新或插入文档"""
        vector = self.embedding_model.embed(content)
        self._collection.upsert(
            ids=[doc_id],
            embeddings=[vector],
            documents=[content],
            metadatas=[metadata or {}],
        )

    def upsert_batch(
        self,
        doc_ids: List[str],
        contents: List[str],
        metadatas: Optional[List[Dict]] = None,
    ) -> None:
        """批量更新或插入文档"""
        if not doc_ids:
            return
        vectors = self.embedding_model.embed_batch(contents)
        self._collection.upsert(
            ids=doc_ids,
            embeddings=vectors,
            documents=contents,
            metadatas=metadatas or [{}] * len(doc_ids),
        )

    def get_content_hash(self, content: str) -> str:
        """计算内容哈希"""
        return hashlib.md5(content.encode()).hexdigest()[:16]

    def generate_id_with_hash(self, document_id: str, chunk_index: int, content: str) -> str:
        """生成带内容哈希的 ID"""
        content_hash = self.get_content_hash(content)
        return f"{document_id}_{chunk_index}_{content_hash}"

    def get_existing_ids(self, doc_ids: List[str]) -> Set[str]:
        """获取已存在的 ID 集合"""
        if not doc_ids:
            return set()
        try:
            # 分批查询，避免单次查询过大
            existing = set()
            batch_size = 1000
            for i in range(0, len(doc_ids), batch_size):
                batch = doc_ids[i:i + batch_size]
                result = self._collection.get(ids=batch, include=[])
                existing.update(result["ids"])
            return existing
        except Exception as e:
            logger.warning(f"查询已存在 ID 失败: {e}")
            return set()

    def add_with_dedup(
        self,
        doc_ids: List[str],
        contents: List[str],
        metadatas: Optional[List[Dict]] = None,
        batch_size: int = 100,
        use_content_hash: bool = True,
    ) -> Dict[str, int]:
        """
        带去重的批量添加

        Args:
            doc_ids: 文档 ID 列表
            contents: 内容列表
            metadatas: 元数据列表
            batch_size: 批量大小
            use_content_hash: 是否使用内容哈希生成 ID

        Returns:
            Dict: {"added": 10, "skipped": 2, "failed": 1}
        """
        stats = {"added": 0, "skipped": 0, "failed": 0, "updated": 0}

        if not doc_ids:
            return stats

        # 1. 使用内容哈希生成 ID
        if use_content_hash:
            doc_ids = [
                self.generate_id_with_hash(doc_id, i, content)
                for i, (doc_id, content) in enumerate(zip(doc_ids, contents))
            ]

        # 2. 检查已存在的 ID
        existing_ids = self.get_existing_ids(doc_ids)

        # 3. 分离新增和需要更新的
        to_add_ids = []
        to_add_contents = []
        to_add_metadatas = []

        to_update_ids = []
        to_update_contents = []
        to_update_metadatas = []

        for i, (doc_id, content) in enumerate(zip(doc_ids, contents)):
            meta = metadatas[i] if metadatas else {}

            if doc_id in existing_ids:
                # 已存在，检查是否需要更新
                existing_content = self._get_content_by_id(doc_id)
                if existing_content != content:
                    to_update_ids.append(doc_id)
                    to_update_contents.append(content)
                    to_update_metadatas.append(meta)
                    stats["updated"] += 1
                else:
                    stats["skipped"] += 1
            else:
                # 新增
                to_add_ids.append(doc_id)
                to_add_contents.append(content)
                to_add_metadatas.append(meta)

        # 4. 批量添加
        if to_add_ids:
            for i in range(0, len(to_add_ids), batch_size):
                batch_ids = to_add_ids[i:i + batch_size]
                batch_contents = to_add_contents[i:i + batch_size]
                batch_metas = to_add_metadatas[i:i + batch_size]

                try:
                    vectors = self.embedding_model.embed_batch(batch_contents)
                    # 检查零向量
                    valid_indices = [
                        j for j, v in enumerate(vectors)
                        if any(x != 0 for x in v)
                    ]

                    if valid_indices:
                        self._collection.add(
                            ids=[batch_ids[j] for j in valid_indices],
                            embeddings=[vectors[j] for j in valid_indices],
                            documents=[batch_contents[j] for j in valid_indices],
                            metadatas=[batch_metas[j] for j in valid_indices],
                        )
                        stats["added"] += len(valid_indices)

                    failed = len(batch_ids) - len(valid_indices)
                    if failed > 0:
                        stats["failed"] += failed

                except Exception as e:
                    logger.error(f"批量添加失败: {e}")
                    stats["failed"] += len(batch_ids)

        # 5. 批量更新
        if to_update_ids:
            for i in range(0, len(to_update_ids), batch_size):
                batch_ids = to_update_ids[i:i + batch_size]
                batch_contents = to_update_contents[i:i + batch_size]
                batch_metas = to_update_metadatas[i:i + batch_size]

                try:
                    vectors = self.embedding_model.embed_batch(batch_contents)
                    self._collection.upsert(
                        ids=batch_ids,
                        embeddings=vectors,
                        documents=batch_contents,
                        metadatas=batch_metas,
                    )
                except Exception as e:
                    logger.error(f"批量更新失败: {e}")
                    stats["failed"] += len(batch_ids)

        logger.info(f"批量添加完成: {stats}")
        return stats

    def _get_content_by_id(self, doc_id: str) -> Optional[str]:
        """根据 ID 获取内容"""
        try:
            result = self._collection.get(ids=[doc_id], include=["documents"])
            if result["documents"]:
                return result["documents"][0]
            return None
        except Exception:
            return None

    def get_by_ids(self, doc_ids: List[str]) -> List[Dict]:
        """
        批量根据 ID 获取记录

        Args:
            doc_ids: 文档/分块 ID 列表

        Returns:
            List[Dict]: 记录列表，每个包含 id, content, metadata
        """
        if not doc_ids:
            return []

        try:
            result = self._collection.get(
                ids=doc_ids,
                include=["documents", "metadatas"],
            )
            output = []
            for i, doc_id in enumerate(result["ids"]):
                output.append({
                    "id": doc_id,
                    "content": result["documents"][i] if result["documents"] else "",
                    "metadata": result["metadatas"][i] if result["metadatas"] else {},
                })
            return output
        except Exception as e:
            logger.error(f"批量获取记录失败: {e}")
            return []

    def delete_by_document(self, document_id: str) -> int:
        """
        删除文档的所有分块

        Args:
            document_id: 文档 ID

        Returns:
            int: 删除数量（-1 表示未知）
        """
        try:
            self._collection.delete(
                where={"document_id": document_id}
            )
            logger.info(f"删除文档 {document_id} 的所有分块")
            return -1
        except Exception as e:
            logger.error(f"删除文档失败: {e}")
            return 0

    def get_by_document(self, document_id: str) -> List[Dict]:
        """
        获取文档的所有分块

        Args:
            document_id: 文档 ID

        Returns:
            List[Dict]: 分块列表
        """
        try:
            result = self._collection.get(
                where={"document_id": document_id},
                include=["documents", "metadatas"],
            )
            chunks = []
            for i, doc_id in enumerate(result["ids"]):
                chunks.append({
                    "id": doc_id,
                    "content": result["documents"][i],
                    "metadata": result["metadatas"][i],
                })
            return chunks
        except Exception as e:
            logger.error(f"获取文档分块失败: {e}")
            return []

    def update_document(
        self,
        document_id: str,
        doc_ids: List[str],
        contents: List[str],
        metadatas: Optional[List[Dict]] = None,
        batch_size: int = 100,
    ) -> Dict[str, int]:
        """
        更新文档（先删除再添加）

        Args:
            document_id: 文档 ID
            doc_ids: 新的分块 ID 列表
            contents: 新的分块内容列表
            metadatas: 新的分块元数据列表
            batch_size: 批量大小

        Returns:
            Dict: {"deleted": 5, "added": 4}
        """
        stats = {"deleted": 0, "added": 0, "failed": 0}

        # 1. 删除旧分块
        self.delete_by_document(document_id)
        stats["deleted"] = -1  # ChromaDB 不返回数量

        # 2. 添加新分块
        if doc_ids:
            for i in range(0, len(doc_ids), batch_size):
                batch_ids = doc_ids[i:i + batch_size]
                batch_contents = contents[i:i + batch_size]
                batch_metas = metadatas[i:i + batch_size] if metadatas else [{}] * len(batch_ids)

                try:
                    vectors = self.embedding_model.embed_batch(batch_contents)
                    # 检查零向量
                    valid_indices = [
                        j for j, v in enumerate(vectors)
                        if any(x != 0 for x in v)
                    ]

                    if valid_indices:
                        self._collection.add(
                            ids=[batch_ids[j] for j in valid_indices],
                            embeddings=[vectors[j] for j in valid_indices],
                            documents=[batch_contents[j] for j in valid_indices],
                            metadatas=[batch_metas[j] for j in valid_indices],
                        )
                        stats["added"] += len(valid_indices)

                    failed = len(batch_ids) - len(valid_indices)
                    if failed > 0:
                        stats["failed"] += failed

                except Exception as e:
                    logger.error(f"批量添加失败: {e}")
                    stats["failed"] += len(batch_ids)

        logger.info(f"文档更新完成: {stats}")
        return stats

    def search(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.0,
        filters: Optional[Dict] = None,
    ) -> List[Tuple[str, float, Dict]]:
        """
        相似度搜索

        Returns:
            List of (doc_id, score, metadata_with_content)
        """
        query_vector = self.embedding_model.embed(query)
        results = self._collection.query(
            query_embeddings=[query_vector],
            n_results=top_k,
            where=filters,
            include=["metadatas", "documents", "distances"],
        )

        output = []
        if not results["ids"]:
            return output

        for doc_id, distance, metadata, document in zip(
            results["ids"][0],
            results["distances"][0],
            results["metadatas"][0],
            results["documents"][0],
        ):
            # ChromaDB cosine distance 范围 [0, 2]，转为 similarity [0, 1]
            similarity = 1.0 - distance / 2.0
            if similarity >= min_score:
                output.append((
                    doc_id,
                    similarity,
                    {**metadata, "content": document},
                ))

        return output

    def delete(self, doc_id: Optional[str] = None, where: Optional[Dict] = None) -> bool:
        """删除文档

        支持按 doc_id 删除或按元数据过滤条件删除。
        """
        try:
            kwargs = {}
            if doc_id:
                kwargs["ids"] = [doc_id]
            if where:
                kwargs["where"] = where
            if not kwargs:
                logger.warning("delete 未提供 doc_id 或 where 条件")
                return False
            self._collection.delete(**kwargs)
            return True
        except Exception as e:
            logger.warning(f"删除 ChromaDB 文档失败: {e}")
            return False

    def delete_by_filter(self, filters: Dict) -> int:
        """按过滤条件删除"""
        try:
            self._collection.delete(where=filters)
            return -1  # ChromaDB 不返回删除数量
        except Exception as e:
            logger.warning(f"按条件删除 ChromaDB 文档失败: {e}")
            return 0

    def get_all(
        self,
        limit: int = 10000,
        include: Optional[List[str]] = None,
    ) -> Dict:
        """获取集合中的所有记录"""
        try:
            include = include or ["metadatas", "documents"]
            return self._collection.get(
                limit=limit,
                include=include,
            )
        except Exception as e:
            logger.warning(f"获取 ChromaDB 全部记录失败: {e}")
            return {"ids": [], "documents": [], "metadatas": [], "embeddings": []}

    def size(self) -> int:
        """记录数量"""
        return self._collection.count()

    def clear(self) -> None:
        """清空集合"""
        try:
            self._client.delete_collection(self.collection_name)
            self._collection = self._client.get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"},
            )
        except Exception as e:
            logger.warning(f"清空 ChromaDB 集合失败: {e}")

    def incremental_update(
        self,
        document_id: str,
        new_chunks: List[Dict],
        batch_size: int = 100,
    ) -> Dict[str, int]:
        """
        增量更新文档

        只更新变化的分块，而不是删除所有再重新添加。

        Args:
            document_id: 文档 ID
            new_chunks: 新的分块列表 [{"id", "text", "metadata"}, ...]
            batch_size: 批量大小

        Returns:
            Dict: {"added": 2, "deleted": 1, "updated": 1, "unchanged": 3}
        """
        stats = {"added": 0, "deleted": 0, "updated": 0, "unchanged": 0, "failed": 0}

        # 1. 获取旧分块
        old_chunks = self.get_by_document(document_id)
        old_chunk_map = {}
        for chunk in old_chunks:
            chunk_index = chunk["metadata"].get("chunk_index")
            if chunk_index is not None:
                old_chunk_map[chunk_index] = chunk

        # 2. 构建新分块映射
        new_chunk_map = {}
        for i, chunk in enumerate(new_chunks):
            chunk_index = chunk.get("metadata", {}).get("chunk_index", i)
            new_chunk_map[chunk_index] = chunk

        # 3. 计算差异
        to_add = []      # 新增的分块
        to_update = []   # 内容变化的分块
        to_delete = []   # 需要删除的分块

        # 检查新增和更新
        for idx, new_chunk in new_chunk_map.items():
            if idx not in old_chunk_map:
                # 新增
                to_add.append(new_chunk)
            else:
                old_chunk = old_chunk_map[idx]
                old_hash = old_chunk["metadata"].get("content_hash", "")
                new_hash = self.get_content_hash(new_chunk.get("text", ""))

                if old_hash != new_hash:
                    # 内容变化，需要更新
                    to_delete.append(old_chunk["id"])
                    to_update.append(new_chunk)
                else:
                    # 内容未变化
                    stats["unchanged"] += 1

        # 检查删除
        for idx, old_chunk in old_chunk_map.items():
            if idx not in new_chunk_map:
                # 旧分块在新内容中不存在
                to_delete.append(old_chunk["id"])

        # 4. 执行删除
        if to_delete:
            for i in range(0, len(to_delete), batch_size):
                batch = to_delete[i:i + batch_size]
                try:
                    self._collection.delete(ids=batch)
                    stats["deleted"] += len(batch)
                except Exception as e:
                    logger.error(f"批量删除失败: {e}")
                    stats["failed"] += len(batch)

        # 5. 执行添加（包括新增和更新）
        all_to_add = to_add + to_update
        if all_to_add:
            for i in range(0, len(all_to_add), batch_size):
                batch = all_to_add[i:i + batch_size]
                batch_ids = [c.get("id", f"{document_id}_chunk_{i + j}") for j, c in enumerate(batch)]
                batch_contents = [c.get("text", "") for c in batch]
                batch_metas = [c.get("metadata", {}) for c in batch]

                # 添加内容哈希
                for meta, content in zip(batch_metas, batch_contents):
                    meta["content_hash"] = self.get_content_hash(content)

                try:
                    vectors = self.embedding_model.embed_batch(batch_contents)
                    # 检查零向量
                    valid_indices = [
                        j for j, v in enumerate(vectors)
                        if any(x != 0 for x in v)
                    ]

                    if valid_indices:
                        self._collection.add(
                            ids=[batch_ids[j] for j in valid_indices],
                            embeddings=[vectors[j] for j in valid_indices],
                            documents=[batch_contents[j] for j in valid_indices],
                            metadatas=[batch_metas[j] for j in valid_indices],
                        )
                        stats["added"] += len([to_add[j] for j in valid_indices if j < len(to_add)])
                        stats["updated"] += len([to_update[j] for j in valid_indices if j >= len(to_add)])

                    failed = len(batch) - len(valid_indices)
                    if failed > 0:
                        stats["failed"] += failed

                except Exception as e:
                    logger.error(f"批量添加失败: {e}")
                    stats["failed"] += len(batch)

        logger.info(f"增量更新完成: {stats}")
        return stats

    def delete_batch(self, doc_ids: List[str]) -> int:
        """
        批量删除分块

        Args:
            doc_ids: 要删除的分块 ID 列表

        Returns:
            int: 删除数量
        """
        if not doc_ids:
            return 0

        try:
            self._collection.delete(ids=doc_ids)
            logger.info(f"批量删除 {len(doc_ids)} 个分块")
            return len(doc_ids)
        except Exception as e:
            logger.error(f"批量删除失败: {e}")
            return 0
