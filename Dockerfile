# One image for every agent; OPSRELAY_ROLE picks which one the container serves.
# AgentCore Runtime runs linux/arm64 containers: build with --platform linux/arm64
# (the CDK stack does this for you).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY opsrelay ./opsrelay
RUN pip install . \
    && useradd --create-home --uid 1000 agent \
    && chown -R agent /app
USER agent

# 8080: coordinator (AgentCore HTTP protocol). 9000: specialists (AgentCore A2A protocol).
EXPOSE 8080 9000
CMD ["python", "-m", "opsrelay.runtime"]
