# SQLAlchemy ORM 基础

## 模型定义

SQLAlchemy 是 Python 中最流行的 ORM 工具之一。通过定义模型类，可以把数据库表映射为 Python 对象。

```python
from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    name = Column(String(50))
    email = Column(String(120), unique=True)
```

## Session 与会话

所有数据库操作都通过 Session 进行。Session 负责跟踪对象状态、生成 SQL 并管理事务。

```python
from sqlalchemy.orm import Session

with Session(engine) as session:
    user = User(name='Alice', email='alice@example.com')
    session.add(user)
    session.commit()
```

## CRUD 操作

ORM 提供了直观的增删改查接口：

- **创建**：`session.add(obj)` 后 `session.commit()`。
- **查询**：`session.query(User).filter_by(name='Alice').first()`。
- **更新**：修改对象属性后提交。
- **删除**：`session.delete(obj)` 后提交。

合理使用 ORM 可以减少手写 SQL，同时保持代码的可读性和可维护性。
