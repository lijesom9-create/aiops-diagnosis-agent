# 部署与 CI/CD 全流程总结

> 本文档记录 education-agent（RAG+LangGraph 运维诊断系统）从"本地跑"到"公网访问 + 自动部署"的完整过程，供日后学习与操作参考。

---

## 一、总体架构

```
浏览器 → https://mao91.xyz → Cloudflare 隧道 → VM(192.168.88.131) nginx
                                   │
            ┌──────────────────────┼──────────────────────┐
            │                      │                      │
       rag-src-frontend      rag-src-backend          rag-src-qdrant / mongodb
       (ghcr 镜像 62MB)      (本地构建 3.5GB)          (docker 数据卷)
```

- **域名**：`mao91.xyz`（Dynadot 注册）
- **CDN/隧道**：Cloudflare（NS 由 Dynadot 指向 Cloudflare）
- **公网入口**：Cloudflare 命名隧道（token 方式），路由 `mao91.xyz → http://frontend:80`
- **VM**：Ubuntu 虚拟机 `192.168.88.131`，用户 `jesom`，部署目录 `/home/jesom/rag-src`

---

## 二、涉及的关键文件

| 文件 | 作用 |
|---|---|
| `docker-compose.linux.yml` | Linux 精简部署定义（backend+frontend+qdrant+mongodb，无 Redis/Celery/监控） |
| `docker-compose.linux.prod.yml` | 生产 override：frontend 改从 ghcr 拉镜像，backend 保持本地构建 |
| `backend/Dockerfile` | 后端镜像（含 APT_MIRROR 清华源切换 + 预下载 torch wheel） |
| `backend/Dockerfile.ci` | CI 专用构建（官方 PyTorch + 清华源，不依赖本地缓存） |
| `.github/workflows/ci.yml` | CI：构建 3 个镜像 → 推送 ghcr.io（latest + sha tag） |
| `.github/workflows/deploy.yml` | CD：VM self-hosted runner 自动拉码/拉镜像/重建/重启 |
| `.gitignore` | `/logs/`（注意：根目录用 `/` 前缀避免误伤 `admin-frontend/src/components/Logs/`） |

---

## 三、域名与公网访问（一次性配置）

### 1. Nameserver 切换
Dynadot 控制台把 NS 从默认（`ns1.dyna-ns.net`）改为 Cloudflare：
```
emerie.ns.cloudflare.com
kianchau.ns.cloudflare.com
```
生效验证：`dig mao91.xyz NS +short`（或 RDAP）。

### 2. Cloudflare 添加域名
- Cloudflare → 添加站点 `mao91.xyz`（免费版）
- 等待 DNS 迁移完成后域名状态变 **Active**

### 3. 命名隧道（替换临时 tunnel）
在 Cloudflare Zero Trust → Access → Tunnels 创建命名隧道，拿到 token，在 VM 上跑：
```bash
docker run -d --name cf-tunnel --restart unless-stopped \
  cloudflare/cloudflared:latest tunnel --no-autoupdate run --token <TOKEN>
# 加入应用网络，让隧道能访问前端容器
docker network connect rag-src_default cf-tunnel
```
> 重要：隧道路由地址必须是 **`http://frontend:80`**（容器服务名），而不是 `localhost:3000`——后者指向隧道容器自身，会 502。

---

## 四、CI/CD 流水线

### 触发链路
```
push master
  ├─ CI (.github/workflows/ci.yml)
  │    ├─ frontend build → ghcr.io/lijesom9-create/agent-frontend:latest
  │    ├─ admin-frontend build → ghcr.io/lijesom9-create/agent-admin-frontend:latest
  │    └─ backend build → ghcr.io/lijesom9-create/agent-backend:latest
  └─ CD (.github/workflows/deploy.yml, runs-on [self-hosted, agent-vm])
       ├─ 1. docker login ghcr (用 GITHUB_TOKEN)
       ├─ 2. git fetch + reset --hard origin/master   ← VM 拉最新源码
       ├─ 3. pull frontend（ghcr，62MB，秒级）
       ├─ 4. build backend（本地，清华源 + layer cache）
       └─ 5. up -d backend frontend
```

### 为什么 backend 不走 ghcr？
VM 访问 ghcr 慢，backend 镜像含 3.5GB torch 层，从 ghcr 拉取会断流失败（实测 70 分钟失败）。
所以：
- **frontend**（62MB 小镜像）→ ghcr 拉取
- **backend** → VM 本地构建（源码经 git 同步，build 时命中 layer cache）

### VM 端 runner 配置
- runner 目录：`/home/jesom/actions-runner/`
- systemd 服务：`actions.runner.lijesom9-create-RAG.agent-vm.service`（User=jesom，Restart=always）
- label：`self-hosted`、`agent-vm`（deploy.yml 用 `runs-on: [self-hosted, agent-vm]`）
- VM `rag-src` 是 git 仓库，origin 指向 `git@github.com:lijesom9-create/RAG.git`，用 SSH deploy key 免密拉取

---

## 五、日常开发/发布操作（必看）

### 完整流程
```bash
# 1) Windows 本地改码 + 提交
git add . && git commit -m "feat: xxx"

# 2) 打包传 VM（Windows 直连 GitHub push 不稳定，走 bundle 中转）
git bundle create rag-upd.bundle master
scp rag-upd.bundle jesom@192.168.88.131:/home/jesom/

# 3) VM 上接收 + 推送（推送会触发 CD 自动部署）
ssh jesom@192.168.88.131
cd /home/jesom/rag-src
git fetch /home/jesom/rag-upd.bundle master
git reset --hard FETCH_HEAD
git push origin master      # origin 带 token，直接 push

# 4) 等 CD 完成后验证
curl https://mao91.xyz/api/health/live        # {"status":"ok"}
curl -o /dev/null -w "%{http_code}" https://mao91.xyz   # 200
```

### 验证 RAG 全链路（VM 内）
```bash
ssh jesom@192.168.88.131 "bash /home/jesom/test_chat.sh"
```
返回 token_len + 知识库引用即正常。

---

## 六、遇到的坑与解决办法（重点）

| 问题 | 现象 | 解决 |
|---|---|---|
| **502 网关错误** | 公网访问报错 | 隧道路由改 `http://frontend:80`（原来写 `localhost:3000`） |
| **backend 镜像拉取失败** | ghcr 下载 742MB torch 层 70 分钟失败 | backend 改 VM 本地构建，只从 ghcr 拉 frontend |
| **docker build 卡死 30 分钟+** | apt 访问 deb.debian.org 极慢 | Dockerfile 加 `ARG APT_MIRROR=1`，sed 切清华镜像；compose build 传 `APT_MIRROR=1` |
| **deploy 时 checkout 失败** | VM clone github 超时 | deploy.yml 不用 checkout，直接 `cd /home/jesom/rag-src` 操作 |
| **CI 前端构建失败** | `admin-frontend/src/components/Logs/` 文件缺失 | `.gitignore` 的 `logs/` 改成 `/logs/`（根目录锚定） |
| **CI 后端测试失败** | fastapi TestClient 报错 | ci.yml 依赖对齐 requirements.txt（fastapi 0.115.12 / httpx 0.28.1 等）+ 补 `requests` |
| **VM git 访问 GitHub 慢** | push 超时 | VM push 用带 token 的 https origin；拉取用 SSH deploy key |
| **windows push 不通** | GitHub 直连失败 | 走 `git bundle` → scp → VM 中转 |

---

## 七、数据与密钥说明

- `backend/.env`（含 AI_API_KEY 等）→ 在 `.gitignore`，不会进 git；VM 上手动维护 `/home/jesom/rag-src/backend/.env`
- 持久数据卷：
  - `qdrant-data` → Qdrant 向量库
  - `mongodb-data` → MongoDB
  - `backend/data`（挂载）→ 模型缓存 hf_cache 等
- 模型下载走 `HF_ENDPOINT=https://hf-mirror.com`（国内镜像）

---

## 八、常用运维命令

```bash
# 查看容器状态
docker ps
docker compose -f docker-compose.linux.yml -f docker-compose.linux.prod.yml ps

# 手动重新部署（VM 上）
cd /home/jesom/rag-src
docker compose -f docker-compose.linux.yml -f docker-compose.linux.prod.yml pull frontend
docker compose -f docker-compose.linux.yml -f docker-compose.linux.prod.yml build backend
docker compose -f docker-compose.linux.yml -f docker-compose.linux.prod.yml up -d backend frontend

# 查看日志
docker compose -f docker-compose.linux.yml logs -f backend

# 导入知识库种子数据
docker compose -f docker-compose.linux.yml exec backend python scripts/seed_ops_kb.py

# runner 服务管理（VM 上）
systemctl status actions.runner.lijesom9-create-RAG.agent-vm.service

# 查看 GitHub Actions 运行状态
gh run list --repo lijesom9-create/RAG --limit 5
gh run view <run_id> --repo lijesom9-create/RAG
```

---

## 九、当前状态（2026-08-09）

- 部署流水线全绿：CI（构建+推送 ghcr）✅、CD（VM 自动部署）✅
- 公网访问：`https://mao91.xyz` → 200，health ok，RAG 问答引用真实知识库
- 仓库 master = `e86be00`（APT_MIRROR 修复）
- VM 容器：backend(healthy) / frontend / qdrant / mongodb / cf-tunnel 全部运行中
