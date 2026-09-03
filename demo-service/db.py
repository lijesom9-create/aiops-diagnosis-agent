"""
数据库层：SQLite + SQLAlchemy QueuePool（真实连接池语义）

池配置是有意为之的"真实约束"：
- pool_size=5, max_overflow=0, pool_timeout=3 —— 连接耗尽时第 6 个请求
  会真实阻塞 3 秒后抛 TimeoutError，产生与生产一致的错误与日志：
  "QueuePool limit of size 5 overflow 0 reached, connection timed out"
"""

import os

from loguru import logger
from prometheus_client import Gauge
from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import QueuePool

DB_PATH = os.environ.get("DEMO_DB_PATH", "/data/demo.db")
POOL_LIMIT = int(os.environ.get("DEMO_POOL_SIZE", "5"))

POOL_CHECKED_OUT = Gauge("demo_db_pool_checked_out", "当前借出的数据库连接数")

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    poolclass=QueuePool,
    pool_size=POOL_LIMIT,
    max_overflow=0,          # 不允许溢出：池耗尽 = 真实拒绝
    pool_timeout=3,          # 等待 3 秒后超时（与真实 HikariCP 语义一致）
    pool_pre_ping=True,
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _sqlite_pragma(dbapi_conn, _):
    """WAL + busy_timeout：避免 SQLite 写锁放大连接池压力"""
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=2000")
    cur.close()


def _track_checked_out():
    """借出连接数 Gauge（池饱和度可观测）——从 SQLAlchemy 池状态读取"""
    try:
        POOL_CHECKED_OUT.set(engine.pool.checkedout())
    except Exception:
        pass


def init():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS orders ("
            " order_id TEXT PRIMARY KEY, item TEXT, amount REAL,"
            " status TEXT DEFAULT 'created', created_at TEXT DEFAULT (datetime('now')))"
        ))
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS payments ("
            " order_id TEXT PRIMARY KEY, amount REAL, paid_at TEXT DEFAULT (datetime('now')))"
        ))
    logger.info("数据库初始化完成: {path}", path=DB_PATH)


def ping():
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))


def create_order(item: str, amount: float) -> str:
    import uuid

    order_id = f"ord_{uuid.uuid4().hex[:12]}"
    with engine.connect() as conn:
        conn.execute(
            text("INSERT INTO orders (order_id, item, amount) VALUES (:oid, :item, :amount)"),
            {"oid": order_id, "item": item, "amount": amount},
        )
        conn.commit()
    _track_checked_out()
    return order_id


def get_order(order_id: str):
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT order_id, item, amount, status, created_at FROM orders WHERE order_id = :oid"),
            {"oid": order_id},
        ).mappings().first()
    _track_checked_out()
    return dict(row) if row else None


def mark_paid(order_id: str):
    with engine.begin() as conn:
        conn.execute(text("UPDATE orders SET status = 'paid' WHERE order_id = :oid"), {"oid": order_id})
        conn.execute(text("INSERT INTO payments (order_id, amount) SELECT :oid, amount FROM orders WHERE order_id = :oid"), {"oid": order_id})
    _track_checked_out()
