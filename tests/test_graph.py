from collections.abc import Iterator

from agent_pipeline.graph.build import MAX_PARSE_ATTEMPTS, build_graph
from agent_pipeline.graph.state import GraphState
from agent_pipeline.llm import LLMResponse

GOOD_RESPONSE = '{"value": "ok"}'
BAD_RESPONSE = "not json"


class FakeChatModel:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses: Iterator[LLMResponse] = iter(responses)

    def complete(self, prompt: str) -> LLMResponse:
        return next(self._responses)


def _initial_state(raw_input: str = "task") -> GraphState:
    return {
        "raw_input": raw_input,
        "last_response": None,
        "parsed_result": None,
        "attempt_count": 0,
        "total_tokens": 0,
    }


def test_well_formed_response_finishes_in_one_pass() -> None:
    llm = FakeChatModel([LLMResponse(text=GOOD_RESPONSE, token_usage=10)])
    graph = build_graph(llm)

    result = graph.invoke(_initial_state())

    assert result["attempt_count"] == 1
    assert result["parsed_result"] is not None
    assert result["parsed_result"].value == "ok"


def test_malformed_then_good_response_takes_retry_path() -> None:
    llm = FakeChatModel(
        [
            LLMResponse(text=BAD_RESPONSE, token_usage=5),
            LLMResponse(text=GOOD_RESPONSE, token_usage=7),
        ]
    )
    graph = build_graph(llm)

    result = graph.invoke(_initial_state())

    assert result["attempt_count"] == 2
    assert result["parsed_result"] is not None
    assert result["parsed_result"].value == "ok"


def test_always_malformed_stops_at_max_attempts_without_raising() -> None:
    llm = FakeChatModel(
        [LLMResponse(text=BAD_RESPONSE, token_usage=1) for _ in range(MAX_PARSE_ATTEMPTS)]
    )
    graph = build_graph(llm)

    result = graph.invoke(_initial_state())

    assert result["attempt_count"] == MAX_PARSE_ATTEMPTS
    assert result["parsed_result"] is None


def test_total_tokens_accumulates_across_attempts() -> None:
    llm = FakeChatModel(
        [
            LLMResponse(text=BAD_RESPONSE, token_usage=3),
            LLMResponse(text=BAD_RESPONSE, token_usage=4),
            LLMResponse(text=GOOD_RESPONSE, token_usage=5),
        ]
    )
    graph = build_graph(llm)

    result = graph.invoke(_initial_state())

    assert result["total_tokens"] == 12
    assert result["attempt_count"] == 3
