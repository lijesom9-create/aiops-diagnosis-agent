# FastAPI 路由与依赖注入

## 路径操作

在 FastAPI 中，使用 `@app.get`、`@app.post` 等装饰器定义路由。每个路径操作函数接收请求参数并返回响应数据。FastAPI 会自动根据类型注解生成请求体验证和文档。

```python
from fastapi import FastAPI

app = FastAPI()

@app.get("/items/{item_id}")
async def read_item(item_id: int):
    return {"item_id": item_id}
```

## 路径参数与查询参数

路径参数通过 `{}` 声明在 URL 路径中，查询参数则通过函数参数自动解析。例如 `/items/?skip=0&limit=10` 中的 `skip` 和 `limit` 就是查询参数。

```python
@app.get("/items/")
async def list_items(skip: int = 0, limit: int = 10):
    return {"skip": skip, "limit": limit}
```

## 依赖注入

FastAPI 的依赖注入通过 `Depends` 实现。可以把公共逻辑封装成依赖函数，然后在路径操作中声明使用。依赖可以嵌套，也可以依赖其他依赖，非常适合处理认证、数据库连接等通用需求。

```python
from fastapi import Depends

async def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.get("/users/")
async def read_users(db: Session = Depends(get_db)):
    return db.query(User).all()
```
