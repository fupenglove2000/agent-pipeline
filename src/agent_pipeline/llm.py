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
    """Thin wrapper over the chat model. No retry logic — that's the graph's job.

    Not exercised by the test suite: doing so would need a live API key, and the
    suite must run offline. Intentional gap, not an oversight — graph behaviour
    is covered via the fake in tests/test_graph.py.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._model = ChatOpenAI(
            model=settings.model_name, api_key=SecretStr(settings.openai_api_key)
        )

    def complete(self, prompt: str) -> LLMResponse:
        response = self._model.invoke(prompt)
        text = response.content if isinstance(response.content, str) else str(response.content)
        usage = response.usage_metadata
        token_usage = usage["total_tokens"] if usage is not None else 0
        return LLMResponse(text=text, token_usage=token_usage)
