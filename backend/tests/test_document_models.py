"""Tests for document data models"""

import pytest
from backend.app.document.models import (
    ElementType, ElementMetadata, DocumentElement,
    StructuredDocument, DocumentMetadata, Chunk
)


class TestElementType:
    def test_has_required_types(self):
        assert ElementType.TITLE.value == "title"
        assert ElementType.HEADING.value == "heading"
        assert ElementType.PARAGRAPH.value == "paragraph"
        assert ElementType.TABLE.value == "table"
        assert ElementType.CODE.value == "code"
        assert ElementType.FORMULA.value == "formula"
        assert ElementType.IMAGE.value == "image"
        assert ElementType.LIST.value == "list"


class TestDocumentElement:
    def test_create_text_element(self):
        el = DocumentElement(
            type=ElementType.PARAGRAPH,
            text="Hello world",
            metadata=ElementMetadata(page_number=1, heading_path=["Intro"])
        )
        assert el.type == ElementType.PARAGRAPH
        assert el.text == "Hello world"
        assert el.metadata.page_number == 1
        assert el.metadata.heading_path == ["Intro"]

    def test_create_table_element(self):
        html = "<table><tr><td>a</td></tr></table>"
        el = DocumentElement(
            type=ElementType.TABLE,
            text="a",
            text_as_html=html,
            metadata=ElementMetadata()
        )
        assert el.text_as_html == html
        assert el.formula_latex is None

    def test_create_formula_element(self):
        el = DocumentElement(
            type=ElementType.FORMULA,
            text="E = mc^2",
            formula_latex="E = mc^2",
            metadata=ElementMetadata()
        )
        assert el.formula_latex == "E = mc^2"

    def test_element_heading_path_default(self):
        el = DocumentElement(
            type=ElementType.PARAGRAPH,
            text="test",
            metadata=ElementMetadata()
        )
        assert el.metadata.heading_path == []


class TestStructuredDocument:
    def test_create_document(self):
        doc = StructuredDocument(
            metadata=DocumentMetadata(filename="test.pdf", title="Test"),
            elements=[
                DocumentElement(type=ElementType.HEADING, text="Title", metadata=ElementMetadata()),
                DocumentElement(type=ElementType.PARAGRAPH, text="Body", metadata=ElementMetadata()),
            ]
        )
        assert doc.metadata.filename == "test.pdf"
        assert len(doc.elements) == 2
        assert doc.metadata.page_count == 0  # default

    def test_empty_document(self):
        doc = StructuredDocument(metadata=DocumentMetadata(filename=""), elements=[])
        assert len(doc.elements) == 0


class TestChunk:
    def test_create_chunk(self):
        chunk = Chunk(
            id="chunk_001",
            text="# Intro\nHello",
            element_type="text",
            metadata={"heading_path": ["Intro"]}
        )
        assert chunk.id == "chunk_001"
        assert chunk.element_type == "text"

    def test_chunk_defaults(self):
        chunk = Chunk(id="chunk_002", text="test", element_type="text", metadata={})
        assert chunk.metadata == {}
