"""网络与内容生成工具（T9 从 tools.py 拆出）。

web_search / crawl_webpage / generate_content：互联网搜索、网页爬取、LLM 内容生成。
与检索域零耦合，仅共享 tool_cache。
"""

from langchain_core.tools import tool
from loguru import logger

from .tool_cache import _tool_cache


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
