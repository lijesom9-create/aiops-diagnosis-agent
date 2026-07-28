# 文档解析升级 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将文档解析从"纯文本提取"升级为"结构化文档理解"，引入 Docling 解析器 + 结构化数据模型 + by_title 分块

**Architecture:** 在现有 `backend/app/document/` 目录下新增数据模型、DoclingParser、TextParser、StructureAwareChunker。保持 Uploader 接口不变，内部切换到新管道。解析器通过工厂模式选择，Docling 为主（PDF/DOCX），降级到 PyMuPDF，MD/TXT 用轻量 TextParser。

**Tech Stack:** Python 3.12, Docling v2.115+, PyMuPDF v1.27+, ChromaDB（不变）

## Global Constraints

- Docling 通过 `HF_ENDPOINT=https://hf-mirror.com` 环境变量走国内镜像
- Python 3.12，无 GPU 依赖（CPU only）
- 所有新代码保持与现有 `KnowledgeItem` 和 `UnifiedKnowledgeStore` 接口兼容
- 保持 backward compatibility：旧 `DocumentParser.parse() -> str` 不删除，新代码走新接口
- 测试使用 pytest + pytest-asyncio

---

## 文件结构

```
backend/app/document/
├── __init__.py           # 导出（新增导出）
├── models.py             # [新] 数据模型: DocumentElement, StructuredDocument, Chunk
├── parser.py             # [改] 现有解析器保留作为降级，新增 ParserFactory
├── docling_parser.py     # [新] DoclingParser——主解析器
├── text_parser.py        # [新] TextParser——MD/TXT 轻量解析器
├── struct_chunker.py     # [新] StructureAwareChunker——by_title 分块
├── chunker.py            # [改] 保留现有 chunker（旧 upload 路径仍用）
├── uploader.py           # [改] DocumentUploader 使用新管道
├── pymupdf_parser.py     # 不变
├── ocr.py                # 不变

backend/tests/
├── test_document_models.py       # [新] 测试数据模型
├── test_docling_parser.py        # [新] 测试 DoclingParser
├── test_struct_chunker.py        # [新] 测试 StructureAwareChunker
```

---

### Task 1: 结构化数据模型

**Files:**
- Create: `backend/app/document/models.py`
- Modify: `backend/app/document/__init__.py`

**Interfaces:**
- Consumes: 无
- Produces: `DocumentElement`, `ElementMetadata`, `StructuredDocument`, `Chunk`, `ElementType` — 供解析器和分块器使用

- [ ] **Step 1: Write failing test**

Create `backend/tests/test_document_models.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest backend/tests/test_document_models.py -v --no-header 2>&1 | head -30`
Expected: FAIL with ModuleNotFoundError or ImportError

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/document/models.py`:

```python
"""
Document Data Models - 结构化文档中间表示

定义解析器、分块器之间的统一数据结构。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Dict, Any


class ElementType(str, Enum):
    """文档元素类型"""
    TITLE = "title"
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    TABLE = "table"
    CODE = "code"
    FORMULA = "formula"
    IMAGE = "image"
    LIST = "list"


@dataclass
class ElementMetadata:
    """元素元数据"""
    page_number: Optional[int] = None
    bbox: Optional[tuple] = None  # (x0, y0, x1, y1)
    font: Optional[str] = None
    font_size: Optional[float] = None
    heading_path: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = {}
        if self.page_number is not None:
            d["page_number"] = self.page_number
        if self.font:
            d["font"] = self.font
        if self.font_size:
            d["font_size"] = self.font_size
        if self.heading_path:
            d["heading_path"] = self.heading_path
            d["heading_path_str"] = " > ".join(self.heading_path)
        return d


@dataclass
class DocumentElement:
    """文档元素——解析的最小单元"""
    type: ElementType
    text: str
    metadata: ElementMetadata = field(default_factory=ElementMetadata)

    # 类型专属字段
    text_as_html: Optional[str] = None      # TABLE 专用
    formula_latex: Optional[str] = None      # FORMULA 专用
    image_base64: Optional[str] = None       # IMAGE 专用
    image_desc: Optional[str] = None         # IMAGE 专用

    def to_chunk_dict(self) -> Dict[str, Any]:
        """转换为分块元数据字典"""
        meta = self.metadata.to_dict()
        meta["element_type"] = self.type.value
        if self.text_as_html:
            meta["text_as_html"] = self.text_as_html
        if self.formula_latex:
            meta["formula_latex"] = self.formula_latex
        return meta


@dataclass
class DocumentMetadata:
    """文档级元数据"""
    filename: str
    title: str = ""
    page_count: int = 0
    file_size: int = 0


@dataclass
class StructuredDocument:
    """解析器的统一输出"""
    metadata: DocumentMetadata
    elements: List[DocumentElement] = field(default_factory=list)


@dataclass
class Chunk:
    """分块结果"""
    id: str
    text: str
    element_type: str  # "text" | "table" | "code" | "formula"
    metadata: Dict[str, Any]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest backend/tests/test_document_models.py -v --no-header 2>&1 | tail -20`
Expected: All tests PASS

- [ ] **Step 5: Update `__init__.py` exports**

Edit `backend/app/document/__init__.py`. Add at the end:

```python
from .models import (
    ElementType, ElementMetadata, DocumentElement,
    DocumentMetadata, StructuredDocument, Chunk,
)
```

Update `__all__` to include:

```python
__all__ = [
    "DocumentParser",
    "DocumentChunker",
    "DocumentUploader",
    "PyMuPDFParser",
    "OCRProcessor",
    "PDFWithOCR",
    # 新模型
    "ElementType", "ElementMetadata", "DocumentElement",
    "DocumentMetadata", "StructuredDocument", "Chunk",
]
```

- [ ] **Step 6: Re-run tests and commit**

```bash
pytest backend/tests/test_document_models.py -v --no-header 2>&1 | tail -5
git add backend/app/document/models.py backend/app/document/__init__.py backend/tests/test_document_models.py
git commit -m "feat: add structured document data models (ElementType, StructuredDocument, Chunk)"
```

---

### Task 2: TextParser（MD/TXT 解析器）

**Files:**
- Create: `backend/app/document/text_parser.py`
- Modify: 无（后续集成到工厂）
- Test: `backend/tests/test_document_models.py` 新增测试类

**Interfaces:**
- Consumes: `StructuredDocument`, `DocumentElement`, `ElementType`, `ElementMetadata` (from Task 1)
- Produces: `TextParser.parse(content, filename) -> StructuredDocument`

- [ ] **Step 1: Write failing test**

Add to `backend/tests/test_document_models.py`:

```python
class TestTextParser:
    @pytest.mark.asyncio
    async def test_parse_markdown_with_headings(self):
        from backend.app.document.text_parser import TextParser
        content = b"# Title\n\nIntro text.\n\n## Section 1\n\nBody text."
        doc = TextParser().parse(content, "test.md")
        assert doc.metadata.filename == "test.md"
        assert len(doc.elements) >= 3
        # 第一个元素应该是 HEADING
        assert doc.elements[0].type.value == "heading"

    @pytest.mark.asyncio
    async def test_parse_plain_text(self):
        from backend.app.document.text_parser import TextParser
        content = b"Hello\n\nWorld\n\n- item 1\n- item 2"
        doc = TextParser().parse(content, "test.txt")
        assert len(doc.elements) > 0
        # 纯文本应检测为 PARAGRAPH
        assert doc.elements[0].type.value == "paragraph"

    @pytest.mark.asyncio
    async def test_parse_empty_text(self):
        from backend.app.document.text_parser import TextParser
        doc = TextParser().parse(b"", "empty.txt")
        assert len(doc.elements) == 0

    @pytest.mark.asyncio
    async def test_parse_markdown_code_block(self):
        from backend.app.document.text_parser import TextParser
        content = b"# Code\n\n```python\nx = 1\nprint(x)\n```"
        doc = TextParser().parse(content, "code.md")
        code_elements = [e for e in doc.elements if e.type.value == "code"]
        assert len(code_elements) >= 1
```

Run these tests to confirm they fail first.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest backend/tests/test_document_models.py::TestTextParser -v 2>&1`
Expected: ImportError for TextParser

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/document/text_parser.py`:

```python
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
                    # 结束代码块
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
                    # 可选：提取语言
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
                # 更新标题路径
                heading_path = heading_path[:level - 1] + [heading_text]
                elements.append(DocumentElement(
                    type=ElementType.HEADING,
                    text=line,
                    metadata=ElementMetadata(heading_path=list(heading_path)),
                ))
                i += 1
                continue

            # 空行跳过
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

        return elements

    def _parse_plain_text(self, text: str) -> List[DocumentElement]:
        """解析纯文本"""
        elements = []
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        for para in paragraphs:
            lines = para.split("\n")
            # 检测列表
            is_list = any(re.match(r'^[\s]*[-*+]\s+', l) or re.match(r'^[\s]*\d+\.\s+', l) for l in lines)
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest backend/tests/test_document_models.py::TestTextParser -v 2>&1 | tail -20`
Expected: All tests PASS

- [ ] **Step 5: Update exports**

Edit `backend/app/document/__init__.py`. Add import:

```python
from .text_parser import TextParser
```

Update `__all__` to include `"TextParser"`.

- [ ] **Step 6: Commit**

```bash
git add backend/app/document/text_parser.py backend/app/document/__init__.py backend/tests/test_document_models.py
git commit -m "feat: add TextParser for MD/TXT files"
```

---

### Task 3: DoclingParser（PDF/DOCX 主解析器）

**Files:**
- Create: `backend/app/document/docling_parser.py`
- Test: `backend/tests/test_docling_parser.py`

**Interfaces:**
- Consumes: `StructuredDocument`, `DocumentElement`, `ElementType`, `ElementMetadata` (from Task 1)
- Produces: `DoclingParser.parse(content, filename) -> StructuredDocument`

- [ ] **Step 1: Write failing test**

Create `backend/tests/test_docling_parser.py`:

```python
"""Tests for DoclingParser"""

import pytest
from pathlib import Path


class TestDoclingParser:
    def setup_method(self):
        """Skip if docling not installed"""
        try:
            from backend.app.document.docling_parser import DoclingParser
            self.parser = DoclingParser()
        except ImportError:
            pytest.skip("Docling not installed")

    def test_supported_extensions(self):
        from backend.app.document.docling_parser import DoclingParser
        assert ".pdf" in DoclingParser.SUPPORTED_EXTENSIONS
        assert ".docx" in DoclingParser.SUPPORTED_EXTENSIONS

    def test_parse_simple_pdf(self):
        """用 fpdf2 生成简单 PDF 然后解析"""
        from fpdf import FPDF
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(0, 10, "Test Title", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 12)
        pdf.cell(0, 10, "Hello world paragraph.", new_x="LMARGIN", new_y="NEXT")
        pdf_path = "/tmp/test_simple.pdf"
        pdf.output(pdf_path)

        with open(pdf_path, "rb") as f:
            content = f.read()

        doc = self.parser.parse(content, "test_simple.pdf")

        assert doc.metadata.filename == "test_simple.pdf"
        assert len(doc.elements) > 0
        # 应该有标题元素
        assert doc.elements[0].type.value in ("heading", "title")

    def test_parse_with_table(self):
        """解析含表格的 PDF"""
        from fpdf import FPDF
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 14)
        pdf.cell(0, 10, "Data", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        for row in [["A", "1"], ["B", "2"]]:
            pdf.cell(20, 8, row[0], border=1)
            pdf.cell(20, 8, row[1], border=1)
            pdf.ln()

        pdf_path = "/tmp/test_table.pdf"
        pdf.output(pdf_path)
        with open(pdf_path, "rb") as f:
            content = f.read()

        doc = self.parser.parse(content, "test_table.pdf")
        # 应该有 TABLE 元素
        table_elements = [e for e in doc.elements if e.type.value == "table"]
        assert len(table_elements) >= 1
        if table_elements[0].text_as_html:
            assert "<table>" in table_elements[0].text_as_html

    def test_parse_empty_content(self):
        """空内容"""
        doc = self.parser.parse(b"", "empty.pdf")
        assert len(doc.elements) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest backend/tests/test_docling_parser.py -v 2>&1 | head -20`
Expected: ImportError for DoclingParser

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/document/docling_parser.py`:

```python
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
            # 确保使用国内镜像（如果配置了）
            if "HF_ENDPOINT" not in os.environ:
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
        "PAGE_NUMBER": None,  # 过滤
        "FOOTNOTE": ElementType.PARAGRAPH,
    }

    def parse(self, content: bytes, filename: str) -> StructuredDocument:
        if not content or not content.strip():
            logger.warning(f"DoclingParser: 空内容 {filename}")
            return StructuredDocument(
                metadata=DocumentMetadata(filename=filename, title=Path(filename).stem),
                elements=[],
            )

        # 保存到临时文件（Docling 需要文件路径）
        ext = Path(filename).suffix.lower()
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        try:
            converter = self._get_converter()
            result = converter.convert(tmp_path)
            docling_doc = result.document

            elements = []
            heading_stack = [""] * 10  # 最多 10 级标题

            for item, level in docling_doc.iterate_items():
                label = item.label
                label_name = label.name if hasattr(label, 'name') else str(label)

                # 映射类型
                elem_type = self._LABEL_MAP.get(label_name)
                if elem_type is None:
                    continue  # 过滤页眉页脚等

                # 更新标题栈
                if label_name == "SECTION_HEADER":
                    heading_text = item.text.strip() if item.text else ""
                    heading_stack[level] = heading_text
                    # 清空更低层级
                    for i in range(level + 1, len(heading_stack)):
                        heading_stack[i] = ""

                # 构建标题路径
                heading_path = [h for h in heading_stack if h]

                # 提取文本
                text = item.text.strip() if item.text else ""

                # 提取表格 HTML
                text_as_html = None
                if label_name == "TABLE" and hasattr(item, "export_to_dataframe"):
                    try:
                        df = item.export_to_dataframe()
                        text_as_html = df.to_html(index=False)
                    except Exception:
                        pass

                # 创建我们的 Element
                element = DocumentElement(
                    type=elem_type,
                    text=text,
                    metadata=ElementMetadata(
                        page_number=getattr(item, 'page_number', None),
                        heading_path=heading_path,
                    ),
                    text_as_html=text_as_html,
                )
                elements.append(element)

            logger.info(f"DoclingParser: {filename} → {len(elements)} 个元素")

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `HF_ENDPOINT=https://hf-mirror.com pytest backend/tests/test_docling_parser.py -v 2>&1 | tail -30`
Expected: All tests PASS (may take ~10-30s for model loading)

- [ ] **Step 5: Update exports**

Edit `backend/app/document/__init__.py`:

```python
from .docling_parser import DoclingParser
```

Update `__all__` to include `"DoclingParser"`.

- [ ] **Step 6: Commit**

```bash
git add backend/app/document/docling_parser.py backend/app/document/__init__.py backend/tests/test_docling_parser.py
git commit -m "feat: add DoclingParser for structured PDF/DOCX parsing"
```

---

### Task 4: ParserFactory + parser.py 改造

**Files:**
- Modify: `backend/app/document/parser.py`
- Modify: `backend/app/document/__init__.py`

**Interfaces:**
- Consumes: `DoclingParser`, `TextParser`, `PyMuPDFParser` (existing)
- Produces: `ParserFactory.get_parser(filename) -> BaseParser`, `create_parser(filename) -> parser` 便捷函数

- [ ] **Step 1: Write failing test**

Add to `backend/tests/test_document_models.py`:

```python
class TestParserFactory:
    def test_factory_returns_docling_for_pdf(self):
        from backend.app.document.parser import ParserFactory
        parser = ParserFactory.get_parser("test.pdf")
        from backend.app.document.docling_parser import DoclingParser
        assert isinstance(parser, DoclingParser)

    def test_factory_returns_textparser_for_md(self):
        from backend.app.document.parser import ParserFactory
        parser = ParserFactory.get_parser("test.md")
        from backend.app.document.text_parser import TextParser
        assert isinstance(parser, TextParser)

    def test_factory_returns_textparser_for_txt(self):
        from backend.app.document.parser import ParserFactory
        parser = ParserFactory.get_parser("test.txt")
        from backend.app.document.text_parser import TextParser
        assert isinstance(parser, TextParser)

    def test_factory_unknown_extension(self):
        from backend.app.document.parser import ParserFactory
        import pytest
        with pytest.raises(ValueError, match="不支持的文件格式"):
            ParserFactory.get_parser("test.xyz")

    def test_parser_parse_returns_structured_document(self):
        """端到端：工厂获取解析器→解析→返回结构化文档"""
        from backend.app.document.parser import ParserFactory
        from backend.app.document.models import StructuredDocument

        parser = ParserFactory.get_parser("test.txt")
        doc = parser.parse(b"Hello world", "test.txt")
        assert isinstance(doc, StructuredDocument)
        assert len(doc.elements) >= 1

    def test_supported_extensions(self):
        from backend.app.document.parser import ParserFactory
        exts = ParserFactory.get_supported_extensions()
        assert ".pdf" in exts
        assert ".md" in exts
        assert ".txt" in exts
        assert ".docx" in exts
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest backend/tests/test_document_models.py::TestParserFactory -v 2>&1 | head -20`
Expected: Failures due to ParserFactory not found

- [ ] **Step 3: Implement ParserFactory in parser.py**

Edit `backend/app/document/parser.py`. Add **at the end of the file** (keep existing `DocumentParser` class unchanged for backward compatibility):

```python
# ============================================================
# ParserFactory - 新解析器工厂（基于结构化文档模型）
# ============================================================

class ParserFactory:
    """解析器工厂——根据文件扩展名自动选择合适的解析器"""

    _PARSER_REGISTRY = {}

    @classmethod
    def register(cls, parser_cls):
        """注册解析器"""
        for ext in parser_cls.SUPPORTED_EXTENSIONS:
            cls._PARSER_REGISTRY[ext.lower()] = parser_cls

    @classmethod
    def get_parser(cls, filename: str):
        """
        获取解析器实例

        Args:
            filename: 文件名（用于判断格式）

        Returns:
            BaseParser: 解析器实例

        Raises:
            ValueError: 不支持的文件格式
        """
        ext = Path(filename).suffix.lower()
        if ext in cls._PARSER_REGISTRY:
            return cls._PARSER_REGISTRY[ext]()

        from .docling_parser import DoclingParser
        from .text_parser import TextParser

        if ext in DoclingParser.SUPPORTED_EXTENSIONS:
            return DoclingParser()
        elif ext in TextParser.SUPPORTED_EXTENSIONS:
            return TextParser()
        else:
            raise ValueError(f"不支持的文件格式: {ext}")

    @classmethod
    def get_supported_extensions(cls):
        """获取所有支持的扩展名"""
        exts = set()
        exts.update(DoclingParser.SUPPORTED_EXTENSIONS)
        exts.update(TextParser.SUPPORTED_EXTENSIONS)
        return exts
```

Also add the import at the top of `parser.py`:

```python
from .models import StructuredDocument
```

And add the `parse` method signature hint to the existing `DocumentParser` class to keep it working:

(No change needed since the existing DocumentParser still uses `-> str`)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest backend/tests/test_document_models.py::TestParserFactory -v 2>&1 | tail -20`
Expected: All tests PASS

- [ ] **Step 5: Register parsers at import time**

Edit `backend/app/document/__init__.py`. Add auto-registration after imports:

```python
# 自动注册解析器到工厂
from .parser import ParserFactory
from .docling_parser import DoclingParser
from .text_parser import TextParser

ParserFactory.register(DoclingParser)
ParserFactory.register(TextParser)
```

- [ ] **Step 6: Commit**

```bash
git add backend/app/document/parser.py backend/app/document/__init__.py backend/tests/test_document_models.py
git commit -m "feat: add ParserFactory with DoclingParser and TextParser registration"
```

---

### Task 5: StructureAwareChunker（by_title 分块）

**Files:**
- Create: `backend/app/document/struct_chunker.py`
- Test: `backend/tests/test_struct_chunker.py`

**Interfaces:**
- Consumes: `StructuredDocument`, `DocumentElement`, `Chunk` (from Task 1)
- Produces: `StructureAwareChunker.chunk(doc) -> List[Chunk]`

- [ ] **Step 1: Write failing test**

Create `backend/tests/test_struct_chunker.py`:

```python
"""Tests for StructureAwareChunker"""

import pytest

@pytest.fixture
def sample_doc():
    from backend.app.document.models import (
        StructuredDocument, DocumentMetadata,
        DocumentElement, ElementType, ElementMetadata,
    )
    return StructuredDocument(
        metadata=DocumentMetadata(filename="test.md", title="Test"),
        elements=[
            DocumentElement(type=ElementType.HEADING, text="# Intro", metadata=ElementMetadata(heading_path=["Intro"])),
            DocumentElement(type=ElementType.PARAGRAPH, text="First paragraph.", metadata=ElementMetadata(heading_path=["Intro"])),
            DocumentElement(type=ElementType.PARAGRAPH, text="Second paragraph.", metadata=ElementMetadata(heading_path=["Intro"])),
            DocumentElement(type=ElementType.HEADING, text="## Details", metadata=ElementMetadata(heading_path=["Intro", "Details"])),
            DocumentElement(type=ElementType.PARAGRAPH, text="Detail text.", metadata=ElementMetadata(heading_path=["Intro", "Details"])),
            DocumentElement(type=ElementType.TABLE, text="a\tb", text_as_html="<table><tr><td>a</td><td>b</td></tr></table>",
                          metadata=ElementMetadata(heading_path=["Intro", "Details"])),
        ]
    )


class TestStructureAwareChunker:
    def test_chunk_by_title(self, sample_doc):
        from backend.app.document.struct_chunker import StructureAwareChunker
        chunks = StructureAwareChunker().chunk(sample_doc)
        # HEADING 边界应该分块
        assert len(chunks) >= 3  # Intro, Details, Table

    def test_chunk_has_heading_path(self, sample_doc):
        from backend.app.document.struct_chunker import StructureAwareChunker
        chunks = StructureAwareChunker().chunk(sample_doc)
        for chunk in chunks:
            assert "heading_path" in chunk.metadata

    def test_table_independent_chunk(self, sample_doc):
        """表格应该独立成块"""
        from backend.app.document.struct_chunker import StructureAwareChunker
        chunks = StructureAwareChunker().chunk(sample_doc)
        table_chunks = [c for c in chunks if c.element_type == "table"]
        assert len(table_chunks) >= 1

    def test_chunk_size_limit(self):
        """分块不超过 max_chars"""
        from backend.app.document.models import (
            StructuredDocument, DocumentMetadata,
            DocumentElement, ElementType, ElementMetadata,
        )
        from backend.app.document.struct_chunker import StructureAwareChunker

        long_text = "word " * 500
        doc = StructuredDocument(
            metadata=DocumentMetadata(filename="long.txt"),
            elements=[
                DocumentElement(type=ElementType.PARAGRAPH, text=long_text, metadata=ElementMetadata()),
            ]
        )
        chunks = StructureAwareChunker(max_chars=300).chunk(doc)
        for c in chunks:
            assert len(c.text) <= 300

    def test_empty_doc_returns_empty(self):
        from backend.app.document.models import (
            StructuredDocument, DocumentMetadata,
        )
        from backend.app.document.struct_chunker import StructureAwareChunker
        doc = StructuredDocument(metadata=DocumentMetadata(filename="empty.txt"), elements=[])
        chunks = StructureAwareChunker().chunk(doc)
        assert chunks == []

    def test_real_docling_output(self):
        """用 DoclingParser 解析 PDF，再用 StructureAwareChunker 分块（集成测试）"""
        try:
            from backend.app.document.docling_parser import DoclingParser
        except ImportError:
            pytest.skip("Docling not installed")

        from fpdf import FPDF
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(0, 10, "Chapter 1", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 12)
        pdf.cell(0, 10, "Some content.", new_x="LMARGIN", new_y="NEXT")
        pdf_path = "/tmp/test_chunk.pdf"
        pdf.output(pdf_path)
        with open(pdf_path, "rb") as f:
            content = f.read()

        doc = DoclingParser().parse(content, "test_chunk.pdf")
        from backend.app.document.struct_chunker import StructureAwareChunker
        chunks = StructureAwareChunker().chunk(doc)

        assert len(chunks) >= 1
        # 至少有一个 chunk 包含标题路径
        assert any(c.metadata.get("heading_path") for c in chunks)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest backend/tests/test_struct_chunker.py -v 2>&1 | head -20`
Expected: ImportError for StructureAwareChunker

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/document/struct_chunker.py`:

```python
"""
Structure-Aware Chunker - 基于文档结构的分块器

核心策略 by_title：
- HEADING 元素 → 开新块
- TABLE / CODE / FORMULA → 独立成块
- 同一标题下的 PARAGRAPH / LIST → 合并到同一块
- 每个块继承父标题的面包屑路径
"""

import uuid
from typing import List, Optional
from loguru import logger

from .models import (
    ElementType, StructuredDocument, DocumentElement, Chunk,
)


class StructureAwareChunker:
    """结构感知分块器"""

    def __init__(self, max_chars: int = 500):
        self.max_chars = max_chars

    def chunk(self, doc: StructuredDocument) -> List[Chunk]:
        """
        对结构化文档进行分块

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
            text = element.text or ""

            # 更新标题路径
            if el_type == ElementType.HEADING or el_type == ElementType.TITLE:
                flush()  # 先刷出上一个标题的内容
                current_heading_path = list(element.metadata.heading_path)
                # 标题本身也作为一个元素加入缓冲区
                buffer = [text]
                buffer_type = "text"
                buffer_char_count = len(text)
                continue

            # 独立成块的类型
            if el_type in (ElementType.TABLE, ElementType.CODE, ElementType.FORMULA):
                flush()
                prefix = " > ".join(current_heading_path) + "\n" if current_heading_path else ""
                chunk_text = prefix + text
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

            # 普通文本元素：合并到当前缓冲区
            if not buffer:
                # 新块
                buffer = [text]
                buffer_type = "text"
                buffer_char_count = len(text)
            elif buffer_char_count + len(text) + 1 <= self.max_chars:
                buffer.append(text)
                buffer_char_count += len(text) + 1
            else:
                flush()
                buffer = [text]
                buffer_type = "text"
                buffer_char_count = len(text)

        # 刷新最后一块
        flush()

        logger.debug(f"StructureAwareChunker: {len(doc.elements)} elements → {len(chunks)} chunks")
        return chunks
```

- [ ] **Step 4: Run test to verify it passes**

Run: `HF_ENDPOINT=https://hf-mirror.com pytest backend/tests/test_struct_chunker.py -v 2>&1 | tail -30`
Expected: All tests PASS

- [ ] **Step 5: Update exports**

Edit `backend/app/document/__init__.py`:

```python
from .struct_chunker import StructureAwareChunker
```

Update `__all__` to include `"StructureAwareChunker"`.

- [ ] **Step 6: Commit**

```bash
git add backend/app/document/struct_chunker.py backend/app/document/__init__.py backend/tests/test_struct_chunker.py
git commit -m "feat: add StructureAwareChunker (by_title strategy)"
```

---

### Task 6: DocumentUploader 集成新管道

**Files:**
- Modify: `backend/app/document/uploader.py`

**Interfaces:**
- Consumes: `ParserFactory`, `StructureAwareChunker`, `StructuredDocument` (from Tasks 4&5)
- Produces: 保持 `DocumentUploader.upload(...) -> dict` 接口不变

- [ ] **Step 1: Write failing test**

Add to `backend/tests/test_document_upload.py`:

```python
class TestUploaderNewPipeline:
    @pytest.mark.asyncio
    async def test_upload_text_uses_new_pipeline(self):
        """验证新 pipeline 能正确处理 TXT 文件"""
        from backend.app.document.uploader import DocumentUploader
        from backend.app.knowledge.unified_store import UnifiedKnowledgeStore
        from backend.app.retrieval.embeddings import TFIDFModel

        store = UnifiedKnowledgeStore(
            embedding_model=TFIDFModel(max_features=100),
            collection_name="test_pipeline",
            persist_directory=":memory:",
        )
        uploader = DocumentUploader(knowledge_store=store)

        result = await uploader.upload(
            content=b"# Test\n\nHello world.",
            filename="test.md",
            title="Test Doc",
        )
        assert result["chunk_count"] >= 1
        assert result["filename"] == "test.md"

    @pytest.mark.asyncio
    async def test_upload_empty_content_raises_error(self):
        """空内容应报错"""
        from backend.app.document.uploader import DocumentUploader
        from backend.app.knowledge.unified_store import UnifiedKnowledgeStore
        from backend.app.retrieval.embeddings import TFIDFModel

        store = UnifiedKnowledgeStore(
            embedding_model=TFIDFModel(max_features=100),
            collection_name="test_empty",
            persist_directory=":memory:",
        )
        uploader = DocumentUploader(knowledge_store=store)

        import pytest
        with pytest.raises(ValueError, match="文档内容为空|parse failed|cannot parse"):
            await uploader.upload(content=b"", filename="empty.txt")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest backend/tests/test_document_upload.py::TestUploaderNewPipeline -v 2>&1 | head -20`
Expected: Related to old uploader not using new pipeline

- [ ] **Step 3: Modify DocumentUploader**

Edit `backend/app/document/uploader.py`. Key changes:

1. Change import to use ParserFactory and StructureAwareChunker
2. Modify `upload()` method to use new pipeline while keeping same return format

Replace the import section:

```python
from .parser import ParserFactory  # 新增
from .struct_chunker import StructureAwareChunker  # 新增
from .models import StructuredDocument, Chunk  # 新增
from .chunker import DocumentChunker  # 保留用于降级
```

Modify the `upload` method. Keep the same method signature and return type, but change the internal logic:

```python
async def upload(
    self,
    content: bytes,
    filename: str,
    title: Optional[str] = None,
    course_id: Optional[str] = None,
    user_id: Optional[str] = None,
    topic_id: Optional[str] = None,
    document_id: Optional[str] = None,
) -> dict:
    """上传并索引文档（使用新的结构化管道）"""
    document_id = document_id or str(uuid.uuid4())

    # 1. 使用新管道解析
    try:
        parser = ParserFactory.get_parser(filename)
        doc = parser.parse(content, filename)
    except Exception as e:
        logger.warning(f"新解析器失败，降级到旧解析器: {e}")
        text = self.parser.parse(content, filename)
        if not text.strip():
            raise ValueError("文档内容为空或解析失败")
        # 用旧管道分块
        ext = Path(filename).suffix.lower()
        force_strategy = "markdown" if ext in (".md", ".markdown") else "recursive"
        chunks = self.chunker.chunk(text, document_id, force_strategy=force_strategy)
        items = [KnowledgeItem(
            id=chunk["id"], title=title or filename,
            content=chunk["text"], source="user_document",
            metadata={**self.parser.get_metadata(filename, title), **chunk["metadata"]}
        ) for chunk in chunks]
        if self.knowledge_store:
            self.knowledge_store.add_batch(items)
        return {
            "document_id": document_id, "filename": filename,
            "title": title or filename, "chunk_count": len(chunks),
            "char_count": len(text), "file_path": None,
        }

    # 2. 新的结构化分块
    chunker = StructureAwareChunker(max_chars=self.chunker.chunk_size)
    chunks = chunker.chunk(doc)

    if not chunks:
        raise ValueError("文档分块后为空")

    # 3. 转换为 KnowledgeItem（保持与现有接口兼容）
    metadata_base = {
        "document_id": document_id,
        "filename": filename,
        "title": title or filename,
        "user_id": user_id,
        "topic_id": topic_id,
        "course_id": course_id,
        "source": "user_document",
    }

    items = []
    for chunk in chunks:
        merged = {**metadata_base, **chunk.metadata}

        # 过滤空值
        cleaned = {k: v for k, v in merged.items() if v not in (None, [], "")}

        item = KnowledgeItem(
            id=chunk.id,
            title=title or filename,
            content=chunk.text,
            source="user_document",
            metadata=cleaned,
        )
        items.append(item)

    # 4. 批量入库
    if not self.knowledge_store:
        raise RuntimeError("UnifiedKnowledgeStore 未初始化")
    self.knowledge_store.add_batch(items)

    char_count = sum(len(c.text) for c in chunks)

    logger.info(f"文档上传完成: {filename} -> {document_id}, 共 {len(chunks)} 块 (结构化管道)")
    return {
        "document_id": document_id,
        "filename": filename,
        "title": title or filename,
        "chunk_count": len(chunks),
        "char_count": char_count,
        "file_path": None,
    }
```

Also add `from .chunker import DocumentChunker` in the import section if needed (already imported).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest backend/tests/test_document_upload.py -v 2>&1 | tail -30`
Expected: All tests PASS (both old and new)

- [ ] **Step 5: Run existing full test suite**

Run: `pytest backend/tests/ -v 2>&1 | tail -40`
Expected: No regressions

- [ ] **Step 6: Commit**

```bash
git add backend/app/document/uploader.py
git commit -m "feat: integrate structured parser + by_title chunker into DocumentUploader"
```

---

## 执行后验证

完成上述所有任务后，运行完整测试：

```bash
cd backend
# 验证现有测试不破坏
pytest tests/ -v 2>&1 | tail -30

# 用真实 PDF 验证效果
HF_ENDPOINT=https://hf-mirror.com python -c "
from app.document.uploader import DocumentUploader
from app.knowledge.unified_store import UnifiedKnowledgeStore
from app.retrieval.embeddings import TFIDFModel

store = UnifiedKnowledgeStore(embedding_model=TFIDFModel(max_features=500), collection_name='verify', persist_directory=':memory:')
uploader = DocumentUploader(knowledge_store=store)

# 测试 TXT
result = await uploader.upload(b'# Hello\\n\\nWorld', 'test.md', title='Test')
print(f'TXT: {result}')

# 测试 PDF
with open('/tmp/test_complex_doc.pdf', 'rb') as f:
    result = await uploader.upload(f.read(), 'test.pdf', title='Complex Doc')
print(f'PDF: {result}')
print(f'Chunks: {result[\"chunk_count\"]}')
"
```
