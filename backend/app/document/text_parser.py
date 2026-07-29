"""
Text/Markdown Parser - 轻量解析器

处理 .txt 和 .md/.markdown 文件，不需要 ML 模型。
"""

import re
from typing import List, Optional
from pathlib import Path
from loguru import logger

from .models import (
    ElementType, ElementMetadata, DocumentElement,
    DocumentMetadata, StructuredDocument,
)


class TextParser:
    """纯文本/Markdown 解析器"""

    SUPPORTED_EXTENSIONS = {".txt", ".md", ".markdown"}

    def parse(
        self,
        content: bytes,
        filename: str,
        document_id: Optional[str] = None,  # 兼容多模态 pipeline 调用（TextParser 不使用）
    ) -> StructuredDocument:
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

            # 表格检测：以 | 开头且下一行是分隔行（|---|---|）
            if line.strip().startswith("|") and i + 1 < len(lines):
                next_line = lines[i + 1].strip()
                if re.match(r'^\|[\s:|-]+\|?$', next_line) and "---" in next_line:
                    # 收集表格行
                    table_lines = [line]
                    j = i + 1
                    while j < len(lines) and lines[j].strip().startswith("|"):
                        table_lines.append(lines[j])
                        j += 1
                    table_text = "\n".join(table_lines)
                    elements.append(DocumentElement(
                        type=ElementType.TABLE,
                        text=table_text,
                        text_as_html=self._markdown_table_to_html(table_lines),
                        metadata=ElementMetadata(heading_path=list(heading_path)),
                    ))
                    i = j
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

    @staticmethod
    def _markdown_table_to_html(table_lines: List[str]) -> str:
        """把 Markdown 表格行列表转为 HTML 表格"""
        def _parse_row(line: str) -> List[str]:
            # 去掉首尾的 |，按 | 分割
            cells = line.strip().strip("|").split("|")
            return [c.strip() for c in cells]

        if len(table_lines) < 2:
            return ""

        header = _parse_row(table_lines[0])
        # 第二行是分隔行，跳过
        rows = [_parse_row(line) for line in table_lines[2:]]

        html_parts = ["<table>"]
        # 表头
        html_parts.append("<thead><tr>")
        for cell in header:
            html_parts.append(f"<th>{cell}</th>")
        html_parts.append("</tr></thead>")
        # 数据行
        html_parts.append("<tbody>")
        for row in rows:
            html_parts.append("<tr>")
            for cell in row:
                html_parts.append(f"<td>{cell}</td>")
            html_parts.append("</tr>")
        html_parts.append("</tbody></table>")
        return "".join(html_parts)

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
