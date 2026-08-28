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

# The folder that ships inside the disk image and ends up in the user's
# Applications folder. Space-separated and human-readable: it is what staff see
# in Finder, unlike the bundle name, which has to match electron-builder's
# productName.
MAC_FOLDER_NAME = "Commission Dashboard"

# Double-clicked from the disk image; see _write_mac_installer for why the Mac
# gets an installer script where Windows gets an .exe.
MAC_INSTALLER_NAME = "Install Commission Dashboard.command"

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


def _write_mac_installer(dmg_root: Path) -> None:
    """Put a double-clickable installer beside the app folder in the disk image.

    Windows has an .exe that does this; the Mac had "drag this folder to
    Applications", which fails for two reasons that are invisible to the person
    doing the dragging:

    * Quarantine. Everything out of a downloaded disk image carries
      com.apple.quarantine. On an unsigned build that is the
      unidentified-developer wall, and it applies not only to the .app but to
      runtime/bin/python3 and every .dylib under it, so even an app that opens
      cannot start its own server.
    * App Translocation. A quarantined app launched from anywhere other than a
      folder the user explicitly moved it to is run from a randomised read-only
      mount. __dirname then points into /private/var/folders/..., where
      findCommissionRoot() cannot see "8. Web Dashboard" — the app opens and
      immediately reports "Could not find the dashboard files."

    Both die the moment the quarantine attribute is gone, which a script can do
    and a drag cannot. The script also installs to ~/Applications rather than
    /Applications: no admin password, and the folder stays writable, which the
    dashboard requires — it writes .env, dashboard.db and its logs in place.

    The script itself is quarantined too, so the first run still needs one
    right-click → Open. One deliberate override, once, in exchange for an
    install that then behaves like any other app.
    """
    script = dmg_root / MAC_INSTALLER_NAME
    script.write_text(f"""#!/bin/bash
# Installs the Commission Dashboard into ~/Applications.
#
# Copies the app folder off this disk image, removes the quarantine flag macOS
# puts on downloaded files, and opens the dashboard. Existing settings, the
# local database and saved rules are carried over, not overwritten.

set -u

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC="$SRC_DIR/{MAC_FOLDER_NAME}"
DEST_PARENT="$HOME/Applications"
DEST="$DEST_PARENT/{MAC_FOLDER_NAME}"
APP="$DEST/{MAC_APP_NAME}"

# Files holding live state. Kept across a reinstall for the same reason
# updater.py preserves them across an update: they are the user's, not ours.
KEEP=(
  ".env"
  "8. Web Dashboard/dashboard.db"
  "8. Web Dashboard/data"
  "8. Web Dashboard/special_cases.json"
  "8. Web Dashboard/factory_rates.json"
)

fail() {{
  echo ""
  echo "Install failed: $1"
  echo ""
  echo "Send this whole window to IT and they can take it from here."
  echo "Press Return to close."
  read -r _
  exit 1
}}

echo "Installing the Commission Dashboard..."
echo ""

[ -d "$SRC" ] || fail "could not find \\"{MAC_FOLDER_NAME}\\" next to this installer.
         Open the downloaded .dmg and run the installer from inside it."

mkdir -p "$DEST_PARENT" || fail "could not create $DEST_PARENT"

STASH=""
if [ -d "$DEST" ]; then
  echo "Found an existing install — keeping your settings and data."
  STASH="$(mktemp -d)"
  for rel in "${{KEEP[@]}}"; do
    if [ -e "$DEST/$rel" ]; then
      mkdir -p "$STASH/$(dirname "$rel")"
      cp -R "$DEST/$rel" "$STASH/$rel" || fail "could not back up $rel"
    fi
  done
  rm -rf "$DEST" || fail "could not replace the old install at $DEST"
fi

# ditto, not cp: it is the only copy on macOS that reliably preserves the
# symlinks inside the Electron framework and the code signature that depends
# on them. A plain recursive cp flattens them and the app becomes "damaged".
echo "Copying files (this takes a minute — about 700 MB)..."
ditto "$SRC" "$DEST" || fail "could not copy the app to $DEST"

if [ -n "$STASH" ]; then
  for rel in "${{KEEP[@]}}"; do
    if [ -e "$STASH/$rel" ]; then
      rm -rf "$DEST/$rel"
      mkdir -p "$DEST/$(dirname "$rel")"
      cp -R "$STASH/$rel" "$DEST/$rel" || fail "could not restore $rel"
    fi
  done
  rm -rf "$STASH"
  echo "Restored your settings and data."
fi

# The step a drag-and-drop install cannot do, and the reason it was failing.
echo "Clearing the macOS download quarantine..."
xattr -dr com.apple.quarantine "$DEST" 2>/dev/null

[ -d "$APP" ] || fail "the copy finished but $APP is not there."

echo ""
echo "Installed to: $DEST"
echo "Opening the Commission Dashboard..."
open "$APP" || fail "the app is installed but would not open. Open it from Finder: Go → Home → Applications."

echo ""
echo "Done. From now on, open it from Finder: Go → Home → Applications → {MAC_FOLDER_NAME}."
echo "You can close this window."
""", encoding="utf-8", newline="\n")
    script.chmod(0o755)

    (dmg_root / "READ ME FIRST.txt").write_text(f"""Commission Dashboard — installing on a Mac
==========================================

1. Drag the "{MAC_FOLDER_NAME}" folder onto the
   Applications shortcut next to it.

2. Open Applications, then "{MAC_FOLDER_NAME}", and
   double-click CommissionDashboard.

3. macOS will block it the first time and say it is from an
   unidentified developer. That is expected — this app is not
   distributed through the App Store. Right-click (or Control-click)
   CommissionDashboard, choose Open, then choose Open again.

   On macOS Sequoia or newer there is no Open button in that dialog.
   Go to  Apple menu > System Settings > Privacy & Security, scroll
   down, and click "Open Anyway".

4. It takes a minute or two to start the first time. Sign in with the
   username and password IT gave you.

Steps 1 to 3 are one time only. After that just open it from
Applications like any other app.

Do not run it from this disk image — it has to be copied out first.

If macOS will not let you drag into Applications ("you don't have
permission to..."), double-click "{MAC_INSTALLER_NAME}"
instead. That installs into your own Applications folder — Finder >
Go > Home > Applications — which never has that problem.

Anything unexpected: send IT a photo of the message on screen.
""", encoding="utf-8", newline="\n")
    print(f"dmg: wrote {MAC_INSTALLER_NAME} and READ ME FIRST.txt")


# The disk image window: content size in points, and the centre of each icon
# within it. Shared by the background art and the AppleScript that places the
# icons, so the arrow drawn in the image lands between the two things it points
# from and to. Change one and you must change the other.
DMG_WINDOW = (660, 440)
DMG_ICON_SIZE = 96
DMG_POSITIONS = {
    MAC_FOLDER_NAME: (175, 170),
    "Applications": (485, 170),
    "READ ME FIRST.txt": (175, 345),
    MAC_INSTALLER_NAME: (485, 345),
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
  end tell
end tell
"""


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
        for attempt in range(3):
            if not mount.exists():
                break
            cmd = ["hdiutil", "detach", str(mount)]
            if attempt:
                cmd.append("-force")
            if subprocess.run(cmd, capture_output=True).returncode == 0:
                break
            _time.sleep(2)

    dmg_path.unlink(missing_ok=True)
    run(["hdiutil", "convert", str(temp_dmg), "-format", "UDZO",
         "-imagekey", "zlib-level=9", "-o", str(dmg_path)],
        "compressing the disk image")
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

    # Wrap the payload in a single folder for the DMG, so the disk image holds
    # one obvious thing to install rather than three loose items.
    dmg_root = DIST / "dmg-contents"
    if dmg_root.exists():
        shutil.rmtree(dmg_root)
    dmg_root.mkdir(parents=True)

    # MOVE, never copy. shutil.copytree() defaults to symlinks=False, which
    # resolves every symlink it walks into a full copy of its target. Inside
    # CommissionDashboard.app that means Contents/Frameworks/Electron
    # Framework.framework/Versions/Current and the four aliases beside it stop
    # being links, which both inflates the image by a couple of hundred MB and
    # destroys the bundle layout the code signature is computed over. macOS
    # calls the result damaged and refuses to open it. A rename has no such
    # failure mode, and is instant.
    app_folder = dmg_root / MAC_FOLDER_NAME
    shutil.move(str(payload), str(app_folder))
    print(f"dmg: staged payload -> {app_folder}")

    # Last write into the bundle has happened; anything after this invalidates
    # the signature.
    _codesign_mac(app_folder / MAC_APP_NAME)

    # The shortcut that makes drag-to-install possible at all: without it there
    # is nowhere on the image to drag to, and people drag the folder to the
    # desktop or open the app in place. Dragging is now a supported route --
    # the app clears its own quarantine on first launch -- so it gets the
    # standard Mac affordance rather than being warned against.
    (dmg_root / "Applications").symlink_to("/Applications")
    print("dmg: added Applications shortcut for drag-to-install")

    _write_mac_installer(dmg_root)

    background = _stage_dmg_background(dmg_root)

    arch_suffix = "arm64" if arch == "arm64" else "intel"
    dmg_path = DIST / f"CommissionDashboard-Setup-{version}-macos-{arch_suffix}.dmg"
    _create_styled_dmg(dmg_root, dmg_path, background)

    size = dmg_path.stat().st_size
    print(f"macOS ({arch}): disk image -> {dmg_path} ({size // (1024 * 1024)} MB)")


if __name__ == "__main__":
    raise SystemExit(main())
