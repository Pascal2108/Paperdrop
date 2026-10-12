FROM python:3.12-slim

LABEL org.opencontainers.image.title="Paperdrop" \
      org.opencontainers.image.description="Self-hosted web page that compresses PDFs and photos and sends them to Paperless-ngx after review." \
      org.opencontainers.image.source="https://github.com/Pascal2108/paperdrop" \
      org.opencontainers.image.licenses="AGPL-3.0-only"

RUN apt-get update \
 && apt-get install -y --no-install-recommends ghostscript \
 && rm -rf /var/lib/apt/lists/*

# Log lines appear in `docker logs` / Portainer immediately
ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py LICENSE ./
COPY templates/ templates/
COPY static/ static/

RUN useradd -r -u 1000 app
USER app

EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["gunicorn", "-b", "0.0.0.0:8000", "-w", "1", "--threads", "4", "--timeout", "600", "app:app"]
