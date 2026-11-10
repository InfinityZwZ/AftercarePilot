from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import UUID

import pytest
from sqlalchemy import select, func

from scripts.seed_demo import REFERENCE_TIME
from src.domain.clock import FixedClock
from src.domain.exceptions import AppError
from src.services.aftersales_service import AftersalesService
from src.storage.models import AftersalesTicket, AuditLog, Order, Proposal, ReturnPolicy
from tests.fakes import FakeLLM
from tests.test_proposal_approval import analyze, counts, execute, review


def approved(stage3):
    row = analyze(stage3)
    review(stage3, row)
    return row


def test_execute_commits_ticket_status_and_success_audit_then_independent_read(stage3):
    row = approved(stage3)
    result = execute(stage3, row)
    assert result["ticket"]["status"] == "requested" and result["ticket"]["requested_amount_cents"] == 8999
    assert result["ticket"]["currency"] == "USD" and counts(stage3) == (1, 1)
    assert stage3["proposals"].get(UUID(row["proposal_id"]))["status"] == "executed"
    assert stage3["aftersales"].get_execution(UUID(row["proposal_id"])) == result
    assert stage3["aftersales"].get_ticket(UUID(result["ticket"]["id"])) == result["ticket"]


@pytest.mark.parametrize("phase", ["after_ticket_flush", "after_status_flush", "after_audit_flush", "before_commit"])
def test_failure_rolls_back_all_three_business_writes(stage3, phase):
    row = approved(stage3)
    def fail(where, session):
        if where == phase:
            raise RuntimeError("injected fault")
    service = AftersalesService(stage3["sessions"], stage3["clock"], fault_hook=fail)
    with pytest.raises(AppError) as caught:
        execute(stage3, row, service)
    assert caught.value.status == 500 and counts(stage3) == (0, 0)
    assert stage3["proposals"].get(UUID(row["proposal_id"]))["status"] == "approved"
    with stage3["sessions"]() as session:
        assert session.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.event_type == "execution_failed")) == 1
    assert execute(stage3, row)["status"] == "executed"


def test_same_proposal_replay_including_after_expiry(stage3):
    row = approved(stage3)
    original = execute(stage3, row)
    later = AftersalesService(stage3["sessions"], FixedClock(REFERENCE_TIME + timedelta(days=40)))
    assert execute(stage3, row, later) == original
    assert counts(stage3) == (1, 1)


def test_two_proposals_same_order_not_own_success(stage3):
    first, second = approved(stage3), approved(stage3)
    result = execute(stage3, first)
    with pytest.raises(AppError) as caught:
        execute(stage3, second)
    assert caught.value.code == "RETURN_ALREADY_EXISTS"
    assert caught.value.details["existing_ticket"]["ticket_no"] == result["ticket"]["ticket_no"]
    assert stage3["proposals"].get(UUID(second["proposal_id"]))["status"] == "approved"
    assert counts(stage3) == (1, 1)


@pytest.mark.parametrize("same_proposal", [True, False])
def test_real_pg_concurrent_execute(stage3, same_proposal):
    first = approved(stage3)
    second = first if same_proposal else approved(stage3)
    barrier = Barrier(2)
    def worker(row):
        barrier.wait(timeout=5)
        try:
            return execute(stage3, row)
        except AppError as exc:
            return exc
    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(worker, (first, second)))
    successes = [result for result in outputs if isinstance(result, dict)]
    assert counts(stage3) == (1, 1)
    if same_proposal:
        assert len(successes) == 2 and successes[0]["ticket"]["id"] == successes[1]["ticket"]["id"]
    else:
        assert len(successes) == 1
        assert next(result for result in outputs if isinstance(result, AppError)).code == "RETURN_ALREADY_EXISTS"


def test_response_loss_after_commit_is_unknown_then_readable(stage3):
    row = approved(stage3)
    def lose_response(phase, session):
        if phase == "after_commit":
            raise ConnectionError("HTTP response lost")
    service = AftersalesService(stage3["sessions"], stage3["clock"], fault_hook=lose_response)
    with pytest.raises(AppError) as caught:
        execute(stage3, row, service)
    assert caught.value.status == 202 and caught.value.code == "EXECUTION_UNKNOWN"
    recovered = stage3["aftersales"].get_execution(UUID(row["proposal_id"]))
    assert all(recovered["verification"].values())
    assert execute(stage3, row) == recovered and counts(stage3) == (1, 1)


@pytest.mark.parametrize("field,value", [("total_cents", 10000), ("status", "shipped"), ("delivered_at", REFERENCE_TIME - timedelta(days=5)), ("version", 2)])
def test_changed_order_snapshot_refuses_execution(stage3, field, value):
    row = approved(stage3)
    with stage3["sessions"].begin() as session:
        setattr(session.get(Order, "ORD-1009"), field, value)
    with pytest.raises(AppError) as caught:
        execute(stage3, row)
    assert caught.value.code == "STALE_PROPOSAL" and counts(stage3) == (0, 0)


@pytest.mark.parametrize("change", ["disabled", "rules_without_version"])
def test_changed_policy_even_without_version_increment_is_rejected(stage3, change):
    row = approved(stage3)
    with stage3["sessions"].begin() as session:
        policy = session.get(ReturnPolicy, "demo-return")
        if change == "disabled":
            policy.enabled = False
        else:
            policy.return_window_days = 10
    with pytest.raises(AppError) as caught:
        execute(stage3, row)
    assert caught.value.code == "STALE_PROPOSAL" and counts(stage3) == (0, 0)


def test_expired_proposal_and_wrong_guard(stage3):
    row = approved(stage3)
    late = AftersalesService(stage3["sessions"], FixedClock(REFERENCE_TIME + timedelta(days=2)))
    with pytest.raises(AppError) as caught:
        execute(stage3, row, late)
    assert caught.value.code == "PROPOSAL_EXPIRED"
    with pytest.raises(AppError):
        stage3["aftersales"].execute_proposal(UUID(row["proposal_id"]), 2, row["payload_hash"], "tester")
    assert counts(stage3) == (0, 0)


def test_reply_only_approval_is_never_executable(stage3):
    row = analyze(stage3, message="ORD-1002 物流在哪里", fake=FakeLLM(order_id="ORD-1002", intent="shipping_tracking", kind="reply_only"))
    review(stage3, row)
    with pytest.raises(AppError) as caught:
        execute(stage3, row)
    assert caught.value.status == 422


def test_missing_success_audit_is_inconsistent_not_success(stage3):
    row = approved(stage3)
    execute(stage3, row)
    with stage3["sessions"].begin() as session:
        session.execute(AuditLog.__table__.delete().where(AuditLog.event_type == "ticket_created"))
    with pytest.raises(AppError) as caught:
        stage3["aftersales"].get_execution(UUID(row["proposal_id"]))
    assert caught.value.code == "EXECUTION_INCONSISTENT"


def test_edit_and_execute_race_has_only_one_winner(stage3):
    row = approved(stage3)
    barrier = Barrier(2)
    def action(kind):
        barrier.wait(timeout=5)
        try:
            return execute(stage3, row) if kind == "execute" else stage3["proposals"].revise(UUID(row["proposal_id"]), row["revision"], row["payload_hash"], "并发编辑的新草稿", "tester")
        except AppError as exc:
            return exc
    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(action, ("edit", "execute")))
    assert sum(isinstance(value, dict) for value in outputs) == 1
    assert next(value for value in outputs if isinstance(value, AppError)).status == 409
    old = stage3["proposals"].get(UUID(row["proposal_id"]))
    assert counts(stage3) == ((1, 1) if old["status"] == "executed" else (0, 0))


def test_shared_policy_lock_allows_different_orders_to_execute_together(stage3):
    first = approved(stage3)
    second = analyze(stage3, message="ORD-1001 商品坏了，想退货", fake=FakeLLM(order_id="ORD-1001"))
    review(stage3, second)
    barrier = Barrier(2)
    def meet_after_insert(phase, session):
        if phase == "after_ticket_flush":
            barrier.wait(timeout=5)
    service = AftersalesService(stage3["sessions"], stage3["clock"], fault_hook=meet_after_insert)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(lambda row: execute(stage3, row, service), (first, second)))
    assert all(value["status"] == "executed" for value in outputs)
    assert counts(stage3, "ORD-1009") == counts(stage3, "ORD-1001") == (1, 1)


def test_pg_unique_failure_rolls_back_then_uses_fresh_session(stage3):
    from uuid import uuid4
    row = approved(stage3)
    def collide(phase, session):
        if phase == "after_status_flush":
            original = session.scalar(select(AftersalesTicket).where(AftersalesTicket.proposal_id == UUID(row["proposal_id"])))
            duplicate = {column.name: getattr(original, column.name) for column in AftersalesTicket.__table__.columns}
            duplicate.update(id=uuid4(), ticket_no="TEST-UNIQUE-COLLISION")
            session.add(AftersalesTicket(**duplicate))
            session.flush()
    service = AftersalesService(stage3["sessions"], stage3["clock"], fault_hook=collide)
    with pytest.raises(AppError) as caught:
        execute(stage3, row, service)
    assert caught.value.code == "EXECUTION_CONFLICT" and counts(stage3) == (0, 0)
    assert execute(stage3, row)["status"] == "executed"


def test_database_unavailable_execution_is_unknown_not_not_executed():
    from sqlalchemy.exc import OperationalError
    from uuid import uuid4
    class Broken:
        def __call__(self):
            raise OperationalError("query", {}, Exception("secret connection string"))
    with pytest.raises(AppError) as caught:
        AftersalesService(Broken()).get_execution(uuid4())
    assert caught.value.code == "EXECUTION_UNKNOWN" and caught.value.status == 503
    assert "secret" not in caught.value.message


def test_expiry_rechecked_after_lock_wait_and_before_commit(stage3):
    row = approved(stage3)
    class ClockAfterLock:
        calls = 0
        def now(self):
            self.calls += 1
            return REFERENCE_TIME if self.calls == 1 else REFERENCE_TIME + timedelta(days=2)
    service = AftersalesService(stage3["sessions"], ClockAfterLock())
    with pytest.raises(AppError) as caught:
        execute(stage3, row, service)
    assert caught.value.code == "PROPOSAL_EXPIRED" and counts(stage3) == (0, 0)
    assert stage3["proposals"].get(UUID(row["proposal_id"]))["status"] == "expired"
    row = approved(stage3)
    class MutableClock:
        instant = REFERENCE_TIME
        def now(self):
            return self.instant
    clock = MutableClock()
    def expire_before_commit(phase, session):
        if phase == "before_commit":
            clock.instant += timedelta(days=2)
    service = AftersalesService(stage3["sessions"], clock, fault_hook=expire_before_commit)
    with pytest.raises(AppError) as caught:
        execute(stage3, row, service)
    assert caught.value.code == "PROPOSAL_EXPIRED" and counts(stage3) == (0, 0)
    assert stage3["proposals"].get(UUID(row["proposal_id"]))["status"] == "expired"
