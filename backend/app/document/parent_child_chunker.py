"""
Parent-Child Chunker - 父子文档分块器

核心策略：
- 父块（Parent）：按文档结构中的 section 切分（通常是一级/二级标题下的内容）
- 子块（Child）：在父块内部按语义单元切分（段落、列表项、表格、代码等）
- 检索时命中子块，返回对应父块给 LLM，兼顾精准召回和完整上下文

参考实现借鉴了 LangChain ParentDocumentRetriever 的两级分块思想，
但基于 StructuredDocument 的元素结构进行语义边界保留。
"""

import re
import uuid
from typing import List, Tuple

from loguru import logger

from .models import (
    Chunk,
    DocumentElement,
    ElementType,
    StructuredDocument,
)

# 装饰图判定关键词：caption 命中以下任一词时视为装饰图（品牌 logo / 水印 / 图标）
# 来源：VLM 对品牌标识类图片的典型描述用词
_DECORATIVE_CAPTION_KEYWORDS = [
    "logo", "品牌标识", "品牌标志", "商标", "图标", "水印",
    "品牌", "icon", "brand", "徽标", "标识图",
]


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

        # 0. 噪声过滤：移除版权声明、页码、目录页等噪声元素
        doc.elements = self._filter_noise_elements(doc.elements)

        # 0.3 装饰图过滤：品牌 logo / 水印 / 图标等不携带检索语义的图片直接剔除
        # 避免品牌名（如"黑马程序员"）污染检索结果
        doc.elements = self._filter_decorative_images(doc.elements)

        # 0.5 跨页表格合并：连续 TABLE 元素且页面号连续时合并
        doc.elements = self._merge_cross_page_tables(doc.elements)

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

    # 噪声匹配模式
    _NOISE_PATTERNS = [
        re.compile(r'版权所有|Copyright|All\s+rights\s+reserved|保留所有权利', re.IGNORECASE),
        re.compile(r'^第\s*\d+\s*页$|^Page\s+\d+$|^\d+\s*/\s*\d+$'),  # 页码
    ]
    _TOC_TITLES = {"目录", "目錕", "contents", "table of contents", "索引"}

    def _filter_noise_elements(self, elements: List[DocumentElement]) -> List[DocumentElement]:
        """过滤噪声元素：版权声明、页码、目录页条目"""
        if not elements:
            return elements

        filtered: List[DocumentElement] = []
        skip_toc = False  # 是否正在跳过目录页内容
        removed = 0

        for el in elements:
            text = (el.text or "").strip()
            if not text:
                filtered.append(el)
                continue

            # 目录页检测：标题为"目录"时，跳过后续到下一个同级或更高级标题
            if el.type in (ElementType.HEADING, ElementType.TITLE):
                # 去掉 Markdown 标题前缀（# ## ###）
                clean_title = re.sub(r'^#+\s*', '', text).strip().lower()
                if clean_title in self._TOC_TITLES:
                    skip_toc = True
                    removed += 1
                    continue
                # 遇到非目录标题，停止跳过
                if skip_toc:
                    skip_toc = False

            if skip_toc:
                removed += 1
                continue

            # 版权声明 / 页码
            is_noise = any(p.search(text) for p in self._NOISE_PATTERNS)
            if is_noise:
                removed += 1
                continue

            filtered.append(el)

        if removed > 0:
            logger.debug(f"噪声过滤: 移除 {removed} 个噪声元素，剩余 {len(filtered)} 个")

        return filtered

    def _filter_decorative_images(
        self, elements: List[DocumentElement]
    ) -> List[DocumentElement]:
        """过滤装饰图：品牌 logo / 水印 / 图标等无检索价值的图片

        判定规则（满足任一即剔除）：
        1. image_type 为 "other" 或 "photo" 且 caption 命中装饰图关键词
        2. caption 明确包含品牌标识类描述（如"品牌标识"、"logo"）

        剔除后这些图片不进入父块/子块，避免品牌名等无关文字污染检索。
        """
        if not elements:
            return elements

        filtered: List[DocumentElement] = []
        removed = 0
        for el in elements:
            if el.type == ElementType.IMAGE and self._is_decorative_image(el):
                removed += 1
                continue
            filtered.append(el)

        if removed > 0:
            logger.info(
                f"装饰图过滤: 移除 {removed} 个品牌 logo/水印/图标，剩余 {len(filtered)} 个元素"
            )

        return filtered

    @staticmethod
    def _is_decorative_image(element: DocumentElement) -> bool:
        """判断图片是否为装饰图（品牌 logo / 水印 / 图标，无检索价值）

        判定依据：
        - image_type 为 "other" 或 "photo"（非内容型图片）
        - caption 命中装饰图关键词列表

        保守策略：只过滤明确标识为品牌/logo 的图片，
        保留 diagram/screenshot/chart/table/formula/code 等内容型图片。
        """
        img_type = (element.image_type or "").lower().strip()
        caption = (element.image_desc or "").lower()

        # 只对非内容型图片做关键词检测
        # diagram/screenshot/chart/table/formula/code 是内容型，保留
        if img_type in ("diagram", "screenshot", "chart", "table", "formula", "code"):
            return False

        # image_type 为 other/photo/空 时，检查 caption 是否含品牌标识关键词
        for kw in _DECORATIVE_CAPTION_KEYWORDS:
            if kw in caption:
                return True

        return False

    @staticmethod
    def _count_table_columns(element: DocumentElement) -> int:
        """估算表格列数（通过 HTML 或 Markdown 文本）"""
        if element.text_as_html:
            # 数 <th> 标签（表头列数）
            return element.text_as_html.count("<th>")
        if element.text:
            # 数第一行的 | 数量
            first_line = element.text.split("\n")[0] if "\n" in element.text else element.text
            return first_line.count("|") - 1 if first_line.count("|") > 1 else 0
        return 0

    def _merge_cross_page_tables(self, elements: List[DocumentElement]) -> List[DocumentElement]:
        """合并跨页被截断的表格：连续 TABLE 元素且列数相同、页面号连续时合并"""
        if len(elements) < 2:
            return elements

        merged: List[DocumentElement] = []
        merged_count = 0

        i = 0
        while i < len(elements):
            current = elements[i]

            # 只处理 TABLE 类型
            if current.type != ElementType.TABLE:
                merged.append(current)
                i += 1
                continue

            # 查看后续是否有可合并的 TABLE
            while i + 1 < len(elements) and elements[i + 1].type == ElementType.TABLE:
                nxt = elements[i + 1]
                cur_cols = self._count_table_columns(current)
                nxt_cols = self._count_table_columns(nxt)

                # 列数相同且页面号连续（或缺失页面号）
                cur_page = current.metadata.page_number or 0
                nxt_page = nxt.metadata.page_number or 0
                page_ok = (cur_page == 0 or nxt_page == 0 or nxt_page - cur_page <= 1)

                if cur_cols > 0 and cur_cols == nxt_cols and page_ok:
                    # 合并：text 拼接，text_as_html 去掉第二个表头后拼接
                    current.text = (current.text or "") + "\n" + (nxt.text or "")
                    if current.text_as_html and nxt.text_as_html:
                        # 去掉第二个表格的 <thead>...</thead>
                        nxt_body = nxt.text_as_html
                        if "<thead>" in nxt_body:
                            import re as _re
                            nxt_body = _re.sub(r'<thead>.*?</thead>', '', nxt_body, flags=_re.DOTALL)
                            # 补上 <tbody> 如果被去掉了
                            if not nxt_body.startswith("<tbody>") and "<tbody>" in nxt_body:
                                current.text_as_html = current.text_as_html.replace(
                                    "</table>", nxt_body.replace("<table>", "").replace("</table>", "") + "</table>"
                                )
                            else:
                                current.text_as_html = current.text_as_html.replace(
                                    "</table>", nxt_body + "</table>"
                                )
                        else:
                            current.text_as_html = current.text_as_html.replace(
                                "</table>", nxt_body + "</table>"
                            )
                    merged_count += 1
                    i += 1  # 跳过被合并的元素
                else:
                    break  # 不满足合并条件，退出内层循环

            merged.append(current)
            i += 1

        if merged_count > 0:
            logger.debug(f"跨页表格合并: 合并了 {merged_count} 个表格片段")

        return merged

    # ========== 结构化元素描述生成 ==========

    def _build_element_description(self, element: DocumentElement) -> str:
        """为结构化元素生成描述前缀，增强向量化和 BM25 的语义匹配"""
        if element.type == ElementType.TABLE:
            return self._build_table_description(element)
        elif element.type == ElementType.CODE:
            return self._build_code_description(element)
        elif element.type == ElementType.FORMULA:
            return "[公式]"
        return ""

    @staticmethod
    def _build_table_description(element: DocumentElement) -> str:
        """从 HTML 或 Markdown 中提取表格结构信息，生成描述前缀"""
        cols: List[str] = []
        rows = 0

        # 优先从 HTML 提取列名
        if element.text_as_html:
            ths = re.findall(r'<th>(.*?)</th>', element.text_as_html)
            cols = [th.strip() for th in ths if th.strip()]
            trs = re.findall(r'<tr>', element.text_as_html)
            rows = max(len(trs) - 1, 0)  # 减去表头行

        # 从 Markdown 提取（如果没有 HTML）
        if not cols and element.text:
            lines = element.text.strip().split("\n")
            if lines and "|" in lines[0]:
                cols = [c.strip() for c in lines[0].strip("|").split("|") if c.strip()]
            if len(lines) > 2:
                rows = len(lines) - 2  # 减去表头和分隔行

        if not cols and rows == 0:
            return "[表格]"

        parts = ["[表格]"]
        if cols and rows:
            parts.append(f"这是一个{len(cols)}列{rows}行的表格，列名：{'/'.join(cols)}。")
        elif cols:
            parts.append(f"列名：{'/'.join(cols)}。")

        return " ".join(parts)

    @staticmethod
    def _build_code_description(element: DocumentElement) -> str:
        """从代码中提取语言和功能信息，生成描述前缀"""
        text = element.text or ""
        if not text.strip():
            return "[代码]"

        # 检测语言
        lang = ""
        first_line = text.split("\n")[0]
        if first_line.startswith("# language:"):
            lang = first_line.replace("# language:", "").strip()

        # 提取函数名/类名
        func_name = ""
        for pattern in [
            r'^def\s+(\w+)',
            r'^class\s+(\w+)',
            r'^function\s+(\w+)',
            r'^public\s+(?:static\s+)?(?:void|class)\s+(\w+)',
        ]:
            m = re.search(pattern, text, re.MULTILINE)
            if m:
                func_name = m.group(1)
                break

        # 提取 docstring 第一行作为描述
        comment = ""
        doc = re.search(r'"""(.+?)"""', text, re.DOTALL) or re.search(r"'''(.+?)'''", text, re.DOTALL)
        if doc:
            comment = doc.group(1).strip().split("\n")[0][:60]
        else:
            # 尝试 # 注释
            comment_match = re.search(r'^#\s*(.+)$', text, re.MULTILINE)
            if comment_match and not comment_match.group(1).startswith("language:"):
                comment = comment_match.group(1).strip()[:60]

        # 组装（语言名首字母大写）
        lang_label = f"[{lang.capitalize()}代码]" if lang else "[代码]"
        desc_parts = [lang_label]
        if func_name:
            desc_parts.append(f"{func_name}")
        if comment:
            desc_parts.append(f"：{comment}")

        return " ".join(desc_parts)

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
        # 多模态 RAG：图片元素用 caption 占位，避免父块文本缺失
        parent_text_parts: List[str] = []
        for element in section:
            text = self._element_display_text(element)
            if text:
                parent_text_parts.append(text)
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

    @staticmethod
    def _element_display_text(element: DocumentElement) -> str:
        """
        获取元素用于父块文本拼接的展示文本

        多模态 RAG：
        - 普通元素：直接用 text
        - IMAGE 元素：用 image_desc（VLM caption）+ OCR 文本
          没有描述时用占位符"[图片]"，避免父块丢失上下文
        """
        if element.type != ElementType.IMAGE:
            return element.text or ""

        parts = []
        if element.image_desc:
            parts.append(f"[图片描述] {element.image_desc}")
        if element.ocr_text:
            parts.append(f"[图片文字] {element.ocr_text}")
        if not parts:
            # 既没有 caption 也没有 OCR，至少留个占位
            parts.append("[图片]")
        return "\n".join(parts)

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

            # 多模态 RAG：IMAGE 元素独立成子块
            # 子块文本使用 VLM caption + OCR 文本 + 关键词
            # 元数据携带 image_path 供上层展示
            if element.type == ElementType.IMAGE:
                flush_child(buffer)

                # 构造图片子块的检索文本
                img_text_parts: List[str] = []
                if element.image_desc:
                    img_text_parts.append(element.image_desc)
                if element.ocr_text:
                    img_text_parts.append(f"图中文字：{element.ocr_text}")
                if element.image_keywords:
                    img_text_parts.append("关键词：" + "、".join(element.image_keywords))

                img_text = "\n".join(img_text_parts) if img_text_parts else "[图片]"
                if not img_text.strip():
                    img_text = "[图片]"

                # 超长 caption 切分
                if len(img_text) > self.child_max_chars:
                    parts = self._split_text(img_text)
                else:
                    parts = [img_text]

                meta_extra = {
                    "element_type": "image",
                    "image_path": element.image_path,
                    "image_type": element.image_type,
                    "image_keywords": list(element.image_keywords) if element.image_keywords else [],
                    "has_caption": bool(element.image_desc),
                    "has_ocr": bool(element.ocr_text),
                }
                # 去掉 None 值，避免 metadata 序列化问题
                meta_extra = {k: v for k, v in meta_extra.items() if v not in (None, [], "")}

                for part in parts:
                    flush_child([part], "image")
                    if child_chunks:
                        child_chunks[-1].metadata.update(meta_extra)
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

                # 结构化描述前缀：增强 embedding 和 BM25 的语义匹配
                desc = self._build_element_description(element)
                enriched_text = (desc + "\n" + text) if desc else text

                # 超长元素切分（描述前缀只在第一部分保留）
                if len(enriched_text) > self.child_max_chars:
                    parts = self._split_text(enriched_text)
                    for part in parts:
                        flush_child([part], element.type.value)
                else:
                    flush_child([enriched_text], element.type.value)
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
