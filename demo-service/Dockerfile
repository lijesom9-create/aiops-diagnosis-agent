FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py db.py faults.py ./

RUN useradd -m appuser && mkdir -p /data && chown appuser /data
USER appuser

EXPOSE 8000

# 健康检查用 python urllib（slim 镜像无 curl）；探活要碰存储（真实探活）
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz', timeout=3)" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
