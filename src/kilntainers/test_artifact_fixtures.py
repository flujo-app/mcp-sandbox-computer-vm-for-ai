"""Do not confuse idle-removal races with Docker daemon failures."""

import importlib.util
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("missing", [True, False])
def test_artifact_inspection_race_only_accepts_exact_missing_container(
    monkeypatch, missing
):
    spec = importlib.util.spec_from_file_location(
        "artifact_smoke",
        Path(__file__).resolve().parents[2] / "scripts" / "smoke-package.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    container_id = "a" * 64
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        if args[0] == "ps":
            return subprocess.CompletedProcess(args, 0, container_id, "")
        assert args == ("inspect", container_id)
        message = (
            f"Error: No such object: {container_id}"
            if missing
            else "Cannot connect to Docker daemon"
        )
        return subprocess.CompletedProcess(args, 1, "", message)

    monkeypatch.setattr(module, "docker", docker)
    if missing:
        assert module.owned_containers() == []
    else:
        with pytest.raises(subprocess.CalledProcessError):
            module.owned_containers()
    assert all("rm" not in args for args in calls)
