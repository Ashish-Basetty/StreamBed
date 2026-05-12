# Advisor edge inference container.
# Build from project root so the StreamBed shared/ package and the
# advisor experiments/ tree (with checkpoints) are inside the build context.
#
#   docker build -f experiments/advisor/edge/Dockerfile \
#                -t ashishbasetty/streambed-advisor-edge:latest .
#
# Listens on TCP 9100 for frame_gen and UDP 9102 for advice from the
# StreamBed sidecar. Outbound features go to SIDECAR_HOST:SIDECAR_FEED_PORT
# (set by the deployment daemon at /deploy time).
#
# Container layout mirrors the repo so module-style imports resolve correctly:
#   /app/                          ← _REPO_ROOT (parents[3])
#   /app/shared/...
#   /app/experiments/advisor/      ← _ADVISOR_ROOT (parents[1])
#   /app/experiments/advisor/edge/edge_inference.py
FROM python:3.11-slim

WORKDIR /app

# OpenCV needs libgl/libglib at runtime (crafter's render path can pull cv2).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*

# Install torch CPU first, then the rest. Cached separately so source
# changes don't redownload torch wheels.
COPY experiments/advisor/edge/requirements.txt /tmp/req.txt
# Torch CPU wheel pinned to a version compatible with SB3 2.8+. Wheel index
# omitted so pip falls back to PyPI (the CPU index doesn't carry every
# 2.x point release); the size penalty is acceptable for an inference
# container that runs short rollouts.
RUN pip install --no-cache-dir \
      --index-url https://download.pytorch.org/whl/cpu \
      "torch>=2.4,<3" "torchvision>=0.19,<1" && \
    pip install --no-cache-dir -r /tmp/req.txt

# StreamBed shared interfaces (sender/receiver, stream framing, chunk codec).
COPY shared/ /app/shared/

# Advisor library + bench module + edge inference script.
COPY experiments/advisor/advisorlib/ /app/experiments/advisor/advisorlib/
COPY experiments/advisor/bench/      /app/experiments/advisor/bench/
COPY experiments/advisor/edge/       /app/experiments/advisor/edge/
COPY experiments/__init__.py         /app/experiments/__init__.py
COPY experiments/advisor/__init__.py /app/experiments/advisor/__init__.py

# Bake checkpoints into the image so /deploy doesn't need a shared volume.
COPY experiments/advisor/checkpoints/teacher.zip          /app/experiments/advisor/checkpoints/teacher.zip
COPY experiments/advisor/checkpoints/students/shared_h4.pt /app/experiments/advisor/checkpoints/students/shared_h4.pt

ENV TEACHER_PATH=/app/experiments/advisor/checkpoints/teacher.zip \
    STUDENT_PATH=/app/experiments/advisor/checkpoints/students/shared_h4.pt \
    EDGE_TCP_PORT=9100 \
    ADVICE_LISTEN_PORT=9102 \
    PYTHONPATH=/app

EXPOSE 9100/tcp 9102/udp

CMD ["python", "-u", "-m", "experiments.advisor.edge.edge_inference"]
