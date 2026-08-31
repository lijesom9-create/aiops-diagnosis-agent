"""
记忆管理 API

提供对话历史、用户画像、档案记忆管理功能。
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from pydantic import BaseModel, Field

from ..core.auth import UserResponse, get_current_user
from ..shared_services import get_memory_manager

router = APIRouter(prefix="/api/memory", tags=["记忆管理"])


# ========== 请求/响应模型 ==========

class UserProfileUpdate(BaseModel):
    """用户画像更新请求"""
    name: Optional[str] = None
    learning_style: Optional[str] = None
    preferences: Optional[Dict[str, Any]] = None


class MemoryAddRequest(BaseModel):
    """添加记忆请求"""
    content: str = Field(..., description="内容")
    category: str = Field(default="note", description="类别: note, learning, important")
    tags: List[str] = Field(default_factory=list, description="标签")


class UserProfileResponse(BaseModel):
    """用户画像响应"""
    user_id: str
    name: str
    learning_style: str
    weak_topics: List[str]
    strong_topics: List[str]
    preferences: Dict[str, Any]


class MemoryStatsResponse(BaseModel):
    """记忆统计响应"""
    user_id: str
    has_profile: bool
    weak_topics: int
    strong_topics: int
    sessions: int
    total_turns: int
    archival_entries: int


# ========== 用户画像 API ==========

@router.get("/profile", response_model=UserProfileResponse)
async def get_profile(
    current_user: UserResponse = Depends(get_current_user),
):
    """获取用户画像"""
    try:
        memory = get_memory_manager()
        profile = memory.get_user_profile(current_user.user_id)

        return UserProfileResponse(
            user_id=profile.user_id,
            name=profile.name,
            learning_style=profile.learning_style,
            weak_topics=profile.weak_topics,
            strong_topics=profile.strong_topics,
            preferences=profile.preferences,
        )

    except Exception as e:
        logger.error(f"获取用户画像失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取用户画像失败: {str(e)}"
        )


@router.put("/profile")
async def update_profile(
    request: UserProfileUpdate,
    current_user: UserResponse = Depends(get_current_user),
):
    """更新用户画像"""
    try:
        memory = get_memory_manager()

        update_data = {}
        if request.name is not None:
            update_data["name"] = request.name
        if request.learning_style is not None:
            update_data["learning_style"] = request.learning_style
        if request.preferences is not None:
            update_data["preferences"] = request.preferences

        if update_data:
            memory.update_user_profile(current_user.user_id, **update_data)

        return {"message": "用户画像已更新"}

    except Exception as e:
        logger.error(f"更新用户画像失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"更新用户画像失败: {str(e)}"
        )


@router.post("/profile/weak-topic")
async def add_weak_topic(
    topic: str,
    current_user: UserResponse = Depends(get_current_user),
):
    """添加薄弱知识点"""
    try:
        memory = get_memory_manager()
        memory.add_weak_topic(current_user.user_id, topic)
        return {"message": f"已添加薄弱知识点: {topic}"}

    except Exception as e:
        logger.error(f"添加薄弱知识点失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"添加薄弱知识点失败: {str(e)}"
        )


@router.post("/profile/strong-topic")
async def add_strong_topic(
    topic: str,
    current_user: UserResponse = Depends(get_current_user),
):
    """添加擅长领域"""
    try:
        memory = get_memory_manager()
        memory.add_strong_topic(current_user.user_id, topic)
        return {"message": f"已添加擅长领域: {topic}"}

    except Exception as e:
        logger.error(f"添加擅长领域失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"添加擅长领域失败: {str(e)}"
        )


# ========== 档案记忆 API ==========

@router.post("/entries")
async def add_memory_entry(
    request: MemoryAddRequest,
    current_user: UserResponse = Depends(get_current_user),
):
    """添加档案记忆"""
    try:
        memory = get_memory_manager()

        entry = memory.add_memory(
            user_id=current_user.user_id,
            content=request.content,
            category=request.category,
            tags=request.tags,
        )

        return {
            "message": "记忆已添加",
            "entry_id": entry.id,
        }

    except Exception as e:
        logger.error(f"添加记忆失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"添加记忆失败: {str(e)}"
        )


@router.get("/entries")
async def search_memory(
    query: str,
    category: Optional[str] = None,
    limit: int = 10,
    current_user: UserResponse = Depends(get_current_user),
):
    """搜索档案记忆"""
    try:
        memory = get_memory_manager()

        entries = memory.search_memory(
            query=query,
            user_id=current_user.user_id,
            top_k=limit,
            category=category,
        )

        return {
            "entries": [
                {
                    "id": e.id,
                    "content": e.content,
                    "category": e.category,
                    "tags": e.tags,
                    "created_at": e.created_at,
                }
                for e in entries
            ],
            "total": len(entries),
        }

    except Exception as e:
        logger.error(f"搜索记忆失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"搜索记忆失败: {str(e)}"
        )


# ========== 统计 API ==========

@router.get("/stats", response_model=MemoryStatsResponse)
async def get_stats(
    current_user: UserResponse = Depends(get_current_user),
):
    """获取记忆统计"""
    try:
        memory = get_memory_manager()
        stats = memory.get_stats(current_user.user_id)

        return MemoryStatsResponse(**stats)

    except Exception as e:
        logger.error(f"获取统计失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取统计失败: {str(e)}"
        )


@router.delete("/clear")
async def clear_all_memory(
    current_user: UserResponse = Depends(get_current_user),
):
    """清空所有记忆"""
    try:
        memory = get_memory_manager()
        stats = memory.clear_user_data(current_user.user_id)

        return {
            "message": "已清空所有记忆",
            "stats": stats,
        }

    except Exception as e:
        logger.error(f"清空记忆失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"清空记忆失败: {str(e)}"
        )
