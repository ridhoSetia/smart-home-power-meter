# ==============================================================================
# SMART POWER METER IoT - DOCKERFILE
# Multi-purpose, lightweight, production-ready container image
# ==============================================================================
FROM python:3.11-slim

# Mencegah pembuatan file .pyc dan memastikan log Python dialirkan langsung tanpa buffering
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=7080

# Buat grup dan pengguna non-root dengan UID 1000 (sesuai standar Linux host)
RUN groupadd -g 1000 appuser && \
    useradd -u 1000 -g appuser -m -s /bin/bash appuser

# Set working directory
WORKDIR /app

# Cache layer dependensi: copy requirements terlebih dahulu
COPY requirements.txt .

# Install dependensi Python tanpa cache untuk menghemat ruang image
RUN pip install --no-cache-dir -r requirements.txt

RUN mkdir -p /app/data && chown -R appuser:appuser /app && chmod -R 777 /app/data

# Salin source code aplikasi
COPY --chown=appuser:appuser main.py recommender_engine.py ./
COPY --chown=appuser:appuser templates/ ./templates/

# Menghindari masalah izin tulis (permission denied) pada bind-mount folder host dengan UID berbeda
# USER appuser

# Port default aplikasi
EXPOSE 7080

# Healthcheck menggunakan modul bawaan Python (tanpa perlu paket eksternal seperti curl)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:7080/docs')" || exit 1

# Perintah default menjalankan server FastAPI dengan Uvicorn
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7080"]

