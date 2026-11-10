from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A timezone-aware datetime is required")
    return value.astimezone(timezone.utc)


def to_local(value: datetime) -> datetime:
    return utc(value).astimezone()


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now().astimezone()


@dataclass(frozen=True)
class FixedClock:
    instant: datetime

    def __post_init__(self):
        utc(self.instant)

    def now(self) -> datetime:
        return utc(self.instant)
