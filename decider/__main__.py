"""`python -m decider`: the decider on 127.0.0.1:8090 (DECIDER_PORT to change it). Loopback only."""

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("decider.main:app", host="127.0.0.1", port=int(os.getenv("DECIDER_PORT", "8090")))
