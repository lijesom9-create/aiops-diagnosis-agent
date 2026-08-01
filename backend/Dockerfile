FROM python:3.12-slim

WORKDIR /app

# 安装系统依赖（gcc 编译 C 扩展，curl 健康检查，git 拉取模型）
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# 创建非root用户
RUN groupadd -r appuser && useradd -r -g appuser appuser

# 先复制依赖文件，利用 Docker 缓存层
COPY requirements.txt .
# 使用国内 PyPI 镜像源加速依赖安装
RUN pip install --no-cache-dir -r requirements.txt \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    --trusted-host pypi.tuna.tsinghua.edu.cn \
    --extra-index-url https://download.pytorch.org/whl/cpu

# 复制项目代码
COPY . .

# 设置文件所有权
RUN chown -R appuser:appuser /app

# 创建数据目录
RUN mkdir -p /app/data /app/uploads && chown -R appuser:appuser /app/data /app/uploads

USER appuser

EXPOSE 8000

# 健康检查（docker-compose 也会覆盖，但 Dockerfile 自带更通用）
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=30s \
    CMD curl -f http://localhost:8000/api/health/live || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
