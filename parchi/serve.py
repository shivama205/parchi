"""Server entry point. Reads PORT, which is how Cloud Run assigns it."""

from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    uvicorn.run(
        "parchi.server:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8080")),
        workers=1,
        log_level=os.getenv("LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
