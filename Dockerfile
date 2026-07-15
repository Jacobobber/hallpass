# hallpass container image. Multi-stage: the build stage compiles the wheel and
# its dependencies; the runtime stage carries only the installed packages, runs
# as a non-root user, and ships no build toolchain. Secrets (HALLPASS_VAULT_KEY,
# HALLPASS_DATABASE_URL, HALLPASS_REDIS_URL) are NEVER baked in -- they arrive at
# runtime as env / mounted secrets. Pin the base by digest in your registry for
# a reproducible build; a patch tag is pinned here so the public example builds.
FROM python:3.12-slim-bookworm AS build
WORKDIR /src
# Install into an isolated prefix so the runtime stage copies just the result.
COPY . .
RUN pip install --no-cache-dir --prefix=/install ".[postgres,redis,mcp,http]"

FROM python:3.12-slim-bookworm
# A non-root, non-privileged user; nothing in the image needs root.
RUN useradd --create-home --uid 10001 hallpass
COPY --from=build /install /usr/local
USER hallpass
# 0.0.0.0 so the load balancer can reach the server inside the container (the
# library default is 127.0.0.1, which would be unreachable from outside the pod).
ENV HALLPASS_HOST=0.0.0.0 \
    HALLPASS_PORT=8000 \
    PYTHONUNBUFFERED=1
EXPOSE 8000
# Liveness, not readiness: /healthz is a static process-up check, so a database
# blip does not flap the container's health (readiness/LB draining is /readyz).
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status==200 else 1)"]
ENTRYPOINT ["hallpass"]
CMD ["serve"]
