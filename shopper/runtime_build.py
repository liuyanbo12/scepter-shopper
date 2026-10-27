"""Public code identity, excluding secrets, task history and dependencies."""
from __future__ import annotations

import hashlib
from pathlib import Path


def build_fingerprint(root: Path) -> str:
    root = Path(root)
    files = []
    for name in ("app.py", "launcher.cjs", "service_host.cjs", "runtime_build.cjs", "restart-services.ps1"):
        target = root / name
        if target.is_file() and not target.is_symlink():
            files.append(name)

    def walk(relative: str, suffixes: set[str] | None):
        directory = root / relative
        if not directory.exists():
            return
        for target in directory.iterdir():
            if target.is_symlink():
                continue
            name = relative + "/" + target.name
            if target.is_dir() and target.name not in {"node_modules", "__pycache__"}:
                walk(name, suffixes)
            elif target.is_file() and (suffixes is None or target.suffix in suffixes):
                files.append(name)

    walk("shopper", {".py", ".js"})
    walk("browser-runtime", {".cjs", ".js", ".json"})
    walk("frontend/dist", None)
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode("utf-8") + b"\0")
        digest.update((root / name).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
