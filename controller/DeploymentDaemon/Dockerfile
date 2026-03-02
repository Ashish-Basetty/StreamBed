FROM python:3.12-slim

WORKDIR /app

# Copy application code (build context is project root when used from docker-compose)
COPY controller/DeploymentDaemon/ .

# Install dependencies (add to requirements.txt as needed)
RUN pip install --no-cache-dir -r requirements.txt

# Expose daemon API port
EXPOSE 9090

# Note: When running, mount Docker socket for container management:
#   -v /var/run/docker.sock:/var/run/docker.sock
# Optional: persist deployment state across restarts
#   -v streambed-daemon-data:/app/data
VOLUME ["/app/data"]
CMD ["python", "main.py"]
