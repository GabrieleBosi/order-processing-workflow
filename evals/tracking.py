"""The one place MLflow is configured, and the facts every run must record.

Same shape as mlops-loop's `tracking` module: the tracking store is a local
SQLite file, the experiment is created on first use, and provenance (git
commit, prompt hashes, suite version) is computed here so no caller can log a
run that cannot be traced back to the code and data that produced it.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import mlflow
from mlflow.tracking import MlflowClient

REPO_ROOT = Path(__file__).resolve().parent.parent
CASES_DIR = REPO_ROOT / "evals" / "cases"
SUITE_VERSION_FILE = REPO_ROOT / "evals" / "suite_version.txt"

# The two files that hold the prompts under test. Their git blob hash is a
# run parameter, so a prompt edit always shows up as a different run.
PROMPT_FILES = {
    "extract": REPO_ROOT / "src" / "order_workflow" / "steps" / "extract.py",
    "check": REPO_ROOT / "src" / "order_workflow" / "steps" / "check.py",
}

DEFAULT_TRACKING_URI = "sqlite:///mlflow.db"
DEFAULT_EXPERIMENT = "order-processing-evals"


def configure(tracking_uri: str | None = None, experiment: str | None = None) -> str:
    """Point MLflow at the store and select the experiment. Returns its id."""
    uri = tracking_uri or os.environ.get("MLFLOW_TRACKING_URI") or DEFAULT_TRACKING_URI
    name = experiment or os.environ.get("MLFLOW_EXPERIMENT") or DEFAULT_EXPERIMENT
    mlflow.set_tracking_uri(uri)
    client = MlflowClient()
    existing = client.get_experiment_by_name(name)
    experiment_id = existing.experiment_id if existing else client.create_experiment(name)
    mlflow.set_experiment(experiment_id=experiment_id)
    return experiment_id


def _git(args: list[str]) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def git_info() -> dict[str, str]:
    """Commit and dirty flag; honest placeholders when git is unavailable."""
    commit = _git(["rev-parse", "HEAD"]) or "no-git"
    status = _git(["status", "--porcelain"])
    return {"git_commit": commit, "git_dirty": str(bool(status)) if status is not None else "unknown"}


def prompt_hashes() -> dict[str, str]:
    """Git blob hash per prompt file, plus a content hash of the prompt text.

    The file hash is what the brief asks for. The text hash is the one that
    actually matters when reading two runs side by side: it only moves when
    the prompt string itself moves, not when the surrounding code is
    refactored.
    """
    from order_workflow.steps.check import CHECK_SYSTEM
    from order_workflow.steps.extract import EXTRACTION_SYSTEM

    out: dict[str, str] = {}
    for name, path in PROMPT_FILES.items():
        blob = _git(["hash-object", str(path)])
        if blob is None:  # no git: hash the bytes the same way git would
            data = path.read_bytes()
            blob = hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()  # noqa: S324
        out[f"prompt_file_git_hash_{name}"] = blob
    for name, text in (("extract", EXTRACTION_SYSTEM), ("check", CHECK_SYSTEM)):
        out[f"prompt_text_sha256_{name}"] = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return out


def suite_version() -> dict[str, str]:
    """Declared suite version plus a content hash over every case.json.

    The declared number is bumped by hand when the case set changes meaning;
    the content hash catches the case where someone forgot to bump it.
    """
    declared = SUITE_VERSION_FILE.read_text(encoding="utf-8").strip()
    digest = hashlib.sha256()
    case_files = sorted(CASES_DIR.glob("*/case.json"))
    for path in case_files:
        digest.update(path.parent.name.encode("utf-8"))
        digest.update(
            json.dumps(
                json.loads(path.read_text(encoding="utf-8")), sort_keys=True, ensure_ascii=False
            ).encode("utf-8")
        )
    return {
        "suite_version": declared,
        "suite_content_hash": digest.hexdigest()[:16],
        "suite_n_cases": str(len(case_files)),
    }
