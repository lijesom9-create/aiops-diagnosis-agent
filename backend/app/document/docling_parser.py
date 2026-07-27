"""
Docling Parser - 基于 Docling 的结构化 PDF/DOCX 解析器

通过 DoclingDocument 将 PDF/DOCX 解析为结构化 Element 列表。
自动处理页眉页脚过滤、表格提取、标题层级。
"""

import os
import tempfile
from pathlib import Path
from typing import Optional, Set
from loguru import logger

from .models import (
    ElementType, ElementMetadata, DocumentElement,
    DocumentMetadata, StructuredDocument,
)


class DoclingParser:
    """基于 Docling 的结构化解析器"""

    SUPPORTED_EXTENSIONS = {".pdf", ".docx"}

    def __init__(self):
        self._converter = None

    def _get_converter(self):
        """延迟初始化 Docling converter（第一次使用时加载模型）"""
        if self._converter is None:
            # 确保使用国内镜像
            os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
            os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

            from docling.document_converter import DocumentConverter
            self._converter = DocumentConverter()
            logger.info("DoclingParser: DocumentConverter 初始化完成")
        return self._converter

    # DocItemLabel → ElementType 映射
    _LABEL_MAP = {
        "TITLE": ElementType.TITLE,
        "SECTION_HEADER": ElementType.HEADING,
        "PARAGRAPH": ElementType.PARAGRAPH,
        "TEXT": ElementType.PARAGRAPH,
        "TABLE": ElementType.TABLE,
        "LIST_ITEM": ElementType.LIST,
        "CODE": ElementType.CODE,
        "FORMULA": ElementType.FORMULA,
        "PICTURE": ElementType.IMAGE,
        "CAPTION": ElementType.PARAGRAPH,
        "PAGE_HEADER": None,  # 过滤
        "PAGE_FOOTER": None,  # 过滤
        "FOOTNOTE": ElementType.PARAGRAPH,
    }

    def parse(self, content: bytes, filename: str) -> StructuredDocument:
        """解析 PDF/DOCX 文件为结构化文档"""
        if not content or not content.strip():
            logger.warning(f"DoclingParser: 空内容 {filename}")
            return StructuredDocument(
                metadata=DocumentMetadata(filename=filename, title=Path(filename).stem),
                elements=[],
            )

        ext = Path(filename).suffix.lower()
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        try:
            converter = self._get_converter()
            result = converter.convert(tmp_path)
            docling_doc = result.document

            elements = []
            heading_stack = [""] * 10

            for item, level in docling_doc.iterate_items():
                label = item.label
                label_name = label.name if hasattr(label, "name") else str(label)

                elem_type = self._LABEL_MAP.get(label_name)
                if elem_type is None:
                    continue

                if label_name == "SECTION_HEADER":
                    heading_text = item.text.strip() if item.text else ""
                    heading_stack[level] = heading_text
                    for i in range(level + 1, len(heading_stack)):
                        heading_stack[i] = ""

                heading_path = [h for h in heading_stack if h]
                text = ""
                try:
                    if hasattr(item, "text") and item.text:
                        text = item.text.strip()
                except (AttributeError, TypeError):
                    text = ""

                # 表格 HTML 提取
                text_as_html = None
                if label_name == "TABLE" and hasattr(item, "export_to_dataframe"):
                    try:
                        df = item.export_to_dataframe()
                        text_as_html = df.to_html(index=False)
                    except Exception:
                        pass

                element = DocumentElement(
                    type=elem_type,
                    text=text,
                    metadata=ElementMetadata(
                        page_number=getattr(item, "page_number", None),
                        heading_path=heading_path,
                    ),
                    text_as_html=text_as_html,
                )
                elements.append(element)

            logger.info(f"DoclingParser: {filename} -> {len(elements)} elements")

            return StructuredDocument(
                metadata=DocumentMetadata(
                    filename=filename,
                    title=Path(filename).stem,
                    page_count=len(docling_doc.pages),
                    file_size=len(content),
                ),
                elements=elements,
            )

        except Exception as e:
            logger.error(f"DoclingParser 解析失败 {filename}: {e}")
            raise
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
