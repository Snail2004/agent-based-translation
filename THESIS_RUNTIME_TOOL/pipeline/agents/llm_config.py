from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_PRICING = {
    "input": 0.25,
    "cached_input": 0.025,
    "output": 2.00,
}


@dataclass(frozen=True)
class LLMConfig:
    """Configuration shared by every OpenAI-backed thesis runtime agent."""

    model: str
    temperature: float = 0.3
    seed: int = 20260612
    reasoning_effort: str = "minimal"
    verbosity: str | None = "low"
    max_output_tokens: int = 2048
    daily_token_cap: int = 2_400_000
    pricing: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_PRICING))

    def __post_init__(self) -> None:
        model_lower = self.model.lower()
        if "latest" in model_lower:
            raise ValueError(f"Model must be pinned, not an alias: {self.model}")
        if self.temperature < 0:
            raise ValueError("temperature must be non-negative")
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if self.daily_token_cap <= 0:
            raise ValueError("daily_token_cap must be positive")

        missing = {"input", "cached_input", "output"} - set(self.pricing)
        if missing:
            raise ValueError(f"pricing is missing keys: {sorted(missing)}")

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "LLMConfig":
        return cls(
            model=str(data["model"]),
            temperature=float(data.get("temperature", 0.3)),
            seed=int(data.get("seed", 20260612)),
            reasoning_effort=str(data.get("reasoning_effort", "minimal")),
            verbosity=data.get("verbosity", "low"),
            max_output_tokens=int(data.get("max_output_tokens", 2048)),
            daily_token_cap=int(data.get("daily_token_cap", 2_400_000)),
            pricing={
                key: float(value)
                for key, value in dict(data.get("pricing", DEFAULT_PRICING)).items()
            },
        )


def load_llm_config(path: str | Path) -> LLMConfig:
    """Load an LLMConfig from YAML."""

    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - exercised only without deps.
        raise RuntimeError("PyYAML is required to load LLM config files") from exc

    config_path = Path(path)
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected YAML mapping in {config_path}")
    return LLMConfig.from_mapping(data)
