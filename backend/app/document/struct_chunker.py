"""
Structure-Aware Chunker - 基于文档结构的分块器

核心策略 by_title：
- HEADING 元素 -> 开新块
- TABLE / CODE / FORMULA -> 独立成块
- 同一标题下的 PARAGRAPH / LIST -> 合并到同一块
- 每个块继承父标题的面包屑路径
"""

import uuid
from typing import List
from loguru import logger

from .models import (
    ElementType, StructuredDocument, Chunk,
)


class StructureAwareChunker:
    """结构感知分块器"""

    def __init__(self, max_chars: int = 500):
        self.max_chars = max_chars

    def chunk(self, doc: StructuredDocument) -> List[Chunk]:
        """
        对结构化文档进行分块

        Args:
            doc: 结构化文档

        Returns:
            List[Chunk]: 分块列表
        """
        if not doc.elements:
            return []

        chunks = []
        current_heading_path = []
        buffer = []
        buffer_type = "text"
        buffer_char_count = 0

        def flush():
            nonlocal buffer, buffer_char_count
            if not buffer:
                return
            prefix = " > ".join(current_heading_path) + "\n" if current_heading_path else ""
            text = prefix + "\n".join(buffer)

            chunk_id = f"chunk_{uuid.uuid4().hex[:12]}"
            chunks.append(Chunk(
                id=chunk_id,
                text=text,
                element_type=buffer_type,
                metadata={
                    "heading_path": list(current_heading_path),
                    "heading_path_str": " > ".join(current_heading_path),
                    "element_type": buffer_type,
                },
            ))
            buffer = []
            buffer_char_count = 0

        for element in doc.elements:
            el_type = element.type

            # 标题 -> 刷出旧块，开始新块
            if el_type == ElementType.HEADING or el_type == ElementType.TITLE:
                flush()
                current_heading_path = list(element.metadata.heading_path)
                buffer = [element.text]
                buffer_type = "text"
                buffer_char_count = len(element.text)
                continue

            # 独立成块的类型
            if el_type in (ElementType.TABLE, ElementType.CODE, ElementType.FORMULA):
                flush()
                prefix = " > ".join(current_heading_path) + "\n" if current_heading_path else ""
                chunk_text = prefix + element.text
                chunk_id = f"chunk_{uuid.uuid4().hex[:12]}"

                meta = {
                    "heading_path": list(current_heading_path),
                    "heading_path_str": " > ".join(current_heading_path),
                    "element_type": el_type.value,
                }
                if element.text_as_html:
                    meta["text_as_html"] = element.text_as_html
                if element.formula_latex:
                    meta["formula_latex"] = element.formula_latex

                chunks.append(Chunk(
                    id=chunk_id,
                    text=chunk_text,
                    element_type=el_type.value,
                    metadata=meta,
                ))
                continue

            # 普通文本：合并到当前缓冲区
            text = element.text or ""
            if not text:
                continue

            # 单段文本超长，按句子切分
            if len(text) > self.max_chars:
                flush()
                sentences = text.replace("。", "。\n").replace("！", "！\n").replace("？", "？\n").split("\n")
                for sent in sentences:
                    sent = sent.strip()
                    if not sent:
                        continue
                    if len(sent) > self.max_chars:
                        # 超长句子按字符切分，仍保留标题路径
                        for i in range(0, len(sent), self.max_chars):
                            chunk_text = sent[i:i + self.max_chars]
                            chunks.append(Chunk(
                                id=f"chunk_{uuid.uuid4().hex[:12]}",
                                text=chunk_text,
                                element_type="text",
                                metadata={
                                    "heading_path": list(current_heading_path),
                                    "heading_path_str": " > ".join(current_heading_path),
                                    "element_type": "text",
                                },
                            ))
                    else:
                        chunks.append(Chunk(
                            id=f"chunk_{uuid.uuid4().hex[:12]}",
                            text=sent,
                            element_type="text",
                            metadata={
                                "heading_path": list(current_heading_path),
                                "heading_path_str": " > ".join(current_heading_path),
                                "element_type": "text",
                            },
                        ))
                buffer = []
                buffer_char_count = 0
                continue

            if buffer_char_count + len(text) + 1 <= self.max_chars:
                buffer.append(text)
                buffer_char_count += len(text) + 1
            else:
                flush()
                buffer = [text]
                buffer_type = "text"
                buffer_char_count = len(text)

        flush()

        logger.debug(f"StructureAwareChunker: {len(doc.elements)} elements -> {len(chunks)} chunks")
        return chunks
