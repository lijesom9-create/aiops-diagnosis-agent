# 数据库索引原理与优化

## 什么是索引

索引是数据库中用于加速数据检索的数据结构。类似于书籍的目录，通过索引可以快速定位数据，避免全表扫描。

## 索引类型对比

| 索引类型 | 数据结构 | 适用场景 | 查询复杂度 |
|----------|---------|----------|-----------|
| B+Tree | 平衡多路树 | 范围查询、等值查询 | O(log n) |
| Hash | 哈希表 | 等值查询 | O(1) |
| Full-text | 倒排索引 | 文本搜索 | O(1) |
| Bitmap | 位图 | 低基数列 | O(1) |
| GiST | R树 | 地理空间 | O(log n) |

## B+Tree 原理

B+Tree 是 MySQL InnoDB 的默认索引结构。所有数据存储在叶子节点，非叶子节点只存索引。

$$查找复杂度 = O(\log_m n)$$

其中 m 是树的度（每个节点的最大子节点数），n 是记录数。

## 创建索引

```sql
-- 单列索引
CREATE INDEX idx_user_name ON users(name);

-- 复合索引（最左前缀原则）
CREATE INDEX idx_user_status ON users(status, created_at, name);

-- 唯一索引
CREATE UNIQUE INDEX idx_user_email ON users(email);

-- 查看执行计划
EXPLAIN SELECT * FROM users WHERE status = 'active' AND created_at > '2024-01-01';
```

## 索引失效场景

| 场景 | 示例 | 原因 |
|------|------|------|
| 函数操作 | WHERE YEAR(date) = 2024 | 破坏索引有序性 |
| 类型转换 | WHERE id = '123' | 隐式类型转换 |
| LIKE 前缀通配 | WHERE name LIKE '%abc' | 无法走 B+Tree |
| OR 条件 | WHERE a=1 OR b=2 | 需要两个索引都命中 |
| 负向条件 | WHERE status != 'active' | 无法利用索引 |

## 性能对比

| 操作 | 无索引 | 有索引 | 提升 |
|------|--------|--------|------|
| 等值查询 | O(n) | O(log n) | 1000 倍 |
| 范围查询 | O(n) | O(log n + k) | 100 倍 |
| 排序 | O(n log n) | O(log n) | 显著 |
| 聚合 | O(n) | O(n) | 无提升 |

## 复合索引最左前缀原则

```sql
-- 索引: (status, created_at, name)
-- ✅ 能命中索引
SELECT * FROM users WHERE status = 'active';
SELECT * FROM users WHERE status = 'active' AND created_at > '2024-01-01';
SELECT * FROM users WHERE status = 'active' AND created_at > '2024-01-01' AND name = 'Alice';

-- ❌ 不能命中索引
SELECT * FROM users WHERE created_at > '2024-01-01';
SELECT * FROM users WHERE name = 'Alice';
```
