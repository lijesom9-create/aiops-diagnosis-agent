"""
文件存储服务

将用户上传的文件保存到本地文件系统，支持：
- 文件上传
- 文件下载
- 文件删除
- 文件列表

目录结构：
/uploads/
├── user_001/
│   ├── doc_123_python_tutorial.pdf
│   └── doc_456_data_analysis.docx
└── user_002/
    └── doc_789_machine_learning.md
"""

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger


class FileStorage:
    """文件存储服务"""

    def __init__(self, base_dir: str = "uploads"):
        """
        Args:
            base_dir: 基础存储目录
        """
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"FileStorage 初始化: {self.base_dir.absolute()}")

    def _get_user_dir(self, user_id: str) -> Path:
        """获取用户目录"""
        user_dir = self.base_dir / user_id
        user_dir.mkdir(parents=True, exist_ok=True)
        return user_dir

    def _get_file_path(self, user_id: str, document_id: str, filename: str) -> Path:
        """获取文件完整路径"""
        user_dir = self._get_user_dir(user_id)
        # 文件名格式: {document_id}_{原始文件名}
        safe_filename = self._sanitize_filename(filename)
        return user_dir / f"{document_id}_{safe_filename}"

    def _sanitize_filename(self, filename: str) -> str:
        """清理文件名，移除特殊字符"""
        # 只保留字母、数字、点、下划线、连字符
        import re
        safe = re.sub(r'[^\w\.\-一-鿿]', '_', filename)
        # 移除连续的下划线
        safe = re.sub(r'_+', '_', safe)
        return safe

    async def save(
        self,
        content: bytes,
        filename: str,
        user_id: str,
        document_id: str,
    ) -> Dict[str, Any]:
        """
        保存文件

        Args:
            content: 文件内容
            filename: 原始文件名
            user_id: 用户 ID
            document_id: 文档 ID

        Returns:
            Dict: 文件信息
        """
        try:
            file_path = self._get_file_path(user_id, document_id, filename)

            # 写入文件
            with open(file_path, "wb") as f:
                f.write(content)

            file_size = file_path.stat().st_size

            logger.info(f"文件保存成功: {file_path} ({file_size} bytes)")

            return {
                "file_path": str(file_path),
                "file_size": file_size,
                "filename": filename,
                "user_id": user_id,
                "document_id": document_id,
                "created_at": datetime.now().isoformat(),
            }
        except Exception as e:
            logger.error(f"文件保存失败: {e}")
            raise

    async def get(self, user_id: str, document_id: str, filename: str) -> Optional[bytes]:
        """
        读取文件

        Args:
            user_id: 用户 ID
            document_id: 文档 ID
            filename: 文件名

        Returns:
            bytes: 文件内容，不存在返回 None
        """
        try:
            file_path = self._get_file_path(user_id, document_id, filename)
            if not file_path.exists():
                logger.warning(f"文件不存在: {file_path}")
                return None

            with open(file_path, "rb") as f:
                return f.read()
        except Exception as e:
            logger.error(f"文件读取失败: {e}")
            return None

    async def delete(self, user_id: str, document_id: str, filename: str) -> bool:
        """
        删除文件

        Args:
            user_id: 用户 ID
            document_id: 文档 ID
            filename: 文件名

        Returns:
            bool: 是否删除成功
        """
        try:
            file_path = self._get_file_path(user_id, document_id, filename)
            if file_path.exists():
                file_path.unlink()
                logger.info(f"文件删除成功: {file_path}")
                return True
            return False
        except Exception as e:
            logger.error(f"文件删除失败: {e}")
            return False

    async def delete_by_document(self, user_id: str, document_id: str) -> bool:
        """
        删除文档的所有文件

        Args:
            user_id: 用户 ID
            document_id: 文档 ID

        Returns:
            bool: 是否删除成功
        """
        try:
            user_dir = self._get_user_dir(user_id)
            prefix = f"{document_id}_"

            deleted = False
            for file_path in user_dir.iterdir():
                if file_path.name.startswith(prefix):
                    file_path.unlink()
                    deleted = True
                    logger.info(f"文件删除成功: {file_path}")

            return deleted
        except Exception as e:
            logger.error(f"文件删除失败: {e}")
            return False

    async def list_files(self, user_id: str) -> List[Dict[str, Any]]:
        """
        列出用户的所有文件

        Args:
            user_id: 用户 ID

        Returns:
            List[Dict]: 文件列表
        """
        try:
            user_dir = self._get_user_dir(user_id)
            files = []

            for file_path in user_dir.iterdir():
                if file_path.is_file():
                    # 解析文件名: {document_id}_{filename}
                    parts = file_path.name.split("_", 1)
                    document_id = parts[0] if len(parts) > 1 else ""
                    filename = parts[1] if len(parts) > 1 else file_path.name

                    files.append({
                        "file_path": str(file_path),
                        "file_size": file_path.stat().st_size,
                        "filename": filename,
                        "document_id": document_id,
                        "modified_at": datetime.fromtimestamp(
                            file_path.stat().st_mtime
                        ).isoformat(),
                    })

            return files
        except Exception as e:
            logger.error(f"列出文件失败: {e}")
            return []

    async def exists(self, user_id: str, document_id: str, filename: str) -> bool:
        """
        检查文件是否存在

        Args:
            user_id: 用户 ID
            document_id: 文档 ID
            filename: 文件名

        Returns:
            bool: 文件是否存在
        """
        file_path = self._get_file_path(user_id, document_id, filename)
        return file_path.exists()


# 全局实例
_file_storage: Optional[FileStorage] = None


def get_file_storage() -> FileStorage:
    """获取文件存储实例（全局单例）"""
    global _file_storage
    if _file_storage is None:
        _file_storage = FileStorage()
    return _file_storage
