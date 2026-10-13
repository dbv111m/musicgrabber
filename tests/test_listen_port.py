"""Exercise the real entrypoint without starting a server or touching the DB."""

import os
from pathlib import Path
import subprocess

import pytest


ENTRYPOINT = Path(__file__).resolve().parents[1] / "entrypoint.sh"


@pytest.fixture
def run_entrypoint(tmp_path):
    uvicorn = tmp_path / "uvicorn"
    uvicorn.write_text('#!/bin/bash\nprintf "argument:%s\\n" "$@"\n')
    uvicorn.chmod(0o755)

    def run(port=None, address=None):
        env = {
            key: value for key, value in os.environ.items()
            if key not in {"LISTEN_PORT", "LISTEN_ADDR"}
        }
        env.update({
            "PATH": f"{tmp_path}:{env['PATH']}",
            "PUID": "0", "PGID": "0", "YTDLP_AUTO_UPDATE": "false",
        })
        if port is not None:
            env["LISTEN_PORT"] = port
        if address is not None:
            env["LISTEN_ADDR"] = address
        return subprocess.run(
            ["bash", str(ENTRYPOINT)], env=env, capture_output=True,
            text=True, timeout=5,
        )

    return run


@pytest.mark.parametrize("port", [None, "", "1", "18080", "08080", "65535"])
def test_port_reaches_uvicorn_and_mapping_diagnostics(run_entrypoint, port):
    result = run_entrypoint(port)
    expected = port or "8080"
    assert result.returncode == 0, result.stderr
    arguments = [line.removeprefix("argument:") for line in result.stdout.splitlines()
                 if line.startswith("argument:")]
    assert arguments == ["app:app", "--host", "0.0.0.0", "--port", expected]
    assert f"Configured bind: 0.0.0.0:{expected}" in result.stdout
    assert f"publish port {expected} on the VPN container" in result.stdout
    assert "recreate the container" in result.stdout


def test_custom_address_reaches_uvicorn(run_entrypoint):
    result = run_entrypoint("18080", "::")
    assert result.returncode == 0, result.stderr
    assert "argument:::\nargument:--port\nargument:18080" in result.stdout


@pytest.mark.parametrize("port", ["0", "00000", "65536", "99999999999999999999", "-1", "8080.5", "abc", " 8080"])
def test_invalid_port_fails_before_boot_work(run_entrypoint, port):
    result = run_entrypoint(port)
    assert result.returncode == 1
    assert "LISTEN_PORT must be a whole number from 1 to 65535" in result.stderr
    assert result.stdout == ""
