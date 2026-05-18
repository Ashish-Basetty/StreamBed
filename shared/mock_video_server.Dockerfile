# Minimal image for the mock video TCP server.
# Used by the GCP test stack (infra/gcp/docker-compose.controller.yml).
# Kept separate from the daemon image so the daemon doesn't have to carry
# numpy.
FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir numpy

COPY shared/ /app/shared/

EXPOSE 9200
CMD ["python", "-m", "shared.mock_video_server", "--host", "0.0.0.0", "--port", "9200"]
