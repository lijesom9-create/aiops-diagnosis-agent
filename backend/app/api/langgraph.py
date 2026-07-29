"""
LangGraph API

提供 LangGraph Agent 的 API 接口。
"""

from typing import Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from loguru import logger
from starlette.responses import StreamingResponse

from ..core.auth import get_current_user, UserResponse
from ..core.database import get_db, Database
from ..core.config import settings


router = APIRouter(prefix="/api/langgraph", tags=["LangGraph Agent"])


# ========== 请求/响应模型 ==========

class ChatRequest(BaseModel):
    """聊天请求"""
    message: str = Field(..., description="用户消息")
    session_id: Optional[str] = Field(default=None, description="会话 ID")
    use_web_search: bool = Field(default=False, description="是否使用 Web Search")
    context: Optional[Dict[str, Any]] = Field(default=None, description="额外上下文")


class ChatResponse(BaseModel):
    """聊天响应"""
    content: str
    tools_used: list
    citations: list
    step_count: int


# ========== 全局 Agent 实例 ==========

_agent = None


def get_agent():
    """获取 Agent 实例"""
    global _agent
    if _agent is None:
        from ..langgraph_agent import LangGraphAgent
        from ..shared_services import get_knowledge_store
        from ..core.config import settings
        import os

        # Checkpoint 持久化路径
        checkpoint_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "data", "langgraph_checkpoints.db"
        )

        _agent = LangGraphAgent(
            llm_model=settings.AI_MODEL.split("/")[-1] if "/" in settings.AI_MODEL else settings.AI_MODEL,
            llm_base_url=settings.AI_BASE_URL or "https://api.deepseek.com",
            llm_api_key=settings.AI_API_KEY or "dummy",
            knowledge_store=get_knowledge_store(),
            checkpoint_path=checkpoint_path,
        )

    return _agent


# ========== API 端点 ==========

@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    db: Database = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """
    与 LangGraph Agent 对话

    使用 LangGraph Agent 处理用户消息。
    """
    try:
        agent = get_agent()

        # 使用 user_id 作为 session 的基础，确保同一用户的对话有连续性
        session_id = request.session_id or f"langgraph_{current_user.user_id}"

        # 注入 user_id 到 context
        context = request.context or {}
        context["user_id"] = current_user.user_id

        # 执行 Agent
        result = await agent.run(
            user_input=request.message,
            session_id=session_id,
            context=context,
            use_web_search=request.use_web_search,
        )

        # 保存消息到数据库
        if request.session_id:
            await db.add_message(request.session_id, {
                "role": "user",
                "content": request.message,
            })
            await db.add_message(request.session_id, {
                "role": "assistant",
                "content": result["content"],
                "tools_used": result["tools_used"],
            })

        # 保存到记忆系统（用于后续上下文注入）
        try:
            from ..shared_services import get_memory_manager
            memory = get_memory_manager()
            if memory:
                memory.add_user_message(current_user.user_id, request.message, session_id)
                memory.add_ai_message(current_user.user_id, result["content"], session_id)
        except Exception as e:
            logger.debug(f"保存对话到记忆失败: {e}")

        return ChatResponse(
            content=result["content"],
            tools_used=result["tools_used"],
            citations=result["citations"],
            step_count=result["step_count"],
        )

    except Exception as e:
        logger.error(f"LangGraph Agent 执行失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Agent 执行失败: {str(e)}",
        )


@router.post("/chat/stream")
async def chat_stream(
    request: ChatRequest,
    db: Database = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """
    与 LangGraph Agent 对话（token 级流式）

    使用 Server-Sent Events 流式返回结果。
    事件类型（SSE data 行内 JSON）：
    - {"type": "start"}                                    开始
    - {"type": "tool_calls", "tools": ["search_knowledge"]} 工具调用开始
    - {"type": "tool_result", "name": "...", "content": "..."} 工具结果
    - {"type": "token", "content": "你"}                  LLM token（核心，逐字输出）
    - {"type": "reflection", "content": "正在反思..."}     反思阶段
    - {"type": "done", "tools_used": [...], "step_count": N} 完成
    - {"type": "error", "content": "..."}                  错误
    """
    import json
    import asyncio

    try:
        agent = get_agent()

        # 使用 user_id 作为 session 的基础
        session_id = request.session_id or f"langgraph_{current_user.user_id}"

        # 注入 user_id 到 context
        context = request.context or {}
        context["user_id"] = current_user.user_id

        async def event_generator():
            # 开始事件
            yield f"data: {json.dumps({'type': 'start'}, ensure_ascii=False)}\n\n"

            # 累积完整 answer，用于最后保存到记忆
            full_answer_parts: list = []
            final_meta = {"tools_used": [], "step_count": 0}

            try:
                async for event in agent.run_stream(
                    user_input=request.message,
                    session_id=session_id,
                    context=context,
                    use_web_search=request.use_web_search,
                ):
                    event_type = event.get("type")

                    # 累积 token，形成完整答案用于持久化
                    if event_type == "token":
                        full_answer_parts.append(event.get("content", ""))
                    elif event_type == "done":
                        final_meta["tools_used"] = event.get("tools_used", [])
                        final_meta["step_count"] = event.get("step_count", 0)

                    # 透传事件给客户端
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

            except Exception as e:
                logger.error(f"流式执行异常: {e}")
                yield f"data: {json.dumps({'type': 'error', 'content': f'流式执行失败: {str(e)}'}, ensure_ascii=False)}\n\n"
                return

            # 流结束后，保存到数据库和记忆系统
            full_answer = "".join(full_answer_parts).strip()
            if not full_answer:
                logger.warning("流式输出为空，跳过保存")
                return

            # 保存到数据库（如果有 session_id）
            if request.session_id:
                try:
                    await db.add_message(request.session_id, {
                        "role": "user",
                        "content": request.message,
                    })
                    await db.add_message(request.session_id, {
                        "role": "assistant",
                        "content": full_answer,
                        "tools_used": final_meta["tools_used"],
                    })
                except Exception as e:
                    logger.debug(f"保存消息到数据库失败: {e}")

            # 保存到记忆系统
            try:
                from ..shared_services import get_memory_manager
                memory = get_memory_manager()
                if memory:
                    memory.add_user_message(current_user.user_id, request.message, session_id)
                    memory.add_ai_message(current_user.user_id, full_answer, session_id)
            except Exception as e:
                logger.debug(f"保存到记忆失败: {e}")

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    except Exception as e:
        logger.error(f"LangGraph Agent 流式执行失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Agent 执行失败: {str(e)}",
        )


@router.get("/graph")
async def get_graph(
    current_user: UserResponse = Depends(get_current_user),
):
    """获取 Agent 图结构（用于可视化）"""
    try:
        agent = get_agent()
        graph = agent.get_graph()

        # 获取图结构
        graph_data = {
            "nodes": list(graph.nodes) if hasattr(graph, 'nodes') else [],
            "edges": list(graph.edges) if hasattr(graph, 'edges') else [],
        }

        return graph_data

    except Exception as e:
        logger.error(f"获取图结构失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取图结构失败: {str(e)}",
        )
