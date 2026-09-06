"""方向3 经验回流（incident_ingest）单元测试。

覆盖：质量门判定、充分度提取、知识条目构建（低置信度注记/幂等 id/valid_until）、
入库副作用（写库+失效缓存+指标）与跳过路径。
"""

import pytest

from app.knowledge.incident_ingest import (
    _build_content,
    _build_knowledge_items,
    extract_sufficiency,
    ingest_incident_into_knowledge,
    should_ingest,
)

# ========== 质量门纯函数 ==========

@pytest.mark.parametrize("conf,suff,expected", [
    ("high", "high", (True, False)),
    ("high", "medium", (True, True)),
    ("medium", "medium", (True, True)),
    ("medium", None, (True, True)),          # 充分度缺失时以置信度为准
    ("low", "high", (False, False)),
    ("unknown", "high", (False, False)),
    ("high", "low", (False, False)),
])
def test_should_ingest_gate(conf, suff, expected):
    assert should_ingest(conf, suff) == expected


def test_should_ingest_custom_thresholds():
    # min_sufficiency 提到 high → medium 充分度不过门
    assert should_ingest("high", "medium", min_sufficiency="high") == (False, False)
    # min_confidence 放宽到 low → medium 置信度可入库
    assert should_ingest("medium", "high", min_confidence="low")[0] is True


def test_extract_sufficiency_skips_summary_and_takes_latest():
    incident = {
        "diagnosis_history": [
            {"trigger": "auto", "sufficiency_level": "low"},
            {"trigger": "summary", "sufficiency_level": "high"},  # 摘要不计
            {"trigger": "auto", "sufficiency_level": "medium"},   # 最近非摘要
        ]
    }
    assert extract_sufficiency(incident) == "medium"


def test_extract_sufficiency_missing():
    assert extract_sufficiency({}) is None
    assert extract_sufficiency({"diagnosis_history": []}) is None


# ========== 知识条目构建 ==========

def _incident(**over):
    base = {
        "incident_id": "INC-1",
        "service": "payment-sim",
        "alertnames": ["PaymentHighLatency"],
        "impact_priority": "P1",
        "max_severity": "critical",
        "first_seen_at": "2026-09-04T10:00:00",
        "resolved_at": "2026-09-04T11:00:00",
        "planned_change": True,
        "diagnosis_history": [{"trigger": "auto", "sufficiency_level": "high"}],
    }
    base.update(over)
    return base


def test_build_item_id_is_stable_and_idempotent():
    report = {"root_cause": "连接池耗尽", "confidence_level": "high"}
    i1 = _build_knowledge_items(_incident(), report, [], "摘要", 90)
    i2 = _build_knowledge_items(_incident(), report, [], "摘要", 90)
    # 幂等 upsert，不重复：父条目 id 稳定，子条目由父 id 派生
    assert [it.id for it in i1] == [it.id for it in i2] == [
        "incident_INC-1", "incident_INC-1_c0"]


def test_build_item_metadata_and_valid_until():
    parent, child = _build_knowledge_items(
        _incident(), {"confidence_level": "medium"}, [], "摘要", 90)
    for item in (parent, child):
        assert item.metadata["doc_type"] == "incident"
        assert item.metadata["incident_id"] == "INC-1"
        assert item.metadata["sufficiency_level"] == "high"
        assert item.metadata["confidence_level"] == "medium"
        assert item.metadata["_needs_review"] is True          # medium → 标记复核
        assert item.metadata["auto_ingested"] is True          # 飞轮标记
        assert "valid_until" in item.metadata                   # 过期软降权字段存在
    assert parent.metadata["chunk_type"] == "parent"           # 父子分离入库规范
    assert child.metadata["chunk_type"] == "child"
    assert child.metadata["parent_id"] == parent.id


def test_build_content_annotates_low_confidence():
    incident = _incident(diagnosis_history=[
        {"trigger": "auto", "sufficiency_level": "medium"},
    ])
    content = _build_content(
        incident, {"confidence_level": "medium"},
        [{"item": "扩容连接池", "owner": "支付组", "deadline": "9/6"}],
        "摘要", needs_review=True)
    assert "低置信度，待人工复核" in content
    assert "服务 payment-sim" in content
    assert "扩容连接池（负责人: 支付组，期限: 9/6）" in content  # 行动项格式化
    assert "计划内变更" in content


def test_build_content_high_confidence_no_review_note():
    content = _build_content(_incident(), {"confidence_level": "high"}, [], "摘要", needs_review=False)
    assert "低置信度，待人工复核" not in content


# ========== 入库副作用 ==========

class _FakeStore:
    def __init__(self):
        self.added = []
        self.invalidations = 0
    def add(self, item):
        self.added.append(item)
    def add_batch(self, items):
        for it in items:
            self.add(it)
    def invalidate_caches(self):
        self.invalidations += 1


def _patch_enabled(monkeypatch, value=True):
    from app.core.config import settings
    monkeypatch.setattr(settings, "INCIDENT_KNOWLEDGE_ENABLED", value)


def test_ingest_writes_store_and_invalidates(monkeypatch):
    fake = _FakeStore()
    monkeypatch.setattr("app.shared_services.get_knowledge_store", lambda: fake)
    _patch_enabled(monkeypatch, True)
    incident = _incident()
    report = {"root_cause": "连接池耗尽", "confidence_level": "high"}
    assert ingest_incident_into_knowledge(incident, report, [], "摘要") == "ingested"
    assert len(fake.added) == 2                       # parent + child
    assert fake.added[0].id == "incident_INC-1"       # 父条目（可被父块取回）
    assert fake.added[1].metadata["parent_id"] == "incident_INC-1"
    assert fake.invalidations == 1          # 入库后失效查询缓存


def test_ingest_skips_when_disabled(monkeypatch):
    fake = _FakeStore()
    monkeypatch.setattr("app.shared_services.get_knowledge_store", lambda: fake)
    _patch_enabled(monkeypatch, False)
    assert ingest_incident_into_knowledge(_incident(), {"confidence_level": "high"}, [], "摘要") == "skipped"
    assert fake.added == []                  # 开关关闭不写库


def test_ingest_skips_low_quality(monkeypatch):
    fake = _FakeStore()
    monkeypatch.setattr("app.shared_services.get_knowledge_store", lambda: fake)
    _patch_enabled(monkeypatch, True)
    assert ingest_incident_into_knowledge(
        _incident(), {"confidence_level": "unknown"}, [], "摘要") == "skipped"
    assert fake.added == []                  # 低置信度不入库


def test_ingest_skips_when_no_store(monkeypatch):
    monkeypatch.setattr("app.shared_services.get_knowledge_store", lambda: None)
    _patch_enabled(monkeypatch, True)
    assert ingest_incident_into_knowledge(
        _incident(), {"confidence_level": "high"}, [], "摘要") == "skipped"