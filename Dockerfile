# Playwright's own image already carries Chromium and every system library it
# needs. Installing browsers into a plain python image works too, but pulls
# ~400 MB of apt dependencies that are easy to get subtly wrong.
FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY hotwheels/ ./hotwheels/
COPY web/ ./web/
COPY cli.py config.yaml ./

# The database and the saved browser profile are state, not image content -
# mount them so a rebuild never discards scraped prices or a set address.
VOLUME ["/app/data"]
ENV HOTWHEELS_DB=/app/data/hotwheels.db

EXPOSE 8000

# Serve by default; run scrapes with:
#   docker compose run --rm dashboard python cli.py scrape
CMD ["python", "-m", "uvicorn", "hotwheels.server:app", \
     "--host", "0.0.0.0", "--port", "8000"]
