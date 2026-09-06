"""Tests for the per-user flyctl bootstrap."""

import hashlib
import io
import os
import tarfile
import zipfile

from kilntainers import fly_runtime


def test_install_flyctl_downloads_official_archive(monkeypatch, tmp_path) -> None:
    install_root = tmp_path / "fly"
    zipped = os.name == "nt"
    executable_name = "flyctl.exe" if zipped else "flyctl"
    monkeypatch.setenv("FLYCTL_INSTALL", str(install_root))
    monkeypatch.setattr(fly_runtime.shutil, "which", lambda command: None)
    monkeypatch.setattr(
        fly_runtime,
        "_platform_release",
        lambda: ("windows" if zipped else "Linux", "x86_64", zipped),
    )
    monkeypatch.setattr(
        fly_runtime,
        "_open_url",
        lambda url: io.BytesIO(b"https://downloads.example/flyctl-release"),
    )

    def fake_download(url, destination) -> None:
        assert url.startswith(
            "https://github.com/superfly/flyctl/releases/download/v0.4.99/"
        )
        if zipped:
            with zipfile.ZipFile(destination, mode="w") as bundle:
                bundle.writestr(executable_name, b"flyctl-binary")
                bundle.writestr("wintun.dll", b"wireguard-helper")
        else:
            with tarfile.open(destination, mode="w:gz") as bundle:
                payload = b"flyctl-binary"
                member = tarfile.TarInfo(executable_name)
                member.size = len(payload)
                bundle.addfile(member, io.BytesIO(payload))

    fixture = tmp_path / "fixture"
    fake_download(
        "https://github.com/superfly/flyctl/releases/download/v0.4.99/fixture", fixture
    )
    digests = dict(fly_runtime._RELEASE_DIGESTS)
    digests[("windows" if zipped else "Linux", "x86_64")] = hashlib.sha256(
        fixture.read_bytes()
    ).hexdigest()
    monkeypatch.setattr(fly_runtime, "_RELEASE_DIGESTS", digests)
    monkeypatch.setattr(
        fly_runtime,
        "_download",
        lambda url, path: path.write_bytes(fixture.read_bytes()),
    )

    executable = fly_runtime.install_flyctl()

    assert executable == install_root / "bin" / executable_name
    assert executable.read_bytes() == b"flyctl-binary"
    assert fly_runtime.ensure_flyctl("fly") == str(executable)


def test_install_rejects_checksum_mismatch_before_extracting(monkeypatch, tmp_path):
    import pytest

    from kilntainers.errors import BackendError

    monkeypatch.setenv("FLYCTL_INSTALL", str(tmp_path))
    monkeypatch.setattr(
        fly_runtime, "_download", lambda url, path: path.write_bytes(b"tampered")
    )
    with pytest.raises(BackendError, match="checksum mismatch"):
        fly_runtime.install_flyctl()
    assert not (tmp_path / "bin" / fly_runtime._executable_name()).exists()
