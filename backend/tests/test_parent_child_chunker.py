"""Tests for Parent-Child Chunker"""


from app.document.models import (
    DocumentElement,
    DocumentMetadata,
    ElementMetadata,
    ElementType,
    StructuredDocument,
)
from app.document.parent_child_chunker import ParentChildChunker


def _make_doc(elements_data):
    """helper: 根据 (type, text, heading_path) 快速构建 StructuredDocument"""
    elements = []
    for el_type, text, heading_path in elements_data:
        elements.append(DocumentElement(
            type=el_type,
            text=text,
            metadata=ElementMetadata(heading_path=heading_path),
        ))
    return StructuredDocument(
        metadata=DocumentMetadata(filename="test.md"),
        elements=elements,
    )


class TestParentChildChunker:
    def test_basic_parent_child_split(self):
        doc = _make_doc([
            (ElementType.TITLE, "教材第一章", ["教材第一章"]),
            (ElementType.HEADING, "1.1 引言", ["教材第一章", "1.1 引言"]),
            (ElementType.PARAGRAPH, "这是第一段内容。", ["教材第一章", "1.1 引言"]),
            (ElementType.PARAGRAPH, "这是第二段内容。", ["教材第一章", "1.1 引言"]),
            (ElementType.HEADING, "1.2 核心概念", ["教材第一章", "1.2 核心概念"]),
            (ElementType.PARAGRAPH, "核心概念的内容。", ["教材第一章", "1.2 核心概念"]),
        ])

        chunker = ParentChildChunker(parent_max_chars=1000, child_max_chars=300)
        chunks = chunker.chunk(doc)

        parents = [c for c in chunks if c.metadata.get("chunk_type") == "parent"]
        children = [c for c in chunks if c.metadata.get("chunk_type") == "child"]

        assert len(parents) == 2, "应有 2 个父块（1.1 和 1.2 各一个）"
        assert len(children) >= 2, "子块数量应 >= 2"

        # 每个子块都关联到正确父块
        for child in children:
            assert child.metadata.get("chunk_type") == "child"
            assert child.metadata.get("parent_id") is not None
            assert child.metadata.get("child_index") is not None
            assert child.metadata.get("heading_path_str") != ""

        # 父块包含其所有子块 ID
        for parent in parents:
            child_ids = parent.metadata.get("child_ids", [])
            assert len(child_ids) > 0
            for cid in child_ids:
                assert any(c.id == cid for c in children)

    def test_table_code_formula_as_independent_child(self):
        doc = _make_doc([
            (ElementType.TITLE, "第二章", ["第二章"]),
            (ElementType.HEADING, "2.1 示例", ["第二章", "2.1 示例"]),
            (ElementType.PARAGRAPH, "请看下面的表格。", ["第二章", "2.1 示例"]),
            (ElementType.TABLE, "| a | b |\n|---|---|\n| 1 | 2 |", ["第二章", "2.1 示例"]),
            (ElementType.CODE, "print('hello')", ["第二章", "2.1 示例"]),
            (ElementType.FORMULA, "E = mc^2", ["第二章", "2.1 示例"]),
        ])

        chunker = ParentChildChunker(parent_max_chars=1000, child_max_chars=300)
        chunks = chunker.chunk(doc)

        children = [c for c in chunks if c.metadata.get("chunk_type") == "child"]
        types = [c.element_type for c in children]

        assert "table" in types
        assert "code" in types
        assert "formula" in types

        for c in children:
            if c.element_type == "table":
                assert "| a | b |" in c.text
            elif c.element_type == "code":
                assert "print('hello')" in c.text
            elif c.element_type == "formula":
                assert "E = mc^2" in c.text

    def test_large_section_is_split(self):
        long_text = "这是一段很长的内容。" * 100
        doc = _make_doc([
            (ElementType.TITLE, "大章节", ["大章节"]),
            (ElementType.PARAGRAPH, long_text, ["大章节"]),
        ])

        chunker = ParentChildChunker(parent_max_chars=500, child_max_chars=200)
        chunks = chunker.chunk(doc)

        parents = [c for c in chunks if c.metadata.get("chunk_type") == "parent"]
        # 大章节应被拆成多个父块
        assert len(parents) > 1

        for parent in parents:
            assert len(parent.text) <= chunker.parent_max_chars * 1.5  # 标题上下文可能让父块略超上限

    def test_long_text_split_with_overlap(self):
        long_text = "这是句子一。这是句子二。这是句子三。这是句子四。" * 10
        doc = _make_doc([
            (ElementType.TITLE, "章节", ["章节"]),
            (ElementType.PARAGRAPH, long_text, ["章节"]),
        ])

        chunker = ParentChildChunker(
            parent_max_chars=2000,
            child_max_chars=100,
            child_overlap_chars=20,
        )
        chunks = chunker.chunk(doc)

        children = [c for c in chunks if c.metadata.get("chunk_type") == "child"]
        assert len(children) > 1

        # 检查重叠：相邻子块末尾/开头应有重复内容
        found_overlap = False
        for i in range(len(children) - 1):
            curr_end = children[i].text[-30:]
            next_start = children[i + 1].text[:30]
            # 简单判断是否有共享子串
            if any(part in next_start for part in curr_end.split("。") if part):
                found_overlap = True
                break
        assert found_overlap, "子块之间应存在重叠"

    def test_empty_document(self):
        doc = StructuredDocument(
            metadata=DocumentMetadata(filename="empty.md"),
            elements=[],
        )
        chunker = ParentChildChunker()
        assert chunker.chunk(doc) == []

    def test_child_metadata_has_parent_id(self):
        doc = _make_doc([
            (ElementType.TITLE, "第一章", ["第一章"]),
            (ElementType.HEADING, "1.1 节", ["第一章", "1.1 节"]),
            (ElementType.PARAGRAPH, "内容 A", ["第一章", "1.1 节"]),
            (ElementType.PARAGRAPH, "内容 B", ["第一章", "1.1 节"]),
        ])

        chunks = ParentChildChunker().chunk(doc)
        parent = [c for c in chunks if c.metadata.get("chunk_type") == "parent"][0]
        children = [c for c in chunks if c.metadata.get("chunk_type") == "child"]

        for idx, child in enumerate(children):
            assert child.metadata["parent_id"] == parent.id
            assert child.metadata["child_index"] == idx

    def test_parent_text_contains_all_section_content(self):
        doc = _make_doc([
            (ElementType.TITLE, "第一章", ["第一章"]),
            (ElementType.HEADING, "1.1 节", ["第一章", "1.1 节"]),
            (ElementType.PARAGRAPH, "第一段", ["第一章", "1.1 节"]),
            (ElementType.PARAGRAPH, "第二段", ["第一章", "1.1 节"]),
        ])

        chunks = ParentChildChunker().chunk(doc)
        parent = [c for c in chunks if c.metadata.get("chunk_type") == "parent"][0]

        assert "第一段" in parent.text
        assert "第二段" in parent.text
        assert parent.metadata.get("chunk_type") == "parent"
        assert parent.metadata.get("child_count") == len(parent.metadata.get("child_ids", []))
