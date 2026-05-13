"""CLI/Docker entrypoint for the controller service."""
import uvicorn

from streambed_controller.main import app

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
