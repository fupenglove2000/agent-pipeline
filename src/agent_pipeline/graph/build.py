import logging

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import RetryPolicy
from pydantic import ValidationError

from agent_pipeline.graph.failures import is_transient
from agent_pipeline.graph.state import GraphState, ParsedResult
from agent_pipeline.llm import ChatModel

logger = logging.getLogger(__name__)

# MALFORMED_OUTPUT's cap in ADR-0003's table: how many times call_model ↔ parse
# re-prompts before the graph gives up without a result.
MAX_PARSE_ATTEMPTS = 3

# TRANSIENT's cap and backoff in ADR-0003's table, applied by call_model's
# RetryPolicy. Total calls per call_model step, including the first. Independent
# of MAX_PARSE_ATTEMPTS: a rate-limit storm and bad JSON do not share a budget.
TRANSIENT_MAX_ATTEMPTS = 3
TRANSIENT_RETRY_INITIAL_INTERVAL_S = 0.5


def prepare(state: GraphState) -> dict[str, str]:
    # Prompt construction is the identity of raw_input for now — no templating
    # exists yet, and input normalisation belongs to ADR-0002's module, not here.
    # Kept as a no-op node (not inlined into call_model) so prompt templating has
    # a seam to land in later without changing the graph's shape.
    return {}


def _call_model(state: GraphState, llm: ChatModel) -> dict[str, object]:
    response = llm.complete(state["raw_input"])
    return {
        "last_response": response.text,
        "total_tokens": state["total_tokens"] + response.token_usage,
        "attempt_count": state["attempt_count"] + 1,
    }


def parse(state: GraphState) -> dict[str, ParsedResult | None]:
    try:
        result = ParsedResult.model_validate_json(state["last_response"] or "")
    except ValidationError as exc:
        logger.warning("parse failed on attempt %d: %s", state["attempt_count"], exc)
        return {}
    return {"parsed_result": result}


def _route_after_parse(state: GraphState) -> str:
    if state["parsed_result"] is not None:
        return END
    if state["attempt_count"] >= MAX_PARSE_ATTEMPTS:
        return END
    return "call_model"


def build_graph(llm: ChatModel, *, retry_backoff: bool = True) -> CompiledStateGraph[GraphState]:
    # retry_backoff=False only removes the waiting (zero interval, no jitter) so
    # tests don't sleep; which errors are retried and how often stay identical.
    transient_retry = RetryPolicy(
        max_attempts=TRANSIENT_MAX_ATTEMPTS,
        initial_interval=TRANSIENT_RETRY_INITIAL_INTERVAL_S if retry_backoff else 0.0,
        jitter=retry_backoff,
        retry_on=is_transient,
    )

    graph = StateGraph(GraphState)
    graph.add_node("prepare", prepare)
    graph.add_node(
        "call_model", lambda state: _call_model(state, llm), retry_policy=transient_retry
    )
    graph.add_node("parse", parse)

    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "call_model")
    graph.add_edge("call_model", "parse")
    graph.add_conditional_edges("parse", _route_after_parse)

    return graph.compile()
