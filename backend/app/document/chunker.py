"""
文档分块器

支持四种分块策略：
1. 固定大小分块（简单，快速）
2. 智能分块（按段落、标题，效果好）
3. Markdown 结构化分块（基于 LangChain，保留标题路径）
4. 递归分块（纯文本，按段落/句子/空格递归切分）
"""

import re
from typing import Dict, List, Optional

from loguru import logger


class DocumentChunker:
    """文档分块器"""

    def __init__(
        self,
        chunk_size: int = 500,
        chunk_overlap: int = 50,
        separator: str = "\n",
        strategy: str = "auto",  # auto, fixed, smart, markdown, recursive
    ):
        """
        Args:
            chunk_size: 每个分块的最大字符数
            chunk_overlap: 分块之间的重叠字符数
            separator: 优先在此分隔符处切分
            strategy: 分块策略（auto, fixed, smart, markdown, recursive）
                - auto: 自动检测，Markdown 用 markdown 策略，其他用 recursive
                - fixed: 固定大小分块
                - smart: 智能分块（按段落、标题）
                - markdown: Markdown 结构化分块（保留标题路径）
                - recursive: 递归分块（纯文本，按段落/句子/空格递归切分）
        """
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.separator = separator
        self.strategy = strategy

    def chunk(self, text: str, document_id: str, force_strategy: Optional[str] = None) -> List[dict]:
        """
        将文本分块

        Args:
            text: 文档纯文本或 Markdown
            document_id: 文档唯一标识
            force_strategy: 强制使用指定策略（覆盖 self.strategy）

        Returns:
            List[dict]: 分块列表，每个分块包含 id, text, metadata
        """
        if not text:
            return []

        strategy = force_strategy or self.strategy

        # 自动检测策略
        if strategy == "auto":
            if self._is_markdown(text):
                strategy = "markdown"
                logger.info(f"文档 {document_id} 检测为 Markdown，使用 markdown 策略")
            else:
                strategy = "recursive"
                logger.info(f"文档 {document_id} 检测为纯文本，使用 recursive 策略")

        if strategy == "markdown":
            return self._markdown_chunk(text, document_id)
        elif strategy == "recursive":
            return self._recursive_chunk(text, document_id)
        elif strategy == "smart":
            return self._smart_chunk(text, document_id)
        else:
            return self._fixed_chunk(text, document_id)

    def _is_markdown(self, text: str) -> bool:
        """
        检测文本是否是 Markdown 格式

        检测规则：
        1. 包含 # 标题
        2. 包含 ``` 代码块
        3. 包含 [text](url) 链接
        """
        lines = text.split("\n")
        markdown_indicators = 0

        for line in lines[:50]:  # 只检查前 50 行
            stripped = line.strip()
            # 标题
            if re.match(r'^#{1,6}\s+', stripped):
                markdown_indicators += 2
            # 代码块
            if stripped.startswith("```"):
                markdown_indicators += 2
            # 链接
            if re.search(r'\[.*?\]\(.*?\)', stripped):
                markdown_indicators += 1
            # 列表
            if re.match(r'^[-*+]\s+', stripped):
                markdown_indicators += 1
            # 引用
            if stripped.startswith(">"):
                markdown_indicators += 1

        return markdown_indicators >= 3

    def _markdown_chunk(self, text: str, document_id: str) -> List[dict]:
        """
        Markdown 结构化分块（基于 LangChain）

        保留标题路径（Breadcrumb），如：
        - heading_path: ['Python 装饰器', '基本语法', '示例代码']
        """
        try:
            from langchain_text_splitters import (
                MarkdownHeaderTextSplitter,
                RecursiveCharacterTextSplitter,
            )

            # 1. 定义标题层级
            headers_to_split_on = [
                ("#", "h1"),
                ("##", "h2"),
                ("###", "h3"),
                ("####", "h4"),
            ]

            # 2. 按标题分割
            markdown_splitter = MarkdownHeaderTextSplitter(
                headers_to_split_on=headers_to_split_on,
                strip_headers=False,  # 保留标题行
            )
            md_chunks = markdown_splitter.split_text(text)

            # 3. 对过长的块进一步分割
            text_splitter = RecursiveCharacterTextSplitter(
                chunk_size=self.chunk_size,
                chunk_overlap=self.chunk_overlap,
                length_function=len,
                separators=["\n\n", "\n", "。", "！", "？", ".", "!", "?", " ", ""],
            )

            chunks = []
            chunk_index = 0

            for md_chunk in md_chunks:
                # 构建标题路径（清理 Markdown 链接标记）
                metadata = md_chunk.metadata or {}
                heading_path = []
                for key in ["h1", "h2", "h3", "h4"]:
                    if key in metadata and metadata[key]:
                        clean_heading = self._clean_heading(metadata[key])
                        heading_path.append(clean_heading)

                heading = heading_path[-1] if heading_path else ""

                # 如果块太长，进一步分割
                if len(md_chunk.page_content) > self.chunk_size:
                    sub_chunks = text_splitter.split_text(md_chunk.page_content)
                    for _i, sub_text in enumerate(sub_chunks):
                        chunks.append({
                            "id": f"{document_id}_chunk_{chunk_index}",
                            "text": sub_text.strip(),
                            "metadata": {
                                "document_id": document_id,
                                "chunk_index": chunk_index,
                                "heading": heading,
                                "heading_path": heading_path,
                                "level": len(heading_path),
                                "source": "markdown",
                            },
                        })
                        chunk_index += 1
                else:
                    chunks.append({
                        "id": f"{document_id}_chunk_{chunk_index}",
                        "text": md_chunk.page_content.strip(),
                        "metadata": {
                            "document_id": document_id,
                            "chunk_index": chunk_index,
                            "heading": heading,
                            "heading_path": heading_path,
                            "level": len(heading_path),
                            "source": "markdown",
                        },
                    })
                    chunk_index += 1

            logger.info(f"文档 {document_id} Markdown 结构化分块完成: {len(chunks)} 块")
            return chunks

        except ImportError:
            logger.warning("langchain 未安装，回退到 smart 分块")
            return self._smart_chunk(text, document_id)
        except Exception as e:
            logger.error(f"Markdown 分块失败: {e}，回退到 smart 分块")
            return self._smart_chunk(text, document_id)

    def _clean_heading(self, heading: str) -> str:
        """
        清理标题中的 Markdown 链接标记

        示例:
        - 输入: "9. Classes[¶](#classes "Link to this heading")"
        - 输出: "9. Classes"
        """
        import re

        # 移除 [¶](#xxx "Link to this heading") 格式的链接
        heading = re.sub(r'\[¶\]\([^)]*\)', '', heading)

        # 移除 [text](url) 格式的链接（保留链接文本）
        heading = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', heading)

        # 移除 HTML 标签
        heading = re.sub(r'<[^>]+>', '', heading)

        # 移除多余的空格和标点
        heading = re.sub(r'\s+', ' ', heading)
        heading = heading.strip()

        # 移除末尾的标点符号（如冒号）
        heading = heading.rstrip('：:')

        return heading

    def _recursive_chunk(self, text: str, document_id: str) -> List[dict]:
        """
        递归分块（纯文本）

        按分隔符优先级递归切分：
        1. 段落 (\n\n)
        2. 换行 (\n)
        3. 句子 (。！？.!?)
        4. 空格
        5. 字符

        适用场景：PDF、Word、TXT 等纯文本格式
        """
        try:
            from langchain_text_splitters import RecursiveCharacterTextSplitter

            text_splitter = RecursiveCharacterTextSplitter(
                chunk_size=self.chunk_size,
                chunk_overlap=self.chunk_overlap,
                length_function=len,
                separators=[
                    "\n\n",  # 段落（优先）
                    "\n",    # 换行
                    "。",    # 中文句号
                    "！",    # 中文感叹号
                    "？",    # 中文问号
                    ".",     # 英文句号
                    "!",     # 英文感叹号
                    "?",     # 英文问号
                    ";",     # 分号
                    "；",    # 中文分号
                    " ",     # 空格（单词边界）
                    "",      # 字符（最后手段）
                ],
                keep_separator=True,  # 保留分隔符
            )

            chunk_texts = text_splitter.split_text(text)

            chunks = []
            for i, chunk_text in enumerate(chunk_texts):
                chunks.append({
                    "id": f"{document_id}_chunk_{i}",
                    "text": chunk_text.strip(),
                    "metadata": {
                        "document_id": document_id,
                        "chunk_index": i,
                        "heading": "",
                        "heading_path": [],
                        "level": 0,
                        "source": "recursive",
                    },
                })

            logger.info(f"文档 {document_id} 递归分块完成: {len(chunks)} 块")
            return chunks

        except ImportError:
            logger.warning("langchain 未安装，回退到 fixed 分块")
            return self._fixed_chunk(text, document_id)
        except Exception as e:
            logger.error(f"递归分块失败: {e}，回退到 fixed 分块")
            return self._fixed_chunk(text, document_id)

    def _smart_chunk(self, text: str, document_id: str) -> List[dict]:
        """
        智能分块

        按段落、标题分块，保留文档结构。
        """
        # 1. 按标题分割
        sections = self._split_by_heading(text)

        # 2. 对每个 section 按段落分割
        chunks = []
        chunk_index = 0

        for section in sections:
            section_chunks = self._split_section(section, document_id, chunk_index)
            chunks.extend(section_chunks)
            chunk_index += len(section_chunks)

        # 3. 合并小块
        chunks = self._merge_small_chunks(chunks, min_size=100, max_size=self.chunk_size)

        # 4. 添加重叠
        chunks = self._add_overlap(chunks)

        logger.info(f"文档 {document_id} 智能分块完成: {len(chunks)} 块")
        return chunks

    def _fixed_chunk(self, text: str, document_id: str) -> List[dict]:
        """
        固定大小分块

        简单、快速，但可能破坏文档结构。
        """
        paragraphs = [p.strip() for p in text.split(self.separator) if p.strip()]

        chunks = []
        current_text = ""
        chunk_index = 0

        for para in paragraphs:
            if len(para) > self.chunk_size:
                if current_text:
                    chunks.append(self._make_chunk(document_id, chunk_index, current_text))
                    chunk_index += 1
                    current_text = ""
                for i in range(0, len(para), self.chunk_size - self.chunk_overlap):
                    chunk_text = para[i:i + self.chunk_size]
                    chunks.append(self._make_chunk(document_id, chunk_index, chunk_text))
                    chunk_index += 1
                continue

            if current_text and len(current_text) + len(para) + 1 > self.chunk_size:
                chunks.append(self._make_chunk(document_id, chunk_index, current_text))
                chunk_index += 1
                if self.chunk_overlap > 0 and len(current_text) > self.chunk_overlap:
                    current_text = current_text[-self.chunk_overlap:] + self.separator + para
                else:
                    current_text = para
            else:
                if current_text:
                    current_text += self.separator + para
                else:
                    current_text = para

        if current_text:
            chunks.append(self._make_chunk(document_id, chunk_index, current_text))

        logger.info(f"文档 {document_id} 固定分块完成: {len(chunks)} 块")
        return chunks

    def _split_by_heading(self, text: str) -> List[Dict]:
        """
        按标题分割

        支持 Markdown 标题（# ## ###）和数字标题（1. 2. 3.）
        """
        sections = []
        current_section = {"heading": "", "content": "", "level": 0}

        lines = text.split("\n")

        for line in lines:
            # 检查 Markdown 标题
            heading_match = re.match(r'^(#{1,6})\s+(.+)$', line)
            if heading_match:
                # 保存当前 section
                if current_section["content"].strip():
                    sections.append(current_section)

                # 开始新 section
                level = len(heading_match.group(1))
                current_section = {
                    "heading": heading_match.group(2).strip(),
                    "content": line + "\n",
                    "level": level,
                }
                continue

            # 检查数字标题
            number_match = re.match(r'^(\d+)[\.\)]\s+(.+)$', line)
            if number_match:
                # 保存当前 section
                if current_section["content"].strip():
                    sections.append(current_section)

                # 开始新 section
                current_section = {
                    "heading": number_match.group(2).strip(),
                    "content": line + "\n",
                    "level": 2,
                }
                continue

            # 普通内容
            current_section["content"] += line + "\n"

        # 保存最后一个 section
        if current_section["content"].strip():
            sections.append(current_section)

        return sections

    def _split_section(self, section: Dict, document_id: str, start_index: int) -> List[dict]:
        """
        分割 section

        按段落分割，保留标题信息。
        """
        chunks = []
        heading = section["heading"]
        content = section["content"]

        # 按段落分割
        paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]

        current_text = ""
        chunk_index = start_index

        for para in paragraphs:
            # 如果段落太长，单独分块
            if len(para) > self.chunk_size:
                if current_text:
                    chunks.append(self._make_chunk(
                        document_id, chunk_index, current_text,
                        heading=heading
                    ))
                    chunk_index += 1
                    current_text = ""

                # 分割长段落
                for i in range(0, len(para), self.chunk_size):
                    chunk_text = para[i:i + self.chunk_size]
                    chunks.append(self._make_chunk(
                        document_id, chunk_index, chunk_text,
                        heading=heading
                    ))
                    chunk_index += 1
                continue

            # 合并段落
            if current_text and len(current_text) + len(para) + 2 > self.chunk_size:
                chunks.append(self._make_chunk(
                    document_id, chunk_index, current_text,
                    heading=heading
                ))
                chunk_index += 1
                current_text = para
            else:
                if current_text:
                    current_text += "\n\n" + para
                else:
                    current_text = para

        # 保存最后一块
        if current_text:
            chunks.append(self._make_chunk(
                document_id, chunk_index, current_text,
                heading=heading
            ))

        return chunks

    def _merge_small_chunks(self, chunks: List[dict], min_size: int, max_size: int) -> List[dict]:
        """
        合并小块

        将过小的块合并到相邻块中。
        """
        if not chunks:
            return []

        merged = []
        current = chunks[0]

        for i in range(1, len(chunks)):
            next_chunk = chunks[i]

            # 如果当前块太小，合并到下一块
            if len(current["text"]) < min_size:
                combined_text = current["text"] + "\n\n" + next_chunk["text"]
                if len(combined_text) <= max_size:
                    next_chunk["text"] = combined_text
                    continue

            # 如果下一块太小，合并到当前块
            if len(next_chunk["text"]) < min_size:
                combined_text = current["text"] + "\n\n" + next_chunk["text"]
                if len(combined_text) <= max_size:
                    current["text"] = combined_text
                    continue

            # 无法合并
            merged.append(current)
            current = next_chunk

        merged.append(current)
        return merged

    def _add_overlap(self, chunks: List[dict]) -> List[dict]:
        """
        添加重叠

        在相邻块之间添加重叠内容。
        """
        if self.chunk_overlap <= 0 or len(chunks) <= 1:
            return chunks

        for i in range(1, len(chunks)):
            prev_text = chunks[i - 1]["text"]
            overlap_text = prev_text[-self.chunk_overlap:]
            chunks[i]["text"] = overlap_text + "..." + chunks[i]["text"]

        return chunks

    def _make_chunk(
        self,
        document_id: str,
        index: int,
        text: str,
        heading: str = "",
    ) -> dict:
        """创建分块"""
        return {
            "id": f"{document_id}_chunk_{index}",
            "text": text.strip(),
            "metadata": {
                "document_id": document_id,
                "chunk_index": index,
                "heading": heading,
            },
        }
