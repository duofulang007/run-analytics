"""Backend fingerprint shared by the local server and the CPod adapter."""
import hashlib
from pathlib import Path

REVISION_HEADER = "X-Run-Analytics-Revision"


def source_revision(root: Path) -> str:
    digest = hashlib.sha256()
    for name in ("serve.py", "metrics.py", "tasks.py", "bridge_version.py"):
        digest.update(name.encode("ascii") + b"\0")
        digest.update((root / "tools" / name).read_bytes())
    return digest.hexdigest()
