"""数据库核心层通用工具（无任何依赖，供 db_mixins 与 database.py 复用，避免循环导入）。"""

from __future__ import annotations

import copy
import re
from datetime import datetime
from typing import Dict, List, Optional


def escape_regex(pattern: str) -> str:
    """转义正则表达式特殊字符，防止注入"""
    return re.escape(pattern)

def clean_mongo_doc(doc: Optional[Dict]) -> Optional[Dict]:
    """清理MongoDB文档，移除ObjectId，转换datetime为字符串（不修改原始文档）"""
    if doc is None:
        return None
    result = copy.deepcopy(doc)
    if "_id" in result:
        del result["_id"]
    # 转换datetime对象为字符串
    for key, value in result.items():
        if isinstance(value, datetime):
            result[key] = value.isoformat()
    return result

def clean_mongo_docs(docs: List[Dict]) -> List[Dict]:
    """清理MongoDB文档列表"""
    return [clean_mongo_doc(doc) for doc in docs]
