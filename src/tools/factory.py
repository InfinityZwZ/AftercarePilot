import asyncio
from contextlib import asynccontextmanager
import inspect

from src.config import settings
from src.tools.direct_gateway import DirectReadGateway


def create_read_gateway():
    if settings.read_transport == "direct":
        return DirectReadGateway()
    from src.mcp.gateway import MCPReadGateway
    return MCPReadGateway()


@asynccontextmanager
async def read_session(gateway):
    if hasattr(gateway, "__aenter__"):
        async with gateway:
            yield gateway
    else:
        yield gateway


async def invoke_read(gateway, name, **args):
    method = getattr(gateway, name)
    if inspect.iscoroutinefunction(method):
        return await method(**args)
    return await asyncio.to_thread(method, **args)
