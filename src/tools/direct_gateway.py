from time import perf_counter

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.domain.clock import Clock, SystemClock
from src.domain.schemas import ToolError, ToolMeta, ToolResult
from src.services.eligibility_service import EligibilityService
from src.services.order_service import OrderService
from src.services.policy_service import PolicyService
from src.storage.database import session_factory


class DirectReadGateway:
    transport = "direct"
    def __init__(self, sessions=None, clock: Clock | None = None):
        self._sessions = sessions
        self._clock = clock or SystemClock()

    def _read(self, callback) -> ToolResult:
        started = perf_counter()
        try:
            factory = self._sessions or session_factory(readonly=True)
            with factory() as session:
                session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
                code, data = callback(session)
                return ToolResult(ok=True, code=code, data=data,
                                  meta=ToolMeta(duration_ms=(perf_counter() - started) * 1000))
        except DBAPIError as exc:
            state = getattr(exc.orig, "sqlstate", None)
            retryable = state is None or state.startswith("08") or state in ("40001", "40P01", "57P01")
            return ToolResult(ok=False, code="DATABASE_UNAVAILABLE", retryable=retryable, data=None,
                              error=ToolError(code="DATABASE_UNAVAILABLE", message="PostgreSQL read failed"),
                              meta=ToolMeta(duration_ms=(perf_counter() - started) * 1000))
        except ValueError:
            return ToolResult(ok=False, code="CONFIGURATION_ERROR", data=None,
                              error=ToolError(code="CONFIGURATION_ERROR", message="PostgreSQL read configuration or facts invalid"))

    @staticmethod
    def _invalid() -> ToolResult:
        return ToolResult(ok=False, code="INVALID_ARGUMENT", data=None,
                          error=ToolError(code="INVALID_ARGUMENT", message="An identifier of 1–64 characters is required"))

    def get_order(self, order_id: str) -> ToolResult:
        if not isinstance(order_id, str) or not 1 <= len(order_id.strip()) <= 64:
            return self._invalid()
        def read(session):
            order = OrderService().get_order(session, order_id.strip().upper())
            return ("OK", {"found": True, **order.model_dump(mode="json")}) if order else ("ORDER_NOT_FOUND", {"found": False})
        return self._read(read)

    def get_return_policy(self, policy_id: str) -> ToolResult:
        if not isinstance(policy_id, str) or not 1 <= len(policy_id.strip()) <= 64:
            return self._invalid()
        def read(session):
            policy = PolicyService().get_return_policy(session, policy_id.strip())
            return ("OK", {"found": True, **policy.model_dump(mode="json")}) if policy else ("POLICY_NOT_FOUND", {"found": False})
        return self._read(read)

    def check_return_eligibility(self, order_id: str, reason: str | None = None) -> ToolResult:
        if not isinstance(order_id, str) or not 1 <= len(order_id.strip()) <= 64:
            return self._invalid()
        if reason is not None and (not isinstance(reason, str) or len(reason) > 32):
            return self._invalid()
        def read(session):
            result = EligibilityService().check_return_eligibility(session, order_id.strip().upper(), reason, self._clock)
            return ("ORDER_NOT_FOUND" if result.reason_codes == ["ORDER_NOT_FOUND"] else "OK", result.model_dump(mode="json"))
        return self._read(read)
