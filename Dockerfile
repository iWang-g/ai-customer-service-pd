FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY qqbot ./qqbot
RUN pip install --no-cache-dir . && useradd --create-home bot && mkdir /app/data && chown bot:bot /app/data
USER bot
EXPOSE 8000
CMD ["uvicorn", "qqbot.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
