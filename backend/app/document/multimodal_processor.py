"""
Multimodal Processor - 多模态 RAG 文档处理器

在文档解析后、分块前，对结构化文档中的图片和表格元素进行语义增强：

图片（IMAGE 元素）：
1. 调用 VLM 生成 caption + keywords + image_type
2. 调用 OCR 提取图中文字（可关闭）
3. 把结果写回 DocumentElement.image_desc / image_keywords / ocr_text

表格（TABLE 元素）：
1. 调用 LLM 生成表格摘要（≤150 字）
2. 把摘要写入 DocumentElement.image_desc（复用字段，避免改 dataclass）
3. 父块保留原表格 Markdown/HTML，子块用摘要增强召回

设计要点：
- 异步批量处理，单文档内多图并发
- 失败降级：VLM 失败返回空 caption，OCR 兜底
- 幂等：重复调用不重复扣费（按 image_path 缓存）
"""

import asyncio
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from loguru import logger

from ..core.config import settings
from ..retrieval.vlm_client import VLMProvider, get_vlm_provider
from .image_store import ImageStore, get_image_store
from .models import (
    DocumentElement,
    ElementType,
    StructuredDocument,
)

# 表格摘要 prompt
_TABLE_SUMMARY_PROMPT = """请用 100-150 字概括下面表格的核心内容，包括：
- 表格的主题（如：API 参数表、性能对比、配置项说明）
- 主要列维度
- 关键数据范围或典型值

直接输出摘要文本，不要任何前缀和 markdown 标记。

表格内容（Markdown）：
{table_content}"""


class MultimodalProcessor:
    """多模态文档处理器"""

    def __init__(
        self,
        image_store: Optional[ImageStore] = None,
        vlm_provider: Optional[VLMProvider] = None,
        llm_provider=None,  # 复用 AIModelProvider 做表格 summary
    ):
        """
        Args:
            image_store: 图片存储
            vlm_provider: VLM 客户端（None 时懒加载）
            llm_provider: LLM 客户端（用于表格摘要，None 时跳过表格摘要）
        """
        self._image_store = image_store
        self._vlm_provider = vlm_provider
        self._llm_provider = llm_provider

    @property
    def image_store(self) -> ImageStore:
        if self._image_store is None:
            self._image_store = get_image_store()
        return self._image_store

    @property
    def vlm_provider(self) -> Optional[VLMProvider]:
        if self._vlm_provider is None:
            self._vlm_provider = get_vlm_provider()
        return self._vlm_provider

    def is_enabled(self) -> bool:
        """是否启用多模态处理"""
        return bool(getattr(settings, "MULTIMODAL_ENABLED", False))

    async def process_document(
        self,
        doc: StructuredDocument,
        document_id: str,
    ) -> StructuredDocument:
        """
        对结构化文档执行多模态增强

        Args:
            doc: 解析后的结构化文档
            document_id: 文档 ID（用于图片路径寻址）

        Returns:
            增强后的 doc（原对象，原地修改）
        """
        if not self.is_enabled():
            logger.debug("MultimodalProcessor: MULTIMODAL_ENABLED=False，跳过")
            return doc

        # 收集所有需要处理的图片和表格元素
        image_elements: List[DocumentElement] = []
        table_elements: List[DocumentElement] = []
        for el in doc.elements:
            if el.type == ElementType.IMAGE and el.image_path:
                image_elements.append(el)
            elif (
                el.type == ElementType.TABLE
                and getattr(settings, "MULTIMODAL_TABLE_SUMMARY_ENABLED", True)
                and self._llm_provider is not None
                and el.text
            ):
                table_elements.append(el)

        if not image_elements and not table_elements:
            logger.debug(f"MultimodalProcessor: 文档 {document_id} 无图片/表格需处理")
            return doc

        logger.info(
            f"MultimodalProcessor: 处理文档 {document_id} "
            f"(images={len(image_elements)}, tables={len(table_elements)})"
        )

        # 图片预处理：装饰图过滤 + 内容哈希去重（业界 Tiered Image Processing）
        # 只对唯一图片调 VLM，重复图复用 caption，节省 API 调用
        if image_elements:
            unique_elements, duplicate_groups = self._preprocess_images(image_elements)
        else:
            unique_elements, duplicate_groups = [], []

        # 并发处理唯一图片（VLM caption + OCR）
        # 用 Semaphore 限制并发，配合令牌桶限流器避免大量请求被拒
        if unique_elements:
            sem = asyncio.Semaphore(5)

            async def _process_with_sem(el: DocumentElement) -> None:
                async with sem:
                    await self._process_image_element(el, document_id)

            await asyncio.gather(
                *[_process_with_sem(el) for el in unique_elements],
                return_exceptions=True,
            )

        # 重复图复用原始图片的 caption / OCR / keywords（不重复调 VLM）
        for dup_el, ref_el in duplicate_groups:
            dup_el.image_desc = ref_el.image_desc
            dup_el.image_keywords = list(ref_el.image_keywords) if ref_el.image_keywords else []
            dup_el.image_type = ref_el.image_type
            dup_el.ocr_text = ref_el.ocr_text

        # 并发处理表格
        if table_elements:
            await asyncio.gather(
                *[
                    self._process_table_element(el)
                    for el in table_elements
                ],
                return_exceptions=True,
            )

        # 统计成功数
        success_img = sum(1 for el in image_elements if el.image_desc)
        success_tbl = sum(1 for el in table_elements if el.image_desc)
        logger.info(
            f"MultimodalProcessor 完成 {document_id}: "
            f"images {success_img}/{len(image_elements)} captioned, "
            f"tables {success_tbl}/{len(table_elements)} summarized"
        )
        return doc

    def _preprocess_images(
        self,
        image_elements: List[DocumentElement],
    ) -> Tuple[List[DocumentElement], List[Tuple[DocumentElement, DocumentElement]]]:
        """图片预处理：装饰图过滤 + 内容哈希去重

        业界分层图片处理（Tiered Image Processing）最佳实践：
        1. 尺寸/宽高比过滤：剔除 logo、图标、分隔线等装饰元素（免费）
        2. 内容哈希去重：跨页重复图片（如页眉 logo）只处理一次，复用 caption（省 API）

        装饰图直接跳过（不调 VLM），分块时显示 [图片] 占位。
        重复图复用第一张的 caption/ocr_text/keywords。

        Returns:
            (unique_elements, duplicate_groups)
            - unique_elements: 需要调 VLM 的唯一图片
            - duplicate_groups: [(重复element, 原始element)]，复用原始的 caption
        """
        import hashlib
        from io import BytesIO

        from PIL import Image

        MIN_SIZE = 80       # 最小尺寸（px）：小于此值视为 logo/图标
        MAX_RATIO = 10.0    # 最大宽高比：超过此值视为分隔线/边框

        hash_to_element: Dict[str, DocumentElement] = {}
        unique_elements: List[DocumentElement] = []
        duplicate_groups: List[Tuple[DocumentElement, DocumentElement]] = []
        skipped_decorative = 0

        for el in image_elements:
            image_bytes = self.image_store.read_bytes(el.image_path)
            if image_bytes is None:
                continue

            # 1. 尺寸 + 宽高比过滤（装饰图：logo/图标/分隔线）
            try:
                img = Image.open(BytesIO(image_bytes))
                w, h = img.size
                if w < MIN_SIZE or h < MIN_SIZE:
                    skipped_decorative += 1
                    continue
                ratio = max(w, h) / max(min(w, h), 1)
                if ratio > MAX_RATIO:
                    skipped_decorative += 1
                    continue
            except Exception as e:
                logger.debug(f"图片尺寸读取失败，保留处理: {e}")

            # 2. 内容哈希去重（跨页重复的 logo/页眉页脚图）
            img_hash = hashlib.md5(image_bytes).hexdigest()
            if img_hash in hash_to_element:
                duplicate_groups.append((el, hash_to_element[img_hash]))
            else:
                hash_to_element[img_hash] = el
                unique_elements.append(el)

        logger.info(
            f"图片预处理: {len(image_elements)} 张 -> 唯一 {len(unique_elements)} 张 "
            f"(过滤装饰图 {skipped_decorative}, 去重 {len(duplicate_groups)})"
        )
        return unique_elements, duplicate_groups

    async def _process_image_element(
        self,
        element: DocumentElement,
        document_id: str,
    ) -> None:
        """处理单个图片元素：VLM caption + OCR"""
        image_path = element.image_path
        if not image_path:
            return

        image_bytes = self.image_store.read_bytes(image_path)
        if image_bytes is None:
            logger.warning(f"图片读取失败，跳过 VLM: {image_path}")
            return

        # 推断 MIME
        ext = Path(image_path).suffix.lower()
        mime_map = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }
        mime = mime_map.get(ext, "image/png")

        # VLM 生成 caption + text_in_image（图中文字/代码转录）
        vlm = self.vlm_provider
        if vlm is not None:
            try:
                result = await vlm.describe_image(image_bytes, mime_type=mime)
                if result.get("caption"):
                    element.image_desc = result["caption"]
                # VLM 转录的图中文字直接作为 ocr_text（语义一致，复用字段）
                # 这是图片型课件的关键：让代码截图/文字截图的内容可被检索
                if result.get("text_in_image"):
                    element.ocr_text = result["text_in_image"]
                if result.get("keywords"):
                    element.image_keywords = list(result["keywords"])[:8]
                if result.get("image_type"):
                    element.image_type = result["image_type"]
            except Exception as e:
                logger.warning(f"VLM 处理图片失败 {image_path}: {e}")
                if getattr(settings, "MULTIMODAL_VLM_REQUIRED", False):
                    raise
        else:
            logger.debug(f"VLM 未配置，跳过 caption: {image_path}")

        # OCR 兜底：仅当 VLM 未提取到图中文字时执行（分层处理，避免重复调用）
        if not element.ocr_text and getattr(settings, "MULTIMODAL_USE_OCR", True):
            ocr_text = await self._run_ocr(image_bytes)
            if ocr_text:
                element.ocr_text = ocr_text

    async def _run_ocr(self, image_bytes: bytes) -> Optional[str]:
        """调用 OCR 提取图片文字（异步封装，失败返回 None）"""
        try:
            # 在线程池里跑同步 OCR（OCR 库都是同步的）
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._ocr_sync, image_bytes)
        except Exception as e:
            logger.debug(f"OCR 失败: {e}")
            return None

    @staticmethod
    def _ocr_sync(image_bytes: bytes) -> Optional[str]:
        """同步 OCR 实现：优先 PaddleOCR，失败降级 tesseract"""
        from io import BytesIO

        from PIL import Image

        img = Image.open(BytesIO(image_bytes))
        if img.mode != "RGB":
            img = img.convert("RGB")

        # 保存临时文件给 OCR 库
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            img.save(tmp, format="PNG")
            tmp_path = tmp.name

        try:
            # 优先 paddleocr（中文更好）
            try:
                from .ocr import OCRProcessor
                ocr = OCRProcessor(engine="paddleocr")
                text = ocr.ocr_image(tmp_path)
                if text and text.strip():
                    return text.strip()
            except Exception as e:
                logger.debug(f"PaddleOCR 失败: {e}")

            # 降级 tesseract
            try:
                from .ocr import OCRProcessor
                ocr = OCRProcessor(engine="tesseract")
                text = ocr.ocr_image(tmp_path)
                if text and text.strip():
                    return text.strip()
            except Exception as e:
                logger.debug(f"tesseract OCR 失败: {e}")

            return None
        finally:
            try:
                import os as _os
                _os.unlink(tmp_path)
            except Exception:
                pass

    async def _process_table_element(self, element: DocumentElement) -> None:
        """处理单个表格元素：生成 LLM 摘要"""
        if self._llm_provider is None:
            return

        table_content = element.text
        if not table_content or len(table_content) < 30:
            # 太短的表格不值得生成摘要
            return

        prompt = _TABLE_SUMMARY_PROMPT.format(table_content=table_content[:3000])
        try:
            messages = [{"role": "user", "content": prompt}]
            resp = await self._llm_provider.chat(messages)
            summary = ""
            if isinstance(resp, dict):
                summary = (
                    resp.get("content")
                    or resp.get("message", {}).get("content", "")
                    or ""
                )
            elif isinstance(resp, str):
                summary = resp

            summary = summary.strip()
            if summary:
                # 复用 image_desc 字段（统一为"语义描述"语义）
                element.image_desc = summary
                logger.debug(f"表格摘要: {summary[:80]}...")
        except Exception as e:
            logger.warning(f"表格摘要生成失败: {e}")


# 模块级单例
_processor: Optional[MultimodalProcessor] = None


def get_multimodal_processor() -> MultimodalProcessor:
    """获取全局 MultimodalProcessor 单例"""
    global _processor
    if _processor is None:
        # 尝试复用 ai_service 的 LLM provider
        llm = None
        try:
            from ..core.ai_service import create_ai_provider
            llm = create_ai_provider()
        except Exception as e:
            logger.debug(f"未注入 LLM provider 到 MultimodalProcessor: {e}")

        _processor = MultimodalProcessor(llm_provider=llm)
    return _processor
