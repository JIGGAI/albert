FROM python:3.12.15-slim-bookworm AS runtime

ARG ALBERT_INSTALL_SEMANTIC=false

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --system albert && useradd --system --gid albert --home /app albert
WORKDIR /app

COPY pyproject.toml constraints.txt README.md ./
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
RUN if [ "$ALBERT_INSTALL_SEMANTIC" = "true" ]; then \
      pip install --constraint constraints.txt '.[semantic]'; \
    else \
      pip install --constraint constraints.txt .; \
    fi

# Named volumes mount root-owned unless the mount point already exists with the
# right owner; the embedding model cache must be writable by the service user.
RUN mkdir -p /var/cache/albert-models && chown albert:albert /var/cache/albert-models
# The default home of a ChatGPT sign-in (ALBERT_CHATGPT_CREDENTIALS_FILE), shared
# by the API and worker through a volume mounted here.
RUN mkdir -p /var/lib/albert/secrets && chown albert:albert /var/lib/albert/secrets \
    && chmod 700 /var/lib/albert/secrets
USER albert
EXPOSE 8080 8081
CMD ["uvicorn", "albert.api:app", "--host", "0.0.0.0", "--port", "8080"]
