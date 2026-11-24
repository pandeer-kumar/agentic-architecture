FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml README.md ./
COPY src ./src
COPY schema ./schema
COPY contracts ./contracts
RUN pip install --no-cache-dir .
# config files are read from the repository layout at runtime
ENV ITSM_REPO_ROOT=/app
EXPOSE 8080
CMD ["sh", "-c", "itsm-agent init-db && itsm-agent serve --port 8080"]
