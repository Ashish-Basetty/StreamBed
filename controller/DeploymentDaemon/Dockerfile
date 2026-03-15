FROM python:3.12-slim

WORKDIR /app

# Copy only requirements first (to leverage Docker caching)
COPY controller/DeploymentDaemon/requirements.txt .

# Install Python dependencies (only rebuilds if requirements.txt changes)
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared package and daemon code
COPY shared/ /app/shared/
COPY controller/DeploymentDaemon/ .

# Expose daemon API port
EXPOSE 9090

# Note: When running, mount Docker socket for container management:
#   -v /var/run/docker.sock:/var/run/docker.sock
# Optional: persist deployment state across restarts
#   -v streambed-daemon-data:/app/data
VOLUME ["/app/data"]
CMD ["python", "main.py"]
