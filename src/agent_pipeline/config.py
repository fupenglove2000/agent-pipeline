"""Runtime configuration for agent-pipeline.

Budget ceilings (per-task and per-run) live here as configuration rather than
as constants because they are an operational knob, not a code decision. The
acceptable cost of a task or a run depends on the deployment (staging vs.
prod), the batch being processed, and how much risk the operator is willing
to take on a given day — none of which is known at the time the code is
written. Per ADR-0001, cost is enforced at LangGraph node boundaries at
runtime; making the ceiling a constant would mean a code change and a
redeploy every time that risk tolerance changes, which turns a business
decision into an engineering task.
"""

from decimal import Decimal
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = (
        "postgresql+psycopg://agent_pipeline:agent_pipeline@localhost:5432/agent_pipeline"
    )
    openai_api_key: str = ""
    model_name: str = "gpt-4o-mini"

    per_task_cost_ceiling_usd: Decimal = Decimal("0.50")
    per_run_cost_ceiling_usd: Decimal = Decimal("50.00")

    task_lease_timeout_seconds: int = 900

    @model_validator(mode="after")
    def _check_ceilings(self) -> "Settings":
        if self.per_run_cost_ceiling_usd < self.per_task_cost_ceiling_usd:
            raise ValueError(
                "per_run_cost_ceiling_usd must not be below per_task_cost_ceiling_usd "
                f"({self.per_run_cost_ceiling_usd} < {self.per_task_cost_ceiling_usd})"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
