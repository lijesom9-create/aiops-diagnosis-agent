"""
认证和授权模块
JWT token管理和用户认证
"""

from datetime import datetime, timedelta, timezone
from typing import Optional
import jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from loguru import logger
from enum import Enum
import uuid

from .config import settings
from .database import db


# 密码加密上下文
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# JWT Bearer认证
security = HTTPBearer()


# 数据模型
class UserRole(str, Enum):
    """用户角色枚举"""
    STUDENT = "student"
    TEACHER = "teacher"
    ADMIN = "admin"


class UserCreate(BaseModel):
    """用户注册请求"""
    username: str = Field(..., min_length=2, max_length=50)
    password: str = Field(..., min_length=6, max_length=128)
    email: str
    role: UserRole = UserRole.STUDENT
    org_name: str = Field(..., min_length=1, max_length=100)


class UserLogin(BaseModel):
    """用户登录请求"""
    username: str
    password: str


class Token(BaseModel):
    """Token响应"""
    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    """用户信息响应"""
    user_id: str
    username: str
    email: str
    role: str
    org_id: str = ""
    org_name: str = ""


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """验证密码"""
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    """获取密码哈希"""
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """创建访问令牌"""
    to_encode = data.copy()

    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

    return encoded_jwt


async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> UserResponse:
    """获取当前用户（依赖注入）"""

    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="无法验证凭据",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        # 解码JWT
        payload = jwt.decode(credentials.credentials, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        user_id: str = payload.get("sub")
        org_id: str = payload.get("org_id", "")

        if user_id is None:
            raise credentials_exception

        # 从数据库获取用户
        user = await db.get_user(user_id)

        if user is None:
            raise credentials_exception

        # 获取组织名称
        org_name = ""
        if org_id:
            org = await db.get_org(org_id)
            if org:
                org_name = org.get("name", "")

        return UserResponse(
            user_id=user["user_id"],
            username=user["username"],
            email=user["email"],
            role=user["role"],
            org_id=org_id,
            org_name=org_name,
        )

    except jwt.PyJWTError as e:
        logger.error(f"JWT解析错误: {e}")
        raise credentials_exception


async def require_admin_or_teacher(current_user: UserResponse = Depends(get_current_user)) -> UserResponse:
    """要求管理员或教师权限"""
    if current_user.role not in ("admin", "teacher"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员或教师权限"
        )
    return current_user


async def register_user(user_data: UserCreate) -> Token:
    """注册用户"""

    # 检查用户名是否已存在
    existing_user = await db.get_user_by_username(user_data.username)
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="用户名已存在"
        )

    # 检查组织名是否已存在
    existing_org = await db.get_org_by_name(user_data.org_name)
    if existing_org:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="组织名已存在"
        )

    # 生成用户ID（UUID保证唯一性）
    user_id = f"user_{uuid.uuid4().hex[:12]}"

    # 创建组织
    org_id = await db.create_org(name=user_data.org_name, owner_id=user_id)

    # 创建用户数据
    user_dict = {
        "user_id": user_id,
        "username": user_data.username,
        "email": user_data.email,
        "role": user_data.role.value if isinstance(user_data.role, Enum) else user_data.role,
        "hashed_password": get_password_hash(user_data.password),
        "org_id": org_id,
    }

    # 保存到数据库
    await db.create_user(user_dict)

    # 创建访问令牌（携带 org_id）
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user_id, "org_id": org_id},
        expires_delta=access_token_expires
    )

    logger.info(f"用户注册成功: {user_data.username}, 组织: {user_data.org_name}")

    return Token(access_token=access_token)


async def login_user(user_data: UserLogin) -> Token:
    """用户登录"""

    # 查找用户
    user = await db.get_user_by_username(user_data.username)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误"
        )

    # 验证密码
    if not verify_password(user_data.password, user["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误"
        )

    # 创建访问令牌（携带 org_id）
    org_id = user.get("org_id", "")
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user["user_id"], "org_id": org_id},
        expires_delta=access_token_expires
    )

    logger.info(f"用户登录成功: {user_data.username}")

    return Token(access_token=access_token)
