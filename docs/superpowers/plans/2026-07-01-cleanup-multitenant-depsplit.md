# Cleanup + Multi-Tenant Phase 1 + Dependency Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform the current single-user RAG knowledge assistant into a multi-tenant foundation, clean up dead test files, and split dependencies for faster CI.

**Architecture:** Shared MongoDB database with `org_id` field isolation. Registration auto-creates an organization. ChromaDB documents tagged with `org_id` for vector-level isolation. ML dependencies moved to separate requirements file.

**Tech Stack:** Python 3.11+, FastAPI, MongoDB, ChromaDB, JWT

## Global Constraints

- All existing API endpoints must continue working (backward compatible where possible)
- Register endpoint adds `org_name` as a required field
- JWT payload adds `org_id` claim
- ChromaDB queries filter by `org_id` via `$and` clause
- In-memory database mode must also support org_id filtering
- `requirements.txt` must NOT include chromadb or sentence-transformers after split
- CI must install only `requirements.txt`

---

# File Structure

| File | Action | Responsibility |
|---|---|---|
| `backend/tests/test_models.py` | **Delete** | Dead code — references non-existent modules |
| `backend/tests/test_retrieval.py` | **Delete** | Dead code — references non-existent modules |
| `backend/tests/test_memory.py` | **Delete** | Dead code |
| `backend/tests/test_evaluation.py` | **Delete** | Dead code |
| `backend/tests/test_integration.py` | **Delete** | Dead code |
| `backend/tests/test_knowledge_new.py` | **Delete** | Dead code |
| `backend/tests/test_learning.py` | **Delete** | Dead code |
| `backend/tests/test_learning_path.py` | **Delete** | Dead code |
| `backend/tests/test_lifecycle.py` | **Delete** | Dead code |
| `backend/tests/test_optimization.py` | **Delete** | Dead code |
| `backend/tests/test_regression_fixes.py` | **Delete** | Dead code |
| `backend/tests/test_e2e_test.py` | **Delete** | Dead code |
| `README.md` | **Modify** | Remove references to `agent/` and `agent_v2/` |
| `ARCHITECTURE.md` | **Modify** | Remove references to `agent/` and `agent_v2/` |
| `backend/requirements.txt` | **Modify** | Remove ML dependencies (chromadb, sentence-transformers) |
| `backend/requirements-ml.txt` | **Create** | ML dependencies (chromadb, sentence-transformers) |
| `.github/workflows/ci.yml` | **Modify** | Install only `requirements.txt` (not ML) |
| `backend/app/core/database.py` | **Modify** | Add Organization CRUD methods; add `org_id` to user queries |
| `backend/app/core/auth.py` | **Modify** | `UserCreate` + `org_name`; `register_user` creates org; JWT includes `org_id`; `UserResponse` includes org info |
| `backend/app/api/auth.py` | **Modify** | `RegisterRequest` + `org_name` field |
| `backend/app/retrieval/chroma_store.py` | **Modify** | Add `org_id` to metadata; filter queries by `org_id` |
| `backend/app/knowledge/unified_store.py` | **Modify** | Add `org_id` to metadata; filter queries by `org_id` |

---

## Task 1: Cleanup — Delete Dead Test Files

**Files:**
- Delete: `backend/tests/test_models.py`
- Delete: `backend/tests/test_retrieval.py`
- Delete: `backend/tests/test_memory.py`
- Delete: `backend/tests/test_evaluation.py`
- Delete: `backend/tests/test_integration.py`
- Delete: `backend/tests/test_knowledge_new.py`
- Delete: `backend/tests/test_learning.py`
- Delete: `backend/tests/test_learning_path.py`
- Delete: `backend/tests/test_lifecycle.py`
- Delete: `backend/tests/test_optimization.py`
- Delete: `backend/tests/test_regression_fixes.py`
- Delete: `backend/tests/test_e2e_test.py`

- [ ] Delete all 12 files

```bash
cd backend/tests
rm \
  test_models.py test_retrieval.py test_memory.py \
  test_evaluation.py test_integration.py test_knowledge_new.py \
  test_learning.py test_learning_path.py test_lifecycle.py \
  test_optimization.py test_regression_fixes.py test_e2e_test.py
```

- [ ] Verify remaining tests still collect

```bash
cd backend && python -m pytest tests/ --collect-only -q
```
Expected: no ImportError, only the 7 existing test files are collected

- [ ] Commit

```bash
git add backend/tests/
git commit -m "chore: remove 12 dead test files referencing deleted modules"
```

---

## Task 2: Update Documentation

**Files:**
- Modify: `README.md`
- Modify: `ARCHITECTURE.md`

- [ ] In `README.md`, replace or remove any lines referencing `app/core/agent/` or `app/core/agent_v2/`

```bash
grep -n "agent/\|agent_v2" README.md ARCHITECTURE.md
```

If found, update the text to reference `app/langgraph_agent/` instead, or remove the passage entirely.

- [ ] In `ARCHITECTURE.md`, same treatment for any `agent/` or `agent_v2/` references

- [ ] Commit

```bash
git add README.md ARCHITECTURE.md
git commit -m "docs: update references from deleted agent/agent_v2 to langgraph_agent"
```

---

## Task 3: Split Dependencies

**Files:**
- Create: `backend/requirements-ml.txt`
- Modify: `backend/requirements.txt`

- [ ] Create `backend/requirements-ml.txt`

Content:
```
# ML dependencies (optional — heavy, not needed for CI or basic operation)
chromadb>=1.5.0
sentence-transformers>=2.2.0
```

- [ ] Remove ML lines from `backend/requirements.txt`

Current lines to remove (lines 21-22):
```
# 向量数据库与语义嵌入
chromadb>=1.5.0
sentence-transformers>=2.2.0
```

Result: `requirements.txt` no longer depends on chromadb or sentence-transformers.

- [ ] Verify `requirements.txt` installs cleanly

```bash
cd backend && pip install --dry-run -r requirements.txt 2>&1 | grep -i "error"
```
Expected: no errors

- [ ] Commit

```bash
git add backend/requirements.txt backend/requirements-ml.txt
git commit -m "refactor: split ML dependencies into requirements-ml.txt"
```

---

## Task 4: Organization CRUD in Database Layer

**Files:**
- Modify: `backend/app/core/database.py`

**Interfaces:**
- Produces: `Database.create_org(name, owner_id) -> str`, `Database.get_org(org_id) -> dict`, `Database.get_org_by_name(name) -> dict`, `Database.get_user_orgs(user_id) -> list`

- [ ] Add Organization in-memory storage and CRUD methods to `Database.__init__`

Add `self._organizations: List[Dict] = []` to the `__init__` method.

- [ ] Add `create_org` method

```python
async def create_org(self, name: str, owner_id: str) -> str:
    """Create a new organization."""
    await self.connect()
    org_id = f"org_{uuid.uuid4().hex[:12]}"
    org = {
        "org_id": org_id,
        "name": name,
        "owner_id": owner_id,
        "created_at": datetime.now(),
        "updated_at": datetime.now(),
    }
    if self._use_mongo:
        await self._mongo.organizations.insert_one(org)
    else:
        self._organizations.append(org)
    return org_id
```

- [ ] Add `get_org` method

```python
async def get_org(self, org_id: str) -> Optional[Dict]:
    """Get organization by ID."""
    await self.connect()
    if self._use_mongo:
        return await self._mongo.organizations.find_one({"org_id": org_id}, {"_id": 0})
    for org in self._organizations:
        if org.get("org_id") == org_id:
            return org
    return None
```

- [ ] Add `get_org_by_name` method

```python
async def get_org_by_name(self, name: str) -> Optional[Dict]:
    """Get organization by name."""
    await self.connect()
    if self._use_mongo:
        return await self._mongo.organizations.find_one({"name": name}, {"_id": 0})
    for org in self._organizations:
        if org.get("name") == name:
            return org
    return None
```

- [ ] Add `get_user_orgs` method

```python
async def get_user_orgs(self, user_id: str) -> List[Dict]:
    """Get all organizations where user is owner or member."""
    await self.connect()
    if self._use_mongo:
        cursor = self._mongo.organizations.find(
            {"owner_id": user_id}, {"_id": 0}
        )
        return await cursor.to_list(100)
    return [org for org in self._organizations if org.get("owner_id") == user_id]
```

- [ ] Run the existing database tests to verify nothing broke

```bash
cd backend && python -m pytest tests/test_e2e_fixes.py::TestDatabaseFixes -v --tb=short
```
Expected: PASS

- [ ] Commit

```bash
git add backend/app/core/database.py
git commit -m "feat: add Organization CRUD to database layer"
```

---

## Task 5: Auth — Registration with Organization

**Files:**
- Modify: `backend/app/core/auth.py`

**Interfaces:**
- Consumes: `Database.create_org(name, owner_id) -> str`, `Database.get_org_by_name(name) -> dict`, `Database.get_org(org_id) -> dict`
- Produces: `UserCreate.org_name: str`, `Token` with org_id in payload, `UserResponse` with `org_id`, `org_name`

- [ ] Add `org_name` to `UserCreate`

```python
class UserCreate(BaseModel):
    username: str = Field(..., min_length=2, max_length=50)
    password: str = Field(..., min_length=6, max_length=128)
    email: str
    role: UserRole = UserRole.STUDENT
    org_name: str = Field(..., min_length=1, max_length=100)  # NEW
```

- [ ] Add `org_id` and `org_name` to `UserResponse`

```python
class UserResponse(BaseModel):
    user_id: str
    username: str
    email: str
    role: str
    org_id: str = ""       # NEW
    org_name: str = ""     # NEW
```

- [ ] Modify `create_access_token` to accept optional `org_id`

```python
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt
```
(No signature change needed — `data` dict already carries whatever we put in it.)

- [ ] Modify `register_user` to create org and embed `org_id` in JWT

```python
async def register_user(user_data: UserCreate) -> Token:
    # Check existing user
    existing_user = await db.get_user_by_username(user_data.username)
    if existing_user:
        raise HTTPException(status_code=400, detail="用户名已存在")

    # Check org name uniqueness
    existing_org = await db.get_org_by_name(user_data.org_name)
    if existing_org:
        raise HTTPException(status_code=400, detail="组织名已存在")

    user_id = f"user_{uuid.uuid4().hex[:12]}"

    # Create organization
    org_id = await db.create_org(name=user_data.org_name, owner_id=user_id)

    # Create user with org_id
    user_dict = {
        "user_id": user_id,
        "username": user_data.username,
        "email": user_data.email,
        "role": user_data.role.value if isinstance(user_data.role, Enum) else user_data.role,
        "hashed_password": get_password_hash(user_data.password),
        "org_id": org_id,                                           # NEW
    }
    await db.create_user(user_dict)

    # Create token with org_id in payload
    access_token = create_access_token(
        data={"sub": user_id, "org_id": org_id},                    # MODIFIED
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )

    return Token(access_token=access_token)
```

- [ ] Modify `get_current_user` to return `org_id` and `org_name`

```python
async def get_current_user(credentials=Depends(security)) -> UserResponse:
    # ... existing JWT decode ...
    payload = jwt.decode(credentials.credentials, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    user_id: str = payload.get("sub")
    org_id: str = payload.get("org_id", "")       # NEW

    if user_id is None:
        raise credentials_exception

    user = await db.get_user(user_id)
    if user is None:
        raise credentials_exception

    # Look up org name
    org_name = ""
    if org_id:
        org = await db.get_org(org_id)
        if org:
            org_name = org.get("name", "")

    return UserResponse(
        user_id=user["user_id"],
        username=user["username"],
        email=user["email"],
        role=user["role"],
        org_id=org_id,
        org_name=org_name,
    )
```

- [ ] Modify `login_user` to embed `org_id` in token

```python
async def login_user(user_data: UserLogin) -> Token:
    user = await db.get_user_by_username(user_data.username)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if not verify_password(user_data.password, user["hashed_password"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    org_id = user.get("org_id", "")                                  # NEW
    access_token = create_access_token(
        data={"sub": user["user_id"], "org_id": org_id},             # MODIFIED
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    return Token(access_token=access_token)
```

- [ ] Run auth tests to verify

```bash
cd backend && python -m pytest tests/test_api_e2e.py::TestAuthAPI tests/test_api_e2e.py::TestHealthAndConfig -v --tb=short
```
Expected: PASS (note: registration tests might need org_name field — tests will need updating in Task 6)

- [ ] Commit

```bash
git add backend/app/core/auth.py
git commit -m "feat: add org support to auth — register creates org, JWT carries org_id"
```

---

## Task 6: Update API Register Request

**Files:**
- Modify: `backend/app/api/auth.py`

- [ ] Add `org_name` field to `RegisterRequest`

```python
class RegisterRequest(BaseModel):
    username: str
    password: str
    email: str
    role: UserRole = UserRole.STUDENT
    org_name: str           # NEW — required
```

- [ ] Pass `org_name` through in the register handler

```python
@router.post("/register", response_model=Token)
async def register(request: RegisterRequest):
    user_data = UserCreate(
        username=request.username,
        password=request.password,
        email=request.email,
        role=request.role,
        org_name=request.org_name,       # NEW
    )
    return await register_user(user_data)
```

- [ ] Update existing auth tests to include `org_name`

The test files `test_api_e2e.py`, `test_documents.py`, `test_search.py`, `test_e2e_fixes.py`, `test_feishu.py`, `test_study_plans.py` all call registration. Each registration payload must add `"org_name": "test-org"`.

For example, in `test_api_e2e.py::TestAuthAPI::test_register_student`:
```python
# Before
resp = client.post("/api/auth/register", json={
    "username": username, "password": "test123456",
    "email": f"{username}@test.com", "role": "student",
})
# After
resp = client.post("/api/auth/register", json={
    "username": username, "password": "test123456",
    "email": f"{username}@test.com", "role": "student",
    "org_name": "test-org",
})
```

Update the `student_token` fixture in each test file (6 files):
- `backend/tests/test_api_e2e.py`
- `backend/tests/test_documents.py`
- `backend/tests/test_search.py`
- `backend/tests/test_e2e_fixes.py`
- `backend/tests/test_feishu.py`
- `backend/tests/test_study_plans.py`

Search pattern: find every `client.post("/api/auth/register", json={...})` and add `"org_name": "test-org"`.

Also update the hardcoded test user in `database.py` (line 106-112):
```python
self._users.append({
    "user_id": "test_user_001",
    "username": "test",
    "email": "test@example.com",
    "role": "student",
    "hashed_password": hashed_password,
    "org_id": "org_test",          # NEW
})
```

And in the in-memory fallback, add a test org:
```python
self._organizations.append({
    "org_id": "org_test",
    "name": "test-org",
    "owner_id": "test_user_001",
    "created_at": datetime.now(),
    "updated_at": datetime.now(),
})
```

- [ ] Run all working tests

```bash
cd backend && python -m pytest tests/test_api_e2e.py tests/test_document_upload.py tests/test_e2e_fixes.py tests/test_feishu.py -v --tb=short
```
Expected: PASS

- [ ] Commit

```bash
git add backend/app/api/auth.py backend/tests/ backend/app/core/database.py
git commit -m "feat: add org_name to registration API, update all tests"
```

---

## Task 7: Unified Store org_id Isolation

**Files:**
- Modify: `backend/app/knowledge/unified_store.py`

**Interfaces:**
- Consumes: `Database.get_org(org_id)`
- Produces: `KnowledgeItem.org_id: str`, `UnifiedKnowledgeStore.add()` includes org_id in metadata, `UnifiedKnowledgeStore.search()/hybrid_search()` filters by `org_id`

- [ ] Add `org_id` field to `KnowledgeItem`

```python
@dataclass
class KnowledgeItem:
    id: str
    title: str
    content: str
    source: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    org_id: str = ""  # NEW
```

- [ ] Update `to_chroma()` to include `org_id` in metadata

```python
def to_chroma(self) -> Dict[str, Any]:
    return {
        "id": self.id,
        "content": self.content,
        "metadata": {
            "title": self.title,
            "source": self.source,
            "created_at": self.created_at,
            "org_id": self.org_id,  # NEW
            **self.metadata,
        }
    }
```

- [ ] Update `search()` method to filter by `org_id`

Add `org_id` parameter to the `search()` method signature:
```python
def search(
    self,
    query: str,
    top_k: int = 5,
    min_score: float = 0.5,
    source: Optional[str] = None,
    user_id: Optional[str] = None,
    topic_id: Optional[str] = None,
    org_id: Optional[str] = None,  # NEW
) -> List[Dict[str, Any]]:
```

Add org_id post-processing filter alongside existing user_id filter (around line 396-401):
```python
# 后处理：用户隔离 + 组织隔离
output = []
for doc_id, score, metadata in results:
    # 组织隔离
    if org_id:
        doc_org_id = metadata.get("org_id")
        if doc_org_id and doc_org_id != org_id:
            continue  # 跳过其他组织的数据

    # 用户隔离（沿用现有逻辑）
    if user_id:
        doc_user_id = metadata.get("user_id")
        if doc_user_id and doc_user_id != user_id:
            continue
    # ... rest same
```

Also update `search_by_type()` and `hybrid_search()` to pass through `org_id`:
```python
def search_by_type(self, query, source, top_k=5, user_id=None, org_id=None):
    return self.search(query=query, top_k=top_k, source=source, user_id=user_id, org_id=org_id)

def hybrid_search(self, query, top_k=5, min_score=0.0, source=None, user_id=None, org_id=None, rewrite_query=True):
    # ... same signature change, pass org_id through to self.search()
```

- [ ] Commit

```bash
git add backend/app/knowledge/unified_store.py
git commit -m "feat: add org_id isolation to unified knowledge store"
```

---

## Task 8: ChromaDB Store org_id passthrough

**Files:**
- Modify: `backend/app/retrieval/chroma_store.py`

**Note:** `ChromaDBVectorStore` is a generic wrapper. The org_id isolation happens at the `UnifiedKnowledgeStore` level (Task 7). The ChromaDB store just needs to pass metadata through as-is — which it already does.

No code changes needed in `chroma_store.py`. The metadata dicts (which now contain `org_id`) flow through transparently to ChromaDB.

Skip this task unless review shows the ChromaDB store has its own hardcoded user_id filtering that bypasses the unified store.

- [ ] Verify no hardcoded `user_id` filters exist in `chroma_store.py`

Search for patterns like `"user_id"` in chroma_store.py — the file should have none (it's generic).

```bash
grep -n "user_id" backend/app/retrieval/chroma_store.py
```
Expected: no results (chroma_store.py is generic, doesn't know about user_id or org_id)  
If found: those are bugs from refactoring — remove them, isolation happens in unified_store.py

- [ ] (If no changes needed, skip commit)

---

## Task 9: Update CI Workflow

**Files:**
- Modify: `.github/workflows/ci.yml`

- [ ] Update CI to install only `requirements.txt` (not ML deps)

In the backend step, change:
```yaml
pip install --no-cache-dir -r requirements.txt
```
(The CI already installs only requirements.txt, but let's verify no ML deps crept in.)

Also verify the CI workflow correctly references the simplified requirements.

- [ ] Commit

```bash
git add .github/workflows/ci.yml
git commit -m "ci: use simplified requirements (ML split out)"
```
