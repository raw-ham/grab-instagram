FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium chromium-driver ca-certificates fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY scraper.py .
COPY grab_in_server.py .
COPY test_server.py .
COPY reddit_scraper.py .
COPY test_reddit.py .
RUN mkdir -p /app/output/debug /app/data

# Default command is the CLI scraper; the compose "server" service overrides this
# with uvicorn to run the REST API instead.
ENTRYPOINT ["python", "-u", "scraper.py"]
