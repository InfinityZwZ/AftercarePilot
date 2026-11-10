import asyncio
from copy import deepcopy
from uuid import UUID

import pytest
from sqlalchemy import select, func

from src.agent.nodes import AnalysisAgent
from src.domain.exceptions import AppError
from src.storage.models import AftersalesTicket, AuditLog, Proposal
from tests.fakes import FakeLLM


def analyze(stage3, *, message="ORD-1009 音箱坏了，我想退货", request_id=None, fake=None):
    return asyncio.run(stage3["proposals"].analyze(message, request_id=request_id, language="zh", operator="tester",
                       agent=AnalysisAgent(fake or FakeLLM(), stage3["gateway"])))


def review(stage3, row, **kwargs):
    return stage3["proposals"].review(UUID(row["proposal_id"]), row["revision"], row["payload_hash"], "tester", **kwargs)


def execute(stage3, row, service=None):
    return (service or stage3["aftersales"]).execute_proposal(UUID(row["proposal_id"]), row["revision"], row["payload_hash"], "tester")


def counts(stage3, order_id="ORD-1009"):
    with stage3["sessions"]() as session:
        tickets = session.scalar(select(func.count()).select_from(AftersalesTicket).where(AftersalesTicket.order_id == order_id))
        audit_count = session.scalar(select(func.count()).select_from(AuditLog).join(Proposal, AuditLog.proposal_id == Proposal.id).where(Proposal.order_id == order_id, AuditLog.event_type == "ticket_created"))
        return tickets, audit_count


def test_pending_and_approval_are_zero_ticket_writes(stage3):
    row = analyze(stage3)
    assert row["status"] == "pending_approval" and counts(stage3) == (0, 0)
    with pytest.raises(AppError, match="approved") as caught:
        execute(stage3, row)
    assert caught.value.status == 403 and counts(stage3) == (0, 0)
    approved = review(stage3, row)
    assert approved["status"] == "approved" and approved["reviewer_id"] == "tester"
    assert approved["approved_payload_hash"] == approved["payload_hash"]
    assert counts(stage3) == (0, 0)
    assert review(stage3, row)["reviewed_at"] == approved["reviewed_at"]
    with stage3["sessions"]() as session:
        assert session.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.event_type == "proposal_approved")) == 1


@pytest.mark.parametrize("approve_first", [False, True])
def test_edit_creates_new_revision_and_old_version_is_rejected(stage3, approve_first):
    old = analyze(stage3)
    if approve_first:
        review(stage3, old)
    new = stage3["proposals"].revise(UUID(old["proposal_id"]), 1, old["payload_hash"], "修改后的未发送草稿", "tester")
    assert new["proposal_id"] != old["proposal_id"] and new["revision"] == 2 and new["payload_hash"] != old["payload_hash"]
    assert new["status"] == "pending_approval" and new["approved_payload_hash"] is None
    assert stage3["proposals"].get(UUID(old["proposal_id"]))["status"] == "superseded"
    for action in (lambda: review(stage3, old), lambda: execute(stage3, old)):
        with pytest.raises(AppError) as caught:
            action()
        assert caught.value.status == 409
    assert counts(stage3) == (0, 0)
    review(stage3, new)
    assert execute(stage3, new)["ticket"]["approved_reply"] == "修改后的未发送草稿"


def test_rejection_requires_new_revision(stage3):
    row = analyze(stage3)
    rejected = review(stage3, row, reject_reason="需要客户补充证据")
    assert rejected["status"] == "rejected" and counts(stage3) == (0, 0)
    with pytest.raises(AppError) as caught:
        execute(stage3, row)
    assert caught.value.status == 403
    with pytest.raises(AppError):
        review(stage3, row)
    new = stage3["proposals"].revise(UUID(row["proposal_id"]), row["revision"], row["payload_hash"], "新版本草稿", "tester")
    assert new["status"] == "pending_approval"
    assert stage3["proposals"].get(UUID(row["proposal_id"]))["status"] == "rejected"


def test_payload_changed_without_hash_is_detected(stage3):
    row = analyze(stage3)
    with stage3["sessions"].begin() as session:
        stored = session.get(Proposal, UUID(row["proposal_id"]))
        changed = deepcopy(stored.payload)
        changed["draft_reply"] = "tampered"
        stored.payload = changed
    with pytest.raises(AppError) as caught:
        review(stage3, row)
    assert caught.value.code == "PAYLOAD_TAMPERED"
    assert counts(stage3) == (0, 0)


def test_approved_payload_tampering_is_detected_at_execute(stage3):
    row = analyze(stage3)
    review(stage3, row)
    with stage3["sessions"].begin() as session:
        stored = session.get(Proposal, UUID(row["proposal_id"]))
        changed = deepcopy(stored.payload)
        changed["requested_amount_cents"] = 1
        stored.payload = changed
    with pytest.raises(AppError) as caught:
        execute(stage3, row)
    assert caught.value.code == "PAYLOAD_TAMPERED" and counts(stage3) == (0, 0)


def test_missing_reason_can_continue_same_request_only(stage3):
    first = analyze(stage3, message="ORD-1009 想退货", fake=FakeLLM(reason=None))
    assert first["status"] == "needs_info" and first["payload"]["order_id"] is None
    second = analyze(stage3, message="音箱坏了", request_id=UUID(first["request_id"]))
    assert second["revision"] == 2 and second["payload"]["order_id"] == "ORD-1009"
    separate = analyze(stage3, message="音箱坏了")
    assert separate["request_id"] != first["request_id"] and separate["kind"] == "handoff"
    assert separate["entities"]["order_id"] is None and separate["entities"]["intent"] == "general"
    assert counts(stage3) == (0,0)


def test_explicit_new_order_overrides_context(stage3):
    first = analyze(stage3)
    other = analyze(stage3, message="其实是 ORD-1002，商品坏了", request_id=UUID(first["request_id"]), fake=FakeLLM(order_id="ORD-1002", kind="reply_only"))
    assert other["payload"]["order_id"] == "ORD-1002" and other["kind"] == "reply_only"
    assert counts(stage3) == (0, 0)


def test_failed_analysis_is_persisted_with_evidence(stage3):
    fake = FakeLLM()
    from langchain_core.messages import AIMessage
    fake.script = [fake.triage(), fake.reads(), AIMessage(content="bad"), AIMessage(content="still bad")]
    with pytest.raises(AppError) as caught:
        analyze(stage3, fake=fake)
    row = stage3["proposals"].get(UUID(caught.value.details["proposal_id"]))
    assert row["status"] == "failed" and len(row["payload"]["evidence"]) == 3


def test_recover_interrupted_analysis(stage3):
    identity = stage3["proposals"]._start("ORD-1009 broken", None, "en", "tester")
    stage3["proposals"].recover_interrupted()
    assert stage3["proposals"].get(identity["proposal_id"])["error"]["code"] == "ANALYSIS_INTERRUPTED"


def test_analyzing_request_rejects_another_analysis_without_model_calls(stage3):
    identity = stage3["proposals"]._start("ORD-1009 broken", None, "en", "tester")
    fake = FakeLLM()
    with pytest.raises(AppError) as caught:
        analyze(stage3, request_id=identity["request_id"], fake=fake)
    assert caught.value.code == "REQUEST_BUSY" and not fake.history


def test_total_timeout_preserves_completed_evidence(stage3, monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "analysis_timeout_seconds", 0.2)
    class SlowFinal(FakeLLM):
        async def ainvoke(self, messages):
            if self.sequence == 2:
                await asyncio.sleep(2)
            return await super().ainvoke(messages)
    with pytest.raises(AppError) as caught:
        analyze(stage3, fake=SlowFinal())
    row = stage3["proposals"].get(UUID(caught.value.details["proposal_id"]))
    assert row["status"] == "failed" and row["error"]["code"] == "ANALYSIS_TIMEOUT"
    assert len(row["payload"]["evidence"]) == 3 and row["usage"]["llm_calls"] == 3


def test_changing_facts_between_read_calls_do_not_make_executable_proposal(stage3):
    from src.storage.models import Order
    class ChangingGateway:
        def get_order(self, order_id):
            return stage3["gateway"].get_order(order_id)
        def get_return_policy(self, policy_id):
            with stage3["sessions"].begin() as session:
                session.get(Order, "ORD-1009").total_cents += 1
            return stage3["gateway"].get_return_policy(policy_id)
        def check_return_eligibility(self, order_id, reason):
            return stage3["gateway"].check_return_eligibility(order_id, reason)
    with pytest.raises(AppError) as caught:
        asyncio.run(stage3["proposals"].analyze("ORD-1009 音箱坏了", operator="tester", agent=AnalysisAgent(FakeLLM(), ChangingGateway())))
    assert caught.value.code == "EVIDENCE_INCONSISTENT"
    assert counts(stage3) == (0, 0)


@pytest.mark.parametrize("order_id,kind", [("ORD-DEMO-UNKNOWN", "handoff"), ("ORD-DEMO-EXPIRED", "reply_only"), ("ORD-DEMO-DUPLICATE", "reply_only")])
def test_normal_negative_decisions_are_saved_not_retried(stage3, order_id, kind):
    row = analyze(stage3, message=f"{order_id} 商品坏了，想退货", fake=FakeLLM(order_id=order_id, kind=kind))
    assert row["kind"] == kind and row["status"] == "pending_approval"
    assert row["usage"]["tool_calls"] == 3


def test_missing_order_is_normal_and_foreign_key_stays_null(stage3):
    row = analyze(stage3, message="ORD-NOT-FOUND 商品坏了", fake=FakeLLM(order_id="ORD-NOT-FOUND", kind="reply_only"))
    assert row["status"] == "pending_approval" and row["payload"]["order_id"] is None
    assert row["entities"]["order_id"] == "ORD-NOT-FOUND"
