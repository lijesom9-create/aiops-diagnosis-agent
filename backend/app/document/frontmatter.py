"""
Markdown YAML frontmatter 解析（运维文档业务元数据）

运维手册/事故/SOP/复盘文档通常在文件头部用 YAML frontmatter 声明业务字段：
    ---
    doc_type: manual
    service: kafka-gateway
    severity: P2
    ---

这些字段注入到每个 chunk 的 metadata，支撑 hybrid_search_parent_child 的
metadata_filter 精准过滤（如 service='kafka-gateway' AND doc_type='manual'）。

API 上传路径与 scripts/seed_ops_kb.py 共用本模块，保证行为一致。
"""

import re
from typing import Dict, Tuple, List

# 业务字段（从 frontmatter 提取，注入到 chunk metadata）
# 与 qdrant_store._ensure_payload_indexes 的索引字段对齐
BUSINESS_FIELDS: List[str] = [
    "doc_type", "service", "severity", "incident_id",
    "root_cause", "resolution", "occurred_at", "tags", "source",
    # 知识时效治理：valid_until 过期后检索软降权（×0.5），
    # effective_date 用于历史结论冲突时"取更新者"的判据
    "valid_until", "effective_date",
]

_FRONTMATTER_RE = re.compile(r'^---\n(.*?)\n---\n?(.*)$', re.DOTALL)


def parse_frontmatter(content: str) -> Tuple[Dict, str]:
    """解析 Markdown 文件的 YAML frontmatter

    返回 (frontmatter_dict, body_text)
    frontmatter 用 --- 包裹，位于文件开头；无 frontmatter 时返回 ({}, content)。
    """
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return {}, content
    yaml_text = match.group(1)
    body = match.group(2)
    try:
        import yaml
        fm = yaml.safe_load(yaml_text) or {}
        return (fm if isinstance(fm, dict) else {}), body
    except ImportError:
        # pyyaml 不可用时降级为简单键值解析
        fm: Dict = {}
        for line in yaml_text.split('\n'):
            if ':' in line:
                k, _, v = line.partition(':')
                k = k.strip()
                v = v.strip().strip('"\'')
                if k and v:
                    fm[k] = v
        return fm, body
    except Exception:
        return {}, content


def extract_business_metadata(frontmatter: Dict) -> Dict:
    """从 frontmatter 提取业务字段（过滤掉 title 等非业务字段）

    返回的 dict 直接作为 DocumentUploader.upload(extra_metadata=...) 注入，
    使 chunk metadata 携带 doc_type/service/severity 等可过滤字段。
    """
    meta: Dict = {}
    for field in BUSINESS_FIELDS:
        val = frontmatter.get(field)
        if val is not None:
            meta[field] = val
    return meta


# ========== 无 frontmatter 时的自动分类兜底 ==========

# doc_type 推断规则：文件名/标题关键词 → doc_type（按优先级排列）
_DOC_TYPE_RULES: List[Tuple[str, str]] = [
    ("postmortem", "postmortem"),
    ("复盘", "postmortem"),
    ("incident", "incident"),
    ("inc-", "incident"),   # 事故编号前缀（INC-2026-001）
    ("事故", "incident"),
    ("sop", "sop"),
    ("预案", "sop"),
    ("runbook", "sop"),
    ("manual", "manual"),
    ("手册", "manual"),
    ("运维", "manual"),
    ("架构", "manual"),
    ("api", "manual"),
    ("部署", "manual"),
]

# service 推断：匹配常见微服务命名模式（xx-service / xx-db 等）
_SERVICE_RE = re.compile(
    r'\b([a-z][a-z0-9-]{2,30}-(?:service|api|gateway|db|mysql|redis|worker|job))\b',
    re.IGNORECASE,
)


def infer_business_metadata(filename: str, title: str = "",
                            content_head: str = "") -> Dict:
    """无 frontmatter 时从文件名/标题/内容开头推断业务元数据（自动分类兜底）

    真实文档通常不带 frontmatter，而 Agent 的 service+doc_type 过滤依赖这些
    字段——缺字段会导致检索命中不到。推断值标记 source=auto_inferred，
    与人工 frontmatter 标注（source 缺省/auto）区分，便于后续复核。

    推断纪律：宁缺勿错——doc_type 有默认值 manual；service 无高置信命中则不填，
    避免错误标注把文档过滤进错误的服务视图。

    Args:
        filename: 文件名（含扩展名）
        title: 文档标题（可选）
        content_head: 内容开头文本（前 ~2000 字符，可选）

    Returns:
        推断出的业务字段 dict（可能为空），已过滤 BUSINESS_FIELDS 之外的字段
    """
    haystack = " ".join(filter(None, [filename, title])).lower()
    content_lower = (content_head or "").lower()

    inferred: Dict = {}

    # doc_type：先看文件名/标题（强信号），再看内容开头（弱信号）
    doc_type = None
    for keyword, dtype in _DOC_TYPE_RULES:
        if keyword in haystack:
            doc_type = dtype
            break
    if not doc_type:
        for keyword, dtype in _DOC_TYPE_RULES[:6]:  # 内容只信事故/复盘类强信号
            if keyword in content_lower:
                doc_type = dtype
                break
    if doc_type:
        inferred["doc_type"] = doc_type

    # service：文件名/标题优先，内容开头兜底
    m = _SERVICE_RE.search(haystack) or _SERVICE_RE.search(content_lower)
    if m:
        inferred["service"] = m.group(1).lower()

    if inferred:
        inferred["source"] = "auto_inferred"
    return inferred
