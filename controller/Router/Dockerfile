FROM python:3.12-slim

WORKDIR /app

# Copy application code
COPY . .

# Install dependencies (add to requirements.txt as needed)
RUN pip install --no-cache-dir -r requirements.txt 2>/dev/null || true

# Expose router port
EXPOSE 80

CMD ["python", "main.py"]
