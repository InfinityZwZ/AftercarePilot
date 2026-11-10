"""Native model tool schemas and transport-aware wrappers; exactly three reads."""
from langchain_core.tools import tool

from src.config import settings
from src.domain.schemas import ReturnReason
from src.tools.factory import create_read_gateway, invoke_read, read_session


@tool
async def get_order(order_id: str) -> str:
    """Read an order by ORD identifier, including its simulated shipment. Ask for the order ID if missing."""
    async with read_session(create_read_gateway()) as gateway:
        return (await invoke_read(gateway, "get_order", order_id=order_id)).model_dump_json()


@tool
async def get_return_policy(policy_id: str = settings.default_policy_id) -> str:
    """Read a return policy by policy ID. Use the order's policy_id or the configured demo policy."""
    async with read_session(create_read_gateway()) as gateway:
        return (await invoke_read(gateway, "get_return_policy", policy_id=policy_id)).model_dump_json()


@tool
async def check_return_eligibility(order_id: str, reason: ReturnReason | None = None) -> str:
    """Check deterministic whole-order return eligibility. This reads facts and cannot create a ticket or refund."""
    async with read_session(create_read_gateway()) as gateway:
        return (await invoke_read(gateway, "check_return_eligibility", order_id=order_id, reason=reason)).model_dump_json()


ALL_TOOLS = [get_order, get_return_policy, check_return_eligibility]
