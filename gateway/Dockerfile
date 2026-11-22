FROM python:3.12-slim
WORKDIR /app
COPY gateway/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY comun ./comun
COPY gateway/app ./app
ENV PYTHONPATH=/app
# 8080: API pública. 9100: métricas para Prometheus (solo red interna).
EXPOSE 8080 9100
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
