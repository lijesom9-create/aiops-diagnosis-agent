"""mypy 棘轮门禁：按「文件级错误计数」与基线比较，新增错误即失败（exit 1）。

用法：
    python scripts/mypy_ratchet.py             # 门禁模式（CI 阻塞）
    python scripts/mypy_ratchet.py --update    # 重新生成基线（显式放宽，需在提交说明注明）

基线文件 backend/mypy-baseline.json（{文件路径: 错误数}）。
按文件计数而非逐行比较：行号漂移不误报；跨文件搬代码会在两侧同时 -/+，
超出基线的一侧按新增错误处理——这是棘轮的本意（有意变动必须显式更新基线）。
"""
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
BASELINE = BACKEND_DIR / "mypy-baseline.json"
TARGETS = ["app/core", "app/api"]


def run_mypy() -> tuple:
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", *TARGETS],
        cwd=BACKEND_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    output = proc.stdout + proc.stderr
    lines = [ln for ln in output.splitlines() if "error:" in ln]
    # 防护：mypy 未正常执行（未安装/崩溃）时输出里没有 "error:" 行也没有
    # "Found"/"Success" 汇总——绝不能当成 0 错误（否则 --update 会把基线腐蚀成空）
    if not lines and ("Found" not in output) and ("Success" not in output):
        print("mypy 未能正常执行（未安装或崩溃），原始输出：")
        print(output[-2000:] or "(无输出)")
        sys.exit(2)
    return lines


def per_file_counts(lines: list) -> Counter:
    counts = Counter()
    for ln in lines:
        m = re.match(r"^(.+?):\d+: error:", ln)
        if m:
            counts[m.group(1).replace("\\", "/")] += 1
    return counts


def main() -> int:
    lines = run_mypy()
    counts = per_file_counts(lines)

    if "--update" in sys.argv[1:]:
        BASELINE.write_text(
            json.dumps(dict(sorted(counts.items())), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"基线已更新: {sum(counts.values())} errors in {len(counts)} files")
        return 0

    baseline = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}
    total_now = sum(counts.values())
    total_base = sum(baseline.values())
    print(f"mypy 棘轮: 当前 {total_now} / 基线 {total_base} 错误（{len(counts)} 文件）")

    regressions = {
        f: (n, baseline.get(f, 0))
        for f, n in counts.items()
        if n > baseline.get(f, 0)
    }
    if regressions:
        print("新增错误（超出基线）:")
        for f, (now, base) in sorted(regressions.items()):
            print(f"  {f}: {base} -> {now}")
        print("如为有意改动，运行 python scripts/mypy_ratchet.py --update 并在提交说明注明")
        return 1
    print("通过: 无新增 mypy 错误")
    return 0


if __name__ == "__main__":
    sys.exit(main())
