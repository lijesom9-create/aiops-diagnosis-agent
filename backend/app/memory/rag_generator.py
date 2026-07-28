"""
RAG 生成器 (RAG Generator)

整合记忆系统、RAG 检索和 Web Search，生成带上下文的答案。

流程：
1. 获取核心记忆（用户画像、Agent 人设）
2. 检索相关知识（RAG）
3. 如果知识库结果不足，使用 Web Search 补充
4. 检索相关对话历史（回忆记忆）
5. 检索相关档案记忆
6. 组装上下文
7. LLM 生成答案
8. 保存到记忆
"""

from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
from loguru import logger

from .memory_manager import MemoryManager


@dataclass
class GenerationResult:
    """生成结果"""
    answer: str
    sources: List[Dict] = field(default_factory=list)
    context_used: Dict[str, bool] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "answer": self.answer,
            "sources": self.sources,
            "context_used": self.context_used,
            "metadata": self.metadata,
        }


class RAGGenerator:
    """
    RAG 生成器

    整合记忆系统和 RAG 检索，生成带上下文的答案。
    """

    def __init__(
        self,
        llm_service,
        memory_manager: MemoryManager,
        extra_instructions: Optional[str] = None,
    ):
        """
        Args:
            llm_service: LLM 服务
            memory_manager: 记忆管理器
            extra_instructions: 附加到 prompt 的额外要求
        """
        self.llm = llm_service
        self.memory = memory_manager
        self.extra_instructions = extra_instructions

    async def generate(
        self,
        query: str,
        user_id: str,
        session_id: Optional[str] = None,
        use_core_memory: bool = True,
        use_recall_memory: bool = True,
        use_archival_memory: bool = True,
        use_rag: bool = True,
        use_web_search: bool = False,
        max_history_turns: int = 10,
        max_rag_results: int = 5,
        max_archival_results: int = 3,
        max_web_results: int = 3,
        save_to_memory: bool = True,
    ) -> GenerationResult:
        """
        生成答案

        Args:
            query: 用户查询
            user_id: 用户 ID
            session_id: 会话 ID
            use_core_memory: 是否使用核心记忆
            use_recall_memory: 是否使用回忆记忆
            use_archival_memory: 是否使用档案记忆
            use_rag: 是否使用 RAG 知识
            use_web_search: 是否使用 Web Search 补充
            max_history_turns: 最大历史轮次
            max_rag_results: 最大 RAG 结果数
            max_archival_results: 最大档案结果数
            max_web_results: 最大 Web 搜索结果数
            save_to_memory: 是否保存到记忆

        Returns:
            GenerationResult: 生成结果
        """
        logger.info(f"开始 RAG 生成: user={user_id}, query={query[:50]}...")

        # 1. 构建上下文
        context = self.memory.build_context(
            query=query,
            user_id=user_id,
            session_id=session_id,
            include_core=use_core_memory,
            include_recall=use_recall_memory,
            include_archival=use_archival_memory,
            include_rag=use_rag,
            max_history_turns=max_history_turns,
            max_rag_results=max_rag_results,
            max_archival_results=max_archival_results,
        )

        # 2. 构建 Prompt
        prompt = self._build_prompt(query, context)

        # 3. 获取 RAG 来源
        sources = []
        if use_rag:
            sources = self.memory.search_knowledge(query, max_rag_results)

        # 4. Web Search 补充（当启用时）
        web_results = []
        if use_web_search:
            logger.info("启用 Web Search，搜索外部资料")
            web_results = await self._web_search(query, max_web_results)
            if web_results:
                # 将 Web 结果添加到上下文
                web_context = self._format_web_results(web_results)
                context += f"\n\n## 网络搜索结果\n{web_context}"
                prompt = self._build_prompt(query, context)
                # 添加 Web 来源到 sources
                sources.extend(web_results)

        # 4. LLM 生成
        try:
            # 构建消息格式
            messages = [
                {"role": "system", "content": "你是一个专业的知识助手，基于提供的上下文回答用户问题。"},
                {"role": "user", "content": prompt}
            ]

            # 调用 AI 服务
            if hasattr(self.llm, 'generate'):
                answer = await self.llm.generate(prompt)
            elif hasattr(self.llm, 'chat'):
                response = await self.llm.chat(messages)
                answer = response.get("content", "")
            else:
                answer = "抱歉，AI 服务不可用。"
        except Exception as e:
            logger.error(f"LLM 生成失败: {e}")
            answer = f"抱歉，生成答案时出现错误：{str(e)}"

        # 5. 添加引用
        answer_with_citations = self._add_citations(answer, sources)

        # 6. 保存到记忆
        if save_to_memory:
            self.memory.add_user_message(user_id, query, session_id)
            self.memory.add_ai_message(user_id, answer_with_citations, session_id)

        # 7. 构建结果
        result = GenerationResult(
            answer=answer_with_citations,
            sources=sources,
            context_used={
                "core_memory": use_core_memory,
                "recall_memory": use_recall_memory,
                "archival_memory": use_archival_memory,
                "rag": use_rag,
            },
            metadata={
                "user_id": user_id,
                "session_id": session_id,
                "query": query,
            },
        )

        logger.info(f"RAG 生成完成: {len(answer_with_citations)} 字符")
        return result

    def _build_prompt(self, query: str, context: str) -> str:
        """构建 Prompt"""
        extra = ""
        if self.extra_instructions:
            extra = f"\n8. {self.extra_instructions}\n"

        return f"""{context}

## 用户问题
{query}

## 要求
1. 基于提供的上下文回答问题
2. 如果是追问，理解对话历史的上下文
3. **重要**：如果用户问的是"刚才问了什么"、"我们聊了什么"等关于对话历史的问题，请只基于对话历史部分回答，不需要引用知识库或网络搜索结果
4. 引用知识库来源时使用 [1]、[2] 等标记
5. 如果上下文没有相关信息，明确说明
6. 保持回答简洁、准确、有帮助
7. 回答中应明确覆盖用户问题里的关键概念，如技术术语、命令、关键字等，并对每个关键概念给出具体说明，避免只给出简要结论{extra}

## 回答"""

    def _add_citations(self, answer: str, sources: List[Dict]) -> str:
        """添加引用来源"""
        if not sources:
            return answer

        citations = []
        for i, source in enumerate(sources, 1):
            title = source.get("metadata", {}).get("title", "") or source.get("title", "")
            url = source.get("metadata", {}).get("url", "") or source.get("url", "")
            if title:
                if url:
                    citations.append(f"[{i}] [{title}]({url})")
                else:
                    citations.append(f"[{i}] {title}")

        if citations:
            answer += "\n\n---\n**引用来源：**\n" + "\n".join(citations)

        return answer

    async def _web_search(self, query: str, max_results: int = 3) -> List[Dict]:
        """
        使用 Web Search 搜索外部资料

        Args:
            query: 搜索查询
            max_results: 最大结果数

        Returns:
            List[Dict]: 搜索结果列表
        """
        try:
            from ..services.web_search import WebSearchService

            service = WebSearchService()
            results = await service.search(
                query=query,
                max_results=max_results,
                search_depth="basic",
            )

            # 转换为统一格式
            web_sources = []
            for result in results:
                web_sources.append({
                    "id": f"web_{hash(result.get('url', '')) % 10000}",
                    "score": result.get("score", 0.5),
                    "title": result.get("title", ""),
                    "content": result.get("content", "")[:500],  # 限制长度
                    "source": "web_search",
                    "metadata": {
                        "title": result.get("title", ""),
                        "url": result.get("url", ""),
                        "source": "web_search",
                    },
                })

            logger.info(f"Web Search 完成: {len(web_sources)} 个结果")
            return web_sources

        except ImportError:
            logger.warning("WebSearchService 未安装")
            return []
        except ValueError as e:
            logger.warning(f"Web Search 配置错误: {e}")
            return []
        except Exception as e:
            logger.error(f"Web Search 失败: {e}")
            return []

    def _format_web_results(self, web_results: List[Dict]) -> str:
        """格式化 Web 搜索结果为上下文文本"""
        if not web_results:
            return ""

        parts = []
        for i, result in enumerate(web_results, 1):
            title = result.get("title", "未知标题")
            content = result.get("content", "")
            url = result.get("metadata", {}).get("url", "")
            parts.append(f"[{i}] {title}\n{content}\n来源: {url}")

        return "\n\n".join(parts)

    async def generate_with_memory_update(
        self,
        query: str,
        user_id: str,
        session_id: Optional[str] = None,
    ) -> GenerationResult:
        """
        生成答案并更新记忆

        自动分析对话，更新用户画像和档案记忆。
        """
        # 1. 生成答案
        result = await self.generate(
            query=query,
            user_id=user_id,
            session_id=session_id,
        )

        # 2. 分析对话，更新记忆
        await self._analyze_and_update_memory(query, result.answer, user_id)

        return result

    async def _analyze_and_update_memory(
        self,
        query: str,
        answer: str,
        user_id: str,
    ):
        """分析对话，更新记忆"""
        try:
            # 使用 LLM 分析对话
            analysis_prompt = f"""请分析以下对话，提取关键信息：

用户问题：{query}
AI 回答：{answer}

请提取：
1. 用户可能薄弱的知识点（如果有）
2. 用户可能擅长的领域（如果有）
3. 值得记住的重要信息（如果有）

返回 JSON 格式：
{{
    "weak_topics": ["知识点1", "知识点2"],
    "strong_topics": ["领域1"],
    "important_info": ["信息1"]
}}

如果没有相关信息，返回空数组。"""

            analysis = await self.llm.generate(analysis_prompt)

            # 解析结果（简单实现）
            import json
            try:
                # 尝试提取 JSON
                import re
                json_match = re.search(r'\{.*\}', analysis, re.DOTALL)
                if json_match:
                    data = json.loads(json_match.group())

                    # 更新薄弱知识点
                    for topic in data.get("weak_topics", []):
                        if topic:
                            self.memory.add_weak_topic(user_id, topic)

                    # 更新擅长领域
                    for topic in data.get("strong_topics", []):
                        if topic:
                            self.memory.add_strong_topic(user_id, topic)

                    # 保存重要信息
                    for info in data.get("important_info", []):
                        if info:
                            self.memory.add_memory(
                                user_id=user_id,
                                content=info,
                                category="important",
                            )

                    logger.info(f"记忆更新完成: {data}")

            except json.JSONDecodeError:
                logger.debug("分析结果不是有效 JSON，跳过记忆更新")

        except Exception as e:
            logger.warning(f"分析对话失败: {e}")
