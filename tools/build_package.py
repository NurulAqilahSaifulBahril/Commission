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
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DIST = REPO_ROOT / "dist"

# Built by `npm run dist` in "10. Electron App/app". Copied into the installer
# payload as <root>/shell/ but NEVER into the OTA zip — updates stay code-only,
# and updater.py pins "shell" in PRESERVE_PATHS so they can't touch it either.
SHELL_BUILD = REPO_ROOT / "10. Electron App" / "app" / "dist" / "win-unpacked"

# ── Bundled Python runtime ───────────────────────────────────────────────────
# The dashboard runs from source, so the target machine needs an interpreter.
# It used to need its own: the installer demanded Python 3.10+ on PATH and then
# spent minutes building a .venv and pip-installing, behind a black console that
# non-technical staff are told not to close. Shipping python.org's *embeddable*
# distribution with the dependencies already in it removes both steps.
#
# Staged to <root>/runtime/, alongside shell/ and pinned in updater.py's
# PRESERVE_PATHS for the same reason: it is installer-only payload, never part
# of a code-only OTA package, so an update can neither ship nor delete it.
PYTHON_EMBED_VERSION = "3.12.10"
PYTHON_EMBED_URL = (
    f"https://www.python.org/ftp/python/{PYTHON_EMBED_VERSION}"
    f"/python-{PYTHON_EMBED_VERSION}-embed-amd64.zip"
)
# Wheels are resolved FOR this version, not for whatever python is running the
# build — otherwise a 3.13 build host silently stages cp313 binaries into a
# cp312 runtime and every import of pandas/psycopg2 fails on the user's machine.
PYTHON_EMBED_TAG = ".".join(PYTHON_EMBED_VERSION.split(".")[:2])
RUNTIME_CACHE = REPO_ROOT / ".cache" / "python-embed"

# Files/folders copied into the package, as glob patterns relative to the root.
INCLUDE = [
    "version.json",
    "README.md",
    "requirements*.txt",
    "*.py",
    "Launch Dashboard.bat",
    "Setup Environment.bat",
    "run_*.bat",
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
    # Static reference data nfp_paths.get_620w_json_path() requires at
    # runtime -- not live state, so it belongs in the package unlike the rest
    # of "4. data" (tokens, generated reports), which EXCLUDE below still
    # keeps out. Named explicitly rather than "*/4. data/*" so a future file
    # dropped in that folder (a token, an export dump) is never shipped by
    # accident -- every other "4. data" folder in the repo holds exactly that
    # kind of file today.
    "2. NFP Commission/4. data/nfp_620w_schedule.json",
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


def _download_embed_zip() -> Path:
    """The embeddable Python zip, cached so repeat builds do not re-download."""
    RUNTIME_CACHE.mkdir(parents=True, exist_ok=True)
    dest = RUNTIME_CACHE / f"python-{PYTHON_EMBED_VERSION}-embed-amd64.zip"
    if dest.is_file() and dest.stat().st_size > 0:
        print(f"runtime: using cached {dest.name}")
        return dest
    print(f"runtime: downloading {PYTHON_EMBED_URL}")
    with urllib.request.urlopen(PYTHON_EMBED_URL, timeout=120) as resp:
        dest.write_bytes(resp.read())
    print(f"runtime: cached {dest.name} ({dest.stat().st_size // 1024} KB)")
    return dest


def _configure_pth(runtime: Path) -> None:
    """Point the embeddable build's `._pth` at everything the app needs.

    Two separate gotchas, both of which produce a runtime that starts and then
    fails on an import:

    * The shipped `python3xx._pth` comments out `import site`, so anything pip
      puts in Lib/site-packages is invisible -- `import flask` dies.
    * The presence of a `._pth` also puts the interpreter in isolated mode,
      which means the script's own directory is NOT prepended to sys.path the
      way it normally is. `app.py` lives beside `db.py` and imports it as a
      plain `import db`, so that dies too.

    The file's entries are relative to the directory holding python.exe, and
    runtime/ sits at the install root -- so ".." is the install root and
    "..\\8. Web Dashboard" is where the Flask app and its siblings live.
    """
    pth_files = list(runtime.glob("python*._pth"))
    if not pth_files:
        raise RuntimeError(f"no python*._pth found in {runtime}")
    pth = pth_files[0]

    sep = chr(92)  # backslash, spelled out so no escape handling can mangle it
    wanted = [
        "Lib" + sep + "site-packages",   # pip --target lands here
        "..",                            # install root (agent_names, commission_pdf)
        ".." + sep + "8. Web Dashboard",  # app.py's own package directory
    ]

    lines = [l.rstrip() for l in pth.read_text(encoding="utf-8").splitlines()]
    out = []
    for line in lines:
        if line.strip() in ("#import site", "# import site"):
            continue  # re-added at the end, where site.main() should run last
        out.append(line)

    present = {l.strip().lower().replace("/", sep) for l in out}
    for entry in wanted:
        if entry.lower() not in present:
            out.append(entry)
    out.append("import site")

    pth.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"runtime: configured {pth.name} -> {', '.join(wanted)} + import site")


def stage_runtime(payload: Path) -> bool:
    """Put a ready-to-run Python, dependencies included, at payload/runtime."""
    runtime = payload / "runtime"
    try:
        zip_path = _download_embed_zip()
    except Exception as e:
        print(f"WARNING: could not fetch the embeddable Python ({e}). "
              f"Installer will fall back to requiring Python on the machine.")
        return False

    runtime.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(runtime)
    _configure_pth(runtime)

    site_packages = runtime / "Lib" / "site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)
    req = REPO_ROOT / "requirements-dashboard.txt"
    cmd = [
        sys.executable, "-m", "pip", "install",
        "--target", str(site_packages),
        "--python-version", PYTHON_EMBED_TAG,
        "--platform", "win_amd64",
        "--only-binary=:all:",
        "--no-compile",
        "--upgrade",
        "-r", str(req),
    ]
    print(f"runtime: installing dependencies for cp{PYTHON_EMBED_TAG.replace('.', '')}...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout[-3000:])
        print(result.stderr[-3000:])
        raise RuntimeError("dependency install into the bundled runtime failed")

    total = sum(f.stat().st_size for f in runtime.rglob("*") if f.is_file())
    print(f"payload: Python {PYTHON_EMBED_VERSION} runtime -> {runtime} "
          f"({total // (1024 * 1024)} MB)")
    return True


def stage_seed_env(payload: Path, seed_env: Path) -> None:
    """Pre-fill the install's .env so the user never pastes access keys.

    INSTALLER-ONLY, like shell/ and runtime/: this lands in dist/payload for
    Inno to pick up and is never part of the OTA zip (which is built from the
    code file list, where *.env is excluded). An installer produced with this
    carries live credentials — distribute it over internal channels only,
    NEVER as an asset on the public GitHub Releases page. The CI release
    workflow does not pass --seed-env, so public builds stay key-free.

    Only the PG_* proxy lines are taken from the seed file. FLASK_SECRET_KEY
    must stay per-install (the server generates one on first boot), and
    anything else in a working .env (e.g. a retired DATABASE_URL) has no
    business being copied onto six machines.
    """
    allowed = ("PG_PROXY_URL", "PG_PROXY_TOKEN", "PG_PROXY_DB",
               "PG_MIRROR_TOKEN", "PG_MIRROR_DB", "PG_MIRROR_SCHEMA")
    lines = []
    for raw in seed_env.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if "=" not in line or line.startswith("#"):
            continue
        key = line.split("=", 1)[0].strip()
        if key in allowed:
            lines.append(line)
    if not any(l.startswith("PG_MIRROR_TOKEN=") for l in lines):
        raise RuntimeError(f"{seed_env} has no PG_MIRROR_TOKEN line — "
                           f"a seeded install would still show the setup page.")
    (payload / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"payload: seeded .env with {len(lines)} key line(s) "
          f"({', '.join(l.split('=')[0] for l in lines)})")
    print("         >>> This installer will contain live credentials. "
          "Share it internally only — do NOT publish it. <<<")


def build(version: str, with_runtime: bool = True,
          seed_env: Path | None = None) -> Path:
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

    if with_runtime:
        stage_runtime(payload)

    if seed_env is not None:
        stage_seed_env(payload, seed_env)

    if SHELL_BUILD.is_dir():
        shutil.copytree(SHELL_BUILD, payload / "shell")
        print(f"payload: Electron shell -> {payload / 'shell'}")
    else:
        print(
            "WARNING: Electron shell build not found at "
            f"{SHELL_BUILD} — run `npm run dist` in \"10. Electron App/app\" first. "
            "Installer will fall back to Launch Dashboard.bat shortcuts."
        )

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
    parser.add_argument(
        "--no-runtime",
        action="store_true",
        help="Skip bundling the Python runtime. The resulting install then needs "
             "Python on the machine, as it did before the runtime was bundled.",
    )
    parser.add_argument(
        "--seed-env",
        metavar="ENV_FILE",
        help="Pre-fill the install's .env with the PG_* access keys from this "
             "file, so the user never pastes them. The resulting INSTALLER "
             "contains live credentials: distribute internally only, never on "
             "the public Releases page. The OTA update zip is unaffected.",
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

    seed = Path(args.seed_env).resolve() if args.seed_env else None
    if seed is not None and not seed.is_file():
        parser.error(f"--seed-env file not found: {seed}")
    build(version, with_runtime=not args.no_runtime, seed_env=seed)
    write_checksums()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
