FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CLOUD_ENV=true \
    PORT=8000

WORKDIR /app

# Install dependencies first for Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY config.py google_tasks_service.py gemini_service.py whatsapp_service.py telegram_service.py task_resolver.py scheduler_service.py main.py ./

EXPOSE 8000

# Must run with 1 worker so Telegram polling / 24x7 scheduler run as a single instance
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]

