"""
Tests for knowledge management features - 知识库管理单元测试

覆盖：
- 文档上传去重（MD5 哈希）
- 装饰图过滤（parent_child_chunker._filter_decorative_images）
- BM25 索引基本检索
- 知识库统计聚合
"""

import hashlib
import pytest

from app.document.models import (
    DocumentElement,
    DocumentMetadata,
    ElementMetadata,
    ElementType,
    StructuredDocument,
)
from app.document.parent_child_chunker import ParentChildChunker
from app.knowledge.unified_store import BM25Index


class TestUploadDedup:
    """上传去重测试"""

    def test_compute_file_hash(self):
        """相同内容哈希一致，不同内容哈希不同"""
        from app.api.documents import _compute_file_hash

        content1 = b"hello world"
        content2 = b"hello world"
        content3 = b"different content"

        assert _compute_file_hash(content1) == _compute_file_hash(content2)
        assert _compute_file_hash(content1) != _compute_file_hash(content3)
        assert _compute_file_hash(content1) == hashlib.md5(content1).hexdigest()


class TestDecorativeImageFilter:
    """装饰图过滤测试"""

    def _make_image(self, caption: str, image_type: str) -> DocumentElement:
        return DocumentElement(
            type=ElementType.IMAGE,
            text=caption,
            image_desc=caption,
            image_type=image_type,
            metadata={},
        )

    def test_filter_brand_logo(self):
        """品牌 logo 应该被过滤"""
        chunker = ParentChildChunker(parent_max_chars=500, child_max_chars=200)
        elements = [
            DocumentElement(type=ElementType.PARAGRAPH, text="正文", metadata={}),
            self._make_image("这是黑马程序员的品牌标识", "other"),
            self._make_image("公司 logo 图片", "photo"),
        ]

        filtered = chunker._filter_decorative_images(elements)

        assert len(filtered) == 1
        assert filtered[0].type == ElementType.PARAGRAPH

    def test_keep_content_images(self):
        """内容型图片应保留"""
        chunker = ParentChildChunker(parent_max_chars=500, child_max_chars=200)
        elements = [
            self._make_image("系统架构流程图", "diagram"),
            self._make_image("代码截图示例", "code"),
            self._make_image("数据表格", "table"),
        ]

        filtered = chunker._filter_decorative_images(elements)

        assert len(filtered) == 3

    def test_filter_icon_by_ratio_description(self):
        """图标/小 logo 描述应被过滤"""
        chunker = ParentChildChunker(parent_max_chars=500, child_max_chars=200)
        elements = [
            self._make_image("页面顶部的水印图标", "other"),
        ]

        filtered = chunker._filter_decorative_images(elements)

        assert len(filtered) == 0

    def test_chunk_pipeline_excludes_decorative(self):
        """完整分块流程中装饰图不进入 chunks"""
        doc = StructuredDocument(
            metadata=DocumentMetadata(filename="test.md"),
            elements=[
                DocumentElement(
                    type=ElementType.TITLE,
                    text="第一章",
                    metadata=ElementMetadata(heading_path=["第一章"]),
                ),
                DocumentElement(
                    type=ElementType.PARAGRAPH,
                    text="这是正文内容。",
                    metadata=ElementMetadata(heading_path=["第一章"]),
                ),
                self._make_image("黑马程序员 logo", "other"),
            ],
        )

        chunker = ParentChildChunker(parent_max_chars=500, child_max_chars=200)
        chunks = chunker.chunk(doc)

        image_chunks = [c for c in chunks if c.element_type == "image"]
        assert len(image_chunks) == 0, "装饰图不应生成 chunk"


class TestBM25Retrieval:
    """BM25 检索基础测试"""

    def test_bm25_tokenize_and_search(self):
        """BM25 能正确建立索引并检索"""
        bm25 = BM25Index(k1=1.5, b=0.75)

        docs = [
            ("doc1", "FastAPI 是一个现代 Web 框架"),
            ("doc2", "Django 是另一个 Python Web 框架"),
            ("doc3", "FastAPI 支持异步请求处理"),
        ]
        for doc_id, text in docs:
            bm25.add_document(doc_id, text)

        results = bm25.search("FastAPI", top_k=2)

        assert len(results) == 2
        # FastAPI 相关的两篇应排在前面
        ids = [r[0] for r in results]
        assert "doc1" in ids
        assert "doc3" in ids

    def test_bm25_empty_index_returns_empty(self):
        """空索引查询返回空结果"""
        bm25 = BM25Index()
        results = bm25.search("任意查询", top_k=3)
        assert results == []


class TestKnowledgeStatsAggregation:
    """知识库统计聚合测试"""

    def test_aggregate_document_stats_empty(self):
        """空 store 聚合返回空列表"""
        from app.api.knowledge import _aggregate_document_stats

        class FakeStore:
            vector_store = type("VS", (), {"get_all": lambda self, **kw: {"metadatas": []}})()
            _separate_parent_child = True
            _parent_store = type("PS", (), {"get_all": lambda self, **kw: {"metadatas": []}})()

        stats = _aggregate_document_stats(FakeStore())
        assert stats == []

    def test_aggregate_document_stats_counts(self):
        """按 document_id 正确统计 child/parent 数量"""
        from app.api.knowledge import _aggregate_document_stats

        class FakeStore:
            vector_store = type("VS", (), {
                "get_all": lambda self, **kw: {
                    "metadatas": [
                        {"document_id": "docA", "filename": "a.md"},
                        {"document_id": "docA", "filename": "a.md"},
                        {"document_id": "docB", "filename": "b.md"},
                    ]
                }
            })()
            _separate_parent_child = True
            _parent_store = type("PS", (), {
                "get_all": lambda self, **kw: {
                    "metadatas": [
                        {"document_id": "docA", "filename": "a.md"},
                    ]
                }
            })()

        stats = _aggregate_document_stats(FakeStore())
        stats_dict = {s.document_id: s for s in stats}

        assert stats_dict["docA"].child_count == 2
        assert stats_dict["docA"].parent_count == 1
        assert stats_dict["docA"].filename == "a.md"
        assert stats_dict["docB"].child_count == 1
        assert stats_dict["docB"].parent_count == 0
