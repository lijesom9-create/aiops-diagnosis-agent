"""
工具定义

定义 LangGraph Agent 使用的工具。
"""

from typing import Dict, Any, Optional
from langchain_core.tools import tool
from loguru import logger
import time
import hashlib


# 全局变量，用于存储工具依赖
_retriever = None
_knowledge_store = None


class ToolCache:
    """工具结果缓存"""

    def __init__(self, ttl: int = 300):  # 默认 5 分钟过期
        self._cache: Dict[str, Any] = {}
        self._ttl = ttl

    def _make_key(self, func_name: str, *args, **kwargs) -> str:
        """生成缓存键"""
        key_str = f"{func_name}:{args}:{sorted(kwargs.items())}"
        return hashlib.md5(key_str.encode()).hexdigest()

    def get(self, func_name: str, *args, **kwargs) -> Optional[Any]:
        """获取缓存"""
        key = self._make_key(func_name, *args, **kwargs)
        if key in self._cache:
            result, timestamp = self._cache[key]
            if time.time() - timestamp < self._ttl:
                logger.debug(f"缓存命中: {func_name}")
                return result
            else:
                del self._cache[key]
        return None

    def set(self, func_name: str, result: Any, *args, **kwargs):
        """设置缓存"""
        key = self._make_key(func_name, *args, **kwargs)
        self._cache[key] = (result, time.time())
        logger.debug(f"缓存设置: {func_name}")

    def clear(self):
        """清空缓存"""
        self._cache.clear()


# 全局缓存实例
_tool_cache = ToolCache(ttl=300)


def set_retriever(retriever):
    """设置检索器"""
    global _retriever
    _retriever = retriever


def set_knowledge_store(store):
    """设置知识库"""
    global _knowledge_store
    _knowledge_store = store


@tool
def search_knowledge(query: str, limit: int = 5) -> str:
    """
    搜索用户的私有知识库

    仅用于查询用户上传的文档、笔记、资料等私有知识库内容。
    使用混合检索（关键词 + 语义 + RRF 融合）从本地知识库中搜索。

    不适用于通用知识问题（如编程概念、框架原理、常见术语解释等），
    这类问题应该直接由 AI 用训练知识回答。

    适用于：
    - 查找用户上传的文档内容
    - 搜索用户私有知识库
    - 获取用户资料中的参考资料

    Args:
        query: 搜索关键词
        limit: 返回结果数量

    Returns:
        str: 搜索结果
    """
    global _retriever, _knowledge_store, _tool_cache

    # 检查缓存
    cached = _tool_cache.get("search_knowledge", query, limit)
    if cached is not None:
        return cached

    try:
        if _knowledge_store:
            # 使用混合检索（BM25 + Vector + RRF 融合 + 查询重写）
            results = _knowledge_store.hybrid_search_parent_child(
                query, top_k=limit, rewrite_query=True
            )
            if results:
                formatted = []
                for i, r in enumerate(results[:limit], 1):
                    title = r.get("title", "未知")
                    content = r.get("content", "")[:300]
                    score = r.get("score", 0)
                    formatted.append(f"{i}. **{title}** (相关度: {score:.2f})\n{content}")
                result = "\n\n".join(formatted)
                _tool_cache.set("search_knowledge", result, query, limit)
                return result
            return "未找到相关知识"

        return "知识库未初始化"

    except Exception as e:
        logger.error(f"搜索知识库失败: {e}")
        return f"搜索失败: {str(e)}"


@tool
def web_search(query: str, num_results: int = 5) -> str:
    """
    搜索互联网

    从互联网搜索最新信息。适用于：
    - 查找最新资讯
    - 搜索技术文档
    - 获取实际案例

    Args:
        query: 搜索关键词
        num_results: 返回结果数量

    Returns:
        str: 搜索结果
    """
    global _tool_cache

    # 检查缓存
    cached = _tool_cache.get("web_search", query, num_results)
    if cached is not None:
        return cached

    try:
        # 使用同步方式调用
        import asyncio
        from ..services.web_search import WebSearchService

        service = WebSearchService()

        # 如果在异步环境中，直接调用
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 在异步环境中，创建新任务
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(
                        asyncio.run,
                        service.search(query=query, max_results=num_results)
                    )
                    results = future.result(timeout=30)
            else:
                results = asyncio.run(
                    service.search(query=query, max_results=num_results)
                )
        except Exception:
            results = asyncio.run(
                service.search(query=query, max_results=num_results)
            )

        if results:
            formatted = []
            for i, r in enumerate(results[:num_results], 1):
                title = r.get("title", "未知")
                url = r.get("url", "")
                content = r.get("content", "")[:200]
                formatted.append(f"{i}. **{title}**\n   链接: {url}\n   {content}")
            result = "\n\n".join(formatted)
            _tool_cache.set("web_search", result, query, num_results)
            return result

        return "未找到相关结果"

    except Exception as e:
        logger.error(f"网络搜索失败: {e}")
        return f"搜索失败: {str(e)}"


@tool
def crawl_webpage(url: str, use_js: bool = False, extract_mode: str = "markdown") -> str:
    """
    爬取网页内容

    爬取指定 URL 的网页内容。适用于：
    - 获取文档内容
    - 爬取博客文章
    - 提取网页正文

    Args:
        url: 网页 URL
        use_js: 是否使用 JavaScript 渲染（适用于 React/Vue 等动态页面），默认 False
        extract_mode: 提取模式：
            - markdown: 转为 Markdown 格式（默认，推荐）
            - article: 只提取正文
            - trafilatura: 智能提取（推荐用于复杂网页）
            - text: 纯文本
            - full: 保留完整 HTML

    Returns:
        str: 网页内容
    """
    try:
        import asyncio
        from ..tools.web_crawler import WebCrawlerTool

        crawler = WebCrawlerTool()

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(
                        asyncio.run,
                        crawler.execute(url=url, use_js=use_js, extract_mode=extract_mode)
                    )
                    result = future.result(timeout=60)  # JS 渲染需要更多时间
            else:
                result = asyncio.run(
                    crawler.execute(url=url, use_js=use_js, extract_mode=extract_mode)
                )
        except Exception:
            result = asyncio.run(
                crawler.execute(url=url, use_js=use_js, extract_mode=extract_mode)
            )

        if result.success:
            content = result.data.get("content", "")
            title = result.data.get("title", "未知")
            content_length = result.data.get("content_length", 0)
            return f"**{title}** (长度: {content_length} 字符)\n\n{content[:3000]}"

        return f"爬取失败: {result.error}"

    except Exception as e:
        logger.error(f"爬取网页失败: {e}")
        return f"爬取失败: {str(e)}"


@tool
def generate_content(prompt: str, style: str = "technical") -> str:
    """
    生成内容

    使用 LLM 生成内容。适用于：
    - 生成文章
    - 撰写文档
    - 总结内容

    Args:
        prompt: 生成提示
        style: 风格（technical, casual, formal）

    Returns:
        str: 生成的内容
    """
    try:
        import asyncio
        from ..core.ai_service import ai_service

        system_prompt = {
            "technical": "你是一个技术写作专家，擅长撰写技术文档和博客。",
            "casual": "你是一个轻松的写手，擅长写通俗易懂的内容。",
            "formal": "你是一个正式的写手，擅长撰写商务文档。",
        }.get(style, "你是一个专业的写手。")

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(asyncio.run, ai_service.chat(messages))
                    response = future.result(timeout=60)
            else:
                response = asyncio.run(ai_service.chat(messages))
        except Exception:
            response = asyncio.run(ai_service.chat(messages))

        return response.get("content", "生成失败")

    except Exception as e:
        logger.error(f"生成内容失败: {e}")
        return f"生成失败: {str(e)}"


@tool
def rag_search(query: str, limit: int = 5) -> str:
    """
    RAG 检索

    使用混合检索（关键词 + 向量 + BM25）搜索知识库。
    适用于：
    - 精确问答
    - 文档检索
    - 知识查找

    Args:
        query: 搜索查询
        limit: 返回结果数量

    Returns:
        str: 检索结果（包含引用）
    """
    global _knowledge_store

    try:
        import asyncio

        if not _knowledge_store:
            return "知识库未初始化"

        # 使用混合检索
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(
                        asyncio.run,
                        _knowledge_store.hybrid_search_parent_child(query, top_k=limit)
                    )
                    results = future.result(timeout=30)
            else:
                results = asyncio.run(
                    _knowledge_store.hybrid_search_parent_child(query, top_k=limit)
                )
        except Exception:
            results = asyncio.run(
                _knowledge_store.hybrid_search_parent_child(query, top_k=limit)
            )

        if not results:
            return "未找到相关信息"

        # 格式化结果（包含引用）
        formatted = []
        for i, r in enumerate(results[:limit], 1):
            title = r.get("title", "未知")
            content = r.get("content", "")[:300]
            source = r.get("source", "unknown")
            score = r.get("score", 0.0)

            # 添加引用标记
            formatted.append(f"[{i}] **{title}** (来源: {source}, 相关度: {score:.2f})\n{content}")

        return "\n\n".join(formatted)

    except Exception as e:
        logger.error(f"RAG 检索失败: {e}")
        return f"检索失败: {str(e)}"


@tool
def summarize_documents(query: str, max_docs: int = 5) -> str:
    """
    总结文档

    搜索并总结相关文档内容。适用于：
    - 快速了解某个主题
    - 获取文档摘要
    - 整理信息

    Args:
        query: 主题查询
        max_docs: 最大文档数

    Returns:
        str: 总结内容
    """
    global _knowledge_store

    try:
        import asyncio
        from ..core.ai_service import ai_service

        if not _knowledge_store:
            return "知识库未初始化"

        # 搜索相关文档
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(
                        asyncio.run,
                        _knowledge_store.hybrid_search_parent_child(query, top_k=max_docs)
                    )
                    results = future.result(timeout=30)
            else:
                results = asyncio.run(
                    _knowledge_store.hybrid_search_parent_child(query, top_k=max_docs)
                )
        except Exception:
            results = asyncio.run(
                _knowledge_store.hybrid_search_parent_child(query, top_k=max_docs)
            )

        if not results:
            return "未找到相关文档"

        # 构建总结提示
        docs_text = "\n\n".join([
            f"文档 {i+1}: {r.get('title', '未知')}\n{r.get('content', '')[:500]}"
            for i, r in enumerate(results)
        ])

        prompt = f"""请总结以下文档的核心内容：

主题：{query}

文档内容：
{docs_text}

请提供：
1. 核心要点（3-5 个）
2. 关键概念
3. 总结（100-200 字）
"""

        messages = [
            {"role": "system", "content": "你是一个专业的文档总结专家。"},
            {"role": "user", "content": prompt},
        ]

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(asyncio.run, ai_service.chat(messages))
                    response = future.result(timeout=60)
            else:
                response = asyncio.run(ai_service.chat(messages))
        except Exception:
            response = asyncio.run(ai_service.chat(messages))

        return response.get("content", "总结失败")

    except Exception as e:
        logger.error(f"文档总结失败: {e}")
        return f"总结失败: {str(e)}"


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


def create_tools() -> list:
    """创建工具列表"""
    return [
        search_knowledge,
        web_search,
        crawl_webpage,
        generate_content,
        rag_search,
        summarize_documents,
        get_user_profile,
        save_memory,
        search_memory,
    ]
