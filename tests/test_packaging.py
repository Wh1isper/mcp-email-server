from __future__ import annotations

import contextlib
import hashlib
import http.cookiejar
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

pty: ModuleType | None
try:
    import pty
except ImportError:  # pragma: no cover - collected only by non-POSIX test runners
    pty = None

REPOSITORY = Path(__file__).resolve().parents[1]
STATIC_PREFIX = "mcp_email_server/web_ui/static/"
_LAUNCH_URL = re.compile(r"http://127\.0\.0\.1:\d+/manage-[A-Za-z0-9_-]+/#bootstrap=[A-Za-z0-9_-]+")


def _start_ui_in_terminal(
    command: list[str],
    *,
    environment: dict[str, str],
) -> tuple[subprocess.Popen[bytes], int, str]:
    if pty is None:
        raise RuntimeError("PTY-backed UI smoke is unavailable on this platform")
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(  # noqa: S603 - exact test-owned executable and arguments
            command,
            stdin=subprocess.DEVNULL,
            stdout=slave,
            stderr=slave,
            env=environment,
            start_new_session=True,
        )
    finally:
        os.close(slave)

    output = ""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        readable, _, _ = select.select([master], [], [], min(0.25, deadline - time.monotonic()))
        if not readable:
            continue
        try:
            output = f"{output}{os.read(master, 4096).decode(errors='replace')}"[-16_384:]
        except OSError:
            break
        match = _LAUNCH_URL.search(output)
        if match is not None:
            return process, master, match.group(0)

    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    process.wait(timeout=10)
    os.close(master)
    raise AssertionError("Installed UI did not produce an interactive launch URL")


def _stop_ui(process: subprocess.Popen[bytes], master: int) -> None:
    if process.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
    os.close(master)


def _assert_authenticated_ui(launch_url: str) -> None:
    parsed = urllib.parse.urlsplit(launch_url)
    fragment = urllib.parse.parse_qs(parsed.fragment, strict_parsing=True)
    assert set(fragment) == {"bootstrap"}
    assert len(fragment["bootstrap"]) == 1
    token = fragment["bootstrap"][0]
    base = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    origin = f"{parsed.scheme}://{parsed.netloc}"
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    with opener.open(base, timeout=10) as response:
        html = response.read()
        assert response.status == 200
        assert b"Local Email Management" in html
        assert response.headers["Cache-Control"] == "no-store"

    bootstrap = urllib.request.Request(  # noqa: S310 - exact loopback child process
        urllib.parse.urljoin(base, "api/bootstrap"),
        data=b"{}",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Origin": origin,
            "Sec-Fetch-Site": "same-origin",
        },
    )
    with opener.open(bootstrap, timeout=10) as response:
        exchange = json.loads(response.read())
        assert response.status == 200
        assert isinstance(exchange.get("csrf"), str)

    with opener.open(urllib.parse.urljoin(base, "api/status"), timeout=10) as response:
        status = json.loads(response.read())
        assert response.status == 200
        assert status["mode"] == "legacy"
        assert response.headers["Cache-Control"] == "no-store"


def _release_distributions() -> Path | None:
    value = os.getenv("MCP_EMAIL_SERVER_TEST_DIST_DIR")
    return Path(value).resolve() if value else None


def _static_files(wheel: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(wheel) as archive:
        return {
            name.removeprefix(STATIC_PREFIX): archive.read(name)
            for name in archive.namelist()
            if name.startswith(STATIC_PREFIX) and not name.endswith("/")
        }


def _static_digest(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name, content in sorted(files.items()):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(content)
    return digest.hexdigest()


def test_distribution_is_node_free_and_embeds_reproducible_ui(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    assert uv is not None
    distributions = _release_distributions()
    if distributions is None:
        distributions = tmp_path / "distributions"
        subprocess.run(  # noqa: S603 - controlled test command
            [uv, "build", "--out-dir", str(distributions)],
            cwd=REPOSITORY,
            check=True,
            capture_output=True,
            text=True,
        )
    sdist = next(distributions.glob("*.tar.gz"))
    wheel = next(distributions.glob("*.whl"))
    packaged = _static_files(wheel)
    staged = {
        path.relative_to(REPOSITORY / STATIC_PREFIX).as_posix(): path.read_bytes()
        for path in (REPOSITORY / STATIC_PREFIX).rglob("*")
        if path.is_file()
    }
    assert packaged == staged
    assert set(packaged) >= {"index.html", "THIRD_PARTY_NOTICES.md"}
    assert any(name.startswith("assets/") and name.endswith(".js") for name in packaged)
    assert any(name.startswith("assets/") and name.endswith(".css") for name in packaged)
    assert not any(name.endswith(".map") for name in packaged)

    extracted = tmp_path / "source"
    with tarfile.open(sdist) as archive:
        archive.extractall(extracted, filter="data")
    source = next(extracted.iterdir())
    for required in (
        "frontend/package.json",
        "frontend/package-lock.json",
        "frontend/THIRD_PARTY_NOTICES.md",
        "frontend/embedded-assets.json",
        "frontend/src/App.tsx",
        "frontend/e2e/local-management.spec.ts",
        "frontend/playwright.config.ts",
        "mcp_email_server/web_ui/static/index.html",
    ):
        assert (source / required).is_file()
    assert not (source / "frontend/node_modules").exists()
    assert not (source / "frontend/dist").exists()
    assert not (source / "frontend/test-results").exists()
    assert not (source / "frontend/playwright-report").exists()

    blockers = tmp_path / "blockers"
    blockers.mkdir()
    for executable in ("node", "npm", "npx"):
        path = blockers / executable
        path.write_text(f"#!/bin/sh\necho '{executable} must not run' >&2\nexit 97\n")
        path.chmod(0o755)
    rebuilt = tmp_path / "rebuilt"
    environment = os.environ.copy()
    environment["PATH"] = f"{blockers}{os.pathsep}{environment['PATH']}"
    frontend_check = [sys.executable, "dev/build_frontend.py", "--check"]
    subprocess.run(  # noqa: S603 - fixed interpreter and repository script
        frontend_check,
        cwd=source,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(  # noqa: S603 - controlled test command
        [uv, "build", "--wheel", "--out-dir", str(rebuilt)],
        cwd=source,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    rebuilt_wheel = next(rebuilt.glob("*.whl"))
    rebuilt_files = _static_files(rebuilt_wheel)
    assert rebuilt_files == packaged
    assert _static_digest(rebuilt_files) == _static_digest(staged)


@pytest.mark.skipif(os.name != "posix", reason="The V2 UI filesystem and PTY contract is POSIX-only")
def test_isolated_wheel_and_local_uvx_serve_authenticated_ui(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    assert uv is not None
    distributions = _release_distributions()
    if distributions is None:
        distributions = tmp_path / "distributions"
        subprocess.run(  # noqa: S603 - controlled test command
            [uv, "build", "--wheel", "--out-dir", str(distributions)],
            cwd=REPOSITORY,
            check=True,
            capture_output=True,
            text=True,
        )
    wheel = next(distributions.glob("*.whl"))
    environment = tmp_path / "environment"
    subprocess.run(  # noqa: S603 - controlled test command
        [uv, "venv", "--python", sys.executable, str(environment)],
        check=True,
        capture_output=True,
        text=True,
    )
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    executable = environment / ("Scripts/mcp-email-server.exe" if os.name == "nt" else "bin/mcp-email-server")
    subprocess.run(  # noqa: S603 - controlled test command
        [uv, "pip", "install", "--python", str(python), str(wheel)],
        check=True,
        capture_output=True,
        text=True,
    )

    run_root = tmp_path / "run"
    run_root.mkdir(mode=0o700)
    run_root.chmod(0o700)
    runtime_environment = os.environ.copy()
    runtime_environment["MCP_EMAIL_SERVER_CONFIG_PATH"] = str(run_root / "config.toml")

    process, terminal, launch_url = _start_ui_in_terminal(
        [str(executable), "ui", "--no-open", "--port", "0"],
        environment=runtime_environment,
    )
    try:
        _assert_authenticated_ui(launch_url)
    finally:
        _stop_ui(process, terminal)

    uvx_process, uvx_terminal, uvx_launch_url = _start_ui_in_terminal(
        [uv, "tool", "run", "--from", str(wheel), "mcp-email-server", "ui", "--no-open", "--port", "0"],
        environment=runtime_environment,
    )
    try:
        _assert_authenticated_ui(uvx_launch_url)
    finally:
        _stop_ui(uvx_process, uvx_terminal)
