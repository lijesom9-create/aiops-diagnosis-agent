# Celery 异步任务

## 核心组件

Celery 是一个分布式任务队列，主要由以下组件构成：

- **Task**：任务函数，使用 `@app.task` 装饰器注册。
- **Broker**：消息中间件，负责接收和转发任务，例如 Redis 或 RabbitMQ。
- **Worker**：执行任务的进程，从 Broker 获取任务并运行。
- **Backend**：结果后端，用于存储任务执行结果。

## 定义与调用任务

使用装饰器可以快速定义一个异步任务：

```python
from celery import Celery

app = Celery('tasks', broker='redis://localhost:6379/0')

@app.task
def add(x, y):
    return x + y
```

调用时使用 `delay` 方法即可将任务发送到队列：

```python
result = add.delay(4, 5)
print(result.get(timeout=10))
```

## 定时任务

Celery Beat 是 Celery 的调度器，可以按固定周期执行任务。配合 `celerybeat-schedule` 文件或数据库存储定时计划。

```python
app.conf.beat_schedule = {
    'add-every-30-seconds': {
        'task': 'tasks.add',
        'schedule': 30.0,
        'args': (16, 16)
    },
}
```
