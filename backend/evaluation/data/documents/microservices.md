# 微服务架构设计

## 微服务 vs 单体架构

| 维度 | 单体架构 | 微服务架构 |
|------|---------|-----------|
| 部署 | 单个包 | 多个独立服务 |
| 技术栈 | 统一 | 可异构 |
| 扩展性 | 整体扩展 | 按需扩展 |
| 数据存储 | 共享数据库 | 每服务独立数据库 |
| 团队协作 | 冲突多 | 独立开发 |
| 运维成本 | 低 | 高 |
| 故障影响 | 全局 | 局部隔离 |

## 服务通信方式

### 同步通信（REST/gRPC）

```python
from fastapi import FastAPI, HTTPException
import httpx

app = FastAPI()

@app.get("/api/orders/{order_id}")
async def get_order(order_id: str):
    # 同步调用用户服务
    async with httpx.AsyncClient() as client:
        user_resp = await client.get(f"http://user-service:8001/users/{order_id}")

    if user_resp.status_code != 200:
        raise HTTPException(status_code=404, detail="用户不存在")

    return {"order_id": order_id, "user": user_resp.json()}
```

### 异步通信（消息队列）

```python
import pika
import json

def publish_event(event_type: str, data: dict):
    connection = pika.BlockingConnection(pika.ConnectionParameters('rabbitmq'))
    channel = connection.channel()

    channel.basic_publish(
        exchange='events',
        routing_key=event_type,
        body=json.dumps(data),
        properties=pika.BasicProperties(
            content_type='application/json',
            delivery_mode=2,  # 持久化
        )
    )
    connection.close()
```

## 服务注册与发现

| 方案 | 特点 | 适用场景 |
|------|------|---------|
| Consul | CP 模型，支持健康检查 | 强一致性场景 |
| Eureka | AP 模型，简单易用 | Spring Cloud 生态 |
| Nacos | 支持 AP/CP 切换 | 阿里云生态 |
| K8s DNS | 内置，无需额外组件 | K8s 集群 |

## 微服务设计原则

1. **单一职责**：每个服务只做一件事
2. **数据库独立**：每个服务拥有自己的数据库
3. **API 优先**：先定义 API 契约再实现
4. **无状态服务**：状态存外部（Redis/DB）

## 分布式事务方案

| 方案 | 一致性 | 性能 | 复杂度 |
|------|:---:|:---:|:---:|
| 2PC | 强一致 | 低 | 中 |
| TCC | 强一致 | 中 | 高 |
| Saga | 最终一致 | 高 | 中 |
| 本地消息表 | 最终一致 | 高 | 低 |
