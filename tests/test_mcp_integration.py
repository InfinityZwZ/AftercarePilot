import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import subprocess
from uuid import uuid4

import pytest
from sqlalchemy import text
from mcp.types import CallToolResult, TextContent

from scripts.seed_demo import REFERENCE_TIME
from src.agent.nodes import AnalysisAgent
from src.domain.exceptions import AppError
from src.domain.schemas import ToolResult
from src.mcp.gateway import MCPReadGateway, decode_result, server_environment
from tests.fakes import FakeLLM
from tests.test_proposal_approval import counts, execute, review

ROOT = Path(__file__).resolve().parents[1]


def reader_url(fixture, *, writer=False):
    engine = fixture["engine"] if writer else fixture.get("reader_engine")
    if engine is None:
        engine = fixture["readers"].kw["bind"]
    with fixture["engine"].connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
    return engine.url.set(query={"options": f"-c search_path={schema}"}).render_as_string(hide_password=False)


def gateway(fixture, tmp_path, **kwargs):
    return MCPReadGateway(reader_url(fixture), reference_time=REFERENCE_TIME,
                          log_file=tmp_path / "mcp.log", **kwargs)


def peer(fixture, tmp_path, mode, **kwargs):
    return MCPReadGateway(reader_url(fixture), args=["-m", "tests.mcp_fault_server", mode],
                          log_file=tmp_path / "mcp.log", **kwargs)


def server_pid(path):
    values = re.findall(r"server started pid=(\d+)", path.read_text(encoding="utf-8"))
    assert values
    return int(values[-1])


def alive(pid):
    if os.name == "nt":
        result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        return f'"{pid}"'.encode() in result.stdout
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def identity():
    return dict(proposal_id=uuid4(), request_id=uuid4(), revision=1, context={}, message="ORD-1009 音箱坏了，想退货", language="zh")


def test_real_catalog_exactly_three_and_protocol_calls_read_pg(pg, tmp_path):
    async def check():
        async with gateway(pg, tmp_path) as mcp:
            assert mcp.startup_error is None
            assert {tool.name for tool in mcp.catalog} == {"get_order", "get_return_policy", "check_return_eligibility"}
            assert all(tool.annotations.readOnlyHint and not tool.annotations.destructiveHint for tool in mcp.catalog)
            assert await mcp._client.list_resources() == []
            assert await mcp._client.list_prompts() == []
            result = await mcp.get_order("ORD-1002")
            assert result.ok and result.data["shipment"]["simulated"] and result.data["shipment"]["status"] == "in_transit"
            raw = await mcp._client.call_tool_mcp("get_order", {"order_id": "ORD-1009"})
            assert raw.structuredContent["ok"] and raw.structuredContent["data"]["total_cents"] == 8999
            denied = await mcp._client.call_tool_mcp("create_ticket", {"order_id": "ORD-1009"})
            assert denied.isError
    asyncio.run(check())
    assert not alive(server_pid(tmp_path / "mcp.log"))


@pytest.mark.parametrize("order_id", ["ORD-1009", "ORD-1002", "ORD-DEMO-EXPIRED", "ORD-DEMO-BOUNDARY", "ORD-DEMO-UNKNOWN", "ORD-DEMO-DUPLICATE", "ORD-DEMO-CANCELLED", "ORD-MISSING"])
def test_direct_mcp_fixed_clock_contract(pg, tmp_path, order_id, gateway):
    async def check():
        async with MCPReadGateway(reader_url(pg), reference_time=REFERENCE_TIME, log_file=tmp_path / "mcp.log") as mcp:
            actual = await mcp.check_return_eligibility(order_id, "defective")
            expected = gateway.check_return_eligibility(order_id, "defective")
            assert (actual.ok, actual.code, actual.retryable, actual.data, actual.error) == (expected.ok, expected.code, expected.retryable, expected.data, expected.error)
    asyncio.run(check())


def test_policy_order_and_one_microsecond_boundary_contract(pg, tmp_path, gateway):
    async def check():
        async with MCPReadGateway(reader_url(pg), reference_time=REFERENCE_TIME, log_file=tmp_path / "mcp.log") as mcp:
            assert (await mcp.get_order("ORD-1009")).data == gateway.get_order("ORD-1009").data
            assert (await mcp.get_return_policy("demo-return")).data == gateway.get_return_policy("demo-return").data
        async with MCPReadGateway(reader_url(pg), reference_time=REFERENCE_TIME + timedelta(microseconds=1), log_file=tmp_path / "mcp.log") as mcp:
            result = await mcp.check_return_eligibility("ORD-DEMO-BOUNDARY")
            assert result.ok and not result.retryable and result.data["reason_codes"] == ["RETURN_WINDOW_EXPIRED"]
    asyncio.run(check())


def test_one_real_process_for_complete_analysis_then_backend_approval_execution(stage3, tmp_path):
    fake = FakeLLM()
    mcp = gateway(stage3, tmp_path)
    row = asyncio.run(stage3["proposals"].analyze("ORD-1009 音箱坏了，想退货", operator="tester", agent=AnalysisAgent(fake, mcp)))
    assert row["status"] == "pending_approval" and len(row["payload"]["evidence"]) == 3
    assert row["usage"]["tool_calls"] == 3 and counts(stage3) == (0, 0)
    assert len(re.findall(r"server started pid=", mcp.log_file.read_text(encoding="utf-8"))) == 1
    assert mcp._client is None and not alive(server_pid(mcp.log_file))
    review(stage3, row)
    assert counts(stage3) == (0, 0)
    result = execute(stage3, row)
    assert result["ticket"]["status"] == "requested" and counts(stage3) == (1, 1)
    assert execute(stage3, row)["ticket"]["id"] == result["ticket"]["id"]


def test_server_rejects_writer_credentials(stage3, tmp_path):
    async def check():
        async with MCPReadGateway(reader_url(stage3, writer=True), log_file=tmp_path / "mcp.log") as mcp:
            assert mcp.startup_error.code == "MCP_UNAVAILABLE"
            assert (await mcp.get_order("ORD-1009")).code == "MCP_UNAVAILABLE"
    asyncio.run(check())


def test_stdio_environment_and_dotenv_are_isolated(pg, tmp_path, monkeypatch):
    for name in ("DATABASE_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPERATOR_TOKEN", "LANGFUSE_SECRET_KEY"):
        monkeypatch.setenv(name, "test-parent-secret-not-for-mcp")
    directory = tmp_path / "working directory with spaces"
    directory.mkdir()
    (directory / ".env").write_text("OPENAI_API_KEY=test-dotenv-sentinel\n", encoding="utf-8")
    async def check():
        async with peer(pg, tmp_path, "environment", cwd=directory) as mcp:
            assert mcp.startup_error is None
            assert (await mcp.get_order("ORD-1009")).ok
            assert mcp.command == __import__("sys").executable
            assert "--" not in mcp.command
    asyncio.run(check())
    assert "test-parent-secret" not in (tmp_path / "mcp.log").read_text(encoding="utf-8")
    required = {"READONLY_DATABASE_URL", "PYTHONPATH", "PYTHONUTF8", "PYTHONUNBUFFERED", "FASTMCP_ENV_FILE"}
    assert required <= set(server_environment("placeholder")) <= required | {'TZ'}


@pytest.mark.parametrize("mode,code", [("bad_result", "MCP_PROTOCOL_ERROR"), ("is_error", "MCP_PROTOCOL_ERROR"), ("extra", "MCP_TOOLSET_INVALID"), ("pollution", "MCP_PROTOCOL_ERROR")])
def test_real_fault_peers_fail_explicitly_not_direct(pg, tmp_path, mode, code):
    async def check():
        async with peer(pg, tmp_path, mode) as mcp:
            result = await mcp.get_order("ORD-1009")
            assert not result.ok and result.code == code
            assert result.data is None and "must-not-leak" not in result.model_dump_json()
    asyncio.run(check())
    assert not alive(server_pid(tmp_path / "mcp.log"))


def test_legacy_json_text_protocol_contract(pg, tmp_path):
    async def check():
        async with peer(pg, tmp_path, "text") as mcp:
            result = await mcp.get_order("ORD-MISSING")
            assert result.ok and result.code == "ORDER_NOT_FOUND" and not result.retryable
    asyncio.run(check())


def test_missing_server_never_falls_back_or_calls_model(stage3, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("MCP failures must not fall back to direct")
    from src.tools.direct_gateway import DirectReadGateway
    monkeypatch.setattr(DirectReadGateway, "get_order", forbidden)
    fake = FakeLLM()
    mcp = gateway(stage3, tmp_path, args=["-m", "tests.mcp_module_that_does_not_exist"])
    with pytest.raises(AppError) as caught:
        asyncio.run(stage3["proposals"].analyze("ORD-1009 音箱坏了", operator="tester", agent=AnalysisAgent(fake, mcp)))
    assert caught.value.code == "MCP_UNAVAILABLE" and caught.value.status == 503
    assert not fake.history and counts(stage3) == (0, 0)


def test_read_timeout_reaps_real_process(pg, tmp_path):
    async def check():
        async with peer(pg, tmp_path, "slow") as mcp:
            assert mcp.startup_error is None
            mcp.timeout = 0.1
            result = await mcp.get_order("ORD-1009")
            assert result.code == "MCP_UNAVAILABLE" and result.retryable
    asyncio.run(check())
    assert not alive(server_pid(tmp_path / "mcp.log"))


@pytest.mark.parametrize("interrupt", ["cancel", "keyboard_interrupt"])
def test_cancel_and_control_c_cleanup_reaps_real_stdio_server(pg, tmp_path, interrupt):
    async def check():
        mcp = peer(pg, tmp_path, "slow")
        async with mcp:
            assert mcp.startup_error is None
            if interrupt == "keyboard_interrupt":
                raise KeyboardInterrupt()
            current = asyncio.current_task()
            asyncio.get_running_loop().call_later(0.1, current.cancel)
            await mcp.get_order("ORD-1009")
    with pytest.raises(KeyboardInterrupt if interrupt == "keyboard_interrupt" else asyncio.CancelledError):
        asyncio.run(check())
    assert not alive(server_pid(tmp_path / "mcp.log"))


@pytest.mark.parametrize("interrupt", [asyncio.CancelledError, KeyboardInterrupt])
def test_interrupted_startup_reaps_started_process(pg, tmp_path, monkeypatch, interrupt):
    from fastmcp import Client
    mcp = gateway(pg, tmp_path)
    def interrupted_catalog(self):
        assert alive(server_pid(mcp.log_file))
        raise interrupt()
    monkeypatch.setattr(Client, "list_tools", interrupted_catalog)
    async def check():
        async with mcp:
            pytest.fail("Interrupted startup must not enter the analysis session")
    with pytest.raises(interrupt):
        asyncio.run(check())
    assert not mcp._active and mcp._client is None and mcp._transport is None
    assert not alive(server_pid(mcp.log_file))


def test_structured_priority_and_strict_protocol_validation():
    valid = ToolResult(ok=True, code="ORDER_NOT_FOUND", data={"found": False}).model_dump(mode="json")
    raw = CallToolResult(content=[TextContent(type="text", text="broken")], structuredContent=valid)
    assert decode_result(raw).ok
    raw = CallToolResult(content=[TextContent(type="text", text=json.dumps(valid))], structuredContent={"bad": "data"})
    assert decode_result(raw).code == "MCP_PROTOCOL_ERROR"
    raw = CallToolResult(content=[TextContent(type="text", text="sensitive fake error")], isError=True)
    assert "sensitive" not in decode_result(raw).model_dump_json()


def test_default_transport_is_mcp_and_explicit_direct_is_supported(monkeypatch):
    from src.config import Settings, settings
    from src.tools.factory import create_read_gateway
    from src.tools.direct_gateway import DirectReadGateway
    assert Settings(_env_file=None).read_transport == "mcp"
    monkeypatch.setattr(settings, "read_transport", "mcp")
    assert isinstance(create_read_gateway(), MCPReadGateway)
    monkeypatch.setattr(settings, "read_transport", "direct")
    assert isinstance(create_read_gateway(), DirectReadGateway)


def test_server_dies_mid_session_is_unavailable_and_no_fallback(pg, tmp_path, monkeypatch):
    from src.tools.direct_gateway import DirectReadGateway
    monkeypatch.setattr(DirectReadGateway, "get_order", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fallback")))
    async def check():
        async with gateway(pg, tmp_path) as mcp:
            assert (await mcp.get_order("ORD-1009")).ok
            pid = server_pid(mcp.log_file)
            assert alive(pid)
            if os.name == "nt":
                stopped = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                assert stopped.returncode == 0
            else:
                import signal
                os.kill(pid, signal.SIGTERM)
            result = await mcp.get_order("ORD-1009")
            assert result.code == "MCP_UNAVAILABLE" and result.data is None
    asyncio.run(check())
    assert not alive(server_pid(tmp_path / "mcp.log"))


def test_fresh_sessions_work_across_event_loops(pg, tmp_path):
    mcp = gateway(pg, tmp_path)
    async def one():
        async with mcp:
            assert (await mcp.get_order("ORD-1009")).ok
    asyncio.run(one())
    first = server_pid(mcp.log_file)
    assert not alive(first)
    asyncio.run(one())
    second = server_pid(mcp.log_file)
    assert second != first and not alive(second)


def test_two_analysis_requests_have_independent_real_sessions(stage3, tmp_path):
    async def check():
        agents = [AnalysisAgent(FakeLLM(), gateway(stage3, tmp_path / str(index))) for index in range(2)]
        results = await asyncio.gather(*(stage3["proposals"].analyze("ORD-1009 音箱坏了", operator="tester", agent=agent) for agent in agents))
        assert results[0]["request_id"] != results[1]["request_id"]
        assert all(row["status"] == "pending_approval" for row in results)
        pids = [server_pid(agent.gateway.log_file) for agent in agents]
        assert len(set(pids)) == 2 and all(not alive(pid) for pid in pids)
    asyncio.run(check())
    assert counts(stage3) == (0, 0)
