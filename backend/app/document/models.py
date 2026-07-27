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
        if self.bbox is not None:
            d["bbox"] = self.bbox
        if self.font is not None:
            d["font"] = self.font
        if self.font_size is not None:
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
