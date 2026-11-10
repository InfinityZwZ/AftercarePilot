from uuid import UUID
from typing import Literal
from pydantic import Field, StrictInt, field_validator

from src.domain.proposals import Hash, Language, StrictModel


class AnalyzeRequest(StrictModel):
    message: str = Field(min_length=1, max_length=4000)
    request_id: UUID | None = None
    language: Language = "zh"

    @field_validator("message")
    @classmethod
    def question_nonempty(cls, value):
        if not value.strip():
            raise ValueError("Question cannot be empty")
        return value.strip()


class ChatRequest(AnalyzeRequest):
    session_id: str = Field(default="default", max_length=64)
    language: Language = "en"


class VersionRequest(StrictModel):
    expected_revision: StrictInt = Field(ge=1)
    expected_payload_hash: Hash


class RevisionRequest(VersionRequest):
    draft_reply: str = Field(min_length=1, max_length=8000)

    @field_validator("draft_reply")
    @classmethod
    def draft_nonempty(cls, value):
        if not value.strip():
            raise ValueError("Draft cannot be empty")
        return value


class RejectRequest(VersionRequest):
    rejection_reason: str = Field(min_length=1, max_length=1000)

    @field_validator("rejection_reason")
    @classmethod
    def rejection_nonempty(cls, value):
        if not value.strip():
            raise ValueError("Rejection reason cannot be empty")
        return value.strip()


class ManagementRequest(StrictModel):
    management_token: Hash


class ManagementItem(ManagementRequest):
    request_id: UUID


class BatchManagementRequest(StrictModel):
    items: list[ManagementItem] = Field(min_length=1, max_length=50)

    @field_validator('items')
    @classmethod
    def unique_requests(cls, values):
        if len({item.request_id for item in values}) != len(values):
            raise ValueError('Duplicate request')
        return values


class PurgeRequest(ManagementRequest):
    reason: Literal['test_data', 'duplicate_request', 'no_longer_needed']
    confirmation: Literal['永久清除']
