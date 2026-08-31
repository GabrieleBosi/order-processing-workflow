"""Runtime configuration, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent.parent


def _default_data_dir() -> Path:
    return Path(os.environ.get("ORDERFLOW_DATA_DIR", REPO_ROOT / "data"))


@dataclass
class Config:
    # LLM
    llm_mode: str = "auto"  # auto | anthropic | stub
    model: str = "claude-opus-5"
    max_tokens: int = 16000
    use_server_fallbacks: bool = True

    # Business rules (step 4)
    price_review_tolerance_pct: float = 2.0   # |delta| above this -> review
    price_block_tolerance_pct: float = 5.0    # |delta| above this -> reject
    min_lead_days: int = 2                    # delivery must be >= today + N days

    # Paths
    data_dir: Path = field(default_factory=_default_data_dir)
    runs_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("ORDERFLOW_RUNS_DIR", REPO_ROOT / "runs"))
    )
    erp_db_path: Path = field(
        default_factory=lambda: Path(os.environ.get("ORDERFLOW_ERP_DB", REPO_ROOT / "erp.sqlite"))
    )

    @property
    def reference_dir(self) -> Path:
        return self.data_dir / "reference"

    @property
    def samples_dir(self) -> Path:
        return self.data_dir / "samples"

    def llm_enabled(self) -> bool:
        if self.llm_mode == "stub":
            return False
        if self.llm_mode == "anthropic":
            return True
        # auto: enabled only when credentials are present
        return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def load_config() -> Config:
    cfg = Config()
    cfg.llm_mode = os.environ.get("ORDERFLOW_LLM", cfg.llm_mode).lower()
    cfg.model = os.environ.get("ORDERFLOW_MODEL", cfg.model)
    if os.environ.get("ORDERFLOW_NO_FALLBACKS"):
        cfg.use_server_fallbacks = False
    if os.environ.get("ORDERFLOW_PRICE_REVIEW_TOL"):
        cfg.price_review_tolerance_pct = float(os.environ["ORDERFLOW_PRICE_REVIEW_TOL"])
    if os.environ.get("ORDERFLOW_PRICE_BLOCK_TOL"):
        cfg.price_block_tolerance_pct = float(os.environ["ORDERFLOW_PRICE_BLOCK_TOL"])
    if os.environ.get("ORDERFLOW_MIN_LEAD_DAYS"):
        cfg.min_lead_days = int(os.environ["ORDERFLOW_MIN_LEAD_DAYS"])
    return cfg
