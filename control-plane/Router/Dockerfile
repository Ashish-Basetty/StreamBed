FROM python:3.12-slim

WORKDIR /app

COPY controller/Router/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY controller/Router/ .
COPY frontend/ /app/frontend/

EXPOSE 8090

VOLUME ["/app/data"]

CMD ["python", "main.py"]
