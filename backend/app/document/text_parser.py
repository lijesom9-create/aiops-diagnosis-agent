"""
Text/Markdown Parser - 轻量解析器

处理 .txt 和 .md/.markdown 文件，不需要 ML 模型。
"""

import re
from typing import List
from pathlib import Path
from loguru import logger

from .models import (
    ElementType, ElementMetadata, DocumentElement,
    DocumentMetadata, StructuredDocument,
)


class TextParser:
    """纯文本/Markdown 解析器"""

    SUPPORTED_EXTENSIONS = {".txt", ".md", ".markdown"}

    def parse(self, content: bytes, filename: str) -> StructuredDocument:
        """解析文本文件为结构化文档"""
        text = self._decode(content)
        if not text.strip():
            return StructuredDocument(
                metadata=DocumentMetadata(filename=filename, title=Path(filename).stem),
                elements=[],
            )

        ext = Path(filename).suffix.lower()
        if ext in (".md", ".markdown"):
            elements = self._parse_markdown(text)
        else:
            elements = self._parse_plain_text(text)

        return StructuredDocument(
            metadata=DocumentMetadata(
                filename=filename,
                title=Path(filename).stem,
                page_count=1,
                file_size=len(content),
            ),
            elements=elements,
        )

    def _decode(self, content: bytes) -> str:
        """编码检测解码"""
        for enc in ("utf-8", "gbk", "gb2312", "gb18030", "latin-1"):
            try:
                return content.decode(enc)
            except UnicodeDecodeError:
                continue
        return content.decode("utf-8", errors="ignore")

    def _parse_markdown(self, text: str) -> List[DocumentElement]:
        """解析 Markdown 文本"""
        elements = []
        lines = text.split("\n")
        i = 0
        in_code_block = False
        code_buffer = []
        heading_path = []

        while i < len(lines):
            line = lines[i]

            # 代码块检测
            if line.strip().startswith("```"):
                if in_code_block:
                    code_text = "\n".join(code_buffer)
                    elements.append(DocumentElement(
                        type=ElementType.CODE,
                        text=code_text,
                        metadata=ElementMetadata(heading_path=list(heading_path)),
                    ))
                    code_buffer = []
                    in_code_block = False
                else:
                    in_code_block = True
                    lang = line.strip()[3:].strip()
                    if lang:
                        code_buffer.append(f"# language: {lang}")
                i += 1
                continue

            if in_code_block:
                code_buffer.append(line)
                i += 1
                continue

            # 标题检测
            heading_match = re.match(r'^(#{1,6})\s+(.+)$', line)
            if heading_match:
                level = len(heading_match.group(1))
                heading_text = heading_match.group(2).strip()
                heading_path = heading_path[:level - 1] + [heading_text]
                elements.append(DocumentElement(
                    type=ElementType.HEADING,
                    text=line,
                    metadata=ElementMetadata(heading_path=list(heading_path)),
                ))
                i += 1
                continue

            if not line.strip():
                i += 1
                continue

            # 列表项检测
            if re.match(r'^[\s]*[-*+]\s+', line) or re.match(r'^[\s]*\d+\.\s+', line):
                elements.append(DocumentElement(
                    type=ElementType.LIST,
                    text=line.strip(),
                    metadata=ElementMetadata(heading_path=list(heading_path)),
                ))
                i += 1
                continue

            # 普通段落
            elements.append(DocumentElement(
                type=ElementType.PARAGRAPH,
                text=line.strip(),
                metadata=ElementMetadata(heading_path=list(heading_path)),
            ))
            i += 1

        # 关闭未闭合的代码块
        if in_code_block and code_buffer:
            elements.append(DocumentElement(
                type=ElementType.CODE,
                text="\n".join(code_buffer),
                metadata=ElementMetadata(heading_path=list(heading_path)),
            ))

        return elements

    def _parse_plain_text(self, text: str) -> List[DocumentElement]:
        """解析纯文本"""
        elements = []
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        for para in paragraphs:
            lines = para.split("\n")
            is_list = any(
                re.match(r'^[\s]*[-*+]\s+', l) or re.match(r'^[\s]*\d+\.\s+', l)
                for l in lines
            )
            if is_list:
                for line in lines:
                    if line.strip():
                        elements.append(DocumentElement(
                            type=ElementType.LIST,
                            text=line.strip(),
                            metadata=ElementMetadata(),
                        ))
            else:
                elements.append(DocumentElement(
                    type=ElementType.PARAGRAPH,
                    text=para,
                    metadata=ElementMetadata(),
                ))
        return elements
