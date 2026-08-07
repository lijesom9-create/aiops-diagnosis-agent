"""
管理后台 API（仅管理员）

提供：
- 知识库统计（文档数/向量数/用户数/任务状态分布）
- 用户管理（分页列表/角色变更）
- 任务监控（文档处理任务列表）
"""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel
from loguru import logger

from ..core.auth import require_admin, UserResponse
from ..core.database import get_db, Database
from ..shared_services import get_knowledge_store

router = APIRouter(prefix="/api/admin", tags=["管理后台"])

# 合法角色
_VALID_ROLES = {"admin", "teacher", "student"}


class UserRoleUpdate(BaseModel):
    """用户角色更新请求"""
    role: str  # admin/teacher/student


@router.get("/stats")
async def get_admin_stats(
    current_user: UserResponse = Depends(require_admin),
    db: Database = Depends(get_db),
):
    """知识库统计（文档/用户/向量/任务状态分布）"""
    try:
        # 文档统计（按状态分桶）
        all_docs = await db.get_all_documents()
        doc_stats = {"total": len(all_docs), "completed": 0, "processing": 0, "pending": 0, "failed": 0}
        for d in all_docs:
            s = d.get("status", "pending")
            if s in doc_stats:
                doc_stats[s] += 1

        # 用户统计
        user_count = await db.count_users()

        # 向量统计（知识库条目数）
        vector_count = 0
        store = get_knowledge_store()
        if store:
            try:
                vector_count = store.size()
            except Exception as e:
                logger.warning(f"获取向量数失败: {e}")

        return {
            "documents": doc_stats,
            "users": {"total": user_count},
            "vectors": {"total": vector_count},
        }
    except Exception as e:
        logger.exception(f"获取统计失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="获取统计失败"
        )


@router.get("/users")
async def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: UserResponse = Depends(require_admin),
    db: Database = Depends(get_db),
):
    """列出所有用户（分页，已过滤 hashed_password）"""
    try:
        users, total = await db.get_all_users(page=page, page_size=page_size)
        return {
            "users": users,
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    except Exception as e:
        logger.exception(f"列出用户失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="列出用户失败"
        )


@router.patch("/users/{user_id}/role")
async def update_user_role(
    user_id: str,
    body: UserRoleUpdate,
    current_user: UserResponse = Depends(require_admin),
    db: Database = Depends(get_db),
):
    """修改用户角色（admin/teacher/student）

    安全约束：不能降级自己的管理员角色（防止误操作导致系统无管理员）。
    """
    if body.role not in _VALID_ROLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"role 必须是 {sorted(_VALID_ROLES)} 之一"
        )

    # 不能降级自己（防止误操作导致无管理员）
    if user_id == current_user.user_id and body.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不能降级自己的管理员角色"
        )

    user = await db.get_user(user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在"
        )

    old_role = user.get("role", "student")
    ok = await db.update_user(user_id, {"role": body.role})
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="更新失败"
        )

    logger.info(f"用户角色变更: {user_id} {old_role} -> {body.role} (操作者: {current_user.user_id})")
    return {"user_id": user_id, "username": user.get("username"), "old_role": old_role, "new_role": body.role}


@router.get("/tasks")
async def list_tasks(
    status_filter: Optional[str] = Query(None, description="按状态过滤: pending/processing/completed/failed"),
    current_user: UserResponse = Depends(require_admin),
    db: Database = Depends(get_db),
):
    """任务监控：列出文档处理任务（有 task_id 的文档）"""
    try:
        all_docs = await db.get_all_documents()
        tasks = []
        for d in all_docs:
            if not d.get("task_id"):
                continue
            if status_filter and d.get("status") != status_filter:
                continue
            tasks.append({
                "document_id": d.get("document_id"),
                "filename": d.get("filename"),
                "title": d.get("title"),
                "status": d.get("status"),
                "task_id": d.get("task_id"),
                "error_message": d.get("error_message", ""),
                "created_at": d.get("created_at"),
                "finished_at": d.get("finished_at"),
            })
        # 按创建时间倒序
        tasks.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return {"tasks": tasks, "total": len(tasks)}
    except Exception as e:
        logger.exception(f"列出任务失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="列出任务失败"
        )

