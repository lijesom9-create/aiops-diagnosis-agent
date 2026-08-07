"""
Embedding Cache - 嵌入向量缓存

设计动机：
- 查询 embedding 是检索的热点路径，每次 query 都要重新推理
- 本地 bge-small-zh-v1.5 单次推理约 50-100ms，缓存后接近 0ms
- 文档 embedding 在入库时一次性计算，重复入库可命中缓存

实现策略：
1. 进程内 LRU 缓存（默认 2048 条，命中率高、延迟极低）
2. 可选磁盘持久化（重启后仍可命中，避免重新计算文档 embedding）
3. 装饰器模式包装 EmbeddingModel，不侵入原实现

缓存键：SHA256(text)[:16] + ":" + model_name
- 短哈希避免长文本作为 key 的内存浪费
- 带 model_name 防止不同模型向量混用
"""

import hashlib
import json
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import List, Optional, Dict
from loguru import logger


def _cache_key(text: str, model_name: str) -> str:
    """生成缓存键：短哈希 + 模型名"""
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"{model_name}:{h}"


class _LRUCache:
    """线程安全的 LRU 缓存"""

    def __init__(self, capacity: int = 2048):
        self.capacity = capacity
        self._data: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key: str):
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                self._hits += 1
                return self._data[key]
            self._misses += 1
            return None

    def put(self, key: str, value) -> None:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
            self._data[key] = value
            if len(self._data) > self.capacity:
                self._data.popitem(last=False)

    def stats(self) -> Dict[str, int]:
        with self._lock:
            total = self._hits + self._misses
            return {
                "size": len(self._data),
                "capacity": self.capacity,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total, 3) if total > 0 else 0.0,
            }


class _DiskCache:
    """
    磁盘缓存（JSON Lines 格式，每行一条记录）

    格式：{"key": "model:hash", "vec": [0.1, 0.2, ...]}
    加载时全部读入内存（后写覆盖先写），写入时增量追加，定期 compaction 去重。
    适合 embedding 向量这种"写一次读多次"的场景。

    写入策略（P1 优化）：
    - 平时：append 模式只追加新增 key，避免全量重写（O(新增) 而非 O(全量)）
    - 累积到一定比例的新增条目后，触发一次 compaction（全量重写去重）
    - compaction 使用 .tmp + os.replace 原子替换，避免写入中途崩溃损坏缓存
    """

    # 触发 compaction 的阈值：新增条目数 >= 磁盘初始条目数 * 此比例时全量重写
    _COMPACTION_RATIO = 0.5

    def __init__(self, path: Path):
        self.path = path
        self._data: Dict[str, List[float]] = {}
        self._lock = threading.Lock()
        self._loaded = False
        self._dirty = False
        # 自上次 flush 后新增/更新的 key（待追加写入）
        self._pending_keys: set = set()
        # 加载时磁盘中的条目数（用于判断是否需要 compaction）
        self._initial_disk_size = 0

    def _load(self):
        if self._loaded:
            return
        self._loaded = True
        if not self.path.exists():
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        self._data[rec["key"]] = rec["vec"]
                    except (json.JSONDecodeError, KeyError):
                        continue
            self._initial_disk_size = len(self._data)
            logger.info(f"Embedding 磁盘缓存加载: {len(self._data)} 条")
        except Exception as e:
            logger.warning(f"Embedding 磁盘缓存加载失败: {e}")

    def get(self, key: str):
        self._load()
        return self._data.get(key)

    def put(self, key: str, value: List[float]) -> None:
        # 必须先加载磁盘数据，否则 _initial_disk_size 为 0 会误触发 compaction，
        # 且无法检测已存在的 key（导致重复写入）
        self._load()
        with self._lock:
            # 相同 key 且 value 相同：跳过（避免无谓的 dirty 标记）
            old = self._data.get(key)
            if old is not None and len(old) == len(value) and old == value:
                return
            self._data[key] = value
            self._pending_keys.add(key)
            self._dirty = True

    def flush(self) -> None:
        """将新增记录写入磁盘（增量追加 + 定期 compaction）"""
        if not self._dirty:
            return
        with self._lock:
            if not self._pending_keys:
                self._dirty = False
                return

            self.path.parent.mkdir(parents=True, exist_ok=True)

            # 判断是否触发 compaction：
            # - 磁盘为空（首次写入）
            # - 新增条目数 >= 磁盘初始条目数 * _COMPACTION_RATIO（重复比例过高）
            need_compact = (
                self._initial_disk_size == 0
                or len(self._pending_keys) >= self._initial_disk_size * self._COMPACTION_RATIO
            )

            if need_compact:
                # 全量重写去重（原子替换）
                tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
                with open(tmp_path, "w", encoding="utf-8") as f:
                    for k, v in self._data.items():
                        f.write(json.dumps({"key": k, "vec": v}, ensure_ascii=False) + "\n")
                os.replace(str(tmp_path), str(self.path))
                self._initial_disk_size = len(self._data)
                logger.debug(
                    f"Embedding 磁盘缓存 compaction: 全量重写 {len(self._data)} 条 "
                    f"(含新增 {len(self._pending_keys)})"
                )
            else:
                # 增量追加（仅写新增/更新的 key）
                # 注意：相同 key 的旧记录仍留在文件中，下次 _load 时后写覆盖先写，
                # 不影响正确性；累积到阈值后由 compaction 清理。
                with open(self.path, "a", encoding="utf-8") as f:
                    for k in self._pending_keys:
                        v = self._data[k]
                        f.write(json.dumps({"key": k, "vec": v}, ensure_ascii=False) + "\n")
                logger.debug(
                    f"Embedding 磁盘缓存增量追加: {len(self._pending_keys)} 条 "
                    f"(磁盘总量约 {self._initial_disk_size + len(self._pending_keys)})"
                )

            self._pending_keys.clear()
            self._dirty = False

    def size(self) -> int:
        self._load()
        return len(self._data)


class CachedEmbeddingModel:
    """
    带缓存的 EmbeddingModel 包装器（装饰器模式）

    用法：
        base = SentenceTransformerEmbedding("BAAI/bge-small-zh-v1.5")
        cached = CachedEmbeddingModel(base, cache_dir="./data/embedding_cache")

    策略：
    - embed(text): 先查内存 LRU，再查磁盘，最后才调用 base.embed
    - embed_batch(texts): 批量查询缓存，未命中的批量计算
    """

    def __init__(
        self,
        base_model,
        cache_dir: Optional[str] = None,
        lru_capacity: int = 2048,
        enable_disk: bool = True,
    ):
        self.base = base_model
        self.model_name = getattr(base_model, "model_name", "unknown")
        self._lru = _LRUCache(capacity=lru_capacity)
        self._disk: Optional[_DiskCache] = None

        if enable_disk and cache_dir:
            cache_path = Path(cache_dir) / f"{self.model_name.replace('/', '_')}.jsonl"
            self._disk = _DiskCache(cache_path)

        logger.info(
            f"CachedEmbeddingModel 初始化: model={self.model_name}, "
            f"lru_capacity={lru_capacity}, disk={'on' if self._disk else 'off'}"
        )

    def __getattr__(self, name: str):
        """
        透传未识别的属性/方法到底层模型
        （如 BGEM3Embedding 的 embed_sparse / embed_dense_sparse 等方法不缓存，直接透传）
        """
        return getattr(self.base, name)

    def embed(self, text: str) -> List[float]:
        """单条文本嵌入（带缓存）"""
        key = _cache_key(text, self.model_name)

        # 1. LRU 内存缓存
        cached = self._lru.get(key)
        if cached is not None:
            return cached

        # 2. 磁盘缓存
        if self._disk is not None:
            cached = self._disk.get(key)
            if cached is not None:
                self._lru.put(key, cached)
                return cached

        # 3. 计算并缓存
        vec = self.base.embed(text)
        self._lru.put(key, vec)
        if self._disk is not None:
            self._disk.put(key, vec)

        return vec

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """批量嵌入：先批量查缓存，未命中的批量计算"""
        if not texts:
            return []

        keys = [_cache_key(t, self.model_name) for t in texts]
        results: List[Optional[List[float]]] = [None] * len(texts)

        # 1. 查 LRU
        miss_indices = []
        for i, key in enumerate(keys):
            cached = self._lru.get(key)
            if cached is not None:
                results[i] = cached
            else:
                miss_indices.append(i)

        # 2. 查磁盘（仅 LRU 未命中的）
        if self._disk is not None and miss_indices:
            still_miss = []
            for i in miss_indices:
                cached = self._disk.get(keys[i])
                if cached is not None:
                    results[i] = cached
                    self._lru.put(keys[i], cached)
                else:
                    still_miss.append(i)
            miss_indices = still_miss

        # 3. 批量计算未命中的
        if miss_indices:
            miss_texts = [texts[i] for i in miss_indices]
            new_vecs = self.base.embed_batch(miss_texts)
            for idx, vec in zip(miss_indices, new_vecs):
                results[idx] = vec
                key = keys[idx]
                self._lru.put(key, vec)
                if self._disk is not None:
                    self._disk.put(key, vec)

        return results

    @property
    def dimension(self) -> int:
        return self.base.dimension

    def cache_stats(self) -> Dict:
        """返回缓存统计信息"""
        stats = {"lru": self._lru.stats()}
        if self._disk is not None:
            stats["disk"] = {"size": self._disk.size()}
        return stats

    def flush(self) -> None:
        """将磁盘缓存刷盘"""
        if self._disk is not None:
            self._disk.flush()
