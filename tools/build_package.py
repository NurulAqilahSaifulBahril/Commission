"""Builds the release artifacts for the Finance Commission Dashboard.

Produces, under ``dist/``:

  payload/                              the installable file tree
  CommissionDashboard-update-<ver>.zip  the OTA package the app downloads
  SHA256SUMS.txt                        checksums the updater verifies against

The package is **code only**. Every ``1. Excel`` / ``2. Output`` / ``4. data``
folder, the dashboard database, logs and .env files are excluded, so installing
an update can never overwrite a month's workbooks or someone's credentials.

    python tools/build_package.py --version 1.2.0
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DIST = REPO_ROOT / "dist"

# Files/folders copied into the package, as glob patterns relative to the root.
INCLUDE = [
    "version.json",
    "README.md",
    "requirements*.txt",
    "*.py",
    "Launch Dashboard.bat",
    "Setup Environment.bat",
    "run_*.bat",
    "agent_name_map.json",
    "8. Web Dashboard/*.py",
    "8. Web Dashboard/*.json",
    "8. Web Dashboard/*.bat",
    "8. Web Dashboard/static/*",
    "9. assets/*",
    "*/3. Python Script/*.py",
    "*/3. Python script/*.py",
    "*/3. Python Script/*.json",
    "*/3. Python script/*.json",
    # app.py imports build_commission_pack from here.
    "7. Presentation/*.py",
    "tools/*.py",
]

# Applied after INCLUDE — anything matching is dropped.
EXCLUDE = [
    "scratch*",
    "*/scratch*",
    "*__pycache__*",
    "*.pyc",
    "*.env",
    "*.db",
    "*.db.backup*",
    "*.log",
    "*/data/*",
    "*/output/*",
    "8. Web Dashboard/plan.md",
    "*pg_proxy_token*",
]


def matches(rel: str, patterns: list[str]) -> bool:
    rel = rel.replace("\\", "/")
    return any(fnmatch.fnmatch(rel, pat) for pat in patterns)


def collect() -> list[Path]:
    """Resolve INCLUDE globs to concrete files, minus EXCLUDE."""
    found: set[Path] = set()
    for pattern in INCLUDE:
        for path in REPO_ROOT.glob(pattern):
            if path.is_file():
                found.add(path)
            elif path.is_dir():
                for child in path.rglob("*"):
                    if child.is_file():
                        found.add(child)

    keep = []
    for path in sorted(found):
        rel = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
        if matches(rel, EXCLUDE):
            continue
        keep.append(path)
    return keep


def write_version(version: str) -> None:
    target = REPO_ROOT / "version.json"
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    data["version"] = version
    data.setdefault("channel", "stable")
    data.setdefault("repo", "NurulAqilahSaifulBahril/Commission")
    target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"version.json -> {version}")


def _clean_dist() -> None:
    """OneDrive/Defender can momentarily hold a handle on a just-written folder,
    so a single rmtree is not reliable here."""
    import stat
    import time as _time

    def on_error(func, path, _exc):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except Exception:
            pass

    for attempt in range(4):
        if not DIST.exists():
            return
        shutil.rmtree(DIST, onerror=on_error)
        if not DIST.exists():
            return
        _time.sleep(1 + attempt)
    raise RuntimeError(f"Could not clear {DIST} — close anything using it and retry.")


def build(version: str) -> Path:
    _clean_dist()
    payload = DIST / "payload"
    payload.mkdir(parents=True)

    files = collect()
    for src in files:
        rel = src.relative_to(REPO_ROOT)
        dst = payload / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    print(f"payload: {len(files)} files -> {payload}")

    zip_path = DIST / f"CommissionDashboard-update-{version}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for src in files:
            zf.write(src, str(src.relative_to(REPO_ROOT)).replace("\\", "/"))
    print(f"package: {zip_path} ({zip_path.stat().st_size // 1024} KB)")
    return zip_path


def write_checksums() -> None:
    lines = []
    for path in sorted(DIST.iterdir()):
        if not path.is_file() or path.name.startswith("SHA256SUMS"):
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.name}")
    (DIST / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("SHA256SUMS.txt written for: " + ", ".join(l.split("  ")[1] for l in lines))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--version",
        help="Version to stamp into version.json. Defaults to whatever it already holds.",
    )
    parser.add_argument(
        "--checksums-only",
        action="store_true",
        help="Re-write SHA256SUMS.txt over the current dist/ contents (used after the installer is built).",
    )
    args = parser.parse_args()

    if args.checksums_only:
        write_checksums()
        return 0

    version = (args.version or "").lstrip("vV")
    if version:
        write_version(version)
    else:
        version = json.loads((REPO_ROOT / "version.json").read_text(encoding="utf-8"))["version"]

    build(version)
    write_checksums()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
