"""Print a fingerprint of the current working tree for binding verification evidence.

The fingerprint covers HEAD, tracked changes and untracked files, excluding `.flow/`.
Any code change after verification produces a different fingerprint.
"""

import hashlib
import subprocess
import sys
from pathlib import Path

EXCLUDE = ":(exclude).flow"


def git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, check=True
    ).stdout


def fingerprint(cwd="."):
    root = Path(git(["rev-parse", "--show-toplevel"], cwd).decode().strip())
    try:
        head = git(["rev-parse", "HEAD"], root).decode().strip()
    except subprocess.CalledProcessError:
        head = "no-commit"
    digest = hashlib.sha256()
    digest.update(git(["diff", "HEAD", "--binary", "--", ".", EXCLUDE], root)
                  if head != "no-commit" else b"")
    untracked = git(
        ["ls-files", "--others", "--exclude-standard", "-z", "--", ".", EXCLUDE], root
    ).split(b"\0")
    for name in sorted(n for n in untracked if n):
        digest.update(name + b"\0")
        path = root / name.decode("utf-8", "surrogateescape")
        if path.is_file():
            digest.update(path.read_bytes())
    return f"{head[:12]}+{digest.hexdigest()[:12]}"


if __name__ == "__main__":
    try:
        print(fingerprint(sys.argv[1] if len(sys.argv) > 1 else "."))
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"fingerprint unavailable: {exc}", file=sys.stderr)
        sys.exit(1)
