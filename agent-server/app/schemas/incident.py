from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


class IncidentWebhookPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    service_name: str = Field(default="spring-backend", alias="serviceName")
    environment: str = Field(default="prod", alias="environment")
    exception_class: str = Field(..., alias="exceptionClass")
    message: str = Field(default="No message", alias="message")
    stack_trace: str = Field(..., alias="stackTrace")
    http_method: str = Field(default="UNKNOWN", alias="httpMethod")
    request_uri: str = Field(default="UNKNOWN", alias="requestUri")
    occurred_at: Optional[str] = Field(default=None, alias="occurredAt")


class IncidentQueuedResponse(BaseModel):
    status: Literal["queued", "duplicate_ignored"]
    incident_id: str
    error_hash: str
    detail: str


class PendingApprovalData(BaseModel):
    incident_id: str
    error_hash: str
    error_category: str
    target_domain: str
    branch_name: str
    patch_diff: str
    patch_explanation: str
    affected_tests: list[str]
    executed_tests_count: int
    total_tests_count: int
    discord_thread_id: Optional[str] = None
