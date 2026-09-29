"""``python -m terminal`` launches the web terminal."""
from __future__ import annotations

import logging
import os

import uvicorn

from terminal import PRODUCT, __version__
from terminal.api.app import create_app
from terminal.config import get_settings


def main() -> None:
    s = get_settings()
    logging.basicConfig(level=getattr(logging, s.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    if not s.is_loopback and not (s.api_auth_token or s.users):
        raise SystemExit("NON_LOCAL_BIND_REQUIRES_API_AUTH_TOKEN_OR_TERMINAL_USERS")
    app = create_app()
    print(f"{PRODUCT} v{__version__}  →  http://{s.bind_host}:{s.port}   mode={s.terminal_mode} env={s.trading_env} data={s.data_source}")
    uvicorn.run(app, host=s.bind_host, port=s.port, log_level="warning", ws_ping_interval=20, ws_ping_timeout=20)


if __name__ == "__main__":
    main()
