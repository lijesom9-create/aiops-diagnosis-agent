# SQL 表连接

## 什么是表连接

表连接用于根据某些条件把两个或多个表的数据组合起来。连接条件通常使用 `ON` 子句指定，用于匹配不同表中的行。

## INNER JOIN

`INNER JOIN` 只返回两个表中满足连接条件的行。如果某一行在另一个表中没有匹配项，则不会出现在结果集中。

```sql
SELECT users.name, orders.total
FROM users
INNER JOIN orders ON users.id = orders.user_id;
```

## LEFT JOIN

`LEFT JOIN` 返回左表中的所有行，以及右表中满足条件的行。如果右表中没有匹配项，则对应列会填充 `NULL`。

```sql
SELECT users.name, orders.total
FROM users
LEFT JOIN orders ON users.id = orders.user_id;
```

## 连接条件

连接条件一般基于主键和外键关系。除了等值连接，还可以使用 `>=`、`<=`、`BETWEEN` 等条件进行非等值连接。
