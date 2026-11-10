import asyncio
from contextlib import suppress
from datetime import datetime
import json
import os
from pathlib import Path
import sys

from pydantic import ValidationError

from src.domain.clock import utc
from src.domain.proposals import READ_ARGS
from src.domain.schemas import ToolError, ToolResult

ROOT = Path(__file__).resolve().parents[2]
TOOL_NAMES = frozenset(READ_ARGS)


def failure(code, message, *, retryable=False):
    return ToolResult(ok=False, code=code, retryable=retryable, data=None, error=ToolError(code=code, message=message))


def server_environment(reader_url):
    # SDK adds its platform-specific OS allowlist. No application env is copied.
    environment = {"READONLY_DATABASE_URL": reader_url, "PYTHONPATH": str(ROOT), "PYTHONUTF8": "1",
                   "PYTHONUNBUFFERED": "1", "FASTMCP_ENV_FILE": os.devnull}
    if zone := os.environ.get('TZ'):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            pass
        else:
            environment['TZ'] = zone
    return environment


def decode_result(raw):
    """Protocol isError and business ok are distinct; structured data has priority."""
    if raw.isError:
        return failure("MCP_PROTOCOL_ERROR", "MCP server rejected the tool call")
    try:
        data = raw.structuredContent
        if data is None:
            if len(raw.content) != 1 or raw.content[0].type != "text":
                raise ValueError()
            def invalid_constant(value):
                raise ValueError("Non-finite JSON")
            data = json.loads(raw.content[0].text, parse_constant=invalid_constant)
        json.dumps(data, allow_nan=False)
        result = ToolResult.model_validate(data, strict=True)
        if (result.ok and (result.data is None or result.error is not None)) or (not result.ok and (result.data is not None or result.error is None)):
            raise ValueError()
        return result
    except (ValueError, TypeError, AttributeError, ValidationError):
        return failure("MCP_PROTOCOL_ERROR", "MCP result does not match the ToolResult contract")


class MCPReadGateway:
    transport = "mcp"

    def __init__(self, reader_url=None, *, reference_time: datetime | None = None, timeout=None,
                 command=None, args=None, cwd=None, log_file=None):
        from src.config import settings
        self._reader_url = reader_url if reader_url is not None else settings.readonly_database_url.get_secret_value()
        self.timeout = timeout if timeout is not None else settings.mcp_read_timeout_seconds
        self.command = command or sys.executable
        self.args = list(args) if args is not None else ["-m", "src.mcp.server"]
        if reference_time is not None:
            self.args += ["--clock", utc(reference_time).isoformat()]
        self.cwd = Path(cwd) if cwd is not None else ROOT
        self.log_file = Path(log_file) if log_file is not None else ROOT / ".local" / "mcp" / "stdio.log"
        self._client = self._transport = None
        self._active = False
        self._terminal_error = None
        self.catalog = []

    @property
    def startup_error(self):
        return self._terminal_error if self._client is None else None

    async def __aenter__(self):
        if self._active:
            raise RuntimeError("A read gateway cannot share an active session between requests")
        self._active = True
        self._terminal_error = None
        try:
            from fastmcp import Client
            from fastmcp.client.transports import StdioTransport
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            self._transport = StdioTransport(command=self.command, args=self.args, env=server_environment(self._reader_url),
                                             cwd=str(self.cwd), keep_alive=False, log_file=self.log_file)
            self._client = Client(self._transport, timeout=self.timeout, init_timeout=self.timeout,
                                  message_handler=self._on_message)
            await asyncio.wait_for(self._client.__aenter__(), timeout=self.timeout)
            self.catalog = await asyncio.wait_for(self._client.list_tools(), timeout=self.timeout)
            if len(self.catalog) != 3 or {tool.name for tool in self.catalog} != TOOL_NAMES or any(
                    tool.annotations is None or tool.annotations.readOnlyHint is not True or tool.annotations.destructiveHint is not False
                    for tool in self.catalog):
                self._terminal_error = failure("MCP_TOOLSET_INVALID", "MCP server must expose exactly the three declared read-only tools")
            if self._terminal_error is not None:
                await self._shutdown()
        except (asyncio.CancelledError, KeyboardInterrupt):
            await self._shutdown_shielded()
            self._active = False
            raise
        except Exception:
            self._terminal_error = self._terminal_error or failure("MCP_UNAVAILABLE", "MCP read server is unavailable", retryable=True)
            await self._shutdown()
        return self

    async def _on_message(self, message):
        if isinstance(message, Exception):
            self._terminal_error = failure("MCP_PROTOCOL_ERROR", "MCP transport received an invalid protocol message")

    async def _shutdown(self):
        client, transport = self._client, self._transport
        self._client = self._transport = None
        if client is not None:
            with suppress(Exception):
                await asyncio.wait_for(client.close(), timeout=5)
        if transport is not None:
            with suppress(Exception):
                await asyncio.wait_for(transport.close(), timeout=5)

    async def _shutdown_shielded(self):
        cleanup = asyncio.create_task(self._shutdown())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup

    async def __aexit__(self, exc_type, exc_value, traceback):
        try:
            await self._shutdown_shielded()
        finally:
            self._active = False

    async def _read(self, name, args):
        try:
            args = READ_ARGS[name].model_validate(args).model_dump()
        except (KeyError, ValueError, ValidationError):
            return failure("INVALID_ARGUMENT", "Read arguments do not match the declared schema")
        if self._terminal_error:
            return self._terminal_error
        if not self._active or self._client is None:
            return failure("MCP_UNAVAILABLE", "No active MCP read session", retryable=True)
        try:
            raw = await self._client.call_tool_mcp(name, args, timeout=self.timeout)
            if self._terminal_error is not None:
                return self._terminal_error
            return decode_result(raw)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._terminal_error = self._terminal_error or failure("MCP_UNAVAILABLE", "MCP read call could not complete", retryable=True)
            return self._terminal_error

    async def get_order(self, order_id):
        return await self._read("get_order", {"order_id": order_id})

    async def get_return_policy(self, policy_id):
        return await self._read("get_return_policy", {"policy_id": policy_id})

    async def check_return_eligibility(self, order_id, reason=None):
        return await self._read("check_return_eligibility", {"order_id": order_id, "reason": reason})
