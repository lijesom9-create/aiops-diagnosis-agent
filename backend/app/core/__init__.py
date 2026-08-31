"""
核心模块
提供教育Agent的核心功能
"""

from .ai_service import ai_service
from .config import settings
from .database import db

__all__ = [
    "settings",
    "ai_service",
    "db",
]
