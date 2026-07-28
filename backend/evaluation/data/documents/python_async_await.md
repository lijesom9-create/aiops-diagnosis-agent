# Python 异步编程

## 协程与 async/await

Python 使用 `async def` 定义协程函数，调用协程函数不会立即执行，而是返回一个协程对象。使用 `await` 可以挂起当前协程，等待另一个协程或异步操作完成。

```python
import asyncio

async def say_hello():
    await asyncio.sleep(1)
    print('hello')

asyncio.run(say_hello())
```

## 事件循环

事件循环是异步编程的核心，负责调度和执行协程。`asyncio.run` 会创建一个新的事件循环并运行入口协程，直到它完成。

## 并发执行

使用 `asyncio.gather` 可以同时运行多个协程，实现并发：

```python
async def main():
    await asyncio.gather(say_hello(), say_hello())

asyncio.run(main())
```

## 与同步代码混用

在异步代码中调用阻塞操作会卡住事件循环。可以使用 `asyncio.to_thread` 将同步任务放到线程池中执行，从而避免阻塞。
