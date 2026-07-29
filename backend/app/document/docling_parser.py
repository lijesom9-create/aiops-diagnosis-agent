"""
Docling Parser - 基于 Docling 的结构化 PDF/DOCX 解析器

通过 DoclingDocument 将 PDF/DOCX 解析为结构化 Element 列表。
自动处理页眉页脚过滤、表格提取、标题层级、图片提取（多模态 RAG）。
"""

import os
import tempfile
from pathlib import Path
from typing import Optional, Set, List
from loguru import logger

from .models import (
    ElementType, ElementMetadata, DocumentElement,
    DocumentMetadata, StructuredDocument,
)


class DoclingParser:
    """基于 Docling 的结构化解析器"""

    SUPPORTED_EXTENSIONS = {".pdf", ".docx"}

    def __init__(
        self,
        image_store=None,
        extract_images: bool = True,
    ):
        """
        Args:
            image_store: ImageStore 实例（None 时不提取图片）
            extract_images: 是否提取图片元素
        """
        self._converter = None
        self._image_store = image_store
        self._extract_images = extract_images

    def _ocr_fallback(self, pdf_path: str):
        """扫描版 PDF 的 OCR 降级：逐页渲染为图片 → OCR 提取文本"""
        import tempfile
        from .models import ElementMetadata

        try:
            import fitz  # PyMuPDF
        except ImportError:
            logger.warning("PyMuPDF 未安装，无法执行 OCR 降级")
            return []

        # 尝试加载 OCRProcessor
        try:
            from .ocr import OCRProcessor
            ocr = OCRProcessor(engine="paddleocr")
        except Exception:
            try:
                from .ocr import OCRProcessor
                ocr = OCRProcessor(engine="tesseract")
            except Exception as e:
                logger.warning(f"OCR 引擎不可用，跳过 OCR 降级: {e}")
                return []

        doc = fitz.open(pdf_path)
        elements = []

        for page_num in range(len(doc)):
            page = doc[page_num]
            # 渲染页面为图片（200 DPI 适合 OCR）
            pix = page.get_pixmap(dpi=200)
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_img:
                pix.save(tmp_img.name)
                img_path = tmp_img.name

            try:
                text = ocr.ocr_image(img_path)
                if text and text.strip():
                    # 按段落分割 OCR 文本
                    for para in text.split("\n\n"):
                        para = para.strip()
                        if len(para) > 5:  # 过滤过短的噪声
                            elements.append(DocumentElement(
                                type=ElementType.PARAGRAPH,
                                text=para,
                                metadata=ElementMetadata(page_number=page_num + 1),
                            ))
            except Exception as e:
                logger.debug(f"第 {page_num + 1} 页 OCR 失败: {e}")
            finally:
                try:
                    os.unlink(img_path)
                except Exception:
                    pass

        doc.close()
        return elements

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

    def parse(
        self,
        content: bytes,
        filename: str,
        document_id: Optional[str] = None,
    ) -> StructuredDocument:
        """
        解析 PDF/DOCX 文件为结构化文档

        Args:
            content: 文件二进制
            filename: 文件名（用于推断格式）
            document_id: 文档 ID（多模态模式下，用于把图片存到独立子目录）
                         多模态关闭或 image_store 未注入时此参数无影响
        """
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

            elements: List[DocumentElement] = []
            heading_stack = [""] * 10
            image_counter = 0  # 图片序号（用于命名）

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

                # 图片提取（多模态 RAG）
                image_path: Optional[str] = None
                if (
                    elem_type == ElementType.IMAGE
                    and self._extract_images
                    and self._image_store is not None
                    and document_id
                ):
                    image_path = self._extract_and_save_image(
                        item, document_id=document_id, idx=image_counter
                    )
                    if image_path:
                        image_counter += 1

                element = DocumentElement(
                    type=elem_type,
                    text=text,
                    metadata=ElementMetadata(
                        page_number=getattr(item, "page_number", None),
                        heading_path=heading_path,
                    ),
                    text_as_html=text_as_html,
                    image_path=image_path,
                )
                elements.append(element)

            logger.info(
                f"DoclingParser: {filename} -> {len(elements)} elements "
                f"(images_extracted={image_counter})"
            )

            # 扫描版 PDF 检测：页数多但文本极少 → OCR 降级
            page_count = len(docling_doc.pages)
            total_chars = sum(len(e.text or "") for e in elements)
            if ext == ".pdf" and page_count > 2 and total_chars < page_count * 50:
                logger.warning(
                    f"疑似扫描版PDF（{page_count}页，仅{total_chars}字符），启动OCR降级"
                )
                ocr_elements = self._ocr_fallback(tmp_path)
                if ocr_elements:
                    logger.info(f"OCR降级成功：提取到 {len(ocr_elements)} 个文本元素")
                    elements = ocr_elements

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

    def _extract_and_save_image(
        self,
        item,
        document_id: str,
        idx: int,
    ) -> Optional[str]:
        """
        从 Docling PictureItem 提取图片并保存到 ImageStore

        Docling 不同版本暴露图片的 API：
        - v2+ : item.image (PictureImageData) -> .pil_image
        - 旧版：item.image.uri / item.image.data
        """
        try:
            pil_image = None
            # 路径 1: 新版 Docling 的 PictureItem.image.pil_image
            if hasattr(item, "image") and item.image is not None:
                img_obj = item.image
                if hasattr(img_obj, "pil_image") and img_obj.pil_image is not None:
                    pil_image = img_obj.pil_image
                elif hasattr(img_obj, "data") and img_obj.data:
                    # 字节流
                    from io import BytesIO
                    from PIL import Image
                    pil_image = Image.open(BytesIO(img_obj.data))

            if pil_image is None:
                logger.debug(f"Docling 图片元素无可提取的图像数据 (idx={idx})")
                return None

            # 统一转 RGB（避免 mode=P/I 等保存失败）
            if pil_image.mode not in ("RGB", "RGBA"):
                pil_image = pil_image.convert("RGB")

            image_id = f"img_{idx:04d}"
            relative_path = self._image_store.save_pil_image(
                pil_image=pil_image,
                document_id=document_id,
                image_id=image_id,
                format="PNG",
            )
            return relative_path

        except Exception as e:
            logger.warning(f"提取并保存图片失败 (idx={idx}): {e}")
            return None
