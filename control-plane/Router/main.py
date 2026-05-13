"""CLI/Docker entrypoint for the Router service (thin shim)."""
import os

import uvicorn

if __name__ == "__main__":
    from streambed_router.main import app

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("ROUTER_PORT", "8090")))
