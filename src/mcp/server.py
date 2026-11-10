"""Independent stdio read server. Does not load application .env or model credentials."""
import argparse
from datetime import datetime
import logging
import os
import sys

# FastMCP's own settings otherwise load .env when the package is imported.
os.environ["FASTMCP_ENV_FILE"] = os.devnull

from fastmcp import FastMCP
from pydantic import Field
from typing import Annotated
from sqlalchemy.orm import sessionmaker

from src.domain.clock import FixedClock, SystemClock
from src.domain.schemas import ReturnReason, ToolResult
from src.storage.database import check_reader_permissions, make_engine
from src.tools.direct_gateway import DirectReadGateway

Identifier = Annotated[str, Field(min_length=1, max_length=64)]
READ_ANNOTATIONS = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}


def build_server(reader_url, clock=None):
    engine = make_engine(reader_url)
    sessions = sessionmaker(engine, expire_on_commit=False)
    try:
        with sessions() as session:
            check_reader_permissions(session)
    except Exception:
        engine.dispose()
        raise
    gateway = DirectReadGateway(sessions, clock or SystemClock())
    server = FastMCP("aftercare-read", version="2.0.0", instructions="Exactly three read-only PostgreSQL tools; no approvals, ticket writes, SQL or model sampling.")

    @server.tool(annotations=READ_ANNOTATIONS)
    def get_order(order_id: Identifier) -> ToolResult:
        """Read an order and its simulated shipment by order ID."""
        return gateway.get_order(order_id)

    @server.tool(annotations=READ_ANNOTATIONS)
    def get_return_policy(policy_id: Identifier) -> ToolResult:
        """Read explicit policy rules and their hash by policy ID."""
        return gateway.get_return_policy(policy_id)

    @server.tool(annotations=READ_ANNOTATIONS)
    def check_return_eligibility(order_id: Identifier, reason: ReturnReason | None = None) -> ToolResult:
        """Read deterministic whole-order return eligibility; never create a ticket."""
        return gateway.check_return_eligibility(order_id, reason)

    return server, engine


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--clock", help="Timezone-aware reference time for explicit test/smoke runs only")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    engine = None
    try:
        clock = FixedClock(datetime.fromisoformat(args.clock.replace("Z", "+00:00"))) if args.clock else SystemClock()
        server, engine = build_server(os.environ.get("READONLY_DATABASE_URL", ""), clock)
        logging.warning("MCP read server started pid=%s", os.getpid())
        server.run(transport="stdio", show_banner=False, log_level="WARNING")
    except KeyboardInterrupt:
        return
    except Exception:
        # A connection string or provider traceback must never reach stdout/logs.
        logging.error("MCP read server could not start or complete; check reader configuration")
        sys.exit(1)
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    main()
