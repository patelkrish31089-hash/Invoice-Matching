from __future__ import annotations

import argparse
import logging
import time

from app.config import settings
from app.db import create_database_engine, create_session_factory
from app.outbox import dispatch_pending_outbox

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("ledgerlens.outbox_dispatcher")


def main() -> int:
    parser = argparse.ArgumentParser(description="Continuously dispatch due invoice-processing outbox events.")
    parser.add_argument("--interval", type=float, default=5.0, help="Polling interval in seconds (minimum 1)")
    parser.add_argument("--batch-size", type=int, default=25, help="Maximum events claimed per poll (1-500)")
    parser.add_argument("--once", action="store_true", help="Run one bounded dispatch pass and exit")
    args = parser.parse_args()
    if args.interval < 1:
        parser.error("--interval must be at least 1 second")
    if not 1 <= args.batch_size <= 500:
        parser.error("--batch-size must be between 1 and 500")

    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        while True:
            try:
                result = dispatch_pending_outbox(session_factory, settings.storage_path, limit=args.batch_size)
                if result["claimed"]:
                    logger.info(
                        "outbox_poll_complete claimed=%s published=%s deferred_or_dead=%s",
                        result["claimed"], result["published"], result["deferred_or_dead"],
                    )
            except Exception as exc:
                logger.error("outbox_poll_failed error_type=%s", type(exc).__name__)
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
