# SPDX-License-Identifier: AGPL-3.0-only
FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    SEQUENCE_REVIEW_BIND=0.0.0.0 HF_HOME=/models
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
ARG TORCH_INDEX=cpu
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/${TORCH_INDEX} torch==2.8.0 \
    && pip install --no-cache-dir -r requirements.txt
COPY app.py catalog.py sequencing.py index.html review.html Dockerfile compose.yaml compose.gpu.yaml README.md LICENSE .dockerignore .gitignore start.ps1 ./
EXPOSE 8765
CMD ["python","app.py"]
