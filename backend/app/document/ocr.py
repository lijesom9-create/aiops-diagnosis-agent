"""
OCR 模块

支持多种 OCR 引擎：
- Tesseract：开源、免费
- PaddleOCR：百度开源、中文效果好
- EasyOCR：开源、多语言支持

依赖安装：
- Tesseract: pip install pytesseract pdf2image
- PaddleOCR: pip install paddleocr paddlepaddle
- EasyOCR: pip install easyocr
"""

import tempfile
from typing import List

from loguru import logger


class OCRProcessor:
    """OCR 处理器"""

    def __init__(self, engine: str = "tesseract", languages: List[str] = None):
        """
        Args:
            engine: OCR 引擎 ("tesseract", "paddleocr", "easyocr")
            languages: 语言列表，如 ["ch_sim", "en"]
        """
        self.engine = engine
        self.languages = languages or ["ch_sim", "en"]
        self._ocr = None

    def _init_ocr(self):
        """初始化 OCR 引擎"""
        if self._ocr is not None:
            return

        if self.engine == "tesseract":
            self._init_tesseract()
        elif self.engine == "paddleocr":
            self._init_paddleocr()
        elif self.engine == "easyocr":
            self._init_easyocr()
        else:
            raise ValueError(f"不支持的 OCR 引擎: {self.engine}")

    def _init_tesseract(self):
        """初始化 Tesseract"""
        try:
            import pytesseract
            from PIL import Image  # noqa: F401  # 可用性探测导入

            # 检查 tesseract 是否可用
            pytesseract.get_tesseract_version()
            self._ocr = pytesseract
            logger.info("Tesseract OCR 初始化成功")
        except ImportError:
            raise ImportError("请安装 pytesseract: pip install pytesseract")
        except Exception as e:
            raise RuntimeError(f"Tesseract 初始化失败: {e}")

    def _init_paddleocr(self):
        """初始化 PaddleOCR"""
        try:
            from paddleocr import PaddleOCR

            self._ocr = PaddleOCR(
                use_angle_cls=True,
                lang="ch",
                show_log=False,
            )
            logger.info("PaddleOCR 初始化成功")
        except ImportError:
            raise ImportError("请安装 PaddleOCR: pip install paddleocr paddlepaddle")

    def _init_easyocr(self):
        """初始化 EasyOCR"""
        try:
            import easyocr

            self._ocr = easyocr.Reader(self.languages)
            logger.info("EasyOCR 初始化成功")
        except ImportError:
            raise ImportError("请安装 EasyOCR: pip install easyocr")

    def ocr_image(self, image_path: str) -> str:
        """
        对图片进行 OCR

        Args:
            image_path: 图片路径

        Returns:
            str: 识别的文本
        """
        self._init_ocr()

        if self.engine == "tesseract":
            return self._ocr_tesseract(image_path)
        elif self.engine == "paddleocr":
            return self._ocr_paddleocr(image_path)
        elif self.engine == "easyocr":
            return self._ocr_easyocr(image_path)

        return ""

    def ocr_pdf(self, pdf_path: str) -> str:
        """
        对 PDF 进行 OCR（适用于扫描版 PDF）

        Args:
            pdf_path: PDF 文件路径

        Returns:
            str: 识别的文本
        """
        try:
            from pdf2image import convert_from_path
        except ImportError:
            raise ImportError("请安装 pdf2image: pip install pdf2image")

        # 将 PDF 转为图片
        logger.info(f"将 PDF 转为图片: {pdf_path}")
        images = convert_from_path(pdf_path, dpi=200)

        texts = []
        for i, image in enumerate(images):
            logger.info(f"OCR 第 {i + 1}/{len(images)} 页")

            # 保存临时图片
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                image.save(tmp.name)
                tmp_path = tmp.name

            try:
                # OCR 识别
                page_text = self.ocr_image(tmp_path)
                if page_text.strip():
                    texts.append(f"--- 第 {i + 1} 页 ---\n{page_text}")
            finally:
                # 清理临时文件
                import os
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        return "\n\n".join(texts)

    def _ocr_tesseract(self, image_path: str) -> str:
        """使用 Tesseract OCR"""
        from PIL import Image

        image = Image.open(image_path)

        # 设置语言
        lang = "+".join(self.languages)
        # Tesseract 语言代码映射
        lang_map = {
            "ch_sim": "chi_sim",
            "ch_tra": "chi_tra",
            "en": "eng",
        }
        lang = lang_map.get(lang, lang)

        text = self._ocr.image_to_string(image, lang=lang)
        return text.strip()

    def _ocr_paddleocr(self, image_path: str) -> str:
        """使用 PaddleOCR"""
        result = self._ocr.ocr(image_path, cls=True)

        if not result or not result[0]:
            return ""

        # 提取文本
        texts = []
        for line in result[0]:
            text = line[1][0]  # 识别的文本
            confidence = line[1][1]  # 置信度

            if confidence > 0.5:  # 过滤低置信度结果
                texts.append(text)

        return "\n".join(texts)

    def _ocr_easyocr(self, image_path: str) -> str:
        """使用 EasyOCR"""
        result = self._ocr.readtext(image_path)

        # 提取文本
        texts = []
        for detection in result:
            text = detection[1]  # 识别的文本
            confidence = detection[2]  # 置信度

            if confidence > 0.5:
                texts.append(text)

        return "\n".join(texts)

    def is_pdf_scanned(self, pdf_path: str, sample_pages: int = 3) -> bool:
        """
        检测 PDF 是否是扫描版

        Args:
            pdf_path: PDF 文件路径
            sample_pages: 采样页数

        Returns:
            bool: 是否是扫描版
        """
        try:
            import fitz

            doc = fitz.open(pdf_path)
            text_pages = 0

            for i, page in enumerate(doc):
                if i >= sample_pages:
                    break

                text = page.get_text()
                if text and len(text.strip()) > 50:
                    text_pages += 1

            doc.close()

            # 如果大部分采样页面没有文本，认为是扫描版
            return text_pages < sample_pages // 2

        except Exception as e:
            logger.warning(f"检测 PDF 类型失败: {e}")
            return False


class PDFWithOCR:
    """带 OCR 的 PDF 解析器"""

    def __init__(self, ocr_engine: str = "tesseract"):
        self.ocr_processor = OCRProcessor(engine=ocr_engine)

    def parse(self, pdf_path: str, force_ocr: bool = False) -> str:
        """
        解析 PDF（自动判断是否需要 OCR）

        Args:
            pdf_path: PDF 文件路径
            force_ocr: 强制使用 OCR

        Returns:
            str: 解析的文本
        """
        # 1. 检测是否需要 OCR
        need_ocr = force_ocr or self.ocr_processor.is_pdf_scanned(pdf_path)

        if need_ocr:
            logger.info(f"使用 OCR 解析 PDF: {pdf_path}")
            return self.ocr_processor.ocr_pdf(pdf_path)
        else:
            logger.info(f"使用常规方式解析 PDF: {pdf_path}")
            return self._extract_text(pdf_path)

    def _extract_text(self, pdf_path: str) -> str:
        """常规文本提取"""
        try:
            import fitz

            doc = fitz.open(pdf_path)
            texts = []

            for page in doc:
                text = page.get_text()
                if text.strip():
                    texts.append(text)

            doc.close()
            return "\n\n".join(texts)

        except Exception as e:
            logger.error(f"PDF 文本提取失败: {e}")
            return ""
