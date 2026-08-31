"""
文档上传与教材解析模块

支持将教材文档解析、分块并索引到 ChromaDB，供 RAG 检索使用。

支持的解析器：
- DocumentParser: 通用文档解析器
- PyMuPDFParser: 高质量 PDF 解析器（支持跨页合并、页眉页脚清理）
- OCRProcessor: OCR 处理器（支持 Tesseract、PaddleOCR、EasyOCR）
"""

from .chunker import DocumentChunker
from .docling_parser import DoclingParser
from .models import (
    Chunk,
    DocumentElement,
    DocumentMetadata,
    ElementMetadata,
    ElementType,
    StructuredDocument,
)
from .ocr import OCRProcessor, PDFWithOCR
from .parent_child_chunker import ParentChildChunker
from .parser import DocumentParser, ParserFactory
from .pymupdf_parser import PyMuPDFParser
from .struct_chunker import StructureAwareChunker
from .text_parser import TextParser
from .uploader import DocumentUploader

# 自动注册解析器到工厂
ParserFactory.register(DoclingParser)
ParserFactory.register(TextParser)

__all__ = [
    "DocumentParser",
    "DocumentChunker",
    "DocumentUploader",
    "PyMuPDFParser",
    "OCRProcessor",
    "PDFWithOCR",
    "TextParser",
    "DoclingParser",
    "ParserFactory",
    "StructureAwareChunker",
    "ParentChildChunker",
    # 新模型
    "ElementType", "ElementMetadata", "DocumentElement",
    "DocumentMetadata", "StructuredDocument", "Chunk",
]
