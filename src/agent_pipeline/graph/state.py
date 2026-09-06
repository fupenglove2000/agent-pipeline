from typing import TypedDict

from pydantic import BaseModel


class ParsedResult(BaseModel):
    """Placeholder shape for a successfully parsed model output.

    Real task output schemas are a later concern; this exists so `parse` has
    something concrete to validate into.
    """

    value: str


class GraphState(TypedDict):
    raw_input: str
    # The only channel from `call_model` to `parse` — the raw output of the most recent attempt.
    last_response: str | None
    parsed_result: ParsedResult | None
    attempt_count: int
    total_tokens: int
