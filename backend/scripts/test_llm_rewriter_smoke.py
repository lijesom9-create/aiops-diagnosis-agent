"""LLM MultiQuery Rewriter 冒烟测试"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.retrieval.llm_query_rewriter import LLMQueryRewriter


def main():
    print("=" * 60)
    print("LLM MultiQuery Rewriter 冒烟测试")
    print("=" * 60)

    queries_to_test = [
        "装饰器的作用是什么",
        "FastAPI 依赖注入怎么用",
        "Redis RDB 和 AOF 有什么区别",
    ]

    for mode in ("llm", "enhanced_llm"):
        print(f"\n--- mode={mode} ---")
        rewriter = LLMQueryRewriter(mode=mode, n_variants=4)
        for q in queries_to_test:
            print(f"\n[query] {q}")
            try:
                variants = rewriter.rewrite(q)
                for i, v in enumerate(variants):
                    print(f"  {i}. {v}")
            except Exception as e:
                print(f"  ✗ 失败: {e}")
                import traceback
                traceback.print_exc()
                return 1
        print(f"\n[stats] {rewriter.stats}")

    print("\n" + "=" * 60)
    print("✓ LLM MultiQuery 冒烟测试通过")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
