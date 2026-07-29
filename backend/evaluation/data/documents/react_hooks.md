# React Hooks 详解

## 核心 Hooks

| Hook | 作用 | 使用场景 | 返回值 |
|------|------|---------|--------|
| useState | 状态管理 | 组件内部状态 | [value, setter] |
| useEffect | 副作用 | 数据获取、订阅 | void |
| useRef | 引用对象 | DOM 操作、缓存 | ref.current |
| useMemo | 计算缓存 | 昂贵计算 | 计算结果 |
| useCallback | 函数缓存 | 传递给子组件 | 回调函数 |
| useContext | 上下文消费 | 全局状态 | context value |

## useState 基础

```jsx
import { useState } from 'react';

function Counter() {
  const [count, setCount] = useState(0);

  return (
    <div>
      <p>点击次数: {count}</p>
      <button onClick={() => setCount(count + 1)}>+1</button>
      <button onClick={() => setCount(0)}>重置</button>
    </div>
  );
}
```

## useEffect 生命周期

```jsx
import { useEffect, useState } from 'react';

function UserProfile({ userId }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;

    async function fetchUser() {
      setLoading(true);
      const response = await fetch(`/api/users/${userId}`);
      const data = await response.json();

      if (!cancelled) {
        setUser(data);
        setLoading(false);
      }
    }

    fetchUser();

    // 清理函数：防止内存泄漏
    return () => {
      cancelled = true;
    };
  }, [userId]); // 依赖数组：userId 变化时重新执行

  if (loading) return <div>加载中...</div>;
  return <div>{user?.name}</div>;
}
```

## useMemo 和 useCallback

```jsx
function ProductList({ products, category }) {
  // useMemo: 缓存计算结果
  const filtered = useMemo(() => {
    return products.filter(p => p.category === category);
  }, [products, category]);

  // useCallback: 缓存回调函数
  const handleDelete = useCallback((id) => {
    setProducts(prev => prev.filter(p => p.id !== id));
  }, []);

  return (
    <ul>
      {filtered.map(p => (
        <li key={p.id} onClick={() => handleDelete(p.id)}>
          {p.name}
        </li>
      ))}
    </ul>
  );
}
```

## Hooks 使用规则

| 规则 | 说明 | 错误示例 |
|------|------|---------|
| 只在顶层调用 | 不能在循环/条件中 | if (x) useState() |
| 只在函数组件 | 不能在普通函数 | class 中用 |
| 依赖完整 | useEffect 依赖 | 遗漏依赖 |

## 自定义 Hook

```jsx
function useFetch(url) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    fetch(url)
      .then(res => res.json())
      .then(setData)
      .catch(setError)
      .finally(() => setLoading(false));
  }, [url]);

  return { data, loading, error };
}

// 使用
function App() {
  const { data, loading, error } = useFetch('/api/users');
  if (loading) return <div>加载中</div>;
  if (error) return <div>错误: {error.message}</div>;
  return <div>{JSON.stringify(data)}</div>;
}
```
