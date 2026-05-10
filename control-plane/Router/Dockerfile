FROM python:3.12-slim

WORKDIR /app

COPY control-plane/Router/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY control-plane/Router/ .
COPY frontend/ /app/frontend/

EXPOSE 8090

VOLUME ["/app/data"]

CMD ["python", "main.py"]
