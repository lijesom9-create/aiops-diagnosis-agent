# Python 装饰器详解

## 什么是装饰器

装饰器是 Python 中一种特殊的高阶函数。它可以在不修改原函数源代码的前提下，为函数添加额外的功能。装饰器本质上是一个接收函数并返回函数的可调用对象。

## 基本用法

使用 `@` 语法糖可以让代码更简洁。例如，下面的 `timer` 装饰器可以统计函数执行时间：

```python
import time

def timer(func):
    def wrapper(*args, **kwargs):
        start = time.time()
        result = func(*args, **kwargs)
        print(f"耗时: {time.time() - start:.4f}s")
        return result
    return wrapper

@timer
def slow_function():
    time.sleep(1)
```

## 常见应用场景

装饰器在实际开发中非常常见，主要应用于以下场景：

- **日志记录**：在函数调用前后打印参数和返回值。
- **权限校验**：在 Web 框架中检查用户是否登录或拥有权限。
- **缓存**：使用 LRU 缓存避免重复计算。
- **性能计时**：统计函数执行耗时，用于性能分析。

合理使用装饰器可以让代码更加简洁、可维护。
