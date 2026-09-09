"""Local development entrypoint. Production uses the container CMD (uvicorn)."""

import os

import uvicorn


if __name__ == "__main__":
    uvicorn.run(
        "planning_poker.app:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        reload=os.environ.get("RELOAD", "1") == "1",
    )
