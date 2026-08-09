# =============================================================================
# playbook-ml -- ML training and batch inference
# Full ML stack: XGBoost, scikit-learn, pandas, SHAP, Optuna. ~900 MB.
# Used by Airflow KubernetesExecutor for training and inference tasks.
# =============================================================================
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends libgomp1 && \
    rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 app && \
    useradd --uid 1000 --gid app --shell /bin/bash --create-home app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /home/app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra ml

COPY app/ app/
COPY airflow/ airflow/
COPY scripts/ scripts/

USER app
