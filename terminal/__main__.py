"""``python -m terminal`` launches the web terminal."""
from __future__ import annotations

import uvicorn

from terminal import PRODUCT, __version__
from terminal.api.app import create_app
from terminal.config import get_settings
from terminal.monitoring.logging_setup import configure_logging
from terminal.preflight import run as preflight


def main() -> None:
    s = get_settings()
    configure_logging(s)
    preflight(s)
    app = create_app()
    print(f"{PRODUCT} v{__version__}  →  http://{s.bind_host}:{s.port}   mode={s.terminal_mode} env={s.trading_env} data={s.data_source}")
    uvicorn.run(app, host=s.bind_host, port=s.port, log_level="warning", ws_ping_interval=20, ws_ping_timeout=20)


if __name__ == "__main__":
    main()
