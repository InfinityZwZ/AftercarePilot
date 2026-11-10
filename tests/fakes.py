import json
from langchain_core.messages import AIMessage, ToolMessage


class FakeLLM:
    """Real LangChain messages, no provider client or HTTP calls."""
    def __init__(self, *, order_id="ORD-1009", reason="defective", intent="return_request", kind="return_request", script=None):
        self.order_id, self.reason, self.intent, self.kind = order_id, reason, intent, kind
        self.script = list(script) if script is not None else None
        self.history = []
        self.bound_names = []
        self.bound_history = []
        self.sequence = 0

    def bind_tools(self, tools):
        self.bound_names = [tool.name for tool in tools]
        self.bound_history.append(list(self.bound_names))
        return self

    def triage(self):
        return AIMessage(content=json.dumps(dict(intent=self.intent, order_id=self.order_id, reason=self.reason, language="zh", missing_fields=[])))

    def reads(self):
        args = [("get_order", {"order_id": self.order_id}), ("get_return_policy", {"policy_id": "demo-return"}),
                ("check_return_eligibility", {"order_id": self.order_id, "reason": self.reason})]
        if self.intent in ("order_status", "shipping_tracking"):
            args = args[:1]
        elif self.intent == "return_policy":
            args = args[1:2]
        return AIMessage(content="", tool_calls=[dict(name=name, args=value, id=f"call-{self.sequence}-{index}") for index, (name, value) in enumerate(args)])

    def draft(self, messages):
        refs = [json.loads(message.content)["call_id"] for message in messages if isinstance(message, ToolMessage) and json.loads(message.content)["result"]["ok"]]
        return AIMessage(content=json.dumps(dict(kind=self.kind, summary="建议按已读取事实审核售后申请。", draft_reply="这是待审核草稿。批准后可提交售后申请，目前没有发生退款或发送消息。", evidence_refs=refs)))

    async def ainvoke(self, messages):
        self.history.append(list(messages))
        self.sequence += 1
        if self.script is not None:
            if not self.script:
                raise AssertionError("FakeLLM script exhausted")
            result = self.script.pop(0)
            return result(messages) if callable(result) else result
        if self.sequence == 1:
            return self.triage()
        if self.sequence == 2 and self.intent != "general":
            return self.reads()
        return self.draft(messages)
