FROM python:3.12-slim AS runtime

ARG ALBERT_INSTALL_SEMANTIC=false

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --system albert && useradd --system --gid albert --home /app albert
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
RUN if [ "$ALBERT_INSTALL_SEMANTIC" = "true" ]; then \
      pip install '.[semantic]'; \
    else \
      pip install .; \
    fi

USER albert
EXPOSE 8080 8081
CMD ["uvicorn", "albert.api:app", "--host", "0.0.0.0", "--port", "8080"]
