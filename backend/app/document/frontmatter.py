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
