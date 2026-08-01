# Python 后端编码规范

| 字段 | 值 |
|------|------|
| 文档分类 | dev_guide |
| 适用范围 | 公司所有 Python 后端项目 |
| Python 版本 | 3.11+ |
| 维护团队 | 后端架构组 |
| 更新日期 | 2026-07-18 |

## 1. 命名规范

### 1.1 总则

- 变量、函数、模块使用 `snake_case`。
- 类使用 `PascalCase`。
- 常量使用 `UPPER_SNAKE_CASE`。
- 私有成员以下划线开头：`_internal_method`。
- 名称应具备业务含义，禁止 `data1`、`temp`、`a/b/c` 等无意义命名。

### 1.2 正确示例

```python
# 正确
user_service = UserService()
MAX_RETRY_COUNT = 3
DEFAULT_PAGE_SIZE = 20

def calculate_order_total(items: list[OrderItem]) -> int:
    pass

class OrderRepository:
    pass
```

### 1.3 错误示例

```python
# 错误：命名无意义、风格不一致
data1 = []
temp = 3
def Calc(x, y):  # 不应使用 PascalCase 命名函数
    pass
class orderRepo:  # 类名应使用 PascalCase
    pass
```

---

## 2. 代码格式

### 2.1 格式要求

- 缩进：4 个空格，禁止 Tab。
- 行宽：单行不超过 120 字符。
- import 顺序：标准库 → 第三方库 → 本项目模块，每组之间空一行。
- 文件末尾保留一个空行。
- 字符串优先使用双引号 `"`，与 Black 默认风格一致。

### 2.2 import 顺序示例

```python
# 正确
import os
import sys
from datetime import datetime

from fastapi import FastAPI, Depends
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.user import User
```

```python
# 错误：混乱的 import
from app.models.user import User
import os
from fastapi import FastAPI
from datetime import datetime
```

### 2.3 工具链

项目统一使用以下工具：

- 格式化：`black`（line-length=120）
- 排序：`isort`（profile=black）
- 检查：`flake8` + `mypy`
- 提交钩子：`pre-commit`

`.pre-commit-config.yaml` 示例：

```yaml
repos:
  - repo: https://github.com/psf/black
    rev: 24.3.0
    hooks:
      - id: black
        args: ["--line-length=120"]
  - repo: https://github.com/pycqa/isort
    rev: 5.13.2
    hooks:
      - id: isort
        args: ["--profile=black"]
  - repo: https://github.com/pycqa/flake8
    rev: 7.0.0
    hooks:
      - id: flake8
        args: ["--max-line-length=120"]
```

---

## 3. 函数设计

### 3.1 长度与参数

- 单函数长度建议不超过 50 行，硬上限 80 行。
- 函数参数不超过 5 个，超过应封装为数据类或 Pydantic 模型。
- 必须添加类型注解，包括返回值。

### 3.2 类型注解

```python
# 正确：完整的类型注解
from typing import Optional, List, Dict, Union
from pydantic import BaseModel


class UserCreateRequest(BaseModel):
    username: str
    email: str
    age: Optional[int] = None
    tags: List[str] = []
    metadata: Dict[str, Union[str, int]] = {}


def get_user(user_id: int) -> Optional[User]:
    pass


def batch_query_users(user_ids: list[int]) -> dict[int, User]:
    pass
```

```python
# 错误：无类型注解
def get_user(user_id):  # 缺少返回类型
    pass

def process(data):  # 缺少参数和返回类型
    pass
```

### 3.3 Python 3.10+ 内置泛型

```python
# 推荐：使用内置泛型（Python 3.10+）
def find_user(ids: list[int]) -> dict[int, User] | None:
    pass

# 兼容写法（3.9 及以下）
from typing import List, Dict, Optional
def find_user(ids: List[int]) -> Optional[Dict[int, User]]:
    pass
```

---

## 4. 异常处理

### 4.1 try-except 规范

- 禁止裸 `except:`，必须捕获具体异常。
- 禁止 `pass` 吞掉异常，至少要记录日志。
- 不要在 `except` 中放置业务逻辑。

```python
# 正确
import logging
logger = logging.getLogger(__name__)

try:
    response = http_client.get(url, timeout=5)
except http_client.TimeoutError as e:
    logger.warning("请求超时 url=%s err=%s", url, e)
    raise RetryableError("请求超时，请重试") from e
except http_client.ConnectionError as e:
    logger.error("连接失败 url=%s err=%s", url, e)
    raise ServiceUnavailableError("服务不可用") from e
```

```python
# 错误
try:
    response = http_client.get(url)
except:  # 裸 except
    pass  # 吞掉异常
```

### 4.2 自定义异常

业务异常应继承自统一基类，并按层级设计：

```python
class AppException(Exception):
    """业务异常基类"""
    code: int = 50000
    message: str = "服务内部错误"

    def __init__(self, message: str | None = None, code: int | None = None):
        self.message = message or self.message
        self.code = code or self.code
        super().__init__(self.message)


class UserNotFoundError(AppException):
    code = 40400
    message = "用户不存在"


class InvalidPasswordError(AppException):
    code = 40101
    message = "账号或密码错误"


# 使用
def get_user(user_id: int) -> User:
    user = user_repo.get(user_id)
    if user is None:
        raise UserNotFoundError(f"用户ID={user_id}不存在")
    return user
```

### 4.3 异常与日志

异常发生时必须记录日志，且包含足够的上下文信息：

```python
# 正确
logger.exception("创建订单失败 user_id=%s amount=%s", user_id, amount)
# 或
logger.error("创建订单失败 user_id=%s amount=%s err=%s", user_id, amount, e, exc_info=True)
```

---

## 5. 日志规范

### 5.1 日志级别

| 级别 | 使用场景 |
|------|----------|
| DEBUG | 调试信息，生产环境关闭 |
| INFO | 关键业务节点：下单、支付、登录等 |
| WARNING | 可恢复的异常：重试、降级、限流 |
| ERROR | 不可恢复的异常：第三方调用失败、数据库异常 |
| CRITICAL | 系统级故障：服务无法启动、数据损坏 |

### 5.2 日志格式

统一使用 JSON 格式输出，便于 ELK 采集：

```python
import logging
import json_log_formatter

formatter = json_log_formatter.JSONFormatter()
handler = logging.StreamHandler()
handler.setFormatter(formatter)

logger = logging.getLogger("app")
logger.setLevel(logging.INFO)
logger.addHandler(handler)

logger.info("订单创建成功", extra={
    "order_id": "OD20260731",
    "user_id": "100123",
    "amount": 27900,
})
```

### 5.3 敏感信息脱敏

禁止打印密码、Token、身份证、手机号等敏感信息。统一使用脱敏工具：

```python
# 正确
from app.core.masking import mask_phone, mask_email

logger.info("用户登录 phone=%s", mask_phone(phone))  # 138****8000
logger.info("用户邮箱 email=%s", mask_email(email))  # a***e@company.com
```

```python
# 错误：明文打印敏感信息
logger.info("用户登录 password=%s phone=%s", password, phone)
```

### 5.4 日志内容规范

- 日志消息使用英文，避免乱码。
- 使用 `%s` 占位符，不要使用 f-string 拼接（影响性能且不符合 logging 规范）。
- 必须包含可定位问题的关键字段：user_id、order_id、request_id。

```python
# 正确
logger.info("order created user_id=%s order_id=%s amount=%s", user_id, order_id, amount)

# 错误
logger.info(f"order created {user_id} {order_id}")  # f-string + 缺少字段名
```

---

## 6. 数据库操作

### 6.1 ORM 使用

统一使用 SQLAlchemy 2.0+ 风格，禁止拼接原生 SQL：

```python
# 正确：使用 ORM
from sqlalchemy import select
from sqlalchemy.orm import Session

def get_user_by_email(db: Session, email: str) -> User | None:
    stmt = select(User).where(User.email == email)
    return db.execute(stmt).scalar_one_or_none()
```

```python
# 错误：SQL 拼接，存在注入风险
def get_user_by_email(db: Session, email: str):
    query = f"SELECT * FROM users WHERE email = '{email}'"
    return db.execute(query).fetchone()
```

### 6.2 事务管理

使用上下文管理器管理事务，避免手动 commit/rollback：

```python
# 正确
from contextlib import contextmanager

@contextmanager
def transaction(db: Session):
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise

# 使用
with transaction(db) as tx:
    tx.add(order)
    tx.add(order_item)
```

### 6.3 连接池

生产环境配置：

```python
# config.py
DATABASE_URL = "postgresql+psycopg://user:pass@db:5432/app"
DB_POOL_SIZE = 20
DB_MAX_OVERFLOW = 10
DB_POOL_TIMEOUT = 30
DB_POOL_RECYCLE = 1800

engine = create_engine(
    DATABASE_URL,
    pool_size=DB_POOL_SIZE,
    max_overflow=DB_MAX_OVERFLOW,
    pool_timeout=DB_POOL_TIMEOUT,
    pool_recycle=DB_POOL_RECYCLE,
    pool_pre_ping=True,  # 防止使用失效连接
)
```

---

## 7. API 设计

### 7.1 RESTful 规范

- 使用 HTTP 动词表达操作：GET 查询、POST 创建、PUT 更新、DELETE 删除。
- 资源名使用复数：`/api/v1/users`、`/api/v1/orders`。
- 路径参数用于资源 ID：`/api/v1/users/{user_id}`。
- 查询参数用于过滤、排序、分页。

### 7.2 版本管理

URL 中携带版本号：`/api/v1/`、`/api/v2/`。版本升级时旧版本至少维护 6 个月。

### 7.3 分页与排序

统一分页参数 `page`、`page_size`，统一排序参数 `sort`，格式为 `field:asc|desc`。

### 7.4 Pydantic 模型示例

```python
from pydantic import BaseModel, Field, EmailStr
from typing import Optional


class UserCreateRequest(BaseModel):
    username: str = Field(..., min_length=4, max_length=32, description="用户名")
    password: str = Field(..., min_length=8, max_length=32, description="密码")
    email: EmailStr
    nickname: Optional[str] = Field(None, max_length=32)


class UserResponse(BaseModel):
    user_id: str
    username: str
    email: str
    nickname: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True
```

---

## 8. 安全规范

### 8.1 SQL 注入防护

- 禁止字符串拼接 SQL，必须使用参数化查询或 ORM。
- 用户输入必须经过校验和转义。

```python
# 正确
stmt = select(User).where(User.email == email)

# 错误
query = f"SELECT * FROM users WHERE email = '{email}'"
```

### 8.2 XSS 防护

- API 返回 JSON 数据，由前端框架自动转义。
- 富文本内容入库前使用 `bleach` 清洗：

```python
import bleach

clean_html = bleach.clean(
    user_input_html,
    tags=["p", "br", "strong", "em"],
    attributes={},
    strip=True,
)
```

### 8.3 CSRF 防护

- 表单提交类接口必须校验 CSRF Token。
- 前后端分离项目使用 SameSite=Strict 的 Cookie。

### 8.4 密码处理

- 禁止明文存储密码，必须使用 `bcrypt` 或 `argon2` 哈希。
- 密码强度校验：至少 8 位，包含大小写字母与数字。

```python
# 正确
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)
```

```python
# 错误
import hashlib

def hash_password(password: str) -> str:
    return hashlib.md5(password.encode()).hexdigest()  # MD5 不安全
```

### 8.5 敏感信息配置

- 密钥、Token 等敏感信息不得硬编码，必须从环境变量或密钥管理服务读取。
- `.env` 文件不得提交到 Git，须加入 `.gitignore`。

```python
# 正确
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    database_url: str
    secret_key: str
    redis_url: str

    class Config:
        env_file = ".env"

settings = Settings()
```

```python
# 错误
SECRET_KEY = "hardcoded-super-secret-key-12345"  # 硬编码
```

---

## 9. 工具与检查

项目应在 CI 中集成以下检查：

| 工具 | 用途 | 失败行为 |
|------|------|----------|
| black | 格式化检查 | CI 失败 |
| isort | import 排序 | CI 失败 |
| flake8 | 代码风格 | CI 失败 |
| mypy | 类型检查 | CI 失败 |
| bandit | 安全扫描 | CI 失败 |
| pytest | 单元测试 | 覆盖率低于 80% 失败 |

```bash
# 本地预检查命令
black --check app/ && isort --check app/ && flake8 app/ && mypy app/ && pytest --cov=app --cov-fail-under=80
```

## 10. 变更记录

| 版本 | 日期 | 变更内容 |
|------|------|----------|
| v2.0 | 2026-07-18 | 升级至 Python 3.11，引入 Pydantic v2 |
| v1.5 | 2026-04-10 | 新增安全规范章节 |
| v1.0 | 2025-12-01 | 首次发布 |
