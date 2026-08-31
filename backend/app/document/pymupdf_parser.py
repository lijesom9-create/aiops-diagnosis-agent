"""
PyMuPDF PDF 解析器

使用 PyMuPDF (fitz) 实现高质量的 PDF 解析：
- 保留坐标信息
- 跨页段落合并
- 表格提取
- 页眉页脚清理
- 多栏布局支持

依赖：
- pymupdf (fitz)
- pdfplumber (表格提取)
"""

import re
from dataclasses import dataclass
from typing import Dict, List, Tuple

from loguru import logger


@dataclass
class TextBlock:
    """文本块"""
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    page: int
    page_width: float
    page_height: float
    font: str = ""
    size: float = 0
    block_no: int = 0

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def is_at_page_top(self) -> bool:
        """是否在页面顶部（可能是页眉）"""
        return self.y0 < self.page_height * 0.1

    @property
    def is_at_page_bottom(self) -> bool:
        """是否在页面底部（可能是页脚）"""
        return self.y1 > self.page_height * 0.9

    @property
    def is_centered(self) -> bool:
        """是否居中（可能是标题）"""
        page_center = self.page_width / 2
        block_center = (self.x0 + self.x1) / 2
        return abs(block_center - page_center) < 50

    def to_dict(self) -> Dict:
        return {
            "text": self.text,
            "bbox": (self.x0, self.y0, self.x1, self.y1),
            "page": self.page,
            "font": self.font,
            "size": self.size,
        }


@dataclass
class PageMetadata:
    """页面元数据"""
    page_no: int
    width: float
    height: float
    header_y: float = 0  # 页眉 Y 坐标
    footer_y: float = 0  # 页脚 Y 坐标
    left_margin: float = 0
    right_margin: float = 0


class PyMuPDFParser:
    """
    PyMuPDF PDF 解析器

    特点：
    - 保留坐标信息，支持精确的布局分析
    - 跨页段落合并
    - 页眉页脚识别和移除
    - 多栏布局支持
    """

    def __init__(
        self,
        remove_headers: bool = True,
        remove_footers: bool = True,
        merge_cross_page: bool = True,
        detect_columns: bool = True,
    ):
        """
        Args:
            remove_headers: 是否移除页眉
            remove_footers: 是否移除页脚
            merge_cross_page: 是否合并跨页段落
            detect_columns: 是否检测多栏布局
        """
        self.remove_headers = remove_headers
        self.remove_footers = remove_footers
        self.merge_cross_page = merge_cross_page
        self.detect_columns = detect_columns

    def parse(self, pdf_path: str) -> Dict:
        """
        解析 PDF 文件

        Args:
            pdf_path: PDF 文件路径

        Returns:
            Dict: {
                "text": str,           # 合并后的文本
                "pages": List[str],    # 每页文本
                "blocks": List[Dict],  # 所有文本块（带坐标）
                "tables": List[Dict],  # 表格
                "metadata": Dict,      # 元数据
            }
        """
        try:
            import fitz  # PyMuPDF
        except ImportError:
            logger.error("PyMuPDF 未安装，请运行: pip install pymupdf")
            raise

        doc = fitz.open(pdf_path)
        logger.info(f"开始解析 PDF: {pdf_path}, 共 {len(doc)} 页")

        # 1. 提取所有页面的文本块
        all_blocks = []
        page_metadata = []

        for page_idx, page in enumerate(doc):
            blocks = self._extract_page_blocks(page, page_idx)
            all_blocks.append(blocks)

            page_metadata.append(PageMetadata(
                page_no=page_idx,
                width=page.rect.width,
                height=page.rect.height,
            ))

        # 2. 识别页眉页脚
        if self.remove_headers or self.remove_footers:
            all_blocks = self._remove_headers_footers(all_blocks, page_metadata)

        # 3. 跨页段落合并
        if self.merge_cross_page:
            merged_blocks = self._merge_cross_page_blocks(all_blocks, page_metadata)
        else:
            merged_blocks = [block for page_blocks in all_blocks for block in page_blocks]

        # 4. 提取文本
        pages_text = []
        for page_blocks in all_blocks:
            page_text = "\n".join(block.text for block in page_blocks if block.text.strip())
            pages_text.append(page_text)

        full_text = "\n\n".join(block.text for block in merged_blocks if block.text.strip())

        # 5. 提取表格（使用 pdfplumber）
        tables = self._extract_tables(pdf_path)

        doc.close()

        logger.info(f"PDF 解析完成: {len(pages_text)} 页, {len(merged_blocks)} 个文本块")

        return {
            "text": full_text,
            "pages": pages_text,
            "blocks": [block.to_dict() for block in merged_blocks],
            "tables": tables,
            "metadata": {
                "page_count": len(pages_text),
                "block_count": len(merged_blocks),
                "table_count": len(tables),
            },
        }

    def _extract_page_blocks(self, page, page_idx: int) -> List[TextBlock]:
        """提取页面的文本块"""
        blocks = []

        # 使用 get_text("dict") 获取详细信息
        text_dict = page.get_text("dict")

        for block in text_dict.get("blocks", []):
            if block.get("type") == 0:  # 文本块
                # 合并同一块内的所有行
                lines_text = []
                for line in block.get("lines", []):
                    line_text = ""
                    for span in line.get("spans", []):
                        line_text += span.get("text", "")
                    if line_text.strip():
                        lines_text.append(line_text.strip())

                if lines_text:
                    text = " ".join(lines_text)
                    bbox = block.get("bbox", (0, 0, 0, 0))

                    # 获取字体信息
                    font = ""
                    size = 0
                    for line in block.get("lines", []):
                        for span in line.get("spans", []):
                            font = span.get("font", "")
                            size = span.get("size", 0)
                            break
                        if font:
                            break

                    blocks.append(TextBlock(
                        text=text,
                        x0=bbox[0],
                        y0=bbox[1],
                        x1=bbox[2],
                        y1=bbox[3],
                        page=page_idx,
                        page_width=page.rect.width,
                        page_height=page.rect.height,
                        font=font,
                        size=size,
                    ))

        return blocks

    def _remove_headers_footers(
        self,
        all_blocks: List[List[TextBlock]],
        page_metadata: List[PageMetadata],
    ) -> List[List[TextBlock]]:
        """识别并移除页眉页脚"""

        if len(all_blocks) < 3:
            return all_blocks

        # 1. 收集所有页面顶部和底部的文本
        top_texts = []  # (text, page_idx)
        bottom_texts = []  # (text, page_idx)

        for page_idx, blocks in enumerate(all_blocks):
            for block in blocks:
                if block.is_at_page_top:
                    top_texts.append((block.text.strip(), page_idx))
                if block.is_at_page_bottom:
                    bottom_texts.append((block.text.strip(), page_idx))

        # 2. 识别页眉（在多个页面顶部重复出现的文本）
        header_texts = self._find_repeating_texts(top_texts, len(all_blocks))

        # 3. 识别页脚（在多个页面底部重复出现的文本）
        footer_texts = self._find_repeating_texts(bottom_texts, len(all_blocks))

        # 4. 移除页眉页脚
        cleaned_blocks = []
        for page_idx, blocks in enumerate(all_blocks):
            page_blocks = []
            for block in blocks:
                text = block.text.strip()

                # 检查是否是页眉
                if self.remove_headers and text in header_texts:
                    if block.is_at_page_top:
                        logger.debug(f"移除页眉 (第 {page_idx + 1} 页): {text[:30]}...")
                        continue

                # 检查是否是页脚
                if self.remove_footers and text in footer_texts:
                    if block.is_at_page_bottom:
                        logger.debug(f"移除页脚 (第 {page_idx + 1} 页): {text[:30]}...")
                        continue

                # 检查是否是页码
                if self._is_page_number(text):
                    if block.is_at_page_bottom or block.is_at_page_top:
                        logger.debug(f"移除页码 (第 {page_idx + 1} 页): {text}")
                        continue

                page_blocks.append(block)

            cleaned_blocks.append(page_blocks)

        return cleaned_blocks

    def _find_repeating_texts(self, texts: List[Tuple[str, int]], total_pages: int) -> set:
        """找到在多个页面重复出现的文本"""
        from collections import Counter

        # 统计每个文本出现的次数
        text_counter = Counter(text for text, _ in texts)

        # 如果文本在超过一半的页面出现，认为是页眉页脚
        threshold = max(2, total_pages // 2)
        repeating = {text for text, count in text_counter.items() if count >= threshold}

        return repeating

    def _is_page_number(self, text: str) -> bool:
        """判断是否是页码"""
        text = text.strip()

        # 纯数字
        if text.isdigit():
            return True

        # 常见页码格式
        page_patterns = [
            r'^\d+$',                    # 1, 2, 3
            r'^第\d+页$',                # 第1页
            r'^Page\s+\d+$',             # Page 1
            r'^-\s*\d+\s*-$',            # - 1 -
            r'^\d+\s*/\s*\d+$',          # 1/10
            r'^第\s*\d+\s*页\s*共\s*\d+\s*页$',  # 第1页 共10页
        ]

        for pattern in page_patterns:
            if re.match(pattern, text):
                return True

        return False

    def _merge_cross_page_blocks(
        self,
        all_blocks: List[List[TextBlock]],
        page_metadata: List[PageMetadata],
    ) -> List[TextBlock]:
        """跨页段落合并"""

        merged = []

        for page_idx, page_blocks in enumerate(all_blocks):
            for block in page_blocks:
                if not block.text.strip():
                    continue

                if not merged:
                    merged.append(block)
                    continue

                prev_block = merged[-1]

                # 判断是否需要合并
                if self._should_merge_blocks(prev_block, block, page_idx):
                    # 合并文本
                    prev_block.text += block.text
                    # 更新坐标
                    prev_block.y1 = block.y1
                    prev_block.x1 = max(prev_block.x1, block.x1)
                else:
                    merged.append(block)

        return merged

    def _should_merge_blocks(
        self,
        prev_block: TextBlock,
        curr_block: TextBlock,
        curr_page_idx: int,
    ) -> bool:
        """判断两个块是否应该合并"""

        # 1. 必须是跨页（上一个块在前一页）
        if prev_block.page >= curr_block.page:
            return False

        # 2. 上一块不在页面底部（可能是段落中间）
        if prev_block.is_at_page_bottom:
            return False

        # 3. 当前块不在页面顶部（可能是新段落）
        if curr_block.is_at_page_top:
            return False

        # 4. 上一块不以段落结束符号结尾
        prev_text = prev_block.text.rstrip()
        if prev_text.endswith(('。', '！', '？', '.', '!', '?', '：', ':', '"', '"', '）', ')')):
            return False

        # 5. 当前块不以段落开始符号开头
        curr_text = curr_block.text.strip()
        if not curr_text:
            return False

        # 新段落开始的特征
        paragraph_start_patterns = [
            r'^\d+[\.\)]',  # 数字列表
            r'^[一二三四五六七八九十]+[、.]',  # 中文数字
            r'^第[一二三四五六七八九十\d]+[章节部分]',  # 章节
            r'^[A-Z]',  # 英文大写开头
            r'^[•·▪]',  # 列表符号
            r'^[-*+]\s',  # 无序列表
        ]

        for pattern in paragraph_start_patterns:
            if re.match(pattern, curr_text):
                return False

        # 6. 字体和字号相似（如果有信息）
        if prev_block.font and curr_block.font:
            if prev_block.font != curr_block.font:
                return False

        if prev_block.size and curr_block.size:
            if abs(prev_block.size - curr_block.size) > 2:
                return False

        return True

    def _extract_tables(self, pdf_path: str) -> List[Dict]:
        """提取表格（使用 pdfplumber）"""
        try:
            import pdfplumber

            tables = []

            with pdfplumber.open(pdf_path) as pdf:
                for page_idx, page in enumerate(pdf.pages):
                    page_tables = page.extract_tables()

                    for table_idx, table in enumerate(page_tables):
                        if table:
                            # 转换为文本
                            table_text = self._format_table(table)

                            tables.append({
                                "page": page_idx + 1,
                                "table_index": table_idx + 1,
                                "content": table_text,
                                "rows": len(table),
                                "cols": len(table[0]) if table else 0,
                            })

            return tables

        except ImportError:
            logger.warning("pdfplumber 未安装，无法提取表格")
            return []
        except Exception as e:
            logger.warning(f"表格提取失败: {e}")
            return []

    def _format_table(self, table: List[List]) -> str:
        """格式化表格为文本"""
        if not table:
            return ""

        rows = []
        for row in table:
            cells = [str(cell or "").strip() for cell in row]
            rows.append(" | ".join(cells))

        return "\n".join(rows)


class PDFPostProcessor:
    """PDF 后处理器"""

    @staticmethod
    def clean_text(text: str) -> str:
        """清理文本"""
        # 1. 移除多余空白
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r'[ \t]+', ' ', text)

        # 2. 移除特殊字符
        text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)

        # 3. 统一换行符
        text = text.replace('\r\n', '\n').replace('\r', '\n')

        return text.strip()

    @staticmethod
    def split_paragraphs(text: str, max_length: int = 1000) -> List[str]:
        """将文本分成段落"""

        # 1. 按双换行分段
        paragraphs = re.split(r'\n\s*\n', text)

        # 2. 处理长段落
        result = []
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            if len(para) > max_length:
                # 按句子分段
                sentences = PDFPostProcessor._split_sentences(para)
                result.extend(sentences)
            else:
                result.append(para)

        return result

    @staticmethod
    def _split_sentences(text: str) -> List[str]:
        """按句子分段"""
        # 中文句子分隔符
        parts = re.split(r'([。！？；])', text)

        sentences = []
        current = ""
        for part in parts:
            if re.match(r'[。！？；]', part):
                current += part
                if current.strip():
                    sentences.append(current.strip())
                current = ""
            else:
                current += part

        if current.strip():
            sentences.append(current.strip())

        return sentences

    @staticmethod
    def detect_language(text: str) -> str:
        """检测文本语言"""
        # 简单的语言检测
        chinese_chars = len(re.findall(r'[一-鿿]', text))
        english_chars = len(re.findall(r'[a-zA-Z]', text))

        if chinese_chars > english_chars:
            return "zh"
        elif english_chars > chinese_chars:
            return "en"
        else:
            return "mixed"
