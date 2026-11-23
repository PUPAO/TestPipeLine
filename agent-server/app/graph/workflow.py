from typing import Any, Literal
from langgraph.graph import END, START, StateGraph

from app.core.config import get_settings
from app.graph.nodes import (
    analyze_and_test_node,
    classify_node,
    escalate_node,
    generate_patch_node,
    request_approval_node,
    retrieve_node,
)
from app.graph.state import AgentState


def route_after_test(state: AgentState) -> Literal["request_approval", "generate_patch", "escalate"]:
    settings = get_settings()
    if state.get("test_passed"):
        return "request_approval"
    if state.get("retry_count", 0) < settings.MAX_PATCH_RETRIES:
        return "generate_patch"
    return "escalate"


def build_incident_graph():
    builder = StateGraph(AgentState)

    builder.add_node("classify", classify_node)
    builder.add_node("retrieve", retrieve_node)
    builder.add_node("generate_patch", generate_patch_node)
    builder.add_node("analyze_and_test", analyze_and_test_node)
    builder.add_node("request_approval", request_approval_node)
    builder.add_node("escalate", escalate_node)

    builder.add_edge(START, "classify")
    builder.add_edge("classify", "retrieve")
    builder.add_edge("retrieve", "generate_patch")
    builder.add_edge("generate_patch", "analyze_and_test")

    builder.add_conditional_edges(
        "analyze_and_test",
        route_after_test,
        {
            "request_approval": "request_approval",
            "generate_patch": "generate_patch",
            "escalate": "escalate",
        },
    )

    builder.add_edge("request_approval", END)
    builder.add_edge("escalate", END)

    return builder.compile()


_compiled_graph = build_incident_graph()


async def run_incident_graph(payload: dict[str, Any]) -> None:
    initial_state: AgentState = {
        "incident_id": payload["incident_id"],
        "error_hash": payload["error_hash"],
        "service_name": payload.get("service_name", "spring-backend"),
        "environment": payload.get("environment", "prod"),
        "exception_class": payload["exception_class"],
        "message": payload.get("message", ""),
        "stack_trace": payload["stack_trace"],
        "http_method": payload.get("http_method", "UNKNOWN"),
        "request_uri": payload.get("request_uri", "UNKNOWN"),
        "discord_thread_id": payload.get("discord_thread_id"),
        "error_category": "UNKNOWN",
        "target_domain": "general",
        "retrieved_files": [],
        "patch_diff": "",
        "patch_explanation": "",
        "branch_name": "",
        "affected_tests": [],
        "total_tests_count": 0,
        "executed_tests_count": 0,
        "test_duration_ms": 0,
        "test_passed": False,
        "test_error_log": None,
        "retry_count": 0,
        "groq_tokens": 0,
        "gemini_tokens": 0,
    }
    await _compiled_graph.ainvoke(initial_state)
