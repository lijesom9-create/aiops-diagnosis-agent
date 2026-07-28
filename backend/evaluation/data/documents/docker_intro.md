# Docker 入门

## 镜像与容器

Docker 镜像是一个只读的模板，包含运行应用所需的代码、运行时、库和配置文件。容器则是镜像的运行实例，可以被创建、启动、停止和删除。

```bash
docker run -d -p 80:80 nginx
```

## Dockerfile 基础指令

Dockerfile 用于定义镜像的构建步骤。常用指令包括：

- `FROM`：指定基础镜像
- `RUN`：执行命令
- `COPY`：复制文件到镜像
- `CMD`：指定容器启动时执行的默认命令
- `EXPOSE`：声明容器暴露的端口

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY . /app
RUN pip install -r requirements.txt
CMD ["python", "main.py"]
```

## 容器网络

Docker 提供多种网络模式，包括 `bridge`、`host` 和 `none`。默认使用 `bridge` 模式，容器可以通过容器名互相访问。
