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


class TestTextParser:
    """Tests for TextParser (Task 2)"""

    def test_parse_markdown_with_headings(self):
        from backend.app.document.text_parser import TextParser
        content = b"# Title\n\nIntro text.\n\n## Section 1\n\nBody text."
        doc = TextParser().parse(content, "test.md")
        assert doc.metadata.filename == "test.md"
        assert len(doc.elements) >= 3
        assert doc.elements[0].type.value == "heading"

    def test_parse_plain_text(self):
        from backend.app.document.text_parser import TextParser
        content = b"Hello\n\nWorld\n\n- item 1\n- item 2"
        doc = TextParser().parse(content, "test.txt")
        assert len(doc.elements) > 0
        paragraphs = [e for e in doc.elements if e.type.value == "paragraph"]
        lists = [e for e in doc.elements if e.type.value == "list"]
        assert len(paragraphs) > 0 or len(lists) > 0

    def test_parse_empty_content(self):
        from backend.app.document.text_parser import TextParser
        doc = TextParser().parse(b"", "empty.txt")
        assert len(doc.elements) == 0

    def test_parse_markdown_code_block(self):
        from backend.app.document.text_parser import TextParser
        content = b"# Code\n\n```python\nx = 1\nprint(x)\n```"
        doc = TextParser().parse(content, "code.md")
        code_elements = [e for e in doc.elements if e.type.value == "code"]
        assert len(code_elements) >= 1
