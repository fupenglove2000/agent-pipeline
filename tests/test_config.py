from decimal import Decimal

import pytest
from pydantic import ValidationError

from agent_pipeline.config import Settings

ENV_VARS = (
    "DATABASE_URL",
    "OPENAI_API_KEY",
    "MODEL_NAME",
    "PER_TASK_COST_CEILING_USD",
    "PER_RUN_COST_CEILING_USD",
    "TASK_LEASE_TIMEOUT_SECONDS",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _settings_without_dotenv(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg,arg-type]


def test_defaults_load_without_env_file() -> None:
    settings = _settings_without_dotenv()

    assert settings.model_name == "gpt-4o-mini"
    assert settings.task_lease_timeout_seconds == 900
    assert settings.per_task_cost_ceiling_usd == Decimal("0.50")
    assert settings.per_run_cost_ceiling_usd == Decimal("50.00")


def test_environment_variables_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_NAME", "gpt-4o")
    monkeypatch.setenv("TASK_LEASE_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("PER_TASK_COST_CEILING_USD", "1.25")

    settings = _settings_without_dotenv()

    assert settings.model_name == "gpt-4o"
    assert settings.task_lease_timeout_seconds == 120
    assert settings.per_task_cost_ceiling_usd == Decimal("1.25")


def test_per_run_ceiling_below_per_task_ceiling_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings_without_dotenv(
            per_task_cost_ceiling_usd=Decimal("5.00"),
            per_run_cost_ceiling_usd=Decimal("1.00"),
        )
