# 设计模式实战

## 创建型模式

| 模式 | 意图 | 使用场景 | 复杂度 |
|------|------|---------|:---:|
| 单例 | 保证唯一实例 | 配置管理、日志器 | 低 |
| 工厂方法 | 定义创建接口 | 多类型对象创建 | 中 |
| 抽象工厂 | 创建相关对象族 | 跨平台 UI 组件 | 高 |
| 建造者 | 分步构建复杂对象 | 配置对象、SQL构建器 | 中 |
| 原型 | 克隆已有对象 | 原型 costly 创建 | 低 |

## 单例模式实现

```python
from functools import wraps

# 线程安全的单例装饰器
def singleton(cls):
    instances = {}
    lock = threading.Lock()

    @wraps(cls)
    def get_instance(*args, **kwargs):
        if cls not in instances:
            with lock:
                if cls not in instances:
                    instances[cls] = cls(*args, **kwargs)
        return instances[cls]

    return get_instance

@singleton
class Database:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.pool = self._create_pool()

    def _create_pool(self):
        """创建连接池"""
        pass
```

## 工厂模式

```python
from enum import Enum

class FileType(Enum):
    PDF = "pdf"
    DOCX = "docx"
    TXT = "txt"

class Parser:
    def parse(self, content: bytes) -> str:
        raise NotImplementedError

class PDFParser(Parser):
    def parse(self, content: bytes) -> str:
        return "PDF content"

class DocxParser(Parser):
    def parse(self, content: bytes) -> str:
        return "DOCX content"

class ParserFactory:
    _parsers = {
        FileType.PDF: PDFParser,
        FileType.DOCX: DocxParser,
    }

    @classmethod
    def create(cls, file_type: FileType) -> Parser:
        parser_class = cls._parsers.get(file_type)
        if not parser_class:
            raise ValueError(f"不支持的类型: {file_type}")
        return parser_class()
```

## 观察者模式

```python
class EventEmitter:
    def __init__(self):
        self._listeners = {}

    def on(self, event: str, callback):
        self._listeners.setdefault(event, []).append(callback)

    def emit(self, event: str, *args, **kwargs):
        for callback in self._listeners.get(event, []):
            callback(*args, **kwargs)

# 使用
emitter = EventEmitter()
emitter.on('user_login', lambda user: print(f"用户登录: {user}"))
emitter.emit('user_login', 'Alice')
```

## 策略模式

| 模式类型 | 典型应用 | Python 实现 |
|---------|---------|------------|
| 策略 | 算法切换 | 函数参数 |
| 观察者 | 事件订阅 | EventEmitter |
| 装饰器 | 功能增强 | @decorator |
| 适配器 | 接口转换 | 类适配器 |
| 模板方法 | 流程框架 | 抽象基类 |
