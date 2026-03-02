FROM python:3.12-slim

WORKDIR /app

# Copy application code (build context is project root when used from docker-compose)
COPY controller/ControllerNode/ .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Expose API port
EXPOSE 8080

# Persist SQLite data
VOLUME ["/app/data"]

CMD ["python", "main.py"]
