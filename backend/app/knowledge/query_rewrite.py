"""
查询重写器（QueryRewriter）

对用户原始查询进行优化，提高检索质量。含：
- 技术术语中英文同义词 / 缩写双向扩展表（_build_expansion_map）
- 基础重写（去后缀 / 关键词组合）
- 增强重写（enhanced_rewrite：同义词 + 缩写 + 关键词组合）

与 UnifiedKnowledgeStore 无类级耦合（分词走 store_utils.tokenize）。
"""
import re
from typing import Dict, List, Set

from .store_utils import tokenize


def _build_expansion_map(
    base_synonyms: Dict[str, List[str]],
    abbreviations: Dict[str, List[str]],
) -> Dict[str, List[str]]:
    """构建双向同义词扩展表（包含术语和缩写）"""
    raw: Dict[str, Set[str]] = {}
    sources = [base_synonyms, abbreviations]
    for source in sources:
        for term, synonyms in source.items():
            key = term.lower()
            raw.setdefault(key, set())
            for syn in synonyms:
                syn_key = syn.lower()
                raw[key].add(syn_key)
                # 反向映射：同义词 -> 原词及其他同义词
                raw.setdefault(syn_key, set())
                raw[syn_key].add(key)
                raw[syn_key].update(
                    s.lower() for s in synonyms if s.lower() != syn_key
                )
    # 移除自身并排序，保证输出稳定
    return {
        k: sorted(v - {k})
        for k, v in raw.items()
        if v - {k}
    }


class QueryRewriter:
    """
    查询重写器

    对用户原始查询进行优化，提高检索质量。
    """

    # 常见后缀，可以去掉以获取核心关键词
    SUFFIXES = ["怎么学", "怎么用", "是什么", "怎么理解", "如何", "为什么", "怎么办", "什么意思", "怎么实现"]

    # 停用词
    STOP_WORDS = {"的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都", "一", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有", "看", "好", "自己", "这", "那", "吗", "呢", "吧", "啊", "请", "帮"}

    # 技术术语中英文对照 / 同义词（key 为小写，中文按字面匹配，英文按单词边界匹配）
    BASE_TECH_SYNONYMS: Dict[str, List[str]] = {
        # Python
        "装饰器": ["decorator"],
        "迭代器": ["iterator"],
        "生成器": ["generator"],
        "生成器表达式": ["generator expression"],
        "列表推导式": ["list comprehension"],
        "高阶函数": ["higher-order function"],
        "闭包": ["closure"],
        "递归": ["recursion"],
        "动态规划": ["dynamic programming", "dp"],
        "记忆化": ["memoization", "lru_cache"],
        # Web / FastAPI
        "fastapi": ["fast api"],
        "依赖注入": ["dependency injection", "depends"],
        "路由": ["routing", "route"],
        "restful": ["rest", "api design"],
        "get": ["http get"],
        "post": ["http post"],
        "状态码": ["status code"],
        # 数据库 / SQLAlchemy
        "sqlalchemy": ["sqlalchemy", "orm"],
        "orm": ["对象关系映射", "模型映射"],
        "session": ["会话", "事务"],
        "crud": ["增删改查", "create", "read", "update", "delete"],
        "表连接": ["join", "sql join"],
        # 缓存 / Redis
        "redis": ["key-value", "缓存数据库"],
        "缓存": ["cache"],
        "持久化": ["persistence"],
        # 消息队列 / Celery
        "celery": ["任务队列", "distributed task queue"],
        "broker": ["消息中间件", "消息代理"],
        "worker": ["工作进程", "任务执行进程"],
        "backend": ["结果后端", "result backend"],
        "定时任务": ["periodic task", "scheduled task"],
        # 异步
        "async": ["异步", "协程"],
        "await": ["等待", "挂起"],
        "asyncio": ["事件循环", "event loop"],
        "gather": ["并发执行", "同时运行"],
        "to_thread": ["线程池", "thread pool"],
        # 版本控制 / Docker
        "git": ["version control", "版本控制"],
        "分支": ["branch"],
        "提交": ["commit"],
        "docker": ["container", "容器化"],
        "镜像": ["image"],
        "容器": ["container"],
        # 通用
        "sql": ["database", "数据库"],
        "api": ["接口"],
        "url": ["统一资源定位符"],
        "lru": ["lru_cache"],
    }

    # 缩写 / 首字母缩写词补全
    ABBREVIATIONS: Dict[str, List[str]] = {
        "orm": ["对象关系映射", "sqlalchemy"],
        "crud": ["增删改查", "创建", "读取", "更新", "删除"],
        "api": ["接口", "application programming interface"],
        "url": ["统一资源定位符"],
        "jwt": ["token", "认证"],
        "rdb": ["redis rdb", "内存快照"],
        "aof": ["append only file", "写操作日志"],
        "sql": ["structured query language", "数据库"],
    }

    _EXPANSION_MAP: Dict[str, List[str]] = _build_expansion_map(
        BASE_TECH_SYNONYMS, ABBREVIATIONS
    )

    @staticmethod
    def _is_english_term(term: str) -> bool:
        """判断是否主要由英文/数字/下划线组成的术语"""
        return bool(re.fullmatch(r"[a-z0-9_.]+|[a-z]+\s+[a-z]+", term))

    @staticmethod
    def _term_in_query(query_lower: str, term: str) -> bool:
        """判断术语是否出现在查询中（英文使用单词边界，中文使用子串）"""
        if not term:
            return False
        if QueryRewriter._is_english_term(term):
            return re.search(r"\b" + re.escape(term) + r"\b", query_lower) is not None
        return term in query_lower

    @staticmethod
    def expand_terms(query: str) -> List[str]:
        """
        扩展查询中的技术术语，返回应补充的同义词列表

        Args:
            query: 原始查询

        Returns:
            List[str]: 需要补充的术语列表（已过滤掉查询中已有的词）
        """
        if not query or not query.strip():
            return []

        query_lower = query.lower().strip()
        expansions: Set[str] = set()
        for term, synonyms in QueryRewriter._EXPANSION_MAP.items():
            if QueryRewriter._term_in_query(query_lower, term):
                for syn in synonyms:
                    if not QueryRewriter._term_in_query(query_lower, syn):
                        expansions.add(syn)
        return sorted(expansions)

    @staticmethod
    def rewrite(query: str) -> List[str]:
        """
        重写查询，返回多个查询变体（基础模式）

        Args:
            query: 原始查询

        Returns:
            List[str]: 查询变体列表（包含原始查询）
        """
        if not query or not query.strip():
            return [query]

        queries = [query.strip()]

        # 1. 去掉常见后缀，提取核心关键词
        for suffix in QueryRewriter.SUFFIXES:
            if query.endswith(suffix) and len(query) > len(suffix):
                core = query[:-len(suffix)].strip()
                if core and core not in queries:
                    queries.append(core)

        # 2. 分词后提取关键词组合
        terms = tokenize(query)
        if len(terms) >= 2:
            # 去掉停用词后的关键词
            keywords = [t for t in terms if t not in QueryRewriter.STOP_WORDS]
            if keywords and keywords != terms:
                keyword_query = " ".join(keywords)
                if keyword_query not in queries:
                    queries.append(keyword_query)

        # 3. 去重
        return list(dict.fromkeys(queries))  # 保持顺序去重

    @staticmethod
    def enhanced_rewrite(query: str) -> List[str]:
        """
        增强重写查询，返回多个查询变体

        在基础模式上增加：
        - 技术术语中英文同义词扩展（双向映射）
        - 缩写补全
        - 核心关键词组合

        Args:
            query: 原始查询

        Returns:
            List[str]: 查询变体列表（包含原始查询）
        """
        if not query or not query.strip():
            return [query]

        queries = QueryRewriter.rewrite(query)
        base_query = query.strip()
        base_query_lower = base_query.lower()

        # 1. 同义词 / 缩写扩展查询
        expanded_terms = QueryRewriter.expand_terms(base_query)
        if expanded_terms:
            expanded_query = base_query_lower + " " + " ".join(expanded_terms)
            if expanded_query not in queries:
                queries.append(expanded_query)

        # 2. 生成仅含核心关键词的英文/中文混合查询
        terms = tokenize(base_query_lower)
        keywords = [t for t in terms if t not in QueryRewriter.STOP_WORDS]
        keyword_queries: List[str] = []
        for term in keywords:
            term_lower = term.lower()
            keyword_queries.append(term_lower)
            # 追加该词的同义词（如果有）
            for syn in QueryRewriter._EXPANSION_MAP.get(term_lower, []):
                keyword_queries.append(syn)

        if keyword_queries:
            keyword_query = " ".join(list(dict.fromkeys(keyword_queries)))
            if keyword_query not in queries:
                queries.append(keyword_query)

        # 3. 去重
        return list(dict.fromkeys(queries))