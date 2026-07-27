"""
文档上传与教材解析模块

支持将教材文档解析、分块并索引到 ChromaDB，供 RAG 检索使用。

支持的解析器：
- DocumentParser: 通用文档解析器
- PyMuPDFParser: 高质量 PDF 解析器（支持跨页合并、页眉页脚清理）
- OCRProcessor: OCR 处理器（支持 Tesseract、PaddleOCR、EasyOCR）
"""

from .parser import DocumentParser
from .chunker import DocumentChunker
from .uploader import DocumentUploader
from .pymupdf_parser import PyMuPDFParser
from .ocr import OCRProcessor, PDFWithOCR
from .models import (
    ElementType, ElementMetadata, DocumentElement,
    DocumentMetadata, StructuredDocument, Chunk,
)
from .text_parser import TextParser
from .docling_parser import DoclingParser
from .parser import ParserFactory
from .struct_chunker import StructureAwareChunker

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
    # 新模型
    "ElementType", "ElementMetadata", "DocumentElement",
    "DocumentMetadata", "StructuredDocument", "Chunk",
]
