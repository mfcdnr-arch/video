FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    fonts-dejavu-core \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Celery кладёт /app в sys.path только на время загрузки tasks.py и потом
# убирает, поэтому импорты внутри задач (ds_notify, routers.*) падали с
# ModuleNotFoundError. С PYTHONPATH каталог приложения виден всегда.
ENV PYTHONPATH=/app
COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       gcc \
       python3-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir -r requirements.txt
COPY . .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
