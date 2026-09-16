"""INIT_TEST_DATA seeding must import scripts/ from the OSS repo root."""

import importlib.util
from pathlib import Path

from preloop.api.app import _REPO_ROOT, _ensure_repo_root_on_sys_path


def test_init_test_data_lives_at_repo_root_not_under_backend() -> None:
    """CI runs ``python -m preloop.server`` with cwd=backend/, not the repo root."""
    assert (_REPO_ROOT / "scripts" / "init_test_data.py").is_file()
    backend = _REPO_ROOT / "backend"
    assert backend.is_dir()
    assert not (backend / "scripts" / "init_test_data.py").exists()


def test_ensure_repo_root_makes_scripts_init_test_data_importable() -> None:
    """Lifespan can import the seeder even when only backend/ is on sys.path."""
    _ensure_repo_root_on_sys_path()
    spec = importlib.util.find_spec("scripts.init_test_data")
    assert spec is not None and spec.origin is not None
    assert (
        Path(spec.origin).resolve()
        == (_REPO_ROOT / "scripts" / "init_test_data.py").resolve()
    )
