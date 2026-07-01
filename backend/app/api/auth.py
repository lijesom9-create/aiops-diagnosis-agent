"""
认证API路由
用户注册、登录、获取用户信息
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from typing import Optional

from ..core.auth import (
    UserCreate, UserLogin, Token, UserResponse, UserRole,
    register_user, login_user, get_current_user
)

router = APIRouter(prefix="/api/auth", tags=["认证"])


class RegisterRequest(BaseModel):
    """注册请求"""
    username: str
    password: str
    email: str
    role: UserRole = UserRole.STUDENT
    org_name: str


class LoginRequest(BaseModel):
    """登录请求"""
    username: str
    password: str


@router.post("/register", response_model=Token)
async def register(request: RegisterRequest):
    """
    用户注册

    - **username**: 用户名（唯一）
    - **password**: 密码
    - **email**: 邮箱
    - **role**: 角色（student/teacher/admin）
    """

    user_data = UserCreate(
        username=request.username,
        password=request.password,
        email=request.email,
        role=request.role,
        org_name=request.org_name,
    )

    return await register_user(user_data)


@router.post("/login", response_model=Token)
async def login(request: LoginRequest):
    """
    用户登录

    - **username**: 用户名
    - **password**: 密码
    """

    user_data = UserLogin(
        username=request.username,
        password=request.password
    )

    return await login_user(user_data)


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: UserResponse = Depends(get_current_user)):
    """
    获取当前用户信息

    需要在请求头中携带Bearer token
    """

    return current_user


@router.get("/verify")
async def verify_token(current_user: UserResponse = Depends(get_current_user)):
    """
    验证token是否有效

    返回用户信息表示token有效
    """

    return {
        "valid": True,
        "user": current_user
    }
