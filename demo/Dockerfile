# grid-quorum live demo: the gridq package plus cryptography (Ed25519 signatures).
# Built by demo/docker-compose.yml with the repository root as context.
FROM python:3.10-slim
RUN pip install --no-cache-dir "cryptography>=42"
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY gridq /app/gridq
EXPOSE 7000/udp 8000/tcp 8080/tcp
