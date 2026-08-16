# Macro Scenario Engine — immagine unica (backend + dashboard server-side).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dipendenze prima del codice: il layer resta in cache fra le build.
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend /app/backend

# Il database SQLite e la cache vivono qui (montati come volume in compose).
RUN mkdir -p /app/data/cache /app/data/mt5

ENV MSE_DB_PATH=/app/data/macro_scenario.db \
    MSE_CACHE_DIR=/app/data/cache \
    MSE_MT5_FILES_DIR=/app/data/mt5 \
    MSE_HOST=0.0.0.0 \
    MSE_PORT=8000

WORKDIR /app/backend
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
