"""Real stdio/PG read smoke; no model calls and no business writes."""
import argparse
import asyncio
from datetime import datetime
import json

from src.config import settings
from src.domain.clock import FixedClock
from src.mcp.gateway import MCPReadGateway
from src.storage.database import session_factory
from src.tools.direct_gateway import DirectReadGateway


async def check(reference):
    direct = DirectReadGateway(session_factory(readonly=True), FixedClock(reference))
    async with MCPReadGateway(reference_time=reference) as mcp:
        assert mcp.startup_error is None, "MCP_UNAVAILABLE"
        for name, args in (
            ("get_order", {"order_id": "ORD-1002"}),
            ("get_return_policy", {"policy_id": settings.default_policy_id}),
            ("check_return_eligibility", {"order_id": "ORD-1009", "reason": "defective"}),
            ("check_return_eligibility", {"order_id": "ORD-DEMO-UNKNOWN"}),
            ("check_return_eligibility", {"order_id": "ORD-DEMO-DUPLICATE"}),
            ("get_order", {"order_id": "ORD-MISSING"}),
        ):
            expected = getattr(direct, name)(**args)
            actual = await getattr(mcp, name)(**args)
            assert (actual.ok, actual.code, actual.retryable, actual.data, actual.error) == (
                expected.ok, expected.code, expected.retryable, expected.data, expected.error), "direct/MCP facts differ"
        print(json.dumps({"transport": "real_stdio", "tools": sorted(tool.name for tool in mcp.catalog),
                          "reader": "PostgreSQL SELECT-only role", "direct_mcp_facts": "matched",
                          "reference_time": reference.isoformat(), "model_calls": 0}, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--clock", default="2026-10-09T12:00:00Z", help="Explicit smoke reference time; runtime is not frozen")
    args = parser.parse_args()
    asyncio.run(check(datetime.fromisoformat(args.clock.replace("Z", "+00:00"))))


if __name__ == "__main__":
    main()
