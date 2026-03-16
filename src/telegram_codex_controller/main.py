from __future__ import annotations

import asyncio
import logging

from .bot import build_application
from .config import Settings


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
    )
    # Python 3.14 no longer creates a default event loop for the main thread.
    asyncio.set_event_loop(asyncio.new_event_loop())
    settings = Settings.load()
    app = build_application(settings)
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
