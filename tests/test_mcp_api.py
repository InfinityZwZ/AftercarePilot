import asyncio
import json
from uuid import UUID

from pydantic import SecretStr

from src.config import settings
from src.api.dependencies import get_analyzer
from src.mcp.gateway import MCPReadGateway
from tests.fakes import FakeLLM
from tests.test_stage3_api import api, create, guard
from tests.test_proposal_approval import counts
from tests.test_mcp_integration import reader_url, server_pid, alive


def configure_default_mcp(stage3, tmp_path, monkeypatch, *, broken=False):
    import src.main as main
    import src.agent.llm as model_module
    import src.mcp.gateway as gateway_module
    monkeypatch.setattr(settings, "read_transport", "mcp")
    monkeypatch.setattr(settings, "readonly_database_url", SecretStr(reader_url(stage3)))
    fake = FakeLLM()
    monkeypatch.setattr(model_module, "get_llm", lambda: fake)
    def factory():
        return MCPReadGateway(reference_time=__import__("scripts.seed_demo", fromlist=["REFERENCE_TIME"]).REFERENCE_TIME,
                              log_file=tmp_path / "mcp.log", **({"args": ["-m", "tests.missing_mcp_server"]} if broken else {}))
    monkeypatch.setattr(gateway_module, "MCPReadGateway", factory)
    main.app.dependency_overrides.pop(get_analyzer, None)
    return fake


def test_actual_application_factory_reads_mcp_then_internal_execute(api, stage3, tmp_path, monkeypatch):
    from src.tools.direct_gateway import DirectReadGateway
    configure_default_mcp(stage3, tmp_path, monkeypatch)
    monkeypatch.setattr(DirectReadGateway, "get_order", lambda *a, **k: (_ for _ in ()).throw(AssertionError("parent direct disabled")))
    row = create(api)
    route = "/api/v1/proposals/" + row["proposal_id"]
    assert row["usage"]["tool_calls"] == 3 and counts(stage3) == (0, 0)
    assert not alive(server_pid(tmp_path / "mcp.log"))
    assert api.post(route + "/execute", json=guard(row)).status_code == 403
    assert api.post(route + "/approve", json=guard(row)).status_code == 200 and counts(stage3) == (0, 0)
    executed = api.post(route + "/execute", json=guard(row)).json()
    assert executed["ticket"]["status"] == "requested" and counts(stage3) == (1, 1)
    verified = api.get(route + "/execution").json()
    assert all(verified["verification"].values())
    assert api.post(route + "/execute", json=guard(row)).json()["ticket"]["id"] == executed["ticket"]["id"]


def test_api_and_legacy_chat_surface_mcp_failure_without_model_or_write(api, stage3, tmp_path, monkeypatch):
    fake = configure_default_mcp(stage3, tmp_path, monkeypatch, broken=True)
    for route in ("/proposals/analyze", "/chat"):
        response = api.post("/api/v1" + route, json={"message": "ORD-1009 音箱坏了"})
        assert response.status_code == 503 and response.json()["error"]["code"] == "MCP_UNAVAILABLE"
        failed = api.get("/api/v1/proposals/" + response.json()["error"]["details"]["proposal_id"]).json()
        assert failed["status"] == "failed" and failed["usage"]["llm_calls"] == 0
    assert not fake.history and counts(stage3) == (0, 0)


def test_native_tool_wrapper_uses_configured_mcp(stage3, tmp_path, monkeypatch):
    import src.agent.tools as tools
    import src.mcp.gateway as gateway_module
    from scripts.seed_demo import REFERENCE_TIME
    monkeypatch.setattr(settings, "read_transport", "mcp")
    monkeypatch.setattr(gateway_module, "MCPReadGateway", lambda: MCPReadGateway(reader_url(stage3), reference_time=REFERENCE_TIME, log_file=tmp_path / "mcp.log"))
    result = json.loads(asyncio.run(tools.get_order.ainvoke({"order_id": "ORD-1009"})))
    assert result["ok"] and result["data"]["total_cents"] == 8999
    assert not alive(server_pid(tmp_path / "mcp.log"))
