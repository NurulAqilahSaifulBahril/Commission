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

# macOS keeps the Windows shape, with the .app standing in for shell/:
#
#   <root>/CommissionDashboard.app   <root>/8. Web Dashboard   <root>/runtime
#
# The bundle is deliberately NOT the install root. Everything the dashboard
# writes -- .env, dashboard.db, logs, data/ -- therefore lands outside it.
# Writing inside a bundle breaks its code signature and macOS then refuses to
# launch it, so this layout is what lets OTA updates keep working on Mac
# exactly as they do on Windows.
MAC_APP_NAME = "CommissionDashboard.app"

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


def stage_runtime_mac(payload: Path, arch: str = "arm64") -> bool:
    """Put a ready-to-run Python for macOS at payload/runtime.

    The python-build-standalone distribution is a relocatable tarball that
    unpacks to runtime/bin/python3 (where main.js expects it on Mac).
    Dependencies are installed the same way as Windows: pip --target into
    site-packages, but with the macOS wheel platform tag.
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
        # The tarball unpacks to cpython-<version>-<platform>/; extract into runtime/
        tf.extractall(runtime)
        # Move the contents up one level so bin/ sits directly under runtime/
        extracted = list(runtime.glob("cpython-*"))
        if extracted:
            for item in extracted[0].iterdir():
                dst = runtime / item.name
                item.replace(dst)
            extracted[0].rmdir()

    site_packages = runtime / "lib" / "python3.12" / "site-packages"
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

    total = sum(f.stat().st_size for f in runtime.rglob("*") if f.is_file())
    print(f"payload: Python {PYTHON_MAC_VERSION} runtime ({arch_name}) -> {runtime} "
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

    app_bundle = _find_mac_app_bundle(arch)
    shutil.copytree(app_bundle, payload / MAC_APP_NAME, symlinks=True)
    print(f"payload: Electron app {app_bundle} -> {payload / MAC_APP_NAME}")

    # Verify the payload has the critical Flask files before creating the DMG
    flask_app = payload / "8. Web Dashboard" / "app.py"
    if not flask_app.exists():
        raise RuntimeError(
            f"FATAL: Flask app not found at {flask_app}\n"
            f"       The payload is incomplete. Payload contents:\n"
            f"       {sorted(p.name for p in payload.iterdir())}"
        )

    # Wrap the payload in a single folder for the DMG so users just drag one
    # folder to Applications instead of managing three separate items.
    dmg_root = DIST / "dmg-contents"
    if dmg_root.exists():
        shutil.rmtree(dmg_root)
    dmg_root.mkdir(parents=True)

    # Create the folder that users will drag to Applications
    app_folder = dmg_root / "Commission Dashboard"
    shutil.copytree(payload, app_folder)
    print(f"dmg: wrapped payload in single folder -> {app_folder}")

    # Create a disk image with architecture suffix
    arch_suffix = "arm64" if arch == "arm64" else "intel"
    dmg_path = DIST / f"CommissionDashboard-Setup-{version}-macos-{arch_suffix}.dmg"
    print(f"Creating disk image: {dmg_path}")
    result = subprocess.run(
        [
            "hdiutil", "create",
            "-volname", "Commission Dashboard",
            "-srcfolder", str(dmg_root),
            "-ov", "-format", "UDZO",
            str(dmg_path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(result.stderr)
        raise RuntimeError(f"Failed to create disk image: {result.stderr}")

    size = dmg_path.stat().st_size
    print(f"macOS ({arch}): disk image -> {dmg_path} ({size // (1024 * 1024)} MB)")


if __name__ == "__main__":
    raise SystemExit(main())
