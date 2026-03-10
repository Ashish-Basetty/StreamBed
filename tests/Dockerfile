# run these on terminal to avoid local env mismatch
# docker build -f tests/Dockerfile -t streambed-tests .
# docker run --rm -v $(pwd):/app streambed-tests python -m pytest tests/ -v -s

# tests/Dockerfile

# takes edge env as base and runs tests with this env
FROM ashishbasetty/streambed-edge:latest

WORKDIR /app
RUN pip install --no-cache-dir pytest numpy

# mount the workspace at runtime, or copy it if you prefer
CMD ["pytest","-q","tests"]