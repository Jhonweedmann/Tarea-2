FROM python:3.12-slim
WORKDIR /app
COPY bff-web/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY cupos/cupos.proto ./cupos.proto
COPY comun ./comun
COPY bff-web/app ./app
RUN python -m grpc_tools.protoc -I. --python_out=app --grpc_python_out=app cupos.proto && sed -i 's/^import cupos_pb2/from . import cupos_pb2/' app/cupos_pb2_grpc.py
ENV PYTHONPATH=/app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
