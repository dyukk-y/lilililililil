FROM python:3.11-slim

# Тариф 1 ГБ RAM / 2 vCPU: меньше арен malloc = меньше «пустой» памяти
# процесса; ONNX/OpenCV/OCR не запускают больше потоков, чем ядер.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    MALLOC_ARENA_MAX=2 \
    OMP_NUM_THREADS=2 \
    OPENCV_NUM_THREADS=2 \
    OMP_THREAD_LIMIT=2

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng \
       libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./

# Bothost keeps /app/data between redeploys. SQLite DB, backups and logs live here.
RUN mkdir -p /app/data /app/data/backups

CMD ["python", "main.py"]
