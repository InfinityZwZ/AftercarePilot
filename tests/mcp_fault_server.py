"""Trusted test-only stdio peers. Never used by the application factory."""
import argparse
import asyncio
import json
import logging
import os
import sys

parser = argparse.ArgumentParser()
parser.add_argument("mode", choices=("bad_result", "text", "is_error", "slow", "extra", "environment", "pollution"))
args = parser.parse_args()
os.environ["FASTMCP_ENV_FILE"] = os.devnull
logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

if args.mode == "environment":
    assert not any(key in os.environ for key in ("DATABASE_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPERATOR_TOKEN", "LANGFUSE_SECRET_KEY"))
    from dotenv.main import DotEnv
    original_stream = DotEnv._get_stream
    def guarded_stream(self):
        assert not self.dotenv_path or os.path.basename(str(self.dotenv_path)) != ".env"
        return original_stream(self)
    DotEnv._get_stream = guarded_stream

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from src.mcp.server import READ_ANNOTATIONS, build_server

if args.mode in ("environment", "extra", "pollution"):
    server, engine = build_server(os.environ["READONLY_DATABASE_URL"])
    assert "src.config" not in sys.modules
    if args.mode == "extra":
        @server.tool(annotations=READ_ANNOTATIONS)
        def unrelated_probe() -> str:
            return "Not an allowed application tool"
    if args.mode == "pollution":
        print("unexpected stdout banner", flush=True)
else:
    server = FastMCP("fault-test-peer")
    engine = None
    def value():
        if args.mode == "is_error":
            raise ToolError("test-sensitive-text-must-not-leak")
        if args.mode == "text":
            return json.dumps({"ok": True, "code": "ORDER_NOT_FOUND", "retryable": False,
                               "data": {"found": False}, "error": None, "meta": {"data_source": "demo_postgresql", "duration_ms": 0}})
        return {"not": "a ToolResult"}
    @server.tool(annotations=READ_ANNOTATIONS, output_schema=None)
    async def get_order(order_id: str):
        if args.mode == "slow":
            await asyncio.sleep(60)
        return value()
    @server.tool(annotations=READ_ANNOTATIONS, output_schema=None)
    def get_return_policy(policy_id: str):
        return value()
    @server.tool(annotations=READ_ANNOTATIONS, output_schema=None)
    def check_return_eligibility(order_id: str, reason: str | None = None):
        return value()

logging.warning("MCP test server started pid=%s", os.getpid())
try:
    server.run(transport="stdio", show_banner=False, log_level="WARNING")
finally:
    if engine is not None:
        engine.dispose()
