"""
文档上传与教材解析测试
"""

import pytest
import io
from unittest.mock import AsyncMock, patch

from app.document.parser import DocumentParser
from app.document.chunker import DocumentChunker
from app.document.uploader import DocumentUploader


class TestDocumentParser:
    """文档解析测试"""

    def test_parse_txt(self):
        """解析 TXT 文件"""
        parser = DocumentParser()
        text = "这是一本关于递归的教材。\n递归是函数调用自身的过程。"
        content = text.encode("utf-8")
        result = parser.parse(content, "test.txt")
        assert result == text

    def test_parse_md(self):
        """解析 Markdown 文件"""
        parser = DocumentParser()
        text = "# 递归\n\n递归是函数调用自身。"
        result = parser.parse(text.encode("utf-8"), "test.md")
        assert result == text

    def test_unsupported_format(self):
        """不支持的格式应报错"""
        parser = DocumentParser()
        with pytest.raises(ValueError):
            parser.parse(b"data", "test.exe")


class TestDocumentChunker:
    """文档分块测试"""

    def test_chunk_text(self):
        """文本分块"""
        chunker = DocumentChunker(chunk_size=50, chunk_overlap=10)
        text = "第一段内容。\n第二段内容。\n第三段内容更长一些，超过分块大小。"
        chunks = chunker.chunk(text, "doc_1")
        assert len(chunks) > 0
        assert all("doc_1_chunk_" in c["id"] for c in chunks)

    def test_empty_text(self):
        """空文本返回空列表"""
        chunker = DocumentChunker()
        chunks = chunker.chunk("", "doc_1")
        assert chunks == []


class TestDocumentUploader:
    """文档上传服务测试"""

    @pytest.mark.asyncio
    async def test_upload_txt(self):
        """上传 TXT 文档"""
        uploader = DocumentUploader(collection_name="test_documents")
        text = "什么是递归？递归是函数调用自身的过程。"
        content = text.encode("utf-8")

        result = await uploader.upload(
            content=content,
            filename="recursion.txt",
            title="递归教材",
        )

        assert result["filename"] == "recursion.txt"
        assert result["title"] == "递归教材"
        assert result["chunk_count"] > 0
        assert result["char_count"] == len(text)

    @pytest.mark.asyncio
    async def test_upload_uses_provided_document_id(self):
        """Uploader should keep API-created document ids in vector metadata."""
        uploader = DocumentUploader(collection_name="test_documents")
        expected_id = "doc_fixed_id"

        result = await uploader.upload(
            content="fixed id document content".encode("utf-8"),
            filename="fixed.txt",
            document_id=expected_id,
        )

        assert result["document_id"] == expected_id
        docs = await uploader.list_documents()
        assert any(d["document_id"] == expected_id for d in docs)

        success = await uploader.delete_document(expected_id)
        assert success

        docs = await uploader.list_documents()
        assert not any(d["document_id"] == expected_id for d in docs)

    @pytest.mark.asyncio
    async def test_list_and_delete_document(self):
        """列出并删除文档"""
        uploader = DocumentUploader(collection_name="test_documents")

        # 先上传一个文档
        result = await uploader.upload(
            content="测试文档内容。".encode("utf-8"),
            filename="test.txt",
        )
        doc_id = result["document_id"]

        # 列出文档
        docs = await uploader.list_documents()
        assert any(d["document_id"] == doc_id for d in docs)

        # 删除文档
        success = await uploader.delete_document(doc_id)
        assert success

        docs = await uploader.list_documents()
        assert not any(d["document_id"] == doc_id for d in docs)


class TestUploaderNewPipeline:
    """验证新的结构化解析 + by_title 分块管道"""

    @pytest.mark.asyncio
    async def test_upload_text_uses_new_pipeline(self):
        """新 pipeline 能正确处理 MD 文件"""
        from app.knowledge.unified_store import UnifiedKnowledgeStore
        from app.retrieval.embeddings import TFIDFModel

        store = UnifiedKnowledgeStore(
            embedding_model=TFIDFModel(max_features=100),
            collection_name="test_pipeline",
            persist_directory="./data/test_pipeline",
        )
        uploader = DocumentUploader(knowledge_store=store)

        result = await uploader.upload(
            content=b"# Test\n\nHello world.",
            filename="test.md",
            title="Test Doc",
        )
        assert result["chunk_count"] >= 1
        assert result["filename"] == "test.md"
        assert result["title"] == "Test Doc"

    @pytest.mark.asyncio
    async def test_upload_empty_content_raises_error(self):
        """空内容应报错"""
        from app.knowledge.unified_store import UnifiedKnowledgeStore
        from app.retrieval.embeddings import TFIDFModel

        store = UnifiedKnowledgeStore(
            embedding_model=TFIDFModel(max_features=100),
            collection_name="test_empty",
            persist_directory="./data/test_empty",
        )
        uploader = DocumentUploader(knowledge_store=store)

        with pytest.raises(ValueError, match="文档内容为空|parse failed|cannot parse|分块后为空"):
            await uploader.upload(content=b"", filename="empty.txt")
