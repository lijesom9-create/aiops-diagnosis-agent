"""
多模态 RAG 单元测试

覆盖：
- ImageStore 增删查
- VLM 客户端创建与降级
- ParentChildChunker 处理 IMAGE 元素
- MultimodalProcessor 失败降级（不注入 VLM 时跳过 caption）
- DocumentUploader 多模态关闭时与现有行为兼容
"""

import asyncio
import os
import tempfile

import pytest

# 关闭多模态，避免单元测试触发真实 VLM 调用
os.environ.setdefault("MULTIMODAL_ENABLED", "False")


# ========== ImageStore 测试 ==========

def test_image_store_save_and_read(tmp_path):
    from app.document.image_store import ImageStore

    store = ImageStore(root_dir=str(tmp_path))
    doc_id = "test_doc_001"
    image_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100  # 伪 PNG

    relative_path = store.save_image(
        image_bytes=image_bytes,
        document_id=doc_id,
        image_id="img_0001",
        ext="png",
    )

    assert relative_path == f"{doc_id}/img_0001.png"

    # 读取
    data = store.read_bytes(relative_path)
    assert data == image_bytes

    # 全路径
    full = store.get_full_path(relative_path)
    assert full.exists()
    assert full.name == "img_0001.png"


def test_image_store_list_and_delete(tmp_path):
    from app.document.image_store import ImageStore

    store = ImageStore(root_dir=str(tmp_path))
    doc_id = "doc_xyz"

    for i in range(3):
        store.save_image(b"data" + bytes([i]), doc_id, f"img_{i}", "png")

    images = store.list_document_images(doc_id)
    assert len(images) == 3

    deleted = store.delete_document_images(doc_id)
    assert deleted == 3
    assert not (tmp_path / doc_id).exists()


def test_image_store_save_pil_image(tmp_path):
    """测试 PIL.Image 保存"""
    from app.document.image_store import ImageStore

    store = ImageStore(root_dir=str(tmp_path))
    try:
        from PIL import Image
    except ImportError:
        pytest.skip("PIL not installed")

    img = Image.new("RGB", (10, 10), color="red")
    relative = store.save_pil_image(img, document_id="doc_pil", image_id="img_pil")
    assert relative.endswith("img_pil.png")

    data = store.read_bytes(relative)
    assert data[:4] == b"\x89PNG"


# ========== VLM 客户端测试 ==========

def test_vlm_provider_factory_returns_none_without_api_key(monkeypatch):
    """没有 API Key 时返回 None"""
    from app.core.config import settings
    from app.retrieval import vlm_client

    monkeypatch.setattr(settings, "VLM_API_KEY", None)
    monkeypatch.setattr(settings, "AI_API_KEY", None)
    monkeypatch.setattr(settings, "VLM_PROVIDER", "openai")

    provider = vlm_client.create_vlm_provider()
    assert provider is None


def test_vlm_provider_factory_creates_provider(monkeypatch):
    """有 API Key 时创建成功"""
    from app.core.config import settings
    from app.retrieval import vlm_client

    monkeypatch.setattr(settings, "VLM_API_KEY", "sk-test-fake-key")
    monkeypatch.setattr(settings, "VLM_PROVIDER", "openai")
    monkeypatch.setattr(settings, "VLM_MODEL", "gpt-4o-mini")

    provider = vlm_client.create_vlm_provider()
    assert provider is not None
    assert provider.model == "gpt-4o-mini"


def test_vlm_get_vlm_provider_disabled(monkeypatch):
    """MULTIMODAL_ENABLED=False 时 get_vlm_provider 返回 None"""
    from app.core.config import settings
    from app.retrieval import vlm_client

    monkeypatch.setattr(settings, "MULTIMODAL_ENABLED", False)
    # 重置单例
    vlm_client._vlm_provider = None

    assert vlm_client.get_vlm_provider() is None


# ========== ParentChildChunker IMAGE 元素处理测试 ==========

def test_parent_child_chunker_handles_image_element():
    """验证 IMAGE 元素被切为独立子块并带 image_path 元数据"""
    from app.document.models import (
        DocumentElement,
        DocumentMetadata,
        ElementMetadata,
        ElementType,
        StructuredDocument,
    )
    from app.document.parent_child_chunker import ParentChildChunker

    doc = StructuredDocument(
        metadata=DocumentMetadata(filename="test.pdf", title="test"),
        elements=[
            DocumentElement(
                type=ElementType.HEADING,
                text="第一章",
                metadata=ElementMetadata(heading_path=["第一章"]),
            ),
            DocumentElement(
                type=ElementType.PARAGRAPH,
                text="这是一段普通文本。",
                metadata=ElementMetadata(heading_path=["第一章"]),
            ),
            DocumentElement(
                type=ElementType.IMAGE,
                text="",  # 图片元素本身没有文本
                metadata=ElementMetadata(heading_path=["第一章"]),
                image_path="doc1/img_0001.png",
                image_desc="一张演示架构图，展示了 RAG 系统的组件交互。",
                image_keywords=["架构", "RAG", "组件"],
                image_type="diagram",
                ocr_text="向量数据库",
            ),
        ],
    )

    chunker = ParentChildChunker(parent_max_chars=500, child_max_chars=200)
    chunks = chunker.chunk(doc)

    # 应有：1 个父块 + 至少 1 个文本子块 + 1 个图片子块
    assert len(chunks) >= 3
    image_chunks = [c for c in chunks if c.metadata.get("element_type") == "image"]
    assert len(image_chunks) == 1

    img_chunk = image_chunks[0]
    assert img_chunk.metadata["image_path"] == "doc1/img_0001.png"
    assert img_chunk.metadata["image_type"] == "diagram"
    assert "RAG" in img_chunk.text or "架构" in img_chunk.text
    assert img_chunk.metadata.get("has_caption") is True
    assert img_chunk.metadata.get("has_ocr") is True


def test_parent_child_chunker_image_placeholder_when_no_caption():
    """图片元素没有 caption 时用 [图片] 占位"""
    from app.document.models import (
        DocumentElement,
        DocumentMetadata,
        ElementMetadata,
        ElementType,
        StructuredDocument,
    )
    from app.document.parent_child_chunker import ParentChildChunker

    doc = StructuredDocument(
        metadata=DocumentMetadata(filename="test.pdf", title="test"),
        elements=[
            DocumentElement(
                type=ElementType.HEADING,
                text="第一节",
                metadata=ElementMetadata(heading_path=["第一节"]),
            ),
            DocumentElement(
                type=ElementType.IMAGE,
                text="",
                metadata=ElementMetadata(heading_path=["第一节"]),
                image_path="doc1/img_0002.png",
                # 故意不设 image_desc 和 ocr_text
            ),
        ],
    )

    chunker = ParentChildChunker()
    chunks = chunker.chunk(doc)

    image_chunks = [c for c in chunks if c.metadata.get("element_type") == "image"]
    assert len(image_chunks) == 1
    assert "[图片]" in image_chunks[0].text


# ========== MultimodalProcessor 测试 ==========

def test_multimodal_processor_disabled_skips_processing():
    """MULTIMODAL_ENABLED=False 时不做任何处理"""
    from app.document.models import (
        DocumentElement,
        DocumentMetadata,
        ElementMetadata,
        ElementType,
        StructuredDocument,
    )
    from app.document.multimodal_processor import MultimodalProcessor

    doc = StructuredDocument(
        metadata=DocumentMetadata(filename="test.pdf", title="test"),
        elements=[
            DocumentElement(
                type=ElementType.IMAGE,
                text="",
                metadata=ElementMetadata(),
                image_path="doc/img.png",
            ),
        ],
    )

    processor = MultimodalProcessor()
    # 多模态关闭
    from app.core.config import settings
    original = settings.MULTIMODAL_ENABLED
    settings.MULTIMODAL_ENABLED = False
    try:
        result = asyncio.run(processor.process_document(doc, document_id="doc"))
        # image_desc 应保持为 None（未被处理）
        assert result.elements[0].image_desc is None
    finally:
        settings.MULTIMODAL_ENABLED = original


def test_multimodal_processor_vlm_failure_falls_back_gracefully(monkeypatch):
    """VLM 失败时不抛异常，元素 caption 保持空"""
    from app.core.config import settings
    from app.document.image_store import ImageStore
    from app.document.models import (
        DocumentElement,
        DocumentMetadata,
        ElementMetadata,
        ElementType,
        StructuredDocument,
    )
    from app.document.multimodal_processor import MultimodalProcessor

    # 准备一个真实的图片文件
    with tempfile.TemporaryDirectory() as tmp:
        store = ImageStore(root_dir=tmp)
        doc_id = "test_doc"
        # 保存一张占位图片
        image_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
        rel_path = store.save_image(image_bytes, doc_id, "img_0001", "png")

        doc = StructuredDocument(
            metadata=DocumentMetadata(filename="test.pdf", title="test"),
            elements=[
                DocumentElement(
                    type=ElementType.IMAGE,
                    text="",
                    metadata=ElementMetadata(),
                    image_path=rel_path,
                ),
            ],
        )

        # Mock VLM 抛异常
        class FailingVLM:
            async def describe_image(self, image_bytes, mime_type="image/png"):
                raise RuntimeError("VLM API down")

        # Mock OCR 也失败
        monkeypatch.setattr(settings, "MULTIMODAL_ENABLED", True)
        monkeypatch.setattr(settings, "MULTIMODAL_USE_OCR", False)

        processor = MultimodalProcessor(
            image_store=store,
            vlm_provider=FailingVLM(),
            llm_provider=None,
        )

        # 不应抛异常
        result = asyncio.run(processor.process_document(doc, document_id=doc_id))
        assert result.elements[0].image_desc is None


# ========== DocumentUploader 多模态开关测试 ==========

def test_document_uploader_multimodal_disabled_by_default(tmp_path):
    """默认 MULTIMODAL_ENABLED=False，uploader 不应注入 image_store 到 parser"""
    from app.document.uploader import DocumentUploader
    from app.knowledge.unified_store import UnifiedKnowledgeStore
    from app.retrieval.embeddings import TFIDFModel

    store = UnifiedKnowledgeStore(
        embedding_model=TFIDFModel(max_features=100),
        collection_name="test_mm_off",
        persist_directory=str(tmp_path / "chroma"),
    )
    uploader = DocumentUploader(knowledge_store=store)

    assert uploader._is_multimodal_enabled() is False


def test_document_uploader_uploads_text_file_without_multimodal(tmp_path):
    """关闭多模态时，上传文本文件应正常工作"""
    import asyncio

    from app.document.uploader import DocumentUploader
    from app.knowledge.unified_store import UnifiedKnowledgeStore
    from app.retrieval.embeddings import TFIDFModel

    store = UnifiedKnowledgeStore(
        embedding_model=TFIDFModel(max_features=100),
        collection_name="test_mm_txt",
        persist_directory=str(tmp_path / "chroma"),
    )
    uploader = DocumentUploader(knowledge_store=store)

    content = "# 测试标题\n\n这是一段测试文本。\n".encode("utf-8")
    result = asyncio.run(uploader.upload(
        content=content,
        filename="test.md",
        document_id="test_doc_mm_001",
    ))

    assert result["chunk_count"] > 0
    assert result["document_id"] == "test_doc_mm_001"


# ========== VLM 响应解析测试 ==========

def test_vlm_response_parsing_json():
    """VLM 返回标准 JSON 时正确解析"""
    from app.retrieval.vlm_client import OpenAICompatibleVLM

    response = '''{
      "caption": "一张展示 RAG 架构的流程图。",
      "keywords": ["RAG", "架构", "向量数据库"],
      "image_type": "diagram"
    }'''
    parsed = OpenAICompatibleVLM._parse_response(response)

    assert parsed["caption"].startswith("一张展示")
    assert "RAG" in parsed["keywords"]
    assert parsed["image_type"] == "diagram"


def test_vlm_response_parsing_markdown_wrapped():
    """VLM 返回 ```json 包裹时正确解析"""
    from app.retrieval.vlm_client import OpenAICompatibleVLM

    response = '''```json
    {
      "caption": "API 调用时序图。",
      "keywords": ["API", "时序"],
      "image_type": "diagram"
    }
    ```'''
    parsed = OpenAICompatibleVLM._parse_response(response)

    assert parsed["caption"] == "API 调用时序图。"
    assert "API" in parsed["keywords"]


def test_vlm_response_parsing_fallback_to_caption():
    """VLM 返回非 JSON 时整段作为 caption"""
    from app.retrieval.vlm_client import OpenAICompatibleVLM

    response = "这是一张图表，展示了用户增长趋势。"
    parsed = OpenAICompatibleVLM._parse_response(response)

    assert parsed["caption"] == response
    assert parsed["keywords"] == []
