# CPU-only. GPU в образ не ставится: сервер без видеокарты, ~5 ГБ свободной RAM.
# Веса модели в образ не запекаются — их скачивает первый /transcribe в /models.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    WHISPER_MODEL=large-v3-turbo \
    WHISPER_COMPUTE_TYPE=int8 \
    WHISPER_DEVICE=cpu \
    WHISPER_CPU_THREADS=8 \
    WHISPER_CACHE_DIR=/models \
    DATA_DIR=/data \
    HF_HUB_CACHE=/models \
    HF_HUB_DISABLE_TELEMETRY=1 \
    OMP_NUM_THREADS=8 \
    WHISPER_IDLE_EXIT_SEC=900

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

EXPOSE 8080
VOLUME ["/models", "/data"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
