# Multi-stage build. See docs/operations/DEPLOYMENT.md.
FROM python:3.12-slim AS build
WORKDIR /build
# Dependencies come from the pinned lock (audited in CI); the package itself is then
# installed without re-resolving so the image matches what pip-audit and the SBOM saw.
COPY requirements.lock pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.lock \
 && pip install --no-cache-dir --no-deps .

FROM python:3.12-slim
# Run as a non-root user.
RUN useradd --create-home --uid 10001 protec
WORKDIR /srv
COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /usr/local/bin /usr/local/bin
COPY app ./app
COPY knowledge ./knowledge
USER protec
EXPOSE 8000
# Liveness only: readiness is the orchestrator's probe (docs/operations/DEPLOYMENT.md §3).
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/live', timeout=3).status==200 else 1)"
# No secrets, no .env, no tests and no evals in the runtime image.
# Configuration is injected at runtime; the app refuses unsafe production settings.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
