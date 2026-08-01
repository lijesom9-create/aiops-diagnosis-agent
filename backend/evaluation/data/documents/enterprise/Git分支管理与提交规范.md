# Git 分支管理与提交规范

| 字段 | 值 |
|------|------|
| 文档分类 | dev_guide |
| 适用范围 | 公司所有研发团队 |
| Git 版本 | 2.40+ |
| 维护团队 | DevOps 工具组 |
| 更新日期 | 2026-07-22 |

## 1. 分支模型

团队采用基于 Git Flow 演进的分支模型，包含 5 类核心分支：

### 1.1 分支说明

| 分支 | 类型 | 生命周期 | 说明 |
|------|------|----------|------|
| main | 长期 | 永久 | 生产环境代码，始终可发布，受保护 |
| develop | 长期 | 永久 | 最新开发成果的集成分支，下个 release 的来源 |
| feature/* | 临时 | 功能周期 | 功能开发分支，从 develop 切出，完成后合回 develop |
| release/* | 临时 | 发布周期 | 发布准备分支，从 develop 切出，完成后合回 main 和 develop |
| hotfix/* | 临时 | 修复周期 | 紧急修复分支，从 main 切出，完成后合回 main 和 develop |

### 1.2 分支图示（文字描述）

```
main      ──●───────────────●────────●────────────●──  (生产)
              \              ↑       /              ↑
               \             │      / hotfix       │ release
                \            │     /               │
develop    ──────●───●───●───●─────●───●───●───●──●──  (集成)
                   \       /         \       /
                    \     /           \     /
feature/a           ─●●●─             ─●●●─
                                   feature/b
```

### 1.3 分支保护规则

- `main` 分支：禁止直接 push，仅允许通过 PR 合并，至少 2 人审查。
- `develop` 分支：禁止直接 push，至少 1 人审查。
- 合并方式：feature/release/hotfix 一律使用 Squash merge，保持历史整洁。

---

## 2. 分支命名规范

### 2.1 命名格式

```
<type>/<issue-id>-<short-description>
```

- `type`：feature / fix / hotfix / release / chore / docs
- `issue-id`：Jira 或内部任务系统的工单号
- `short-description`：小写英文，连字符分隔，不超过 4 个单词

### 2.2 命名示例

```
# 正确
feature/PROJ-1234-user-login
fix/PROJ-1256-order-status-bug
hotfix/PROJ-1290-payment-timeout
release/2.4.0
chore/PROJ-1300-upgrade-deps
docs/PROJ-1310-api-readme
```

```
# 错误
feature/login                # 缺少工单号
fix-bug                      # 格式错误
feature/PROJ-1234_UserLogin  # 大写、下划线
hotfix/临时修复              # 中文命名
my-branch                    # 无 type 前缀
```

---

## 3. 提交信息规范

团队采用 [Conventional Commits](https://www.conventionalcommits.org/) 规范，提交信息格式如下：

```
<type>(<scope>): <subject>

<body>

<footer>
```

### 3.1 type 类型

| type | 含义 | 是否触发版本号变化 |
|------|------|---------------------|
| feat | 新功能 | MINOR |
| fix | Bug 修复 | PATCH |
| docs | 文档变更 | 无 |
| style | 代码格式（不影响功能） | 无 |
| refactor | 重构（非新增功能、非修复 Bug） | 无 |
| perf | 性能优化 | PATCH |
| test | 测试相关 | 无 |
| build | 构建/依赖相关 | 无 |
| ci | CI 配置 | 无 |
| chore | 杂项（不修改 src 或 test） | 无 |
| revert | 回滚某次提交 | 视情况 |

### 3.2 scope 说明

`scope` 用于标识影响的模块，可选。例如：`auth`、`order`、`user`、`payment`、`ci`、`config`。

### 3.3 subject 要求

- 使用祈使句、现在时："add" 而非 "added"。
- 首字母小写。
- 结尾不加句号。
- 不超过 50 字符。

### 3.4 body 要求

- 解释"为什么"做这个改动，而不是"做了什么"（代码本身已说明）。
- 每行不超过 72 字符。
- 可使用列表。

### 3.5 footer 与 Breaking Change

- `BREAKING CHANGE:` 开头的行表示破坏性变更，会触发 MAJOR 版本升级。
- `Closes #123`、`Refs #456` 用于关联工单。

### 3.6 提交示例

```
# 正确示例 1：新功能
feat(auth): 支持手机号验证码登录

为提升登录安全性，新增短信验证码登录方式，复用已有的
SMS 服务。验证码 5 分钟有效，连续失败 5 次锁定 30 分钟。

Closes PROJ-1234
```

```
# 正确示例 2：带 breaking change
feat(order)!: 重构订单状态机为 FSM 引擎

BREAKING CHANGE: 订单状态枚举新增 refunded，废弃 refunded_failed。
下游消费方需更新枚举定义。

Closes PROJ-1300
```

```
# 正确示例 3：Bug 修复
fix(payment): 修复微信支付回调签名校验失败问题

微信支付回调在 amount 含小数时签名不一致，统一使用整数（分）
进行签名。补充单元测试覆盖。

Closes PROJ-1256
```

```
# 错误示例
update code                    # 无 type、无 scope、无描述
fix: bug                       # 描述太笼统
feat: Added new login feature. # 大写开头、句号结尾
wip                            # 临时提交不应进入主分支
fix bug                        # 无冒号、无 scope
```

### 3.7 提交粒度

- 一次提交完成一件完整的事，避免"半成品"提交。
- 单个提交的 diff 不超过 500 行（自动生成的代码除外）。
- 禁止 `--no-verify` 绕过 pre-commit 检查。

---

## 4. 合并规范

### 4.1 PR 流程

1. 从 `develop` 切出 feature 分支。
2. 开发完成并自测通过后，向 `develop` 提交 PR。
3. PR 必须通过 CI（lint、单元测试、覆盖率）。
4. 至少 1 名 Reviewer 批准。
5. 使用 Squash merge 合并，commit message 保留原始 PR 标题。
6. 删除已合并的 feature 分支。

### 4.2 审查要求

| 目标分支 | 最少审查人数 | 附加要求 |
|----------|--------------|----------|
| develop | 1 | 通过 CI |
| main | 2 | 通过 CI + 通过预发布验证 |
| hotfix → main | 2 | 附加 QA 签字 |

### 4.3 合并方式选择

| 方式 | 适用场景 | 是否保留提交历史 |
|------|----------|------------------|
| Squash merge | feature/release/hotfix 合并 | 否（合并为单条） |
| Merge commit | 大特性分支合并到 develop | 是 |
| Rebase merge | 个人分支同步 develop | 是（线性历史） |

---

## 5. 版本管理

### 5.1 语义化版本（SemVer）

版本号格式：`MAJOR.MINOR.PATCH`，例如 `2.4.1`。

| 版本段 | 升级条件 |
|--------|----------|
| MAJOR | 不兼容的 API 变更 |
| MINOR | 向下兼容的新功能 |
| PATCH | 向下兼容的 Bug 修复 |

预发布版本：`2.5.0-alpha.1`、`2.5.0-beta.2`、`2.5.0-rc.1`。

### 5.2 自动化版本管理

使用 `standard-version` 或 `release-please` 自动根据 commit 历史生成版本号和 CHANGELOG：

```bash
# 安装
npm install -g standard-version

# 生成版本
standard-version

# 输出示例
# bump 2.4.0 → 2.5.0
# 生成 CHANGELOG.md
# 创建 commit: chore(release): 2.5.0
# 创建 tag: v2.5.0
```

### 5.3 CHANGELOG 格式

```markdown
# Changelog

## [2.5.0] - 2026-07-31

### Features
- 支持手机号验证码登录
- 订单列表支持多状态过滤

### Bug Fixes
- 修复微信支付回调签名校验问题

### BREAKING CHANGES
- 订单状态枚举新增 refunded，废弃 refunded_failed
```

---

## 6. 发布流程

### 6.1 release 分支流程

1. 从 `develop` 切出 `release/2.5.0`。
2. 在 release 分支上仅允许修复 Bug、更新文档、更新版本号。
3. QA 在 release 分支上进行回归测试。
4. 测试通过后，将 release 合并到 `main` 和 `develop`。
5. 在 `main` 上打 tag `v2.5.0`。
6. CI 自动触发部署到生产环境。

```bash
# 切出 release 分支
git checkout develop
git pull origin develop
git checkout -b release/2.5.0

# 在 release 分支修复问题后
git commit -m "fix(payment): 修复支付回调超时"

# 合并到 main 和 develop
git checkout main
git merge --no-ff release/2.5.0
git tag v2.5.0
git push origin main --tags

git checkout develop
git merge --no-ff release/2.5.0
git push origin develop

# 删除 release 分支
git branch -d release/2.5.0
git push origin --delete release/2.5.0
```

### 6.2 hotfix 流程

1. 从 `main` 切出 `hotfix/PROJ-1290-payment-timeout`。
2. 修复后向 `main` 提交 PR，至少 2 人审查。
3. 合并到 `main` 并打 tag `v2.5.1`。
4. 同步合并到 `develop`，避免下个 release 再次出现该问题。

---

## 7. 常用 Git 命令场景

### 7.1 rebase：保持线性历史

场景：feature 分支落后于 develop，需要同步最新代码。

```bash
git checkout feature/PROJ-1234-user-login
git fetch origin
git rebase origin/develop

# 如有冲突
# 1. 解决冲突
# 2. git add <files>
# 3. git rebase --continue
# 4. 重复直到完成

# 强制推送（仅限 feature 分支，禁止对 main/develop 使用）
git push origin feature/PROJ-1234-user-login --force-with-lease
```

### 7.2 cherry-pick：将特定提交应用到其他分支

场景：hotfix 已合并到 main，需要同步到 develop，但 develop 有未发布功能。

```bash
git checkout develop
git cherry-pick <hotfix-commit-sha>
git push origin develop
```

### 7.3 revert：安全回滚已推送的提交

场景：生产环境发现某次提交导致故障，需要回滚。

```bash
# 推荐：使用 revert，保留历史
git checkout main
git revert <bad-commit-sha>
git push origin main
```

```bash
# 不推荐：reset 会重写历史，影响其他协作者
git reset --hard <previous-commit>
git push origin main --force  # 危险！
```

### 7.4 reset：本地分支重置

场景：本地误提交，尚未推送，需要撤销。

```bash
# 软重置：保留改动在工作区
git reset --soft HEAD~1

# 混合重置（默认）：保留改动，撤销 add
git reset HEAD~1

# 硬重置：彻底丢弃改动（谨慎！）
git reset --hard HEAD~1
```

### 7.5 stash：临时保存工作区

场景：正在开发时需要切换分支处理紧急问题。

```bash
git stash push -m "wip: 用户登录功能"
git checkout hotfix/PROJ-1290-payment-timeout
# 处理完成后切回
git checkout feature/PROJ-1234-user-login
git stash pop
```

---

## 8. .gitignore 最佳实践

### 8.1 通用模板

```gitignore
# ========== 通用 ==========
.DS_Store
Thumbs.db
*.log
*.tmp
*.swp
.idea/
.vscode/
*.code-workspace

# ========== Python ==========
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
build/
develop-eggs/
dist/
downloads/
eggs/
.eggs/
lib/
lib64/
parts/
sdist/
var/
wheels/
*.egg-info/
.installed.cfg
*.egg

# ========== 虚拟环境 ==========
venv/
env/
ENV/
.venv/

# ========== 测试与覆盖率 ==========
.tox/
.coverage
.coverage.*
.cache
nosetests.xml
coverage.xml
*.cover
.pytest_cache/
htmlcov/

# ========== 环境变量与密钥 ==========
.env
.env.local
.env.*.local
*.pem
*.key
secrets/

# ========== 数据库 ==========
*.sqlite3
*.db

# ========== 依赖目录 ==========
node_modules/
```

### 8.2 注意事项

- 敏感文件（`.env`、密钥）必须在项目初始化时就加入 `.gitignore`，避免误提交。
- 若已误提交敏感文件，必须从历史中清除（`git filter-repo` 或 BFG）并轮换密钥。
- 不要忽略 `requirements.txt`、`pyproject.toml` 等依赖声明文件。

---

## 9. 变更记录

| 版本 | 日期 | 变更内容 |
|------|------|----------|
| v1.3 | 2026-07-22 | 新增 cherry-pick 与 stash 场景 |
| v1.2 | 2026-05-10 | 调整 PR 审查人数要求 |
| v1.0 | 2025-11-01 | 首次发布 |
