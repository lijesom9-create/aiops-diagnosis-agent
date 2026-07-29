# 算法时间复杂度分析

## 大 O 表示法

大 O 表示法描述算法在最坏情况下的时间复杂度增长趋势。设输入规模为 n，常见复杂度递推关系：

$$T(n) = O(f(n))$$

其中 $f(n)$ 是关于 $n$ 的增长函数。

## 复杂度等级对比

| 复杂度 | 名称 | 示例算法 | n=10 | n=100 | n=1000 |
|--------|------|----------|------|-------|--------|
| O(1) | 常数 | 哈希表查找 | 1 | 1 | 1 |
| O(log n) | 对数 | 二分查找 | 3 | 7 | 10 |
| O(n) | 线性 | 遍历数组 | 10 | 100 | 1000 |
| O(n log n) | 线性对数 | 快速排序 | 33 | 664 | 9966 |
| O(n²) | 平方 | 冒泡排序 | 100 | 10000 | 10⁶ |
| O(n³) | 立方 | 矩阵乘法 | 1000 | 10⁶ | 10⁹ |
| O(2ⁿ) | 指数 | 递归斐波那契 | 1024 | 10³⁰ | ∞ |

## 快速排序分析

快速排序的平均时间复杂度为：

$$T(n) = 2T(n/2) + O(n) = O(n \log n)$$

最坏情况（已排序数组）：

$$T(n) = T(n-1) + O(n) = O(n^2)$$

```python
def quicksort(arr):
    if len(arr) <= 1:
        return arr

    pivot = arr[len(arr) // 2]
    left = [x for x in arr if x < pivot]
    middle = [x for x in arr if x == pivot]
    right = [x for x in arr if x > pivot]

    return quicksort(left) + middle + quicksort(right)
```

## 空间复杂度

| 算法 | 时间复杂度 | 空间复杂度 | 是否稳定 |
|------|-----------|-----------|:---:|
| 快速排序 | O(n log n) | O(log n) | 否 |
| 归并排序 | O(n log n) | O(n) | 是 |
| 堆排序 | O(n log n) | O(1) | 否 |
| 冒泡排序 | O(n²) | O(1) | 是 |

## 主定理

对于分治递推 $T(n) = aT(n/b) + f(n)$：

$$T(n) = \Theta(n^{\log_b a})$$

当 $f(n) = O(n^{\log_b a - \epsilon})$ 时适用。
