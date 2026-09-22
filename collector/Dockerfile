FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Слушает внутри контейнера все адреса, наружу порт публикуется только на
# 127.0.0.1 — см. docker-compose.yml. Один воркер: нагрузка здесь — это
# десятки пачек в минуту, а общий пул соединений к базе проще держать в
# одном процессе.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
