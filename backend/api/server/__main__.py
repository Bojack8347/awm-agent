"""Run the AWM advisor API with ``python -m api.server``."""

from __future__ import annotations

import os

from .factory import app


def main() -> None:
    port_str = os.getenv("ADVISOR_PORT")
    if not port_str:
        fallback_port = os.getenv("PORT", "").strip()
        port_str = fallback_port if fallback_port and fallback_port != "3000" else "8002"
    port = int(port_str) if port_str else 8002
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"

    print(f"Starting awm-api on port {port}")
    app.run(host="0.0.0.0", port=port, debug=debug)


if __name__ == "__main__":
    main()
