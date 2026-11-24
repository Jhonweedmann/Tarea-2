FROM python:3.12-slim
WORKDIR /app
COPY inscripcion/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY inscripcion/app ./app
COPY inscripcion/openapi.yaml ./openapi.yaml
ENV PYTHONPATH=/app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
