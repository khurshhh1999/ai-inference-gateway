FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app

# Default: mock-only deps (local/CI). For cloud providers:
#   docker build --build-arg INSTALL_EXTRAS='[bedrock]' ...
#   docker build --build-arg INSTALL_EXTRAS='[vertex]' ...
#   docker build --build-arg INSTALL_EXTRAS='[cloud]' ...
ARG INSTALL_EXTRAS=""
RUN pip install --no-cache-dir ".${INSTALL_EXTRAS}"

ENV PORT=8081
EXPOSE 8081

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
