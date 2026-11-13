FROM python:3.13-slim@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb

ENV PYTHONUNBUFFERED=1 UV_NO_PROGRESS=1
WORKDIR /app/backend
RUN pip install --no-cache-dir uv==0.11.23
COPY backend/pyproject.toml backend/uv.lock ./
ARG INSTALL_HYBRID=0
ARG INSTALL_POSTGRES=0
RUN if [ "$INSTALL_HYBRID" = "1" ]; then \
      uv sync --locked --no-dev --no-install-project --extra postgres --extra semantic; \
    elif [ "$INSTALL_POSTGRES" = "1" ]; then \
      uv sync --locked --no-dev --no-install-project --extra postgres; \
    else \
      uv sync --locked --no-dev --no-install-project; \
    fi
COPY backend/app ./app
COPY backend/evaluate_capstone.py ./evaluate_capstone.py
COPY data /app/data
COPY frontend /app/frontend
ENV TAWZEEF_DATABASE=/data/tawzeefai.sqlite3
EXPOSE 8000
CMD ["/app/backend/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
