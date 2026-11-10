import asyncio
import json
from uuid import uuid4

from langchain_core.messages import AIMessage, ToolMessage
import pytest

from src.agent.nodes import AnalysisAgent
from src.domain.exceptions import AppError
from tests.fakes import FakeLLM


def identity(question="ORD-1009 音箱坏了，申请退货", context=None):
    return dict(proposal_id=uuid4(), request_id=uuid4(), revision=1, context=context or {}, message=question, language="zh")


def run(agent, question="ORD-1009 音箱坏了，申请退货", context=None):
    return asyncio.run(agent.run(identity(question, context)))


def test_exactly_three_read_tools():
    from src.agent.tools import ALL_TOOLS
    assert {tool.name for tool in ALL_TOOLS} == {"get_order", "get_return_policy", "check_return_eligibility"}
    assert len(ALL_TOOLS) == 3


def test_policy_consultation_exposes_only_policy_read(stage3):
    fake = FakeLLM(order_id=None, reason=None, intent='return_policy', kind='reply_only')
    result = run(AnalysisAgent(fake, stage3['gateway']), '商品损坏是否属于允许的退货原因？')
    assert result['payload']['kind'] == 'reply_only'
    assert fake.bound_history[0] == ['get_return_policy'] and fake.bound_names == []
    assert [item['name'] for item in result['payload']['evidence']] == ['get_return_policy']


def test_truncated_output_does_not_spend_schema_repair(stage3):
    fake = FakeLLM(script=[AIMessage(content='', response_metadata={'finish_reason': 'length'})])
    result = run(AnalysisAgent(fake, stage3['gateway']))
    assert result['error']['code'] == 'MODEL_OUTPUT_TRUNCATED'
    assert result['llm_calls'] == 1 and result['schema_repairs'] == 0


def test_tool_adapter_reads_pg(gateway, monkeypatch):
    from src.agent import tools
    monkeypatch.setattr(tools, "create_read_gateway", lambda: gateway)
    result = json.loads(asyncio.run(tools.get_order.ainvoke({"order_id": "ORD-1009"})))
    assert result["ok"] and result["data"]["total_cents"] == 8999


def test_graph_structure():
    from src.agent.graph import build_graph
    assert {"triage", "decide", "tools", "validate"}.issubset(build_graph().get_graph().nodes)


def test_proper_messages_and_bound_code_facts(stage3):
    fake = FakeLLM()
    result = run(AnalysisAgent(fake, stage3["gateway"]))
    assert result["status"] == "pending_approval"
    assert result["payload"]["requested_amount_cents"] == 8999
    assert len(result["payload"]["evidence"]) == 3
    assert result["tool_calls"] == 3 and result["llm_calls"] == 3
    messages = fake.history[-1]
    tool_ai = next(message for message in messages if isinstance(message, AIMessage) and message.tool_calls)
    replies = [message for message in messages if isinstance(message, ToolMessage)]
    assert {call["id"] for call in tool_ai.tool_calls} == {message.tool_call_id for message in replies}
    assert len(replies) == 3
    assert set(fake.bound_history[0]) == {"get_order", "get_return_policy", "check_return_eligibility"}
    assert fake.bound_names == []


def test_repeated_tool_name_keeps_both_results(stage3):
    fake = FakeLLM()
    fake.script = [fake.triage(), AIMessage(content="", tool_calls=[dict(name="get_order", args={"order_id": "ORD-1009"}, id="first")]),
                   AIMessage(content="", tool_calls=[dict(name="get_order", args={"order_id": "ORD-1009"}, id="second"),
                   dict(name="get_return_policy", args={"policy_id": "demo-return"}, id="policy"),
                   dict(name="check_return_eligibility", args={"order_id": "ORD-1009", "reason": "defective"}, id="eligibility")]), fake.draft]
    result = run(AnalysisAgent(fake, stage3["gateway"]))
    assert not result["error"]
    assert len(result["payload"]["evidence"]) == 4
    assert len(set(item["call_id"] for item in result["evidence"])) == 4


@pytest.mark.parametrize("name", ["initiate_return", "create_ticket", "approve", "execute", "sql"])
def test_illegal_model_write_tool_cannot_execute(stage3, name):
    fake = FakeLLM(kind="reply_only")
    fake.script = [fake.triage(), AIMessage(content="", tool_calls=[dict(name=name, args={"order_id": "ORD-1009"}, id="illegal")]), fake.draft]
    result = run(AnalysisAgent(fake, stage3["gateway"]))
    assert result["evidence"][0]["result"]["code"] == "UNKNOWN_TOOL"
    assert result["tool_calls"] == 1
    assert result["status"] == "failed"
    from sqlalchemy import text
    with stage3["sessions"]() as session:
        assert session.scalar(text("SELECT count(*) FROM aftersales_tickets WHERE order_id='ORD-1009'")) == 0


def test_missing_reason_clarifies_without_default(stage3):
    result = run(AnalysisAgent(FakeLLM(reason=None), stage3["gateway"]), "ORD-1009 我想退货")
    assert result["payload"]["kind"] == "clarification"
    assert result["missing_fields"] == ["reason"]
    assert result["tool_calls"] == 0 and result["entities"]["reason"] is None


def test_no_invented_order_and_multiple_orders(stage3):
    agent = AnalysisAgent(FakeLLM(), stage3["gateway"])
    result = run(agent, "我想退货，商品坏了")
    assert result["entities"]["order_id"] is None
    assert result["status"] == "needs_info"
    result = run(AnalysisAgent(FakeLLM(), stage3["gateway"]), "ORD-1009 和 ORD-1001 都退货")
    assert result["missing_fields"] == ["single_order_id"]


def test_one_schema_repair_for_whole_run(stage3):
    fake = FakeLLM()
    fake.script = [AIMessage(content="invalid"), fake.triage(), fake.reads(), AIMessage(content="bad final JSON")]
    result = run(AnalysisAgent(fake, stage3["gateway"]))
    assert result["status"] == "failed"
    assert result["schema_repairs"] == 1 and result["llm_calls"] == 4


def test_model_cannot_supply_amount_or_action(stage3):
    fake = FakeLLM()
    bad = AIMessage(content=json.dumps(dict(kind="return_request", summary="x", draft_reply="x", evidence_refs=[], requested_amount_cents=1)))
    fake.script = [fake.triage(), fake.reads(), bad, bad]
    result = run(AnalysisAgent(fake, stage3["gateway"]))
    assert result["status"] == "failed" and result["error"]["code"] == "MODEL_SCHEMA_INVALID"


def test_referenced_evidence_must_exist(stage3):
    fake = FakeLLM()
    fake.script = [fake.triage(), fake.reads(), AIMessage(content=json.dumps(dict(kind="return_request", summary="x", draft_reply="x", evidence_refs=["invented"])))]
    assert run(AnalysisAgent(fake, stage3["gateway"]))["error"]["code"] == "EVIDENCE_INVALID"


def test_third_round_can_finish_but_not_start_fourth(stage3):
    fake = FakeLLM()
    fake.script = [fake.triage(), AIMessage(content="", tool_calls=[dict(name="get_order", args={"order_id": "ORD-1009"}, id="order")]),
                   AIMessage(content="", tool_calls=[dict(name="get_return_policy", args={"policy_id": "demo-return"}, id="policy")]),
                   AIMessage(content="", tool_calls=[dict(name="check_return_eligibility", args={"order_id": "ORD-1009", "reason": "defective"}, id="eligibility")]), fake.draft]
    result = run(AnalysisAgent(fake, stage3["gateway"]))
    assert result["status"] == "pending_approval" and result["tool_rounds"] == 3 and result["llm_calls"] == 5
    fake = FakeLLM()
    repeats = [AIMessage(content="", tool_calls=[dict(name="get_order", args={"order_id": "ORD-1009"}, id=f"order-{i}")]) for i in range(4)]
    fake.script = [fake.triage(), *repeats]
    result = run(AnalysisAgent(fake, stage3["gateway"]))
    assert result["error"]["code"] == "TOOL_BUDGET_EXHAUSTED"
    assert result["tool_calls"] == 3


def test_transient_read_retries_once_and_records_both(stage3):
    from src.domain.schemas import ToolError, ToolResult
    class Flaky:
        attempts = 0
        def get_order(self, order_id):
            self.attempts += 1
            if self.attempts == 1:
                return ToolResult(ok=False, code="TEMP", retryable=True, data=None, error=ToolError(code="TEMP", message="temporary"))
            return stage3["gateway"].get_order(order_id)
        def get_return_policy(self, policy_id):
            return stage3["gateway"].get_return_policy(policy_id)
        def check_return_eligibility(self, order_id, reason):
            return stage3["gateway"].check_return_eligibility(order_id, reason)
    flaky = Flaky()
    result = run(AnalysisAgent(FakeLLM(), flaky))
    assert result["status"] == "pending_approval" and result["tool_calls"] == 4
    assert len(result["evidence"]) == 4 and flaky.attempts == 2


def test_invalid_arguments_and_cross_request_order_are_rejected(stage3):
    fake = FakeLLM(kind="reply_only")
    fake.script = [fake.triage(), AIMessage(content="", tool_calls=[
        dict(name="get_order", args={"order_id": "ORD-1001"}, id="other-order"),
        dict(name="check_return_eligibility", args={"order_id": "ORD-1009", "reason": "defective", "amount": 1}, id="extra-arg")]), fake.draft]
    result = run(AnalysisAgent(fake, stage3["gateway"]))
    assert result["tool_calls"] == 2
    assert all(item["result"]["code"] == "INVALID_ARGUMENT" for item in result["evidence"])


def test_tool_and_llm_budgets_are_hard_limits(stage3, monkeypatch):
    from src.config import settings
    fake = FakeLLM()
    fake.script = [fake.triage(), AIMessage(content="", tool_calls=[dict(name="get_order", args={"order_id": "ORD-1009"}, id=f"call-{i}") for i in range(8)])]
    result = run(AnalysisAgent(fake, stage3["gateway"]))
    assert result["tool_calls"] == 6 and result["error"]["code"] == "TOOL_BUDGET_EXHAUSTED"
    monkeypatch.setattr(settings, "max_llm_calls", 2)
    fake = FakeLLM()
    result = run(AnalysisAgent(fake, stage3["gateway"]))
    assert len(fake.history) == result["llm_calls"] == 2
    assert result["error"]["code"] == "LLM_BUDGET_EXHAUSTED"
