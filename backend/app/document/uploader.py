"""
文档上传服务

将解析后的文档分块存入统一知识库（UnifiedKnowledgeStore）。
支持文件系统存储，保存原始文件。
"""

import uuid
from typing import List, Optional
from pathlib import Path
from loguru import logger

from .parser import DocumentParser, ParserFactory
from .chunker import DocumentChunker
from .struct_chunker import StructureAwareChunker
from .models import Chunk, DocumentElement
from ..knowledge.unified_store import UnifiedKnowledgeStore, KnowledgeItem
from ..storage.file_storage import FileStorage, get_file_storage


class DocumentUploader:
    """文档上传服务"""

    def __init__(
        self,
        knowledge_store: UnifiedKnowledgeStore = None,
        file_storage: FileStorage = None,
        chunk_size: int = 500,
        chunk_overlap: int = 50,
        strategy: str = "auto",  # 自动选择分块策略
        collection_name: Optional[str] = None,  # 兼容旧测试
    ):
        self.parser = DocumentParser()
        self.chunker = DocumentChunker(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            strategy=strategy,
        )
        self.file_storage = file_storage or get_file_storage()
        self._collection_name = collection_name

        if knowledge_store is not None:
            self.knowledge_store = knowledge_store
        elif collection_name:
            # 兼容旧测试：自动创建内存 store
            from ..retrieval.embeddings import TFIDFModel
            self.knowledge_store = UnifiedKnowledgeStore(
                embedding_model=TFIDFModel(max_features=100),
                collection_name=collection_name,
            )
        else:
            self.knowledge_store = None

    async def upload(
        self,
        content: bytes,
        filename: str,
        title: Optional[str] = None,
        course_id: Optional[str] = None,
        user_id: Optional[str] = None,
        topic_id: Optional[str] = None,
        document_id: Optional[str] = None,
    ) -> dict:
        """
        上传并索引文档（使用新的结构化管道，失败时降级到旧管道）

        Args:
            content: 文件二进制内容
            filename: 文件名
            title: 文档标题
            course_id: 关联课程 ID（兼容旧版）
            user_id: 关联用户 ID
            topic_id: 关联主题 ID

        Returns:
            dict: 上传结果，包含 document_id, chunk_count
        """
        document_id = document_id or str(uuid.uuid4())
        file_info = None

        # 保存原始文件
        if user_id and self.file_storage:
            try:
                file_info = await self.file_storage.save(
                    content=content, filename=filename,
                    user_id=user_id, document_id=document_id,
                )
            except Exception as e:
                logger.warning(f"保存原始文件失败: {e}")

        # 尝试新管道（结构化解析 + by_title 分块）
        try:
            result = await self._upload_new_pipeline(
                content=content, filename=filename, title=title,
                document_id=document_id, course_id=course_id,
                user_id=user_id, topic_id=topic_id, file_info=file_info,
            )
            return result
        except Exception as e:
            logger.warning(f"新管道处理失败，降级到旧管道: {e}")
            result = await self._upload_legacy(
                content=content, filename=filename, title=title,
                document_id=document_id, course_id=course_id,
                user_id=user_id, topic_id=topic_id, file_info=file_info,
            )
            return result

    async def _upload_new_pipeline(
        self, content, filename, title, document_id,
        course_id, user_id, topic_id, file_info,
    ) -> dict:
        """使用新的结构化管道（ParserFactory + StructureAwareChunker）"""
        parser = ParserFactory.get_parser(filename)
        doc = parser.parse(content, filename)

        if not doc.elements:
            raise ValueError("文档内容为空或解析失败")

        chunker = StructureAwareChunker(max_chars=self.chunker.chunk_size)
        chunks = chunker.chunk(doc)

        if not chunks:
            raise ValueError("文档分块后为空")

        return self._store_chunks(
            chunks=chunks, document_id=document_id, filename=filename,
            title=title, course_id=course_id, user_id=user_id,
            topic_id=topic_id, file_info=file_info,
        )

    async def _upload_legacy(
        self, content, filename, title, document_id,
        course_id, user_id, topic_id, file_info,
    ) -> dict:
        """使用旧管道（DocumentParser + DocumentChunker，降级路径）"""
        text = self.parser.parse(content, filename)
        if not text.strip():
            raise ValueError("文档内容为空或解析失败")

        ext = Path(filename).suffix.lower()
        force_strategy = "markdown" if ext in (".md", ".markdown") else "recursive"
        old_chunks = self.chunker.chunk(text, document_id, force_strategy=force_strategy)

        if not old_chunks:
            raise ValueError("文档分块后为空")

        # 将旧 chunks 转换为新 Chunk 格式
        chunks = []
        for i, c in enumerate(old_chunks):
            chunks.append(Chunk(
                id=c["id"],
                text=c["text"],
                element_type="text",
                metadata={"chunk_index": i, "source": "legacy"},
            ))

        return self._store_chunks(
            chunks=chunks, document_id=document_id, filename=filename,
            title=title, course_id=course_id, user_id=user_id,
            topic_id=topic_id, file_info=file_info,
        )

    def _store_chunks(
        self, chunks, document_id, filename, title,
        course_id, user_id, topic_id, file_info,
    ) -> dict:
        """将分块存储到统一知识库"""
        metadata_base = {
            "document_id": document_id,
            "filename": filename,
            "title": title or filename,
            "user_id": user_id,
            "topic_id": topic_id,
            "course_id": course_id,
            "source": "user_document",
        }
        if file_info:
            metadata_base["file_path"] = file_info.get("file_path")
            metadata_base["file_size"] = file_info.get("file_size")

        items = []
        total_chars = 0
        for chunk in chunks:
            merged = {**metadata_base, **chunk.metadata}
            cleaned = {k: v for k, v in merged.items() if v not in (None, [], "")}
            total_chars += len(chunk.text)

            items.append(KnowledgeItem(
                id=chunk.id,
                title=title or filename,
                content=chunk.text,
                source="user_document",
                metadata=cleaned,
            ))

        if not self.knowledge_store:
            raise RuntimeError("UnifiedKnowledgeStore 未初始化")
        self.knowledge_store.add_batch(items)

        logger.info(f"文档上传完成: {filename} -> {document_id}, 共 {len(chunks)} 块")
        return {
            "document_id": document_id,
            "filename": filename,
            "title": title or filename,
            "chunk_count": len(chunks),
            "char_count": total_chars,
            "file_path": file_info.get("file_path") if file_info else None,
        }

    async def list_documents(self, user_id: Optional[str] = None) -> List[dict]:
        """列出已上传的文档"""
        if not self.knowledge_store:
            return []

        results = self.knowledge_store.search(
            query="",
            source="user_document",
            user_id=user_id,
            top_k=1000,
            min_score=0.0,
        )

        # 按 document_id 聚合
        docs = {}
        for r in results:
            doc_id = r.get("metadata", {}).get("document_id", "")
            if doc_id and doc_id not in docs:
                docs[doc_id] = {
                    "document_id": doc_id,
                    "title": r.get("title", ""),
                    "user_id": r.get("metadata", {}).get("user_id", ""),
                    "topic_id": r.get("metadata", {}).get("topic_id", ""),
                }

        return list(docs.values())

    async def delete_document(self, document_id: str) -> bool:
        """删除指定文档及其所有分块"""
        if not self.knowledge_store:
            raise RuntimeError("UnifiedKnowledgeStore 未初始化")

        try:
            deleted_count = self.knowledge_store.delete_by_document(document_id)
            logger.info(f"文档已删除: {document_id}, 共 {deleted_count} 条")
            return deleted_count != 0
        except Exception as e:
            logger.error(f"删除文档失败 {document_id}: {e}")
            return False
