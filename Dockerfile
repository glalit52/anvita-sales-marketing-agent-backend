FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

FROM base AS build
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --prefix=/install .

FROM base AS runtime
RUN useradd --create-home --uid 10001 anvita
COPY --from=build /install /usr/local
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY scripts ./scripts
USER anvita
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status == 200 else 1)"
# The same image runs the API (default), the worker (`python -m app.worker`) and migrations (`alembic upgrade head`).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
