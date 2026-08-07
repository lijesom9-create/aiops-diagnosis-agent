FROM python:3.12-slim

WORKDIR /app

# 安装系统依赖（gcc 编译 C 扩展，curl 健康检查，git 拉取模型，poppler-utils 供 pdf2image OCR）
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    curl \
    git \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# 创建非root用户
RUN groupadd -r appuser && useradd -r -g appuser appuser

# 先复制依赖文件，利用 Docker 缓存层
COPY requirements.txt .
# torch 的 +cpu wheel 只在官方 index 提供，但官方 CDN（download-r2.pytorch.org）在国内下载
# 极慢且中途频繁断流（pip 重试又从头下，反复失败）。已在宿主机从交大镜像
# （mirror.sjtu.edu.cn/pytorch-wheels）预下载 wheel 放入 .docker/torch/，构建时本地安装，
# 完全不依赖官方 CDN；torch 的运行时依赖（numpy/sympy 等）走清华镜像。
# +cpu wheel 的 Requires-Dist 不含 cuda/nvidia 包（已核验），可放心直接安装。
# torchvision 也必须装 +cpu 版：docling_ibm_models 会拉非 +cpu 的 torchvision 0.28.0，
# 与 torch 2.13.0+cpu ABI 不匹配会导致 transformers 的 PreTrainedModel 导入崩溃
# （连带 sentence-transformers / optimum-onnx 全挂）。同样用预下载的 +cpu wheel 本地安装。
COPY .docker/torch/torch-*.whl .docker/torch/torchvision-*.whl /tmp/
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install /tmp/torch-2.13.0+cpu-*.whl /tmp/torchvision-0.28.0+cpu-*.whl \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    --trusted-host pypi.tuna.tsinghua.edu.cn

# 其余依赖只走清华镜像；torch 已由上一层装好且满足 requirements 的 pin，此处不会重下。
# 注意：不设 --no-cache-dir，让 pip 缓存写入 cache mount，改依赖重建时复用已下载 wheel
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --retries 5 --timeout 30 -r requirements.txt \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    --trusted-host pypi.tuna.tsinghua.edu.cn

# 复制项目代码（--chown 在复制时设置权限，省去单独 RUN chown 层）
COPY --chown=appuser:appuser . .

# 创建数据目录
RUN mkdir -p /app/data /app/uploads && chown -R appuser:appuser /app/data /app/uploads

USER appuser

EXPOSE 8000

# 健康检查（docker-compose 也会覆盖，但 Dockerfile 自带更通用）
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=30s \
    CMD curl -f http://localhost:8000/api/health/live || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
