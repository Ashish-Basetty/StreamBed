FROM python:3.12-slim

WORKDIR /app
COPY tests/varying_proxy/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY tests/varying_proxy/proxy.py .
CMD ["python", "proxy.py"]
