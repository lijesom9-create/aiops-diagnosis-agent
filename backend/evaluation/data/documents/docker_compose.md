# Docker Compose 多容器编排

## 什么是 Docker Compose

Docker Compose 是用于定义和运行多容器 Docker 应用程序的工具。通过一个 YAML 文件配置应用程序的所有服务，然后使用一条命令创建并启动所有服务。

## 基本命令

| 命令 | 作用 | 示例 |
|------|------|------|
| up | 创建并启动容器 | docker-compose up -d |
| down | 停止并删除容器 | docker-compose down |
| build | 重新构建镜像 | docker-compose build |
| logs | 查看日志 | docker-compose logs -f web |
| ps | 列出容器 | docker-compose ps |

## docker-compose.yml 示例

```yaml
version: '3.8'
services:
  web:
    image: nginx:alpine
    ports:
      - "80:80"
    depends_on:
      - api
    restart: always

  api:
    build: ./api
    environment:
      - DB_HOST=postgres
      - REDIS_URL=redis://redis:6379
    depends_on:
      - postgres
      - redis

  postgres:
    image: postgres:15
    environment:
      POSTGRES_PASSWORD: secret
    volumes:
      - pgdata:/var/lib/postgresql/data

  redis:
    image: redis:alpine
    volumes:
      - redisdata:/data

volumes:
  pgdata:
  redisdata:
```

## 服务依赖管理

`depends_on` 只控制启动顺序，不等待服务就绪。需要配合 `healthcheck` 使用：

```yaml
postgres:
  image: postgres:15
  healthcheck:
    test: ["CMD-SHELL", "pg_isready -U postgres"]
    interval: 10s
    timeout: 5s
    retries: 5
```

## 网络配置

默认情况下 Compose 会为项目创建一个网络。也可以自定义网络：

| 网络模式 | 说明 | 使用场景 |
|----------|------|----------|
| bridge | 默认桥接网络 | 单机多容器通信 |
| host | 使用宿主机网络 | 需要最高网络性能 |
| none | 无网络 | 离线计算 |
| overlay | 跨主机覆盖网络 | Docker Swarm 集群 |
