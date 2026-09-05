"""学习记忆工具（T9 从 tools.py 拆出）。

get_user_profile / save_memory / search_memory：用户画像与档案记忆的读写查，
依赖 shared_services 的 MemoryManager（惰性导入）。
"""

from langchain_core.tools import tool


@tool
def get_user_profile(user_id: str) -> str:
    """
    获取用户画像

    获取用户的学习风格、薄弱知识点、擅长领域等信息。
    适用于：
    - 了解用户背景
    - 个性化回答
    - 推荐学习内容

    Args:
        user_id: 用户 ID

    Returns:
        str: 用户画像信息
    """
    try:
        from ..shared_services import get_memory_manager
        memory = get_memory_manager()

        profile = memory.core_memory.get_user_profile(user_id)
        if profile:
            return f"""用户画像:
- 姓名: {profile.name or '未设置'}
- 学习风格: {profile.learning_style or '未设置'}
- 薄弱知识点: {', '.join(profile.weak_topics) if profile.weak_topics else '无'}
- 擅长领域: {', '.join(profile.strong_topics) if profile.strong_topics else '无'}"""
        return "用户画像未设置"
    except Exception as e:
        return f"获取用户画像失败: {str(e)}"


@tool
def save_memory(user_id: str, content: str, category: str = "note") -> str:
    """
    保存记忆

    将重要信息保存到用户的档案记忆中。适用于：
    - 保存学习笔记
    - 记录重要信息
    - 保存用户偏好

    Args:
        user_id: 用户 ID
        content: 要保存的内容
        category: 类别（note, learning, important）

    Returns:
        str: 保存结果
    """
    try:
        from ..shared_services import get_memory_manager
        memory = get_memory_manager()

        entry = memory.add_memory(
            user_id=user_id,
            content=content,
            category=category,
        )
        return f"已保存记忆: {entry.content[:50]}..."
    except Exception as e:
        return f"保存记忆失败: {str(e)}"


@tool
def search_memory(user_id: str, query: str) -> str:
    """
    搜索记忆

    从用户的档案记忆中搜索相关信息。适用于：
    - 查找之前保存的笔记
    - 回顾学习记录
    - 检索历史信息

    Args:
        user_id: 用户 ID
        query: 搜索关键词

    Returns:
        str: 搜索结果
    """
    try:
        from ..shared_services import get_memory_manager
        memory = get_memory_manager()

        results = memory.search_memory(query=query, user_id=user_id, top_k=3)
        if results:
            formatted = []
            for i, entry in enumerate(results, 1):
                formatted.append(f"{i}. [{entry.category}] {entry.content[:100]}")
            return "\n".join(formatted)
        return "未找到相关记忆"
    except Exception as e:
        return f"搜索记忆失败: {str(e)}"
