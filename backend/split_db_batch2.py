"""T3-B②：把 database.py 剩余方法拆成 incidents/diagnosis/knowledge 三个 Mixin（ast 精确取边界）。

生成的 mixin 自动按实际用法补全导入（datetime/re/uuid/typing/logger/settings/db_utils helpers）。
database.py 移除这些方法；class Database(UsersMixin, ContentMixin, IncidentsMixin, DiagnosisMixin, KnowledgeMixin)。
"""
import ast
import re
from pathlib import Path

BASE = Path(r"D:\Dev\Projects\my-code-space\projects\claude-learning\education-agent\backend\app\core")
DB = BASE / "database.py"
MIX = BASE / "db_mixins"

GROUPS = {
    "incidents": "IncidentsMixin", "diagnosis": "DiagnosisMixin", "knowledge": "KnowledgeMixin",
}
GROUP_MEMBERS = {
 "incidents": ["ensure_incident_indexes","save_incident","get_incident","update_incident_fields",
   "find_incident_by_fingerprint","find_active_incident_by_service","list_incidents",
   "add_incident_fingerprint","ack_incident","find_stale_active_incidents","force_resolve_incident",
   "add_incident_diagnosis","aggregate_incident_patterns","aggregate_diagnosis_quality"],
 "diagnosis": ["save_diagnosis_task","claim_next_diagnosis_task","recover_stale_diagnosis_tasks",
   "update_diagnosis_task","save_question","get_question","get_questions_by_topic",
   "save_tool_audit_log","save_feedback","mark_documents_for_review"],
 "knowledge": ["save_knowledge","get_knowledge","get_knowledge_by_topic","search_knowledge",
   "get_knowledge_by_course","get_courses","save_quiz_record","get_user_quiz_records",
   "get_user_analytics","save_user_analytics","save_learning_state","get_learning_state",
   "get_student_state","save_student_state","get_session_memory","save_session_memory",
   "get_long_term_memory","save_long_term_memory","get_user_profile","save_user_profile",
   "get_knowledge_base","save_knowledge_base","get_all_courses","save_course",
   "get_all_knowledge_points","save_knowledge_point","get_teaching_experiences",
   "save_teaching_experience","get_user_knowledge_documents","save_user_document",
   "delete_user_document","save_evaluation_record","get_evaluation_records",
   "save_observability_trace","update_workflow_state","get_workflow_state",
   "list_workflow_states","delete_workflow_state"],
}

src = DB.read_text(encoding="utf-8")
lines = src.splitlines()
tree = ast.parse(src)
dbcls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "Database")
meth_span = {}
for n in dbcls.body:
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
        start = n.decorator_list[0].lineno if n.decorator_list else n.lineno
        meth_span[n.name] = (start, n.end_lineno)

TY_NAMES = ["Any","Dict","List","Optional","Tuple","Sequence","Mapping"]

def build_mixin(names, class_name):
    body, text = [], []
    for nm in sorted([x for x in names if x in meth_span], key=lambda x: meth_span[x][0]):
        s, e = meth_span[nm]
        for ln in lines[s-1:e]:
            body.append(ln); text.append(ln)
    joined = "\n".join(text)
    used_h = []
    for h in ("clean_mongo_docs","clean_mongo_doc","escape_regex"):
        if re.search(r"\b"+h+r"\b", joined):
            used_h.append(h)
    import_lines = ["from __future__ import annotations", "", "from app.core.db_utils import " + (", ".join(used_h) if used_h else "clean_mongo_docs")]
    uses_datetime = re.search(r"\bdatetime\b", joined) or re.search(r"\btimedelta\b", joined)
    uses_uuid = re.search(r"\buuid\.[a-zA-Z]", joined)
    uses_ty = [t for t in TY_NAMES if re.search(r"\b"+t+r"\b", joined)]
    uses_logger = re.search(r"\blogger\b", joined)
    uses_settings = re.search(r"\bsettings\b", joined)
    if uses_datetime: import_lines.append("from datetime import datetime, timedelta")
    if uses_uuid: import_lines.append("import uuid")
    if uses_ty: import_lines.append("from typing import " + ", ".join(uses_ty))
    import_lines.append("import asyncio")
    if uses_logger: import_lines.append("from loguru import logger")
    if uses_settings: import_lines.append("from app.core.config import settings")
    header = ('"""Database %s 领域 Mixin（T3-B② 从 database.py 拆分）。"""\n\n' + "\n".join(import_lines) + "\n\n") % class_name
    return header + "class " + class_name + ":\n" + "\n".join(body) + "\n"

# 写 mixin 文件
for grp, cls in GROUPS.items():
    names = GROUP_MEMBERS[grp]
    missing = [n for n in names if n not in meth_span]
    print(f"[{grp}] missing: {missing or '无'}")
    (MIX / (grp + ".py")).write_text(build_mixin(names, cls), encoding="utf-8")

# 更新 __init__
MIX.joinpath("__init__.py").write_text(
    "from app.core.db_mixins.users import UsersMixin\n"
    "from app.core.db_mixins.content import ContentMixin\n"
    "from app.core.db_mixins.incidents import IncidentsMixin\n"
    "from app.core.db_mixins.diagnosis import DiagnosisMixin\n"
    "from app.core.db_mixins.knowledge import KnowledgeMixin\n"
    "__all__ = ['UsersMixin','ContentMixin','IncidentsMixin','DiagnosisMixin','KnowledgeMixin']\n", encoding="utf-8")

# 删除 database.py 中的这些方法与继承注入
skip = set()
for names in GROUP_MEMBERS.values():
    for nm in names:
        if nm in meth_span:
            s, e = meth_span[nm]
            for ln in range(s, e+1):
                skip.add(ln)
out = [ln for idx, ln in enumerate(lines, start=1) if idx not in skip]
new = "\n".join(out) + "\n"
new = new.replace("class Database(UsersMixin, ContentMixin):",
                  "class Database(UsersMixin, ContentMixin, IncidentsMixin, DiagnosisMixin, KnowledgeMixin):", 1)
DB.write_text(new, encoding="utf-8")
print("done; removed", len(skip), "lines")