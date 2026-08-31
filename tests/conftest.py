import pytest

from order_workflow.config import REPO_ROOT, Config
from order_workflow.erp import MockERP
from order_workflow.reference import ReferenceData


@pytest.fixture()
def config(tmp_path) -> Config:
    cfg = Config()
    cfg.llm_mode = "stub"
    cfg.runs_dir = tmp_path / "runs"
    cfg.erp_db_path = tmp_path / "erp.sqlite"
    cfg.data_dir = REPO_ROOT / "data"
    return cfg


@pytest.fixture(scope="session")
def reference() -> ReferenceData:
    return ReferenceData(REPO_ROOT / "data" / "reference")


@pytest.fixture()
def erp() -> MockERP:
    return MockERP(":memory:")
