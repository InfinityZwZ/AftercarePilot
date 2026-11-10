from typing import Annotated, Any
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    proposal_id: str
    request_id: str
    revision: int
    question: str
    language: str
    context: dict[str, Any]
    messages: Annotated[list, add_messages]
    entities: dict[str, Any]
    intent: str
    evidence: list[dict[str, Any]]
    missing_fields: list[str]
    tool_rounds: int
    tool_calls: int
    llm_calls: int
    schema_repairs: int
    evidence_completions: int
    model_draft: dict | None
    payload: dict | None
    status: str
    error: dict | None
