"""Path handling for disposable GnuPG integration-test keyrings."""

import subprocess
from pathlib import PurePath


def gnupg_home_path(gpgconf: PurePath, home: PurePath) -> str:
    """Use the selected GnuPG installation's path convention on Windows."""
    if gpgconf.suffix.lower() != ".exe":
        return str(home)

    # Probe an installation path, not homedir: gpgconf can echo a configured
    # GNUPGHOME verbatim even when the rest of GnuPG cannot use that syntax.
    reported = subprocess.run(  # noqa: S603 - sibling GnuPG utility with fixed arguments
        (str(gpgconf), "--list-dirs", "bindir"),
        capture_output=True,
        check=True,
        timeout=10,
    ).stdout
    if not reported.startswith(b"/"):
        return str(home)

    return subprocess.run(  # noqa: S603 - converter from the same MSYS2 installation
        (str(gpgconf.with_name("cygpath.exe")), "-u", str(home)),
        capture_output=True,
        check=True,
        timeout=10,
        encoding="utf-8",
    ).stdout.rstrip("\r\n")
