"""Tests for StructureAwareChunker"""

import pytest


@pytest.fixture
def sample_doc():
    from app.document.models import (
        DocumentElement,
        DocumentMetadata,
        ElementMetadata,
        ElementType,
        StructuredDocument,
    )
    return StructuredDocument(
        metadata=DocumentMetadata(filename="test.md", title="Test"),
        elements=[
            DocumentElement(type=ElementType.HEADING, text="# Intro", metadata=ElementMetadata(heading_path=["Intro"])),
            DocumentElement(type=ElementType.PARAGRAPH, text="First paragraph.", metadata=ElementMetadata(heading_path=["Intro"])),
            DocumentElement(type=ElementType.PARAGRAPH, text="Second paragraph.", metadata=ElementMetadata(heading_path=["Intro"])),
            DocumentElement(type=ElementType.HEADING, text="## Details", metadata=ElementMetadata(heading_path=["Intro", "Details"])),
            DocumentElement(type=ElementType.PARAGRAPH, text="Detail text.", metadata=ElementMetadata(heading_path=["Intro", "Details"])),
            DocumentElement(type=ElementType.TABLE, text="a\tb",
                          text_as_html="<table><tr><td>a</td><td>b</td></tr></table>",
                          metadata=ElementMetadata(heading_path=["Intro", "Details"])),
        ]
    )


class TestStructureAwareChunker:
    def test_chunk_by_title_boundaries(self, sample_doc):
        """按标题边界分块"""
        from app.document.struct_chunker import StructureAwareChunker
        chunks = StructureAwareChunker().chunk(sample_doc)
        # Intro, Details, Table -> 至少3块
        assert len(chunks) >= 3

    def test_chunk_has_heading_path(self, sample_doc):
        """每个块应有标题路径"""
        from app.document.struct_chunker import StructureAwareChunker
        chunks = StructureAwareChunker().chunk(sample_doc)
        for chunk in chunks:
            assert "heading_path" in chunk.metadata

    def test_table_independent_chunk(self, sample_doc):
        """表格应独立成块"""
        from app.document.struct_chunker import StructureAwareChunker
        chunks = StructureAwareChunker().chunk(sample_doc)
        table_chunks = [c for c in chunks if c.element_type == "table"]
        assert len(table_chunks) >= 1

    def test_chunk_size_limit(self):
        """分块不超过 max_chars"""
        from app.document.models import (
            DocumentElement,
            DocumentMetadata,
            ElementMetadata,
            ElementType,
            StructuredDocument,
        )
        from app.document.struct_chunker import StructureAwareChunker

        long_text = "word " * 500
        doc = StructuredDocument(
            metadata=DocumentMetadata(filename="long.txt"),
            elements=[
                DocumentElement(type=ElementType.PARAGRAPH, text=long_text, metadata=ElementMetadata()),
            ]
        )
        chunks = StructureAwareChunker(max_chars=300).chunk(doc)
        for c in chunks:
            assert len(c.text) <= 300

    def test_empty_doc_returns_empty(self):
        from app.document.models import (
            DocumentMetadata,
            StructuredDocument,
        )
        from app.document.struct_chunker import StructureAwareChunker
        doc = StructuredDocument(metadata=DocumentMetadata(filename="empty.txt"), elements=[])
        chunks = StructureAwareChunker().chunk(doc)
        assert chunks == []
