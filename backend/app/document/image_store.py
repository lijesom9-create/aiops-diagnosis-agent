"""
Image Store - 多模态 RAG 图片存储管理

将文档中提取的图片保存到本地文件系统，metadata 中存储相对路径。
检索命中图片块时，通过 image_path 字段返回给上层用于前端展示。

存储结构：
    {IMAGE_ROOT_DIR}/{document_id}/{image_id}.{ext}

其中 IMAGE_ROOT_DIR 默认为 backend/data/images，
可通过环境变量 MULTIMODAL_IMAGE_ROOT_DIR 覆盖。
"""

import os
import uuid
from pathlib import Path
from typing import Optional, List
from loguru import logger


# 默认根目录：backend/data/images（相对 backend 启动目录）
_DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "data" / "images"


class ImageStore:
    """图片本地存储管理器"""

    def __init__(self, root_dir: Optional[str] = None):
        """
        Args:
            root_dir: 图片根目录。None 时按优先级：
                      环境变量 MULTIMODAL_IMAGE_ROOT_DIR > 默认 backend/data/images
        """
        env_root = os.environ.get("MULTIMODAL_IMAGE_ROOT_DIR")
        self.root_dir = Path(root_dir or env_root or _DEFAULT_ROOT).resolve()
        self.root_dir.mkdir(parents=True, exist_ok=True)
        logger.debug(f"ImageStore 初始化: root_dir={self.root_dir}")

    def _doc_dir(self, document_id: str) -> Path:
        """获取文档对应的图片目录（不存在则创建）"""
        d = self.root_dir / document_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _gen_image_id() -> str:
        return f"img_{uuid.uuid4().hex[:12]}"

    def save_image(
        self,
        image_bytes: bytes,
        document_id: str,
        image_id: Optional[str] = None,
        ext: str = "png",
    ) -> str:
        """
        保存一张图片到磁盘

        Args:
            image_bytes: 图片二进制数据
            document_id: 所属文档 ID
            image_id: 图片 ID（None 时自动生成）
            ext: 扩展名（png/jpg）

        Returns:
            relative_path: 相对 root_dir 的路径，用于存入 metadata
                           例如 "doc_abc/img_xxx.png"
        """
        image_id = image_id or self._gen_image_id()
        ext = ext.lstrip(".").lower()
        if ext not in ("png", "jpg", "jpeg", "webp"):
            ext = "png"

        doc_dir = self._doc_dir(document_id)
        filename = f"{image_id}.{ext}"
        full_path = doc_dir / filename

        try:
            with open(full_path, "wb") as f:
                f.write(image_bytes)
        except Exception as e:
            logger.error(f"保存图片失败 {full_path}: {e}")
            raise

        # 返回相对路径（POSIX 风格，便于跨平台和 URL 拼接）
        relative_path = f"{document_id}/{filename}"
        logger.debug(f"ImageStore: 已保存图片 {relative_path} ({len(image_bytes)} bytes)")
        return relative_path

    def save_pil_image(
        self,
        pil_image,
        document_id: str,
        image_id: Optional[str] = None,
        format: str = "PNG",
    ) -> str:
        """
        保存 PIL.Image 对象

        Args:
            pil_image: PIL.Image 实例
            document_id: 文档 ID
            image_id: 图片 ID
            format: PIL 保存格式（PNG/JPEG）

        Returns:
            relative_path
        """
        from io import BytesIO

        image_id = image_id or self._gen_image_id()
        ext = "png" if format.upper() == "PNG" else ("jpg" if format.upper() in ("JPEG", "JPG") else "png")

        buf = BytesIO()
        pil_image.save(buf, format=format)
        image_bytes = buf.getvalue()

        return self.save_image(
            image_bytes=image_bytes,
            document_id=document_id,
            image_id=image_id,
            ext=ext,
        )

    def get_full_path(self, relative_path: str) -> Path:
        """根据 metadata 中的相对路径，获取磁盘绝对路径"""
        return self.root_dir / relative_path

    def read_bytes(self, relative_path: str) -> Optional[bytes]:
        """读取图片二进制"""
        full_path = self.get_full_path(relative_path)
        if not full_path.exists():
            logger.warning(f"图片不存在: {full_path}")
            return None
        try:
            with open(full_path, "rb") as f:
                return f.read()
        except Exception as e:
            logger.error(f"读取图片失败 {full_path}: {e}")
            return None

    def delete_document_images(self, document_id: str) -> int:
        """
        删除某个文档对应的所有图片

        Returns:
            删除的图片数量
        """
        doc_dir = self.root_dir / document_id
        if not doc_dir.exists():
            return 0

        count = 0
        try:
            for f in doc_dir.iterdir():
                if f.is_file():
                    try:
                        f.unlink()
                        count += 1
                    except Exception as e:
                        logger.warning(f"删除图片失败 {f}: {e}")
            doc_dir.rmdir()
            logger.info(f"ImageStore: 已删除文档 {document_id} 的 {count} 张图片")
        except Exception as e:
            logger.error(f"删除文档图片目录失败 {doc_dir}: {e}")
        return count

    def list_document_images(self, document_id: str) -> List[str]:
        """列出某个文档的所有图片相对路径"""
        doc_dir = self.root_dir / document_id
        if not doc_dir.exists():
            return []
        return [
            f"{document_id}/{f.name}"
            for f in doc_dir.iterdir()
            if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
        ]


# 模块级单例
_image_store: Optional[ImageStore] = None


def get_image_store() -> ImageStore:
    """获取全局 ImageStore 单例"""
    global _image_store
    if _image_store is None:
        _image_store = ImageStore()
    return _image_store
