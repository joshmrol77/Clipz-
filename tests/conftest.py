import pytest


@pytest.fixture(autouse=True)
def _own_folder(tmp_path, monkeypatch):
    """Every test runs in its own empty folder, so a test's database never lands in the project."""
    monkeypatch.chdir(tmp_path)
