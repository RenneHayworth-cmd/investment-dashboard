FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Shanghai
COPY web/backend/requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r requirements.lock && useradd --uid 1000 --create-home app
COPY core /app/core
COPY services /app/services
COPY config /app/config
COPY web/backend /app/web/backend
USER app
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "web.backend.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
