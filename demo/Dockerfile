# Image for the two demo helpers: the mock upstreams and the one-shot seeder.
# Build context is the repo root so the seeder can ship Hub's own SQL schemas.
FROM python:3.12-slim
WORKDIR /demo
RUN pip install --no-cache-dir "fastapi>=0.111" "uvicorn>=0.30" "asyncpg>=0.29" "httpx>=0.27"
COPY demo/catalog.py ./catalog.py
COPY demo/upstreams/ ./upstreams/
COPY demo/seed/ ./seed/
COPY hub/backend/sql/ ./sql/
ENV HUB_SQL_DIR=/demo/sql PYTHONUNBUFFERED=1
