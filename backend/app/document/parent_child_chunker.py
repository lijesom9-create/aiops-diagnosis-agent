"""
Parent-Child Chunker - 父子文档分块器

核心策略：
- 父块（Parent）：按文档结构中的 section 切分（通常是一级/二级标题下的内容）
- 子块（Child）：在父块内部按语义单元切分（段落、列表项、表格、代码等）
- 检索时命中子块，返回对应父块给 LLM，兼顾精准召回和完整上下文

参考实现借鉴了 LangChain ParentDocumentRetriever 的两级分块思想，
但基于 StructuredDocument 的元素结构进行语义边界保留。
"""

import uuid
from typing import List, Tuple
from loguru import logger

from .models import (
    ElementType, StructuredDocument, DocumentElement, Chunk,
)


class ParentChildChunker:
    """父子文档分块器"""

    def __init__(
        self,
        parent_max_chars: int = 1500,
        child_max_chars: int = 300,
        child_overlap_chars: int = 50,
        section_heading_depth: int = 2,
    ):
        """
        初始化父子分块器

        Args:
            parent_max_chars: 父块最大字符数（生成上下文用）
            child_max_chars: 子块最大字符数（检索用）
            child_overlap_chars: 子块之间重叠字符数
            section_heading_depth: 多少级标题作为父块边界，默认 2（一/二级标题）
        """
        self.parent_max_chars = parent_max_chars
        self.child_max_chars = child_max_chars
        self.child_overlap_chars = child_overlap_chars
        self.section_heading_depth = section_heading_depth

    def chunk(self, doc: StructuredDocument) -> List[Chunk]:
        """
        对结构化文档进行父子分块

        Args:
            doc: 结构化文档

        Returns:
            List[Chunk]: 包含父块和子块的所有分块
        """
        if not doc.elements:
            return []

        # 1. 按 section 分组
        sections = self._group_into_sections(doc.elements)

        # 2. 对每个 section 生成父块和子块
        all_chunks: List[Chunk] = []
        for section in sections:
            parent_chunk, child_chunks = self._split_section(section, doc.metadata.filename)
            all_chunks.append(parent_chunk)
            all_chunks.extend(child_chunks)

        parent_count = sum(1 for c in all_chunks if c.metadata.get("chunk_type") == "parent")
        child_count = len(all_chunks) - parent_count
        logger.debug(
            f"ParentChildChunker: {len(doc.elements)} elements -> "
            f"{parent_count} parents, {child_count} children"
        )
        return all_chunks

    def _group_into_sections(self, elements: List[DocumentElement]) -> List[List[DocumentElement]]:
        """
        将元素按 section 分组。

        父块边界规则：
        - HEADING 元素且 heading_path 长度 == section_heading_depth 时，开启新 section
        - TITLE 元素和更深层级的标题作为当前 section 的一部分
        - 文档开头的标题/前言会合并到第一个正式 section 中
        """
        if not elements:
            return []

        sections: List[List[DocumentElement]] = []
        current: List[DocumentElement] = []
        preamble: List[DocumentElement] = []
        has_boundary = False

        for element in elements:
            if self._is_section_boundary(element):
                if not has_boundary:
                    # 第一个正式边界之前的所有内容作为 preamble
                    if current:
                        preamble = current
                    current = [element]
                    has_boundary = True
                else:
                    sections.append(current)
                    current = [element]
            else:
                current.append(element)

        if current:
            sections.append(current)

        # 把 preamble 合并到第一个 section，避免标题单独成一个空父块
        if preamble and sections:
            sections[0] = preamble + sections[0]
            preamble = []

        # 拆分过大的 section
        final_sections: List[List[DocumentElement]] = []
        for section in sections:
            final_sections.extend(self._split_large_section(section))

        return final_sections

    def _is_section_boundary(self, element: DocumentElement) -> bool:
        """判断元素是否是父块边界"""
        if element.type != ElementType.HEADING:
            return False
        heading_path = element.metadata.heading_path or []
        return len(heading_path) == self.section_heading_depth

    def _split_large_section(self, section: List[DocumentElement]) -> List[List[DocumentElement]]:
        """如果 section 超过 parent_max_chars，拆分成多个 sub-section"""
        total_len = sum(len(e.text or "") for e in section)
        if total_len <= self.parent_max_chars:
            return [section]

        sub_sections: List[List[DocumentElement]] = []
        current: List[DocumentElement] = []
        current_len = 0

        # section 的标题上下文，用于每个 sub-section 开头保留
        heading_context = [
            e for e in section
            if e.type in (ElementType.TITLE, ElementType.HEADING)
        ]

        def flush_current() -> None:
            nonlocal current, current_len
            if current and current != heading_context:
                sub_sections.append(current)
            current = list(heading_context)
            current_len = sum(len(e.text or "") for e in current)

        for element in section:
            el_len = len(element.text or "")

            # 单个元素超过上限：必须拆分它
            if el_len > self.parent_max_chars:
                flush_current()
                text = element.text or ""
                for i in range(0, el_len, self.parent_max_chars):
                    part_text = text[i:i + self.parent_max_chars]
                    part_element = DocumentElement(
                        type=element.type,
                        text=part_text,
                        metadata=element.metadata,
                    )
                    sub_sections.append(list(heading_context) + [part_element])
                continue

            # 加入当前元素会超限，开启新 sub-section
            if current and current_len + el_len > self.parent_max_chars:
                flush_current()

            current.append(element)
            current_len += el_len

        flush_current()

        return sub_sections

    def _split_section(
        self,
        section: List[DocumentElement],
        doc_name: str,
    ) -> Tuple[Chunk, List[Chunk]]:
        """
        将一个 section 切分为一个父块和若干子块
        """
        parent_id = f"parent_{uuid.uuid4().hex[:12]}"

        # 确定 section 的标题路径：取第一个标题/标题元素的 heading_path
        heading_path: List[str] = []
        for element in section:
            if element.type in (ElementType.TITLE, ElementType.HEADING):
                heading_path = list(element.metadata.heading_path or [])
                break

        # 构建父块文本：section 内所有元素拼接
        parent_text_parts: List[str] = []
        for element in section:
            if element.text:
                parent_text_parts.append(element.text)
        parent_text = "\n".join(parent_text_parts)

        # 子块切分
        child_chunks = self._split_children(section, parent_id, heading_path)

        parent_chunk = Chunk(
            id=parent_id,
            text=parent_text,
            element_type="section",
            metadata={
                "chunk_type": "parent",
                "parent_id": parent_id,
                "heading_path": list(heading_path),
                "heading_path_str": " > ".join(heading_path),
                "element_type": "section",
                "child_ids": [c.id for c in child_chunks],
                "child_count": len(child_chunks),
            },
        )

        return parent_chunk, child_chunks

    def _split_children(
        self,
        section: List[DocumentElement],
        parent_id: str,
        heading_path: List[str],
    ) -> List[Chunk]:
        """
        在 section 内部切分子块。

        策略：
        - 表格/代码/公式：独立成子块
        - 普通文本段落：连续短段落合并，超长段落按句子切分
        - 标题文本不单独成子块（已在父块中保留上下文）
        """
        child_chunks: List[Chunk] = []
        buffer: List[str] = []
        buffer_len = 0
        child_index = 0

        def flush_child(texts: List[str], forced_type: str = "text") -> None:
            nonlocal buffer, buffer_len, child_index
            if not texts:
                return

            # 去重并拼接
            combined = "\n".join(texts).strip()
            if not combined:
                return

            child_id = f"child_{uuid.uuid4().hex[:12]}"
            child_chunks.append(Chunk(
                id=child_id,
                text=combined,
                element_type=forced_type,
                metadata={
                    "chunk_type": "child",
                    "parent_id": parent_id,
                    "child_index": child_index,
                    "heading_path": list(heading_path),
                    "heading_path_str": " > ".join(heading_path),
                    "element_type": forced_type,
                },
            ))
            child_index += 1
            buffer = []
            buffer_len = 0

        for element in section:
            # 标题不单独成子块
            if element.type in (ElementType.TITLE, ElementType.HEADING):
                continue

            text = element.text or ""
            if not text.strip():
                continue

            # 独立元素类型：表格/代码/公式
            if element.type in (ElementType.TABLE, ElementType.CODE, ElementType.FORMULA):
                flush_child(buffer)
                meta_extra = {}
                if element.text_as_html:
                    meta_extra["text_as_html"] = element.text_as_html
                if element.formula_latex:
                    meta_extra["formula_latex"] = element.formula_latex
                flush_child([text], element.type.value)
                # 把额外元数据附加到最后一个子块
                if meta_extra and child_chunks:
                    child_chunks[-1].metadata.update(meta_extra)
                continue

            # 单个元素超长：先刷 buffer，再按句子/字符切分
            if len(text) > self.child_max_chars:
                flush_child(buffer)
                parts = self._split_text(text)
                for part in parts:
                    flush_child([part], element.type.value)
                continue

            # 正常合并到 buffer
            if buffer_len + len(text) + 1 <= self.child_max_chars:
                buffer.append(text)
                buffer_len += len(text) + 1
            else:
                flush_child(buffer)
                buffer = [text]
                buffer_len = len(text)

        flush_child(buffer)
        return child_chunks

    def _split_text(self, text: str) -> List[str]:
        """
        按句子切分超长文本，必要时按字符切分。
        子块之间保留 overlap。
        """
        # 优先按句子切分
        delimiters = ("\n", "。", "！", "？", ". ", "! ", "? ")
        sentences = [text]
        for delim in delimiters:
            new_sentences = []
            for sent in sentences:
                # 保留分隔符
                parts = sent.split(delim)
                for i, part in enumerate(parts):
                    if i < len(parts) - 1:
                        part += delim
                    part = part.strip()
                    if part:
                        new_sentences.append(part)
            sentences = new_sentences

        # 合并短句子成 chunk，保留 overlap
        chunks: List[str] = []
        current = ""
        for sent in sentences:
            if len(current) + len(sent) + 1 <= self.child_max_chars:
                current = f"{current}\n{sent}".strip() if current else sent
            else:
                if current:
                    chunks.append(current)
                # overlap：从上一个 chunk 末尾取一部分
                if current and self.child_overlap_chars > 0:
                    overlap = current[-self.child_overlap_chars:]
                    current = f"{overlap}\n{sent}".strip()
                else:
                    current = sent

                # 如果单句仍然超过最大长度，按字符硬切
                if len(current) > self.child_max_chars:
                    chunks.extend(self._split_by_chars(current))
                    current = ""

        if current:
            chunks.append(current)

        return chunks

    def _split_by_chars(self, text: str) -> List[str]:
        """按字符硬切，保留 overlap"""
        chunks = []
        step = max(1, self.child_max_chars - self.child_overlap_chars)
        for i in range(0, len(text), step):
            chunks.append(text[i:i + self.child_max_chars])
        return chunks
