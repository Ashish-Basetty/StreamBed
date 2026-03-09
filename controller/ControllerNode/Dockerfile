FROM python:3.12-slim

WORKDIR /app

# Copy only requirements first (faster caching)
COPY controller/ControllerNode/requirements.txt .

# Install dependencies (only reruns if requirements.txt changes)
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code (build context is project root when used from docker-compose)
COPY shared/ /app/shared/
COPY controller/ControllerNode/ .

# Expose API port
EXPOSE 8080

# Persist SQLite data
VOLUME ["/app/data"]

CMD ["python", "main.py"]
