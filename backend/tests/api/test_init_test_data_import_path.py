"""INIT_TEST_DATA seeding must import scripts/ from the OSS repo root."""

import importlib.util
import sys
from pathlib import Path

import pytest

from preloop.api.app import _REPO_ROOT, _ensure_repo_root_on_sys_path


def test_init_test_data_lives_at_repo_root_not_under_backend() -> None:
    """CI runs ``python -m preloop.server`` with cwd=backend/, not the repo root."""
    assert (_REPO_ROOT / "scripts" / "init_test_data.py").is_file()
    backend = _REPO_ROOT / "backend"
    assert backend.is_dir()
    assert not (backend / "scripts" / "init_test_data.py").exists()


def _scripts_module_names() -> list[str]:
    return [
        name for name in sys.modules if name == "scripts" or name.startswith("scripts.")
    ]


def test_ensure_repo_root_makes_scripts_init_test_data_importable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lifespan can import the seeder even when only backend/ is on sys.path."""
    resolved_root = _REPO_ROOT.resolve()
    path = [p for p in sys.path if p and Path(p).resolve() != resolved_root]
    monkeypatch.setattr(sys, "path", path)
    saved_scripts = {name: sys.modules[name] for name in _scripts_module_names()}
    for name in list(saved_scripts):
        sys.modules.pop(name, None)
    try:
        _ensure_repo_root_on_sys_path()
        assert str(_REPO_ROOT) in sys.path
        spec = importlib.util.find_spec("scripts.init_test_data")
        assert spec is not None and spec.origin is not None
        assert (
            Path(spec.origin).resolve()
            == (_REPO_ROOT / "scripts" / "init_test_data.py").resolve()
        )
    finally:
        for name in _scripts_module_names():
            if name not in saved_scripts:
                sys.modules.pop(name, None)
        sys.modules.update(saved_scripts)


def test_ensure_repo_root_skips_when_seeder_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Packaged installs must not append a non-checkout parents[3] path."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    fake_root = tmp_path / "not-a-checkout"
    fake_root.mkdir()
    monkeypatch.setattr("preloop.api.app._REPO_ROOT", fake_root)
    before = list(sys.path)
    _ensure_repo_root_on_sys_path()
    assert sys.path == before
    assert str(fake_root) not in sys.path
