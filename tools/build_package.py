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
import plistlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DIST = REPO_ROOT / "dist"

# Built by `npm run dist` in "10. Electron App/app". Copied into the installer
# payload as <root>/shell/ but NEVER into the OTA zip — updates stay code-only,
# and updater.py pins "shell" in PRESERVE_PATHS so they can't touch it either.
SHELL_BUILD = REPO_ROOT / "10. Electron App" / "app" / "dist" / "win-unpacked"
SHELL_SRC = REPO_ROOT / "10. Electron App" / "app"
SHELL_SRC_FILES = ["main.js", "loading.html", "package.json"]

# macOS does NOT keep the Windows shape. It used to try -- the .app standing in
# for shell/, with "8. Web Dashboard" and runtime/ as its siblings in one
# installed folder -- and that is the shape every macOS failure this project
# had traces back to. Two macOS rules make it unworkable:
#
#   * Quarantine is per-file. Everything dragged out of a downloaded image
#     carries com.apple.quarantine, and approving the .app approves the .app.
#     The sibling runtime/bin/python3 stays flagged, so the window opens and
#     the server it spawns is killed on sight.
#   * App Translocation. Finder only exempts an app from it when the user drags
#     the .app ITSELF. Dragging a folder that contains the app does not count,
#     so the app ran from a randomised read-only copy under /private/var/folders
#     and reported that it could not find the dashboard files -- which is what
#     the disk image's own arrow was telling people to do.
#
# So the install tree is packed into the bundle (Contents/Resources/) and
# main.js unpacks it to ~/Library/Application Support/Commission Dashboard on
# first launch. The bundle travels with the app, so translocation is harmless;
# that directory is the app's own, so it is writable and unquarantined; and
# nothing is ever written inside the bundle, so the signature stays valid and
# OTA updates keep working exactly as they do on Windows.
MAC_APP_NAME = "CommissionDashboard.app"

# The disk image's volume name -- what staff see in Finder when it mounts.
# Space-separated and human-readable, unlike the bundle name, which has to
# match electron-builder's productName.
MAC_FOLDER_NAME = "Commission Dashboard"

MAC_PAYLOAD_ARCHIVE = "payload.tar.gz"
MAC_PAYLOAD_STAMP = "payload-version.txt"

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

# macOS has no embeddable distribution -- python.org ships installers, not a
# relocatable tree -- so the Mac runtime comes from python-build-standalone,
# which exists for exactly this. "install_only" is the plain bin/ + lib/
# layout, putting the interpreter at runtime/bin/python3 where main.js looks.
PYTHON_MAC_VERSION = "3.12.13"
PYTHON_MAC_RELEASE = "20260805"

# ARM64 (Apple Silicon, M1+)
PYTHON_MAC_ARM64_ARCHIVE = (f"cpython-{PYTHON_MAC_VERSION}+{PYTHON_MAC_RELEASE}"
                            f"-aarch64-apple-darwin-install_only.tar.gz")
PYTHON_MAC_ARM64_URL = (
    f"https://github.com/astral-sh/python-build-standalone/releases/download/"
    f"{PYTHON_MAC_RELEASE}/{PYTHON_MAC_ARM64_ARCHIVE}"
)
PYTHON_MAC_ARM64_WHEEL_PLATFORM = "macosx_11_0_arm64"

# x86_64 (Intel)
PYTHON_MAC_X86_64_ARCHIVE = (f"cpython-{PYTHON_MAC_VERSION}+{PYTHON_MAC_RELEASE}"
                             f"-x86_64-apple-darwin-install_only.tar.gz")
PYTHON_MAC_X86_64_URL = (
    f"https://github.com/astral-sh/python-build-standalone/releases/download/"
    f"{PYTHON_MAC_RELEASE}/{PYTHON_MAC_X86_64_ARCHIVE}"
)
PYTHON_MAC_X86_64_WHEEL_PLATFORM = "macosx_11_0_x86_64"

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


def _download_python_mac_arch(arch: str) -> tuple[Path, str, str]:
    """Download python-build-standalone for a macOS architecture, cache it locally.

    Returns (archive_path, wheel_platform, arch_name).
    """
    if arch == "arm64":
        archive = PYTHON_MAC_ARM64_ARCHIVE
        url = PYTHON_MAC_ARM64_URL
        platform = PYTHON_MAC_ARM64_WHEEL_PLATFORM
    elif arch == "x86_64":
        archive = PYTHON_MAC_X86_64_ARCHIVE
        url = PYTHON_MAC_X86_64_URL
        platform = PYTHON_MAC_X86_64_WHEEL_PLATFORM
    else:
        raise ValueError(f"Unknown macOS architecture: {arch}")

    cache = REPO_ROOT / ".cache" / "python-mac"
    cache.mkdir(parents=True, exist_ok=True)
    archive_path = cache / archive

    if archive_path.exists():
        print(f"runtime: using cached {archive} ({arch})")
        return archive_path, platform, arch

    print(f"runtime: downloading {archive} ({arch}) ...")
    urllib.request.urlretrieve(url, archive_path)
    print(f"runtime: cached to {archive_path}")
    return archive_path, platform, arch


def _mac_runtime_root(runtime: Path) -> Path:
    """The directory holding bin/python3, wherever the tarball put it.

    Upstream currently unpacks to a "python/" folder, but the name is not part
    of any contract we control, so identify the root by what has to be inside
    it. Returns `runtime` itself when the archive was already flat.
    """
    if (runtime / "bin" / "python3").exists():
        return runtime
    for child in sorted(runtime.iterdir()):
        if child.is_dir() and (child / "bin" / "python3").exists():
            return child
    raise RuntimeError(
        f"no bin/python3 anywhere under {runtime} — the python-build-standalone "
        f"layout has changed. Found: {sorted(p.name for p in runtime.iterdir())}"
    )


def stage_runtime_mac(payload: Path, arch: str = "arm64") -> bool:
    """Put a ready-to-run Python for macOS at payload/runtime.

    The python-build-standalone distribution is a relocatable tarball. It is
    flattened so the interpreter ends up at runtime/bin/python3, which is the
    path main.js tries first on a Mac, and the dependencies go into the
    distribution's own site-packages so they are on sys.path without help.
    """
    runtime = payload / "runtime"
    try:
        archive_path, wheel_platform, arch_name = _download_python_mac_arch(arch)
    except Exception as e:
        print(f"WARNING: could not fetch the macOS {arch} Python runtime ({e}). "
              f"Installer will require Python on the machine.")
        return False

    runtime.mkdir(parents=True, exist_ok=True)
    print(f"runtime: extracting macOS Python ({arch_name}) to {runtime}")
    with tarfile.open(archive_path, "r:gz") as tf:
        tf.extractall(runtime)

    # python-build-standalone's install_only archives unpack to a single
    # top-level "python/" directory. This used to glob for "cpython-*" -- the
    # name of the downloaded *archive*, not of anything inside it -- so the
    # match never happened, the flatten was skipped, and the interpreter stayed
    # at runtime/python/bin/python3, one level below where main.js looks for
    # it. The shell then fell through to bare "python3" on PATH: on a clean Mac
    # that is Apple's stub, which pops a "install the developer tools" prompt,
    # and which could not see the bundled dependencies even if it ran. Every
    # Mac build so far has shipped that way. Locate the extracted root by
    # finding the interpreter, so a change to the upstream folder name shows up
    # as a build failure rather than as a silently misplaced runtime.
    inner = _mac_runtime_root(runtime)
    if inner != runtime:
        for item in inner.iterdir():
            item.replace(runtime / item.name)
        inner.rmdir()
        print(f"runtime: flattened {inner.name}/ into {runtime.name}/")

    # Derive site-packages rather than hard-coding python3.12, and use the
    # distribution's own directory: it is already on sys.path, so nothing has
    # to teach the interpreter where the dependencies went.
    lib_dirs = sorted(runtime.glob("lib/python3.*"))
    if not lib_dirs:
        raise RuntimeError(
            f"no lib/python3.* directory under {runtime} — the runtime did not "
            "unpack in the layout this build expects."
        )
    site_packages = lib_dirs[0] / "site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)
    req = REPO_ROOT / "requirements-dashboard.txt"
    cmd = [
        sys.executable, "-m", "pip", "install",
        "--target", str(site_packages),
        "--python-version", PYTHON_MAC_VERSION.rsplit(".", 1)[0],
        "--platform", wheel_platform,
        "--only-binary=:all:",
        "--no-compile",
        "--upgrade",
        "-r", str(req),
    ]
    print(f"runtime: installing dependencies for macOS {arch_name}...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout[-3000:])
        print(result.stderr[-3000:])
        raise RuntimeError(f"dependency install into the bundled macOS {arch} runtime failed")

    # The one thing that has to be true for the Mac app to work at all, checked
    # where it is cheap to fix rather than on a user's machine. main.js runs
    # exactly this path; a size in MB says nothing about whether it is there.
    interpreter = runtime / "bin" / "python3"
    if not interpreter.exists():
        raise RuntimeError(
            f"{interpreter} is missing after staging — the shell would fall "
            f"back to whatever python3 is on the user's PATH. Contents of "
            f"{runtime}: {sorted(p.name for p in runtime.iterdir())}"
        )
    if not os.access(interpreter, os.X_OK):
        raise RuntimeError(f"{interpreter} is not executable.")

    total = sum(f.stat().st_size for f in runtime.rglob("*") if f.is_file())
    print(f"payload: Python {PYTHON_MAC_VERSION} runtime ({arch_name}) -> {runtime} "
          f"({total // (1024 * 1024)} MB), interpreter at bin/python3, "
          f"dependencies in {site_packages.relative_to(runtime)}")
    return True


def stage_seed_env(payload: Path, seed_env: Path) -> None:
    """Pre-fill the install's .env so the user never pastes access keys.

    INSTALLER-ONLY, like shell/ and runtime/: this lands in dist/payload for
    Inno to pick up and is never part of the OTA zip (which is built from the
    code file list, where *.env is excluded). An installer produced with this
    carries live credentials — distribute it over internal channels only,
    NEVER as an asset on the public GitHub Releases page. The CI release
    workflow does not pass --seed-env, so public builds stay key-free.

    Only the keys in `allowed` below are taken from the seed file. FLASK_SECRET_KEY
    must stay per-install (the server generates one on first boot), and
    anything else in a working .env (e.g. a retired DATABASE_URL) has no
    business being copied onto six machines.
    """
    # UPDATE_GITHUB_TOKEN is here for the macOS path. Windows appends it to
    # dist/payload/.env as a separate build step, which works because the
    # payload directory survives until Inno compiles it; on a Mac the payload
    # is packed into the bundle and deleted, so there is nothing left to append
    # to and the token has to arrive with the rest of the keys. The Windows
    # workflow does not put it in its seed file, so nothing is duplicated.
    allowed = ("PG_PROXY_URL", "PG_PROXY_TOKEN", "PG_PROXY_DB",
               "PG_MIRROR_TOKEN", "PG_MIRROR_DB", "PG_MIRROR_SCHEMA",
               "UPDATE_GITHUB_TOKEN")
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


def check_shell_fresh() -> None:
    """Refuse to ship an Electron shell older than the sources it was built from.

    The shell is a prebuilt folder this script only copies, so edits to main.js
    reach users solely via `npm run dist`. Skipping that rebuild once shipped a
    July shell inside an August installer: the payload had a bundled runtime/
    the shell predated knowing about, so it looked for a .venv that no longer
    exists, never started Flask, and blamed the user for a setup step that had
    been removed. Nothing in the build said a word.
    """
    asar = SHELL_BUILD / "resources" / "app.asar"
    if not asar.exists():
        raise SystemExit(
            f"ERROR: {asar} is missing — the shell build is incomplete.\n"
            "       Run `npm run dist` in \"10. Electron App/app\"."
        )
    built = asar.stat().st_mtime
    stale = [
        name for name in SHELL_SRC_FILES
        if (SHELL_SRC / name).exists() and (SHELL_SRC / name).stat().st_mtime > built
    ]
    if stale:
        raise SystemExit(
            "ERROR: the Electron shell is older than its sources — "
            f"{', '.join(stale)} changed after the last build.\n"
            "       Run `npm run dist` in \"10. Electron App/app\", then build again."
        )


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
        check_shell_fresh()
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
    parser.add_argument(
        "--platform",
        choices=["windows", "macos", "macos-arm64", "macos-x86_64", "macos-all"],
        default="windows",
        help="Target platform (default: windows). "
             "macos = macos-arm64 (Apple Silicon), "
             "macos-arm64 = M1+ Macs only, "
             "macos-x86_64 = Intel Macs only, "
             "macos-all = both architectures in one build.",
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

    # Build the platform-specific package
    if args.platform in ("macos", "macos-arm64"):
        _build_macos(version, arch="arm64", with_runtime=not args.no_runtime, seed_env=seed)
    elif args.platform == "macos-x86_64":
        _build_macos(version, arch="x86_64", with_runtime=not args.no_runtime, seed_env=seed)
    elif args.platform == "macos-all":
        _build_macos(version, arch="arm64", with_runtime=not args.no_runtime, seed_env=seed)
        _build_macos(version, arch="x86_64", with_runtime=not args.no_runtime,
                     seed_env=seed, clean=False)
    else:
        build(version, with_runtime=not args.no_runtime, seed_env=seed)

    write_checksums()
    return 0


def _find_mac_app_bundle(arch: str = "arm64") -> Path:
    """Locate the .app electron-builder produced for the given architecture.

    electron-builder's output directory is arch-dependent: on an arm64 runner
    the arm64 bundle lands in mac-arm64/ and the x64 one in plain mac/ (and
    the names can flip on an x64 host). Rather than hard-code that mapping,
    prefer the directory whose name matches the arch, fall back to the other
    known names, and report what was actually there when nothing matches --
    an earlier attempt hard-coded one path and failed with an error that named
    a path rather than the real problem.
    """
    dist_dir = REPO_ROOT / "10. Electron App" / "app" / "dist"

    # electron-builder appends the arch to the output directory for every arch
    # EXCEPT x64, which gets the bare platform name — on any host. So the
    # arm64 bundle is always in mac-arm64/ and the x64 bundle always in mac/.
    names = ["mac-arm64"] if arch == "arm64" else ["mac", "mac-x64"]

    for name in names:
        candidates = sorted((dist_dir / name).glob("*.app")) if (dist_dir / name).is_dir() else []
        if candidates:
            return candidates[0]

    listing = "\n".join(f"         {p.name}" for p in sorted(dist_dir.iterdir())) \
        if dist_dir.is_dir() else "         (dist/ does not exist)"
    raise SystemExit(
        f"ERROR: no {arch} .app bundle found under {dist_dir}\n"
        f"       dist/ contains:\n{listing}\n"
        "       Run `npm run dist` in \"10. Electron App/app\" on macOS first "
        "(package.json targets both arm64 and x64)."
    )


def _write_mac_entitlements(where: Path) -> Path:
    """The hardened-runtime exceptions Electron and the bundled Python need.

    Only used when signing with a real Developer ID -- an ad-hoc signature
    never runs under the hardened runtime, so it needs none of this. Written to
    a subdirectory rather than dist/ itself: it is a build input, and
    write_checksums() lists every loose file in dist/ as a release asset.
    """
    where = where / "build-temp"
    where.mkdir(parents=True, exist_ok=True)
    plist = where / "mac-entitlements.plist"
    plist.write_text("""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <!-- V8 compiles JavaScript at runtime; without these Electron dies at boot. -->
  <key>com.apple.security.cs.allow-jit</key><true/>
  <key>com.apple.security.cs.allow-unsigned-executable-memory</key><true/>
  <key>com.apple.security.cs.allow-dyld-environment-variables</key><true/>
  <!-- The shell spawns runtime/bin/python3, which is signed by someone else
       entirely (python-build-standalone) and loads its own .dylibs and
       compiled wheels. Library validation would refuse every one of them. -->
  <key>com.apple.security.cs.disable-library-validation</key><true/>
</dict>
</plist>
""", encoding="utf-8", newline="\n")
    return plist


def _codesign_mac(app: Path) -> None:
    """Sign the .app so macOS will consent to launch it.

    electron-builder skips signing on CI -- "cannot find valid Developer ID
    Application identity", plainly in the build log -- and an *unsigned* bundle
    is not merely warned about on Apple Silicon: the kernel will not exec an
    arm64 binary that carries no signature at all. Finder reports that as
    "CommissionDashboard is damaged and can't be opened. You should move it to
    the Trash", with no way past it. That is a dead end, not a prompt, and it is
    what an M-series Mac saw on every build before this one.

    An ad-hoc signature ("-") needs no Apple account and no money, and it turns
    the dead end into the ordinary unidentified-developer prompt -- which the
    installer script clears outright by stripping quarantine. Set
    MAC_SIGN_IDENTITY to a real "Developer ID Application: ..." to sign for
    distribution instead. Signing with a Developer ID and then notarising is
    the only way to remove the prompt itself rather than work around it;
    notarisation is not wired up here, because it needs a paid Apple Developer
    account that this project does not have yet.

    Signing must come AFTER the bundle is in its final place and nothing further
    will be written into it. Any modification of a signed bundle invalidates the
    signature, and an invalid signature reads to macOS as "damaged" -- exactly
    the failure we are here to fix.
    """
    identity = os.environ.get("MAC_SIGN_IDENTITY", "-").strip() or "-"
    adhoc = identity == "-"
    label = "ad-hoc" if adhoc else identity

    # Stray extended attributes make codesign fail with "resource fork, Finder
    # information, or similar detritus not allowed". Cheap to prevent, and the
    # error names a cause that means nothing to whoever reads the build log.
    subprocess.run(["xattr", "-cr", str(app)], capture_output=True, text=True)

    # --deep is deprecated for distribution signing but is the right tool for an
    # ad-hoc pass over a bundle we did not build the signing plan for: it walks
    # every nested helper, framework and dylib. Without it only the outer
    # executable is signed and the Electron helpers stay unsigned -- which fails
    # on arm64 in the same way as signing nothing.
    cmd = ["codesign", "--force", "--deep", "--sign", identity]
    if adhoc:
        # An ad-hoc signature cannot carry a trusted timestamp; asking for one
        # is a hard error rather than a downgrade.
        cmd.append("--timestamp=none")
    else:
        # Notarisation refuses anything without the hardened runtime and a
        # secure timestamp. The entitlements are what keep Electron working
        # under it -- V8 needs writable-executable memory, and the dashboard
        # spawns the bundled python3, which library validation would block.
        entitlements = _write_mac_entitlements(DIST)
        cmd += ["--options", "runtime", "--timestamp",
                "--entitlements", str(entitlements)]
    cmd.append(str(app))
    sign = subprocess.run(cmd, capture_output=True, text=True)
    if sign.returncode != 0:
        print(sign.stdout[-2000:])
        print(sign.stderr[-2000:])
        raise RuntimeError(
            f"codesign failed for {app}. An unsigned bundle will not launch on "
            "Apple Silicon, so this is fatal rather than a warning."
        )

    verify = subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)],
        capture_output=True, text=True,
    )
    if verify.returncode != 0:
        print(verify.stderr[-2000:])
        raise RuntimeError(
            f"{app} does not verify after signing — macOS would call it damaged."
        )
    print(f"macOS: signed {app.name} ({label}) and verified the signature")


def _embed_mac_payload(payload: Path, app: Path, version: str) -> str:
    """Pack the install tree into the .app and return the stamp identifying it.

    A single compressed archive rather than the tree laid out under
    Resources/. Both would ride along with the bundle, but a tree puts hundreds
    of Mach-O files -- runtime/bin/python3, every compiled wheel, every .dylib
    under it -- inside Resources, where `codesign --deep` has to walk and sign
    each one and `--strict` verification then has opinions about nested code in
    a resource directory. One opaque file has none of those problems: codesign
    hashes it and moves on. It also compresses, so the .app is a third of the
    size it would otherwise be.

    main.js unpacks it on first launch, and after any upgrade, into
    ~/Library/Application Support/Commission Dashboard.
    """
    resources = app / "Contents" / "Resources"
    if not resources.is_dir():
        raise RuntimeError(f"{app} has no Contents/Resources — not an app bundle?")

    archive = resources / MAC_PAYLOAD_ARCHIVE
    print(f"macOS: packing the payload into {archive.relative_to(app.parent)} ...")
    with tarfile.open(archive, "w:gz", compresslevel=6) as tf:
        # arcname="." so `tar -xzf ... -C <root>` lands the tree at the root
        # rather than one folder below it. Symlinks are stored as symlinks
        # (tarfile does not dereference by default), which matters for the
        # bundled Python: runtime/bin/python3 is a link to python3.12.
        tf.add(payload, arcname=".")

    mb = archive.stat().st_size // (1024 * 1024)

    # The stamp is what main.js compares against the copy it unpacked last
    # time, so it has to change whenever the archive does -- not merely when
    # the version string does. Two builds of the same version are different
    # builds, and during testing they are the ONLY thing that differs.
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()[:16]
    stamp = f"{version}-{digest}"
    (resources / MAC_PAYLOAD_STAMP).write_text(stamp + "\n",
                                               encoding="utf-8", newline="\n")
    print(f"macOS: payload -> {mb} MB archive, stamp {stamp}")
    return stamp


def _write_mac_readme(dmg_root: Path) -> None:
    """The one note beside the app in the disk image.

    There used to be an "Install Commission Dashboard.command" here as well,
    because a plain drag could not produce a working install: the dashboard
    files sat NEXT TO the .app, and everything dragged out of a downloaded
    image is quarantined. Approving the app approved the app alone, so the
    bundled interpreter beside it was still killed on sight -- and dragging the
    enclosing FOLDER (which is what the window's arrow asked for) does not even
    exempt the app from App Translocation, so it ran from a read-only copy
    under /private/var/folders and could not find those files at all. The
    script existed to strip the quarantine that a drag cannot.

    None of that applies now. The payload ships inside the bundle, so the only
    thing to drag is the .app itself -- the gesture Finder does exempt from
    translocation -- and the files it unpacks into Application Support are
    written by us and carry no quarantine. What is left is the ordinary
    unsigned-app prompt, which is one right-click, once.
    """
    (dmg_root / "READ ME FIRST.txt").write_text(f"""Commission Dashboard — installing on a Mac
==========================================

1. Drag CommissionDashboard onto the Applications shortcut
   beside it.

2. Open your Applications folder and double-click
   CommissionDashboard.

3. The first time only, macOS will say it is from an
   unidentified developer and refuse to open it. That is
   expected — this app is not distributed through the App
   Store. Right-click (or Control-click) CommissionDashboard,
   choose Open, then choose Open again.

   On macOS Sequoia or newer there is no Open button in that
   dialog. Go to Apple menu > System Settings > Privacy &
   Security, scroll down, and click "Open Anyway".

4. The first launch sets itself up before the dashboard
   appears — it unpacks about 700 MB and takes a minute or
   two. The window tells you what it is doing. Later launches
   start straight away.

5. Sign in with the username and password IT gave you.

Steps 1 to 3 are one time only.

The dashboard keeps its settings, database and logs in
  Home > Library > Application Support > Commission Dashboard
and updates itself from inside the app, so you do not need to
download this image again for routine updates.

Anything unexpected: send IT a photo of the message on screen.
""", encoding="utf-8", newline="\n")
    print("dmg: wrote READ ME FIRST.txt")


# The disk image window: content size in points, and the centre of each icon
# within it. Shared by the background art and the AppleScript that places the
# icons, so the arrow drawn in the image lands between the two things it points
# from and to. Change one and you must change the other.
DMG_WINDOW = (660, 440)
DMG_ICON_SIZE = 96
DMG_POSITIONS = {
    MAC_APP_NAME: (175, 170),
    "Applications": (485, 170),
    "READ ME FIRST.txt": (330, 345),
}


def _stage_dmg_background(dmg_root: Path) -> str:
    """Put the window background inside the image, hidden, and name the file.

    Committed as PNG rather than generated here so the build needs no imaging
    library. tiffutil combines the 1x and 2x art into the multi-resolution
    file Finder wants on a retina display; if it is not there or fails, the 1x
    PNG alone still works and merely looks softer.
    """
    src = REPO_ROOT / "10. Electron App" / "assets"
    one, two = src / "dmg-background.png", src / "dmg-background@2x.png"
    if not one.is_file():
        raise SystemExit(f"ERROR: {one} is missing — the disk image has no background art.")

    hidden = dmg_root / ".background"
    hidden.mkdir(parents=True, exist_ok=True)

    if two.is_file():
        tiff = hidden / "background.tiff"
        combined = subprocess.run(
            ["tiffutil", "-cathidpicheck", str(one), str(two), "-out", str(tiff)],
            capture_output=True, text=True,
        )
        if combined.returncode == 0 and tiff.is_file():
            print("dmg: background art -> background.tiff (1x + 2x)")
            return "background.tiff"
        print(f"dmg: tiffutil unavailable or failed, using the 1x PNG "
              f"({combined.stderr.strip()[:120]})")

    shutil.copy2(one, hidden / "background.png")
    print("dmg: background art -> background.png (1x only)")
    return "background.png"


def _folder_size_mb(root: Path) -> int:
    """Bytes on disk, not following symlinks — the Electron framework's links
    would otherwise be counted as full copies of what they point at."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total // (1024 * 1024)


def _dmg_applescript(background: str) -> str:
    """Finder instructions that turn a plain file list into the drag window.

    Positions are icon centres measured from the top-left of the window's
    content area, which is the same origin the background image uses.
    """
    left, top = 200, 120
    width, height = DMG_WINDOW
    places = "\n".join(
        f'      set position of item "{name}" of container window to '
        f"{{{x}, {y}}}"
        for name, (x, y) in DMG_POSITIONS.items()
    )
    return f"""
tell application "Finder"
  tell disk "{MAC_FOLDER_NAME}"
    open
    set current view of container window to icon view
    set toolbar visible of container window to false
    set statusbar visible of container window to false
    set the bounds of container window to {{{left}, {top}, {left + width}, {top + height}}}
    set opts to the icon view options of container window
    set arrangement of opts to not arranged
    set icon size of opts to {DMG_ICON_SIZE}
    set text size of opts to 12
    set background picture of opts to file ".background:{background}"
{places}
    close
    open
    update without registering applications
    delay 2
    -- Leave nothing holding the volume. Ending with the window open kept
    -- Finder attached to it, and the compression step that follows then
    -- failed with "Resource temporarily unavailable" -- an error that names
    -- the symptom and not one word of the cause.
    close
  end tell
end tell
"""


def _attached_devices(image: Path) -> list[str]:
    """The /dev/diskN nodes currently backing this disk image.

    Read from hdiutil's plist rather than its human output. The text form puts
    device, filesystem and mount point on one line, and this volume's name has
    a space in it, so recovering the device from that is guesswork.

    Longest name first, so a slice (/dev/disk4s1) is asked before the whole
    disk (/dev/disk4) that would take it with it.
    """
    r = subprocess.run(["hdiutil", "info", "-plist"], capture_output=True)
    if r.returncode != 0:
        # An empty list here reads as "detached" and lets the build march on
        # into a convert that fails with EAGAIN. Say so rather than let that
        # be the explanation someone has to reconstruct later.
        print(f"dmg: hdiutil info failed ({r.returncode}); "
              "assuming the image is detached")
        return []
    try:
        data = plistlib.loads(r.stdout)
    except Exception as e:
        print(f"dmg: could not parse hdiutil info ({e}); "
              "assuming the image is detached")
        return []
    target = os.path.realpath(str(image))
    devs = []
    for img in data.get("images", []):
        if os.path.realpath(str(img.get("image-path") or "")) != target:
            continue
        for ent in img.get("system-entities", []):
            dev = ent.get("dev-entry")
            if dev:
                devs.append(dev)
    return sorted(set(devs), key=len, reverse=True)


def _holders(mount: Path) -> str:
    """Which processes are keeping a volume busy. Diagnostics only."""
    try:
        r = subprocess.run(["lsof", "+D", str(mount)], capture_output=True,
                           text=True, timeout=60)
        return (r.stdout or "").strip()[:1500] or "(lsof named nothing)"
    except Exception as e:
        return f"(lsof failed: {e})"


def _detach_image(image: Path, mount: Path, timeout: int = 180) -> None:
    """Unmount a writable image and wait for its device to actually go away.

    `hdiutil detach` returning 0 does not mean the device is gone, and
    `hdiutil convert` on a still-attached image fails with EAGAIN -- which is
    what "Resource temporarily unavailable" was in v1.2.24.

    The version after that fixed the wrong half. It asked for a detach five
    times over ten seconds, then polled for ninety more without attempting
    anything further, so a volume that Finder or Spotlight held for longer
    than ten seconds was simply never asked again: the build watched it stay
    mounted and failed. v1.2.26 died there. Keep asking for the whole window,
    escalate to -force after the first try, and go by the device list rather
    than the mount point -- the mount point disappears first and the device
    outliving it is precisely the state that breaks the compression step.

    Failing that, name the process holding it. "Something is holding the
    volume" is a symptom, and it was all the previous message could say.
    """
    import time as _time

    deadline = _time.time() + timeout
    last = ""
    attempt = 0
    while _time.time() < deadline:
        devs = _attached_devices(image)
        if not devs:
            return
        attempt += 1
        targets = devs + ([str(mount)] if mount.exists() else [])
        for target in targets:
            cmd = ["hdiutil", "detach", target]
            if attempt > 1:
                cmd.append("-force")
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode == 0:
                break
            last = f"{' '.join(cmd)} -> {(r.stderr or r.stdout).strip()[:200]}"
        _time.sleep(2)

    raise RuntimeError(
        f"{image} is still attached after {timeout}s and cannot be compressed.\n"
        f"  last detach attempt: {last or '(none failed - it just never left)'}\n"
        f"  holding {mount}:\n{_holders(mount)}"
    )


def _create_styled_dmg(dmg_root: Path, dmg_path: Path, background: str) -> None:
    """Build the disk image, then lay its window out the way Mac users expect.

    A plain `hdiutil create` produces a window that is just a file list, which
    is why the folder had to be explained in a README nobody opens. The layout
    people already know -- app on the left, Applications on the right, arrow
    between -- can only be set by having Finder do it on a *writable* image, so
    the image is built read-write, mounted, arranged, then converted to the
    compressed read-only form that ships.
    """
    import time as _time

    def run(cmd, what, check=True):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if check and r.returncode != 0:
            print(r.stdout[-1500:])
            print(r.stderr[-1500:])
            raise RuntimeError(f"{what} failed: {r.stderr.strip()[:300]}")
        return r

    mount = Path("/Volumes") / MAC_FOLDER_NAME
    # A leftover mount from the previous architecture's build would make the
    # volume name ambiguous, and Finder would arrange the wrong one.
    if mount.exists():
        run(["hdiutil", "detach", str(mount), "-force"], "detaching a stale mount",
            check=False)

    temp_dmg = DIST / "build-temp" / f"{dmg_path.stem}-rw.dmg"
    temp_dmg.parent.mkdir(parents=True, exist_ok=True)
    temp_dmg.unlink(missing_ok=True)

    # Finder writes .DS_Store into the image while arranging it, so the
    # read-write image needs room the payload does not account for.
    size_mb = _folder_size_mb(dmg_root) + 150
    run(["hdiutil", "create", "-volname", MAC_FOLDER_NAME,
         "-srcfolder", str(dmg_root), "-fs", "HFS+", "-format", "UDRW",
         "-size", f"{size_mb}m", "-ov", str(temp_dmg)],
        "creating the writable disk image")

    run(["hdiutil", "attach", str(temp_dmg), "-readwrite", "-noverify"],
        "mounting the writable disk image")

    try:
        script = _dmg_applescript(background)
        # Finder scripting on a build machine occasionally returns an
        # AppleEvent timeout on the first try. Retry before giving up, rather
        # than failing a whole release over a transient one.
        for attempt in range(1, 4):
            styled = subprocess.run(["osascript", "-"], input=script,
                                    capture_output=True, text=True)
            if styled.returncode == 0:
                print(f"dmg: window arranged by Finder (attempt {attempt})")
                break
            print(f"dmg: Finder styling attempt {attempt} failed: "
                  f"{styled.stderr.strip()[:200]}")
            _time.sleep(3)
        else:
            raise RuntimeError(
                "Finder would not arrange the disk image window. The image "
                "would open as a plain file list, which is the thing this "
                "step exists to prevent."
            )
        if not (mount / ".DS_Store").exists():
            raise RuntimeError(
                "Finder reported success but wrote no .DS_Store, so the "
                "layout would not persist."
            )
        subprocess.run(["sync"], capture_output=True)
    finally:
        # Detaching and waiting for the device to be gone are the same problem,
        # so they are the same call now. Splitting them is what let the build
        # stop asking after ten seconds and then wait ninety for an answer it
        # was no longer requesting.
        #
        # This one raises where the old loop swallowed everything, so it must
        # not fire while another exception is already on its way out: a
        # detach failure reported instead of the Finder failure that caused it
        # would send the next person looking in the wrong place.
        if sys.exc_info()[0] is None:
            _detach_image(temp_dmg, mount)
        else:
            try:
                _detach_image(temp_dmg, mount, timeout=30)
            except Exception as cleanup_error:
                print(f"dmg: could not detach during cleanup: {cleanup_error}")

    dmg_path.unlink(missing_ok=True)
    # Belt as well as braces: whatever else might briefly hold the file
    # (Spotlight indexing a freshly detached volume, for one), a retry costs
    # seconds and a failure costs a whole release cycle.
    for attempt in range(1, 4):
        converted = subprocess.run(
            ["hdiutil", "convert", str(temp_dmg), "-format", "UDZO",
             "-imagekey", "zlib-level=9", "-o", str(dmg_path)],
            capture_output=True, text=True,
        )
        if converted.returncode == 0:
            break
        print(f"dmg: compression attempt {attempt} failed: "
              f"{converted.stderr.strip()[:200]}")
        dmg_path.unlink(missing_ok=True)
        _time.sleep(5)
    else:
        raise RuntimeError(
            f"compressing the disk image failed: {converted.stderr.strip()[:300]}")
    temp_dmg.unlink(missing_ok=True)


def _build_macos(version: str, arch: str = "arm64", with_runtime: bool = True,
                 seed_env: Path | None = None, clean: bool = True) -> None:
    """Build a macOS package for a given architecture: payload + app bundle + runtime -> disk image.

    With clean=False only the payload directory is rebuilt and finished disk
    images in dist/ survive — that is what lets macos-all produce two DMGs
    without the second build deleting the first.
    """
    payload = DIST / "payload"
    if clean:
        _clean_dist()
    elif payload.exists():
        shutil.rmtree(payload)
    payload.mkdir(parents=True)

    # Stage the code
    files = collect()
    for src in files:
        rel = src.relative_to(REPO_ROOT)
        dst = payload / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    print(f"payload: {len(files)} files -> {payload}")

    # Add macOS runtime for the specified architecture
    if with_runtime:
        stage_runtime_mac(payload, arch=arch)

    # Add credentials if seeded
    if seed_env is not None:
        stage_seed_env(payload, seed_env)

    # Check the payload before it is sealed into the bundle, where a mistake
    # stops being visible in a directory listing.
    flask_app = payload / "8. Web Dashboard" / "app.py"
    if not flask_app.exists():
        raise RuntimeError(
            f"FATAL: Flask app not found at {flask_app}\n"
            f"       The payload is incomplete. Payload contents:\n"
            f"       {sorted(p.name for p in payload.iterdir())}"
        )

    dmg_root = DIST / "dmg-contents"
    if dmg_root.exists():
        shutil.rmtree(dmg_root)
    dmg_root.mkdir(parents=True)

    # symlinks=True, always. copytree() resolves symlinks by default, which
    # inside CommissionDashboard.app means Contents/Frameworks/Electron
    # Framework.framework/Versions/Current and the four aliases beside it stop
    # being links -- inflating the image by a couple of hundred MB and
    # destroying the bundle layout the code signature is computed over. macOS
    # calls the result damaged and refuses to open it.
    app_bundle = _find_mac_app_bundle(arch)
    app = dmg_root / MAC_APP_NAME
    shutil.copytree(app_bundle, app, symlinks=True)
    print(f"dmg: Electron app {app_bundle} -> {app}")

    # The install tree goes INSIDE the bundle. Nothing may sit beside the .app
    # on a Mac: see the block comment in main.js, and _write_mac_readme below.
    _embed_mac_payload(payload, app, version)
    shutil.rmtree(payload)

    # Last write into the bundle has happened; anything after this invalidates
    # the signature.
    _codesign_mac(app)

    # Drag-to-Applications is the supported route again, and now the correct
    # one: the thing being dragged is the .app itself, which is the only
    # gesture Finder exempts from App Translocation.
    (dmg_root / "Applications").symlink_to("/Applications")
    print("dmg: added Applications shortcut for drag-to-install")

    _write_mac_readme(dmg_root)

    background = _stage_dmg_background(dmg_root)

    arch_suffix = "arm64" if arch == "arm64" else "intel"
    dmg_path = DIST / f"CommissionDashboard-Setup-{version}-macos-{arch_suffix}.dmg"
    _create_styled_dmg(dmg_root, dmg_path, background)

    size = dmg_path.stat().st_size
    print(f"macOS ({arch}): disk image -> {dmg_path} ({size // (1024 * 1024)} MB)")


if __name__ == "__main__":
    raise SystemExit(main())
