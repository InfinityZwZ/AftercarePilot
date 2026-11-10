from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from src.agent.nodes import AnalysisAgent
from src.api.dependencies import get_aftersales_service, get_analyzer, get_proposal_service
from src.config import settings
from src.services.aftersales_service import AftersalesService
from tests.fakes import FakeLLM
from tests.test_proposal_approval import counts


@pytest.fixture
def api(stage3, monkeypatch):
    import src.main as main
    from src.storage import database
    monkeypatch.setattr(settings, "operator_token", SecretStr("test-only-operator-token"))
    monkeypatch.setattr(settings, "operator_id", "server_operator")
    monkeypatch.setattr(main, "init_langfuse", lambda: None)
    monkeypatch.setattr(main, "get_langfuse_client", lambda: None)
    monkeypatch.setattr(database, "session_factory", lambda readonly=False: stage3["readers"] if readonly else stage3["sessions"])
    overrides = dict(main.app.dependency_overrides)
    main.app.dependency_overrides[get_proposal_service] = lambda: stage3["proposals"]
    main.app.dependency_overrides[get_aftersales_service] = lambda: stage3["aftersales"]
    main.app.dependency_overrides[get_analyzer] = lambda: AnalysisAgent(FakeLLM(), stage3["gateway"])
    try:
        with TestClient(main.app, headers={"X-Operator-Token": "test-only-operator-token"}) as client:
            yield client
    finally:
        main.app.dependency_overrides = overrides


def create(api):
    response = api.post("/api/v1/proposals/analyze", json={"message": "ORD-1009 音箱坏了，我想退货", "language": "zh"})
    assert response.status_code == 200, response.json()
    return response.json()


def guard(row):
    return dict(expected_revision=row["revision"], expected_payload_hash=row["payload_hash"])


def test_api_full_loop_and_independent_gets(api, stage3):
    row = create(api)
    route = "/api/v1/proposals/" + row["proposal_id"]
    assert api.post(route + "/execute", json=guard(row)).status_code == 403
    assert counts(stage3) == (0, 0)
    approved = api.post(route + "/approve", json=guard(row))
    assert approved.status_code == 200 and approved.json()["reviewer_id"] == "server_operator"
    assert counts(stage3) == (0, 0)
    executed = api.post(route + "/execute", json=guard(row))
    assert executed.status_code == 200
    ticket = executed.json()["ticket"]
    assert ticket["status"] == "requested" and ticket["created_by"] == "server_operator"
    assert api.get(route + "/execution").json()["verification"] == {"ticket_row_found": True, "proposal_executed": True, "success_audit_found": True}
    assert api.get("/api/v1/aftersales-tickets/" + ticket["id"]).json()["ticket_no"] == ticket["ticket_no"]
    assert api.post(route + "/execute", json=guard(row)).json()["ticket"]["id"] == ticket["id"]
    logs = api.get("/api/v1/audit-logs", params={"request_id": row["request_id"]}).json()["items"]
    assert sum(item["event_type"] == "ticket_created" for item in logs) == 1


def test_old_chat_generates_proposal_without_approval_or_ticket(api, stage3):
    first = api.post("/api/v1/chat", json={"message": "ORD-1009 音箱坏了，立即退款", "session_id": "default"})
    second = api.post("/api/v1/chat", json={"message": "ORD-1009 音箱坏了，立即退款", "session_id": "default"})
    assert first.status_code == second.status_code == 200
    assert first.json()["request_id"] != second.json()["request_id"]
    assert first.json()["proposal"]["status"] == "pending_approval"
    assert first.json()["proposal"]["approved_payload_hash"] is None and counts(stage3) == (0, 0)


@pytest.mark.parametrize("injected", [{"reviewer_id": "model"}, {"requested_amount_cents": 1}, {"order_id": "ORD-1001"}, {"reason": "defective"}])
def test_mutation_schema_rejects_client_business_or_identity_fields(api, stage3, injected):
    row = create(api)
    route = "/api/v1/proposals/" + row["proposal_id"]
    for operation in ("approve", "execute"):
        response = api.post(route + "/" + operation, json={**guard(row), **injected})
        assert response.status_code == 422 and response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert counts(stage3) == (0, 0)


def test_auth_limits_health_ready_and_error_traces(api, stage3):
    assert api.get("/api/v1/health", headers={"X-Operator-Token": "wrong"}).status_code == 200
    assert api.get("/api/v1/ready").status_code == 200
    response = api.get("/api/v1/proposals", headers={"X-Operator-Token": "wrong"})
    assert response.status_code == 401 and response.json()["error"]["code"] == "UNAUTHORIZED"
    assert response.headers["X-Request-Trace-Id"] == response.json()["request_trace_id"]
    assert "test-only-operator-token" not in response.text
    assert api.get("/api/v1/proposals", params={"limit": 51}).status_code == 422
    assert api.get("/api/v1/proposals/not-a-uuid").status_code == 422
    assert counts(stage3) == (0, 0)


def test_edit_reject_and_old_hash_http_contract(api, stage3):
    row = create(api)
    route = "/api/v1/proposals/" + row["proposal_id"]
    assert api.post(route + "/approve", json=guard(row)).status_code == 200
    edited = api.post(route + "/revisions", json={**guard(row), "draft_reply": "修改草稿后重新审核"}).json()
    assert edited["revision"] == 2 and edited["status"] == "pending_approval"
    assert api.post(route + "/execute", json=guard(row)).status_code == 409
    new_route = "/api/v1/proposals/" + edited["proposal_id"]
    assert api.post(new_route + "/approve", json=guard(row)).status_code == 409
    assert api.post(new_route + "/reject", json={**guard(edited), "rejection_reason": "客户尚未补充资料"}).status_code == 200
    assert api.post(new_route + "/execute", json=guard(edited)).status_code == 403
    assert counts(stage3) == (0, 0)


def test_commit_response_lost_then_query_original_proposal(api, stage3):
    import src.main as main
    row = create(api)
    route = "/api/v1/proposals/" + row["proposal_id"]
    api.post(route + "/approve", json=guard(row))
    def lost(phase, session):
        if phase == "after_commit":
            raise ConnectionError("response lost")
    main.app.dependency_overrides[get_aftersales_service] = lambda: AftersalesService(stage3["sessions"], stage3["clock"], fault_hook=lost)
    response = api.post(route + "/execute", json=guard(row))
    assert response.status_code == 202 and response.json()["status"] == "unknown"
    recovered = api.get(route + "/execution")
    assert recovered.status_code == 200 and recovered.json()["status"] == "executed"
    assert api.post(route + "/execute", json=guard(row)).json()["ticket"]["id"] == recovered.json()["ticket"]["id"]
    assert counts(stage3) == (1, 1)


def test_request_context_isolation_at_http_boundary(api, stage3):
    import src.main as main
    main.app.dependency_overrides[get_analyzer] = lambda: AnalysisAgent(FakeLLM(reason=None), stage3["gateway"])
    first = api.post("/api/v1/proposals/analyze", json={"message": "ORD-1009 想退货"}).json()
    assert first["status"] == "needs_info"
    main.app.dependency_overrides[get_analyzer] = lambda: AnalysisAgent(FakeLLM(), stage3["gateway"])
    related = api.post("/api/v1/proposals/analyze", json={"message": "音箱坏了", "request_id": first["request_id"]}).json()
    separate = api.post("/api/v1/proposals/analyze", json={"message": "音箱坏了"}).json()
    assert related["revision"] == 2 and related["kind"] == "return_request"
    assert separate["kind"] == "handoff" and separate["request_id"] != related["request_id"]
    assert separate["entities"]["order_id"] is None and separate["entities"]["intent"] == "general"
    assert counts(stage3) == (0,0)
