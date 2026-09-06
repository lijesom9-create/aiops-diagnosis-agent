"""
LangGraph API

提供 LangGraph Agent 的 API 接口。
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from pydantic import BaseModel, Field, field_validator
from starlette.responses import StreamingResponse

from ..core.auth import UserResponse, get_current_user
from ..core.database import Database, get_db
from ..core.rate_limiter import rate_limit_dep

router = APIRouter(prefix="/api/langgraph", tags=["LangGraph Agent"])


# ========== 请求/响应模型 ==========

# 输入长度上限（字符数）：防止超长输入导致 token 超限或 DoS
# 8000 字符约等于 4000-6000 tokens（中文偏多），覆盖正常运维诊断提问场景
_MAX_MESSAGE_CHARS = 8000


class ChatRequest(BaseModel):
    """聊天请求"""
    # min_length=1 防止空字符串；max_length 防止超长输入 DoS
    # Field 的约束在 Pydantic v2 下自动生成 422 响应，无需手写校验代码
    message: str = Field(
        ...,
        min_length=1,
        max_length=_MAX_MESSAGE_CHARS,
        description="用户消息（1-8000 字符）",
    )
    session_id: Optional[str] = Field(default=None, description="会话 ID")
    use_web_search: bool = Field(default=False, description="是否使用 Web Search")
    context: Optional[Dict[str, Any]] = Field(default=None, description="额外上下文")

    @field_validator("message")
    @classmethod
    def _validate_message_content(cls, v: str) -> str:
        """消息内容校验：去除首尾空白后必须非空

        Field 的 min_length=1 能拦截空字符串，但无法拦截纯空白字符串（"   "）。
        这里用 validator 做语义校验，避免 LLM 收到空消息产生无意义响应。
        """
        if not v or not v.strip():
            raise ValueError("消息内容不能为空或纯空白")
        return v.strip()


class ChatResponse(BaseModel):
    """聊天响应"""
    content: str
    tools_used: list
    citations: list
    step_count: int
    diagnosis_report: Optional[dict] = Field(default=None, description="结构化诊断报告（运维诊断 Agent 专用，非诊断问题为 null）")
    session_id: Optional[str] = Field(default=None, description="会话 ID（首次对话时返回新创建的会话 ID）")
    prompt_version: Optional[str] = Field(default=None, description="系统 prompt 版本（审计/A-B 对照定位用）")


# ========== 会话管理 请求/响应模型 ==========

class CreateSessionRequest(BaseModel):
    """创建会话请求"""
    title: str = Field(default="新对话", description="会话标题")


class UpdateSessionRequest(BaseModel):
    """更新会话请求"""
    title: str = Field(..., description="新的会话标题", min_length=1, max_length=100)


class SessionItem(BaseModel):
    """会话列表项"""
    session_id: str
    user_id: str
    title: str = ""  # 旧会话可能没有 title，默认空字符串
    created_at: str
    updated_at: str
    message_count: int = 0


class SessionListResponse(BaseModel):
    """会话列表响应"""
    sessions: List[SessionItem]
    total: int


class MessageItem(BaseModel):
    """消息项"""
    role: str
    content: str
    tools_used: Optional[list] = None
    timestamp: Optional[str] = None


class MessageListResponse(BaseModel):
    """消息列表响应"""
    session_id: str
    messages: List[MessageItem]
    total: int


# ========== 全局 Agent 实例 ==========

import threading as _threading

_agent = None
_agent_lock = _threading.Lock()


def _resolve_llm_base_url(model_field: str, explicit: Optional[str]) -> str:
    """解析 LLM base_url：显式配置优先，否则按 provider 前缀查默认地址表。

    修复原实现的缺陷：AI_BASE_URL 未设时此前硬编码 deepseek 地址，
    配置 zhipu/qwen 等模型会打到错误端点。
    """
    if explicit:
        return explicit
    from ..core.ai_service import PROVIDER_CONFIGS, parse_model_name
    provider_name, _ = parse_model_name(model_field)
    return PROVIDER_CONFIGS.get(provider_name, {}).get("base_url", "https://api.deepseek.com")


def get_agent():
    """获取 Agent 实例（双重检查加锁：并发首调不会构建两个 Agent）"""
    global _agent
    if _agent is not None:
        return _agent
    with _agent_lock:
        if _agent is not None:
            return _agent
        import os

        from ..core.config import settings
        from ..langgraph_agent import LangGraphAgent
        from ..shared_services import get_knowledge_store

        # Checkpoint 持久化路径
        checkpoint_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "data", "langgraph_checkpoints.db"
        )

        # AI 容灾：配置了 AI_FALLBACK_* 时组装备用模型三元组（ChatOpenAI 不带 provider 前缀）
        llm_fallback = None
        if settings.AI_FALLBACK_MODEL and settings.AI_FALLBACK_API_KEY:
            llm_fallback = {
                "model": settings.AI_FALLBACK_MODEL.split("/")[-1]
                if "/" in settings.AI_FALLBACK_MODEL else settings.AI_FALLBACK_MODEL,
                "base_url": _resolve_llm_base_url(settings.AI_FALLBACK_MODEL, settings.AI_FALLBACK_BASE_URL),
                "api_key": settings.AI_FALLBACK_API_KEY,
            }

        _agent = LangGraphAgent(
            llm_model=settings.AI_MODEL.split("/")[-1] if "/" in settings.AI_MODEL else settings.AI_MODEL,
            llm_base_url=_resolve_llm_base_url(settings.AI_MODEL, settings.AI_BASE_URL),
            llm_api_key=settings.AI_API_KEY or "dummy",
            llm_fallback=llm_fallback,
            knowledge_store=get_knowledge_store(),
            checkpoint_path=checkpoint_path,
        )

    return _agent


# ========== 会话管理 辅助函数 ==========

async def _ensure_session(
    db: Database, user_id: str, session_id: Optional[str]
) -> str:
    """确保返回一个有效的 session_id。

    - 若传入 session_id，校验其属于当前用户后直接使用
    - 若未传入，自动创建新会话

    Args:
        db: 数据库实例
        user_id: 当前用户 ID
        session_id: 请求中传入的会话 ID（可为 None）

    Returns:
        有效的 session_id

    Raises:
        HTTPException: 传入的 session_id 不属于当前用户
    """
    if session_id:
        # 校验会话归属，防止越权访问他人会话
        session = await db.get_session(session_id)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"会话不存在: {session_id}",
            )
        if session.get("user_id") != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="无权访问该会话",
            )
        return session_id

    # 未传入 session_id：自动创建新会话
    new_session_id = await db.create_session(user_id, title="新对话")
    logger.info(f"为用户 {user_id} 创建新会话: {new_session_id}")
    return new_session_id


async def _auto_generate_title(
    db: Database, session_id: str, user_message: str, agent
) -> None:
    """首次对话后根据用户消息自动生成会话标题。

    仅在会话消息数为 2（一问一答）时触发，避免每次对话都调用 LLM。
    生成失败静默降级（保留"新对话"标题），不影响主流程。

    Args:
        db: 数据库实例
        session_id: 会话 ID
        user_message: 用户首条消息
        agent: LangGraph Agent 实例（复用其 LLM）
    """
    try:
        # 只在首轮对话（2 条消息）时生成标题
        msg_count = await db.get_session_message_count(session_id)
        if msg_count != 2:
            return

        # 用 Agent 的 LLM 生成简短标题（限制 token，降低成本）
        from langchain_core.messages import HumanMessage, SystemMessage
        title_messages = [
            SystemMessage(content=(
                "根据用户的首条消息生成一个简短的对话标题。"
                "要求：不超过 20 个字，不要标点符号结尾，"
                "直接输出标题文本，不要加引号或其他说明。"
            )),
            HumanMessage(content=f"用户消息：{user_message[:200]}"),
        ]
        # 低温度 + 限制 max_tokens 控制成本
        title_llm = agent.llm.with_kwargs(temperature=0.3, max_tokens=30)
        response = await title_llm.ainvoke(title_messages)
        title = (response.content or "").strip().strip('"\'').strip()

        # 标题为空或过长则不更新
        if title and len(title) <= 30:
            await db.update_session_title(session_id, title)
            logger.info(f"会话 {session_id} 自动生成标题: {title}")
    except Exception as e:
        # 标题生成失败不影响主流程
        logger.debug(f"自动生成标题失败（静默降级）: {e}")


# ========== API 端点 ==========

@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    db: Database = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
    _: None = Depends(rate_limit_dep),
):
    """
    与 LangGraph Agent 对话

    使用 LangGraph Agent 处理用户消息。
    若未传 session_id，会自动创建新会话并在响应中返回 session_id。
    """
    # Prompt Injection 检测：高风险拦截，中低风险放行
    # 必须在 agent 调用前执行，避免恶意指令劫持 Agent 行为
    from ..core.prompt_guard import detect_prompt_injection
    should_block, block_reason, risk_level = detect_prompt_injection(request.message)
    if should_block:
        logger.warning(
            f"用户 {current_user.user_id} 请求被 prompt injection 检测拦截: {block_reason}"
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"请求被拒绝：{block_reason}",
        )

    try:
        agent = get_agent()

        # 确保有有效的 session_id（未传则自动创建新会话）
        session_id = await _ensure_session(db, current_user.user_id, request.session_id)

        # 注入 user_id 到 context
        context = request.context or {}
        context["user_id"] = current_user.user_id

        # LLM 响应缓存：相同问题跳过 LLM 生成（10 分钟 TTL，文档更新时失效）
        import hashlib as _hashlib

        from ..core.cache import get_cache as _get_llm_cache
        _llm_cache = _get_llm_cache(600)
        _llm_cache_key = _hashlib.md5(f"{current_user.user_id}:{request.message}".encode()).hexdigest()
        _cached_resp = _llm_cache.get("llm_response", _llm_cache_key)

        if _cached_resp is not None:
            # 缓存命中：跳过 agent.run，直接用缓存的答案
            result = {
                "content": _cached_resp["content"],
                "tools_used": _cached_resp["tools_used"],
                "citations": _cached_resp["citations"],
                "diagnosis_report": _cached_resp.get("diagnosis_report"),
                "step_count": 0,
                "prompt_version": _cached_resp.get("prompt_version"),
            }
            _skip_title = True
            logger.info(f"LLM 响应缓存命中，跳过 LLM 调用: {request.message[:30]}...")
        else:
            # 执行 Agent
            result = await agent.run(
                user_input=request.message,
                session_id=session_id,
                context=context,
                use_web_search=request.use_web_search,
            )
            _skip_title = False

        # 保存消息到数据库（现在始终有 session_id）
        await db.add_message(session_id, {
            "role": "user",
            "content": request.message,
        })
        await db.add_message(session_id, {
            "role": "assistant",
            "content": result["content"],
            "tools_used": result["tools_used"],
        })

        # 首次对话后自动生成标题（失败静默降级；缓存命中时跳过，避免额外 LLM 调用）
        if not _skip_title:
            await _auto_generate_title(db, session_id, request.message, agent)

        # 保存到记忆系统（用于后续上下文注入）
        try:
            from ..shared_services import get_memory_manager
            memory = get_memory_manager()
            if memory:
                memory.add_user_message(current_user.user_id, request.message, session_id)
                memory.add_ai_message(current_user.user_id, result["content"], session_id)
        except Exception as e:
            logger.debug(f"保存对话到记忆失败: {e}")

        # 答案脱敏：对 LLM 输出做敏感信息过滤（手机号/身份证/API Key 等）
        from ..core.sanitizer import sanitize_text
        sanitized_content = sanitize_text(result["content"])

        # 写入 LLM 响应缓存（仅未命中时写入，缓存脱敏后的内容）
        if _cached_resp is None:
            _llm_cache.set("llm_response", {
                "content": sanitized_content,
                "tools_used": result["tools_used"],
                "citations": result["citations"],
                "diagnosis_report": result.get("diagnosis_report"),
                "prompt_version": result.get("prompt_version"),
            }, _llm_cache_key)

        return ChatResponse(
            content=sanitized_content,
            tools_used=result["tools_used"],
            citations=result["citations"],
            diagnosis_report=result.get("diagnosis_report"),
            step_count=result["step_count"],
            session_id=session_id,
            prompt_version=result.get("prompt_version"),
        )

    except HTTPException:
        raise
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
    _: None = Depends(rate_limit_dep),
):
    """
    与 LangGraph Agent 对话（token 级流式）

    使用 Server-Sent Events 流式返回结果。
    事件类型（SSE data 行内 JSON）：
    - {"type": "start"}                                    开始
    - {"type": "tool_calls", "tools": ["search_knowledge"]} 工具调用开始
    - {"type": "tool_result", "name": "...", "content": "..."} 工具结果
    - {"type": "token", "content": "你"}                  LLM token（核心，逐字输出）
    - {"type": "done", "tools_used": [...], "step_count": N, "session_id": "..."} 完成
    - {"type": "error", "content": "..."}                  错误
    """
    import json

    # Prompt Injection 检测：与 /chat 保持一致，高风险拦截
    # 流式端点同样需要在 agent 调用前拦截，避免恶意指令在流式过程中劫持
    from ..core.prompt_guard import detect_prompt_injection
    should_block, block_reason, risk_level = detect_prompt_injection(request.message)
    if should_block:
        logger.warning(
            f"用户 {current_user.user_id} 流式请求被 prompt injection 检测拦截: {block_reason}"
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"请求被拒绝：{block_reason}",
        )

    try:
        agent = get_agent()

        # 确保有有效的 session_id（未传则自动创建新会话）
        session_id = await _ensure_session(db, current_user.user_id, request.session_id)

        # 注入 user_id 到 context
        context = request.context or {}
        context["user_id"] = current_user.user_id

        async def event_generator():
            # 开始事件（带上 session_id，让前端能立即记录当前会话）
            yield f"data: {json.dumps({'type': 'start', 'session_id': session_id}, ensure_ascii=False)}\n\n"

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
                        final_meta["citations"] = event.get("citations", [])
                        final_meta["diagnosis_report"] = event.get("diagnosis_report")
                        final_meta["prompt_version"] = event.get("prompt_version")
                        # done 事件补上 session_id，前端据此更新会话列表
                        event["session_id"] = session_id

                        # 答案脱敏：检测完整答案是否含敏感信息
                        # 流式 token 已发出，这里发送脱敏后的完整内容供前端替换
                        from ..core.sanitizer import has_sensitive_info, sanitize_text
                        full_so_far = "".join(full_answer_parts)
                        if has_sensitive_info(full_so_far):
                            event["sanitized_content"] = sanitize_text(full_so_far)

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

            # 保存到数据库（现在始终有 session_id）
            try:
                await db.add_message(session_id, {
                    "role": "user",
                    "content": request.message,
                })
                await db.add_message(session_id, {
                    "role": "assistant",
                    "content": full_answer,
                    "tools_used": final_meta["tools_used"],
                })
            except Exception as e:
                logger.debug(f"保存消息到数据库失败: {e}")

            # 首次对话后自动生成标题（失败静默降级，不阻塞流结束）
            await _auto_generate_title(db, session_id, request.message, agent)

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

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"LangGraph Agent 流式执行失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Agent 执行失败: {str(e)}",
        )


# ========== 会话管理 API 端点 ==========

async def _verify_session_owner(db: Database, session_id: str, user_id: str) -> dict:
    """校验会话存在且属于当前用户，返回会话元数据。越权访问返回 403。"""
    session = await db.get_session(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"会话不存在: {session_id}",
        )
    if session.get("user_id") != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权访问该会话",
        )
    return session


@router.post("/sessions", response_model=SessionItem)
async def create_session(
    request: CreateSessionRequest,
    db: Database = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """创建新会话

    用户开始新主题对话时调用，返回新的 session_id。
    后续 /chat 或 /chat/stream 请求带上此 session_id 即可在同一会话内继续对话。
    """
    try:
        session_id = await db.create_session(current_user.user_id, title=request.title)
        session = await db.get_session(session_id)
        # 补齐 message_count 字段（新建会话为 0）
        session["message_count"] = 0
        return SessionItem(**session)
    except Exception as e:
        logger.error(f"创建会话失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"创建会话失败: {str(e)}",
        )


@router.get("/sessions", response_model=SessionListResponse)
async def list_sessions(
    limit: int = 50,
    db: Database = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """列出当前用户的所有会话

    按 updated_at 降序返回，每项含 message_count（消息数）。
    不返回消息体，避免响应过大。
    """
    try:
        sessions = await db.get_user_sessions(current_user.user_id, limit=limit)
        items = [SessionItem(**s) for s in sessions]
        return SessionListResponse(sessions=items, total=len(items))
    except Exception as e:
        logger.error(f"获取会话列表失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取会话列表失败: {str(e)}",
        )


@router.get("/sessions/{session_id}/messages", response_model=MessageListResponse)
async def get_session_messages(
    session_id: str,
    limit: int = 100,
    db: Database = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """获取指定会话的消息历史

    按 timestamp 升序返回，支持 limit 控制返回数量（默认 100，取最近 N 条）。
    """
    try:
        # 校验会话归属
        await _verify_session_owner(db, session_id, current_user.user_id)

        messages = await db.get_session_messages(session_id, limit=limit)
        items = [MessageItem(**msg) for msg in messages]
        return MessageListResponse(
            session_id=session_id,
            messages=items,
            total=len(items),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取会话消息失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取会话消息失败: {str(e)}",
        )


@router.patch("/sessions/{session_id}", response_model=SessionItem)
async def update_session(
    session_id: str,
    request: UpdateSessionRequest,
    db: Database = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """重命名会话标题"""
    try:
        # 校验会话归属
        await _verify_session_owner(db, session_id, current_user.user_id)

        await db.update_session_title(session_id, request.title)
        session = await db.get_session(session_id)
        # 补齐 message_count
        session["message_count"] = await db.get_session_message_count(session_id)
        return SessionItem(**session)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"更新会话失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"更新会话失败: {str(e)}",
        )


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    db: Database = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """删除会话（含全部消息）

    注意：此操作不可恢复。LangGraph checkpoint 中的对应 thread 历史不会被清理
    （checkpoint 与业务会话表分离），但业务侧的消息记录会被清除。
    """
    try:
        # 校验会话归属
        await _verify_session_owner(db, session_id, current_user.user_id)

        deleted = await db.delete_session(session_id)
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"会话不存在或已删除: {session_id}",
            )
        return {"message": f"会话 {session_id} 已删除", "session_id": session_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"删除会话失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"删除会话失败: {str(e)}",
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


# ========== 答案反馈 ==========

class FeedbackRequest(BaseModel):
    """答案反馈请求"""
    session_id: str = Field(..., description="会话 ID")
    message_content: str = Field(..., description="被反馈的答案内容")
    rating: str = Field(..., description="positive / negative")
    comment: Optional[str] = Field(default=None, description="反馈备注（点踩时填写）")
    document_ids: Optional[List[str]] = Field(
        default=None,
        description="答案引用的文档 ID 列表（点踩时传入，触发知识库待复核标记）",
    )


class FeedbackResponse(BaseModel):
    """答案反馈响应"""
    feedback_id: str
    message: str


@router.post("/feedback", response_model=FeedbackResponse)
async def submit_feedback(
    request: FeedbackRequest,
    current_user: UserResponse = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """提交答案反馈（点赞/点踩）"""
    try:
        if request.rating not in ("positive", "negative"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="rating 必须为 positive 或 negative",
            )

        feedback_id = f"fb_{__import__('uuid').uuid4().hex[:12]}"
        from datetime import datetime
        feedback_doc = {
            "feedback_id": feedback_id,
            "user_id": current_user.user_id,
            "session_id": request.session_id,
            "message_content": request.message_content[:500],  # 截断防止过大
            "rating": request.rating,
            "comment": request.comment or "",
            "document_ids": request.document_ids or [],
            "created_at": datetime.now().isoformat(),
        }

        # 走 Database 方法（兼容内存降级），替代裸写 collection
        await db.save_feedback(feedback_doc)

        # 负反馈 → 知识库质量闭环：将答案引用的文档标记为待复核
        # （SOP 过期/内容错误通常通过点踩暴露，标记后由管理员在文档列表复核）
        reviewed_count = 0
        if request.rating == "negative" and request.document_ids:
            reviewed_count = await db.mark_documents_for_review(
                request.document_ids,
                reason="negative_feedback",
                feedback_id=feedback_id,
            )

        logger.info(
            f"反馈提交: {feedback_id} - {request.rating} (user={current_user.user_id}, "
            f"待复核文档: {reviewed_count})"
        )
        return FeedbackResponse(feedback_id=feedback_id, message="反馈已提交")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"提交反馈失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="提交反馈失败",
        )


@router.get("/feedback/stats")
async def get_feedback_stats(
    current_user: UserResponse = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """获取反馈统计（管理员视角，暂不鉴权细粒度）"""
    try:
        collection = db._mongo["feedback"]
        total = await collection.count_documents({})
        positive = await collection.count_documents({"rating": "positive"})
        negative = await collection.count_documents({"rating": "negative"})

        return {
            "total": total,
            "positive": positive,
            "negative": negative,
            "satisfaction_rate": round(positive / total, 4) if total > 0 else 0,
        }
    except Exception as e:
        logger.error(f"获取反馈统计失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="获取反馈统计失败",
        )
