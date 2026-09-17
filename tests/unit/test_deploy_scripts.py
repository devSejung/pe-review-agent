from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"


SCRIPTS = [
    "bootstrap-host.sh",
    "build-local.sh",
    "build-release.sh",
    "configure-corporate-host.sh",
    "configure.sh",
    "doctor.sh",
    "install.sh",
    "lib.sh",
    "logs.sh",
    "manage.sh",
    "restart.sh",
    "set-admin-port.sh",
    "start.sh",
    "status.sh",
    "stop.sh",
]


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_deploy_shell_scripts_have_valid_syntax() -> None:
    for name in SCRIPTS:
        subprocess.run(
            ["bash", "-n", f"deploy/{name}"],
            check=True,
            cwd=ROOT,
        )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_manage_help_lists_lifecycle_commands() -> None:
    result = subprocess.run(
        ["bash", "deploy/manage.sh", "help"],
        check=True,
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    for command in ("install", "start", "stop", "restart", "status", "logs", "doctor"):
        assert command in result.stdout


def test_release_contains_operational_scripts() -> None:
    release_builder = (DEPLOY / "build-release.sh").read_text(encoding="utf-8")
    for name in (
        "manage.sh",
        "start.sh",
        "stop.sh",
        "restart.sh",
        "status.sh",
        "logs.sh",
        "doctor.sh",
        "configure.sh",
        "configure-corporate-host.sh",
        "build-local.sh",
    ):
        assert name in release_builder
    assert "corporate.env.example" in release_builder


def test_corporate_example_does_not_commit_private_infrastructure() -> None:
    example = (DEPLOY / "corporate.env.example").read_text(encoding="utf-8")
    assert "example.internal" in example
    assert "samsungds.net" not in example
    assert "10.166." not in example


def test_install_delegates_to_managed_install_flow() -> None:
    install = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    assert 'exec "$here/manage.sh" install' in install


def test_corporate_build_args_are_supported() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG PIP_INDEX_URL" in dockerfile
    assert "ARG PIP_TRUSTED_HOST" in dockerfile
    assert 'ARG APT_DEBIAN_MIRROR_URL=""' in dockerfile
    assert 'ARG APT_DEBIAN_SECURITY_MIRROR_URL=""' in dockerfile
