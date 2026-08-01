"""
认证API路由
用户注册、登录、获取用户信息
支持 httpOnly cookie 认证（防 XSS 窃取 token）
"""

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, EmailStr
from typing import Optional

from ..core.auth import (
    UserCreate, UserLogin, Token, UserResponse, UserRole,
    register_user, login_user, get_current_user
)
from ..core.config import settings

router = APIRouter(prefix="/api/auth", tags=["认证"])

# Cookie 配置常量
_COOKIE_KEY = "access_token"
_COOKIE_PATH = "/api"
_COOKIE_MAX_AGE = settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60


def _set_auth_cookie(response: Response, token: str):
    """设置 httpOnly 认证 cookie

    - HttpOnly: JavaScript 不可读（防 XSS 窃取）
    - Secure: 生产环境仅 HTTPS 传输
    - SameSite: 生产环境 Strict（防 CSRF），开发环境 Lax（便于调试）
    """
    response.set_cookie(
        key=_COOKIE_KEY,
        value=token,
        httponly=True,
        secure=settings.is_production,
        samesite="strict" if settings.is_production else "lax",
        max_age=_COOKIE_MAX_AGE,
        path=_COOKIE_PATH,
    )


def _clear_auth_cookie(response: Response):
    """清除认证 cookie"""
    response.delete_cookie(key=_COOKIE_KEY, path=_COOKIE_PATH)


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
async def register(request: RegisterRequest, response: Response):
    """
    用户注册

    - **username**: 用户名（唯一）
    - **password**: 密码
    - **email**: 邮箱
    - **role**: 角色（student/teacher/admin）

    注册成功后自动设置 httpOnly cookie，无需前端手动管理 token
    """

    user_data = UserCreate(
        username=request.username,
        password=request.password,
        email=request.email,
        role=request.role,
        org_name=request.org_name,
    )

    token = await register_user(user_data)
    _set_auth_cookie(response, token.access_token)
    return token


@router.post("/login", response_model=Token)
async def login(request: LoginRequest, response: Response):
    """
    用户登录

    - **username**: 用户名
    - **password**: 密码

    登录成功后自动设置 httpOnly cookie，无需前端手动管理 token
    """

    user_data = UserLogin(
        username=request.username,
        password=request.password
    )

    token = await login_user(user_data)
    _set_auth_cookie(response, token.access_token)
    return token


@router.post("/logout")
async def logout(response: Response):
    """用户登出（清除 httpOnly cookie）"""
    _clear_auth_cookie(response)
    return {"message": "已登出"}


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: UserResponse = Depends(get_current_user)):
    """
    获取当前用户信息

    认证方式：httpOnly cookie（主）或 Authorization: Bearer 头（兼容）
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
