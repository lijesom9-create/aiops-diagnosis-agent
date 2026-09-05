"""
demo 真实知识库导入脚本

将 data/demo_docs/ 下 payment-sim 的真实文档（manual/sop/incident/postmortem）
导入 Qdrant 知识库。与 seed_ops_kb.py（206 篇演示语料）**分层共存**：
- 本批文档 service=payment-sim，真实故障注入验证（R5 验证矩阵）依赖它们
- 检索纪律（service+doc_type 过滤）保证两层互不污染

用法：
    cd backend
    python scripts/seed_demo_kb.py
"""
import asyncio
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

DOCS_DIR = BACKEND_DIR / "data" / "demo_docs"
DOC_TYPES = ("manual", "sop", "incident", "postmortem")

from app.document.frontmatter import extract_business_metadata, parse_frontmatter  # noqa: E402


def collect_docs() -> list:
    docs: list = []
    for sub in DOC_TYPES:
        sub_dir = DOCS_DIR / sub
        if sub_dir.exists():
            docs.extend(sorted(sub_dir.glob("*.md")))
    return [d for d in docs if not d.name.startswith("_")]


async def seed():
    from app.document.uploader import DocumentUploader
    from app.shared_services import init_knowledge_store

    docs = collect_docs()
    if not docs:
        print(f"✗ 未找到 demo 文档: {DOCS_DIR}")
        return
    print(f"找到 {len(docs)} 篇 payment-sim 真实文档")

    store = init_knowledge_store()
    uploader = DocumentUploader(knowledge_store=store, chunking_strategy="parent_child")

    success, fail = 0, 0
    for i, doc_path in enumerate(docs, 1):
        content = doc_path.read_text(encoding="utf-8")
        frontmatter, _ = parse_frontmatter(content)
        biz_meta = extract_business_metadata(frontmatter)
        title = frontmatter.get("title", doc_path.stem)
        print(f"[{i}/{len(docs)}] {doc_path.name} | {biz_meta.get('doc_type')} | {title[:40]}")
        try:
            result = await uploader.upload(
                content=content.encode("utf-8"),
                filename=doc_path.name,
                title=title,
                extra_metadata=biz_meta,
            )
            print(f"  ✓ {result.get('chunk_count', 0)} 块")
            success += 1
        except Exception as e:
            print(f"  ✗ 失败: {e}")
            fail += 1

    print(f"\n导入完成: 成功 {success} / 失败 {fail}")


if __name__ == "__main__":
    asyncio.run(seed())
