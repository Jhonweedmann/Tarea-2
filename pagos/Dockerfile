FROM python:3.12-slim
WORKDIR /app
COPY pagos/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
# Modulo compartido de observabilidad (logs JSON, X-Correlation-Id, metricas).
COPY comun ./comun
COPY pagos/app ./app
ENV PYTHONPATH=/app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
