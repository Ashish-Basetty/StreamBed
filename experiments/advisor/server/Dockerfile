# Advisor server inference container.
# Build from project root:
#
#   docker build -f experiments/advisor/server/Dockerfile \
#                -t ashishbasetty/streambed-advisor-server:latest .
#
# Listens on UDP FEED_LISTEN_PORT (default 9000) for incoming features and
# sends advice to SIDECAR_HOST:SIDECAR_REVERSE_PORT — both set by the
# deployment daemon when /deploy launches the container.
#
# Container layout mirrors the repo so module-style imports resolve correctly.
# See edge/Dockerfile for the mirroring rationale.
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*

COPY experiments/advisor/server/requirements.txt /tmp/req.txt
# Torch CPU wheel pinned to a version compatible with SB3 2.8+. Wheel index
# omitted so pip falls back to PyPI (the CPU index doesn't carry every
# 2.x point release); the size penalty is acceptable for an inference
# container that runs short rollouts.
RUN pip install --no-cache-dir \
      --index-url https://download.pytorch.org/whl/cpu \
      "torch>=2.4,<3" "torchvision>=0.19,<1" && \
    pip install --no-cache-dir -r /tmp/req.txt

COPY shared/ /app/shared/
COPY experiments/advisor/advisorlib/ /app/experiments/advisor/advisorlib/
COPY experiments/advisor/bench/      /app/experiments/advisor/bench/
COPY experiments/advisor/server/     /app/experiments/advisor/server/
COPY experiments/__init__.py         /app/experiments/__init__.py
COPY experiments/advisor/__init__.py /app/experiments/advisor/__init__.py

# Server only needs the teacher; it runs teacher.policy.mlp_extractor +
# action_net on incoming features to produce advice.
COPY experiments/advisor/checkpoints/teacher.zip /app/experiments/advisor/checkpoints/teacher.zip

ENV TEACHER_PATH=/app/experiments/advisor/checkpoints/teacher.zip \
    FEED_LISTEN_PORT=9000 \
    PYTHONPATH=/app

EXPOSE 9000/udp

CMD ["python", "-u", "-m", "experiments.advisor.server.advisor_server"]
