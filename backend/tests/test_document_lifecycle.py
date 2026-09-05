"""文档幂等与卡死恢复单元测试（批次 B，P0）。

覆盖：
- submit_document_processing 投递失败补偿：幽灵 PENDING → FAILED（可重试）
- find_stale_documents / recover_stale_documents：卡死捞回
- delete_document_with_cleanup：向量清理失败挂起 deleting，恢复后 sweep finalize 移除
- purge_document_artifacts 返回契约：(vector_ok, images_ok)
- process_batch_file：类型/空内容返回 failed 条目、去重 skipped、正常 pending
"""
import uuid

import pytest
import pytest_asyncio
from fastapi import BackgroundTasks

from app.core.config import settings
from app.services import document_service as ds


@pytest_asyncio.fixture
async def test_db():
    """独立测试库实例（conftest 的 test_db 为未启用的旧 fixture，strict 模式下不可用）"""
    from app.core.database import Database
    db = Database()  # 库名由 conftest 预设的 MONGODB_DB_NAME（education_agent_test）决定
    await db.connect()
    yield db
    try:
        await db._mongo.client.drop_database("education_agent_test")
    except Exception:
        pass
    await db.disconnect()


class _StubKnowledgeStore:
    """可编程知识库桩：控制 delete_by_document 是否抛错"""

    def __init__(self, fail=False):
        self.fail = fail
        self.delete_calls = 0

    def delete_by_document(self, document_id):
        self.delete_calls += 1
        if self.fail:
            raise RuntimeError("qdrant unreachable")
        return True


class _StubUploader:
    async def upload(self, **kwargs):
        return {"chunk_count": 2, "char_count": 10}


class _FakeFile:
    def __init__(self, filename: str, content: bytes = b"hello"):
        self.filename = filename
        self._content = content

    async def read(self) -> bytes:
        return self._content


@pytest.fixture
def kb_store():
    """注入可控知识库桩，测试后还原全局引用"""
    stub = _StubKnowledgeStore()
    ds.set_knowledge_store(stub)
    yield stub
    ds.set_knowledge_store(None)


async def _create_pending_doc(test_db, filename="a.md", content_hash="h1"):
    doc_id, document = ds.build_document_record(
        admin_user_id="admin-1",
        filename=filename,
        title=filename,
        doc_type="md",
        file_hash=content_hash,
        biz_meta={},
        category="other",
        tags=[],
    )
    await test_db.create_document(document)
    return doc_id


async def _get_doc(test_db, doc_id):
    return await test_db.get_document(doc_id)


# ========== 投递失败补偿 ==========

@pytest.mark.asyncio
async def test_submit_failure_marks_failed(test_db, monkeypatch):
    doc_id, document = ds.build_document_record(
        admin_user_id="admin-1", filename="a.md", title="a.md",
        doc_type="md", file_hash="h1", biz_meta={},
    )
    await test_db.create_document(document)

    monkeypatch.setattr(settings, "USE_CELERY", False)

    def _boom():
        raise RuntimeError("uploader unavailable")

    monkeypatch.setattr(ds, "get_document_uploader", _boom)

    with pytest.raises(RuntimeError):
        await ds.submit_document_processing(
            test_db, BackgroundTasks(),
            document_id=doc_id, content=b"x", filename="a.md",
            title="a.md", admin_user_id="admin-1", biz_meta={},
        )

    doc = await _get_doc(test_db, doc_id)
    assert doc["status"] == "failed"
    assert "投递失败" in doc["error_message"]


# ========== 卡死捞回 ==========

@pytest.mark.asyncio
async def test_recover_stale_documents(test_db, monkeypatch):
    stale_id = await _create_pending_doc(test_db, content_hash="stale-1")
    healthy_id = await _create_pending_doc(test_db, filename="b.md", content_hash="fresh-1")
    await test_db.update_document(healthy_id, {"status": "completed"})

    # 阈值 0：任何未推进到终态的文档都算卡死（updated_at 在 cutoff 之前）
    monkeypatch.setattr(settings, "DOC_STALE_SECONDS", 0)
    recovered = await ds.recover_stale_documents()
    assert recovered >= 1

    stale_doc = await _get_doc(test_db, stale_id)
    assert stale_doc["status"] == "failed"
    assert "可重试" in stale_doc["error_message"]
    healthy_doc = await _get_doc(test_db, healthy_id)
    assert healthy_doc["status"] == "completed"  # 终态不受影响


# ========== 删除挂起与 finalize ==========

@pytest.mark.asyncio
async def test_delete_defers_on_vector_failure_then_finalizes(test_db, kb_store):
    doc_id = await _create_pending_doc(test_db, content_hash="del-1")

    kb_store.fail = True
    outcome = await ds.delete_document_with_cleanup(test_db, doc_id)
    assert outcome == "deferred"

    doc = await _get_doc(test_db, doc_id)
    assert doc["status"] == "deleting"       # 记录保留（防孤儿向量）
    assert doc.get("purge_pending") is True

    kb_store.fail = False                     # 向量库恢复
    finalized = await ds.finalize_pending_deletes()
    assert finalized == 1
    assert await _get_doc(test_db, doc_id) is None  # 清理成功后记录移除


@pytest.mark.asyncio
async def test_delete_success_when_vector_ok(test_db, kb_store):
    doc_id = await _create_pending_doc(test_db, content_hash="del-2")
    outcome = await ds.delete_document_with_cleanup(test_db, doc_id)
    assert outcome == "deleted"
    assert await _get_doc(test_db, doc_id) is None


@pytest.mark.asyncio
async def test_purge_returns_contract(test_db, kb_store, monkeypatch):
    doc_id = await _create_pending_doc(test_db, content_hash="purge-1")

    kb_store.fail = True

    class _BadImageStore:
        def delete_document_images(self, document_id):
            raise RuntimeError("disk error")

    import app.document.image_store as image_store_mod
    monkeypatch.setattr(image_store_mod, "get_image_store", lambda: _BadImageStore())

    vector_ok, images_ok = await ds.purge_document_artifacts(doc_id)
    assert vector_ok is False
    assert images_ok is False


# ========== 批量单文件编排 ==========

@pytest.mark.asyncio
async def test_batch_file_unsupported_type(test_db):
    entry = await ds.process_batch_file(
        test_db, BackgroundTasks(), file=_FakeFile("virus.exe"),
        admin_user_id="admin-1", skip_duplicate=True, shared_to_diagnosis=True,
    )
    assert entry["status"] == "failed"
    assert "不支持的文件类型" in entry["reason"]


@pytest.mark.asyncio
async def test_batch_file_empty_content(test_db):
    entry = await ds.process_batch_file(
        test_db, BackgroundTasks(), file=_FakeFile("empty.md", content=b""),
        admin_user_id="admin-1", skip_duplicate=True, shared_to_diagnosis=True,
    )
    assert entry["status"] == "failed"
    assert entry["reason"] == "文件内容为空"


@pytest.mark.asyncio
async def test_batch_file_success_then_duplicate_skipped(test_db, monkeypatch):
    monkeypatch.setattr(ds, "get_document_uploader", lambda: _StubUploader())

    file = _FakeFile(f"ops-{uuid.uuid4().hex[:6]}.md", content=b"# runbook\nrestart service")
    first = await ds.process_batch_file(
        test_db, BackgroundTasks(), file=file,
        admin_user_id="admin-1", skip_duplicate=True, shared_to_diagnosis=True,
    )
    assert first["status"] == "pending"
    doc = await _get_doc(test_db, first["document_id"])
    assert doc["status"] == "pending"  # BackgroundTasks 仅注册未执行

    second = await ds.process_batch_file(
        test_db, BackgroundTasks(), file=file,
        admin_user_id="admin-1", skip_duplicate=True, shared_to_diagnosis=True,
    )
    assert second["status"] == "skipped"
