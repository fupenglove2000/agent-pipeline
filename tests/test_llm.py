from collections.abc import Iterator

import pytest

from agent_pipeline.config import get_settings
from agent_pipeline.llm import OpenAIChatModel


@pytest.fixture
def fake_api_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_sdk_level_retries_are_disabled(fake_api_key: None) -> None:
    # Retrying belongs to call_model's RetryPolicy alone (ADR-0003); a non-zero
    # value here would multiply its cap. Constructing the client sends no request.
    model = OpenAIChatModel()

    assert model._model.root_client.max_retries == 0
