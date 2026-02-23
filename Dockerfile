FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY templates ./templates
COPY static ./static

ENV WEB_HOST=0.0.0.0 \
    WEB_PORT=7070 \
    CONFIG_PATH=/config \
    PUID=1000 \
    PGID=1000

VOLUME ["/config"]
EXPOSE 7070

CMD ["python", "app.py"]
