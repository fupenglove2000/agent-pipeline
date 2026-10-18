from dataclasses import dataclass
from typing import Protocol

from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from agent_pipeline.config import get_settings


@dataclass(frozen=True)
class LLMResponse:
    text: str
    token_usage: int


class ChatModel(Protocol):
    def complete(self, prompt: str) -> LLMResponse: ...


class OpenAIChatModel:
    """Thin wrapper over the chat model.

    The openai SDK retries on its own by default (2 retries on connection errors,
    timeouts, 408/409/429 and 5xx). That would stack under call_model's RetryPolicy
    and turn ADR-0003's cap of 3 into up to 3 × 3 requests, so it is switched off
    with max_retries=0: retrying lives in one place, and the cap is the real cap.
    The cost: the SDK honours a rate-limit response's Retry-After header, and our
    RetryPolicy does not read it yet — it backs off on its own schedule instead.

    complete() is not exercised by the test suite: that would need a live API key,
    and the suite must run offline. Graph behaviour is covered via fakes instead.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._model = ChatOpenAI(
            model=settings.model_name,
            api_key=SecretStr(settings.openai_api_key),
            max_retries=0,
        )

    def complete(self, prompt: str) -> LLMResponse:
        response = self._model.invoke(prompt)
        text = response.content if isinstance(response.content, str) else str(response.content)
        usage = response.usage_metadata
        token_usage = usage["total_tokens"] if usage is not None else 0
        return LLMResponse(text=text, token_usage=token_usage)
