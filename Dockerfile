FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY templates ./templates
COPY static ./static

ENV WEB_HOST=0.0.0.0 \
    WEB_PORT=7070 \
    CONFIG_PATH=/data \
    PUID=1000 \
    PGID=1000

EXPOSE 7070

CMD gunicorn --bind "0.0.0.0:${PORT:-7070}" --workers 2 "app:create_app()"
