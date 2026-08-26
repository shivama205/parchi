# Cloud Run entry point. Scales to zero (BR-18 / NFR-4).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY parchi ./parchi

# Cloud Run supplies PORT. One worker: the work is I/O bound on Vertex calls,
# and reconciliation is microseconds of pure Python.
ENV PORT=8080
EXPOSE 8080
CMD ["python", "-m", "parchi.serve"]
