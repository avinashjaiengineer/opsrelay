"""Container entrypoint: `python -m opsrelay.runtime` serves the agent named by OPSRELAY_ROLE."""

import logging

from ..config import get_settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    role = get_settings().role
    if role == "coordinator":
        from .coordinator import serve

        serve(port=8080)
    else:
        from .specialist import serve

        serve(role)


if __name__ == "__main__":
    main()
