"""Over-the-air self-update for the Finance Commission Dashboard.

The dashboard is installed once (via the Windows installer published on the
GitHub Releases page) and updates itself from then on. This module owns the
"detect / download / stage" half of that; the actual file swap is handed to
``apply_update.py``, which runs as a detached process *after* this server has
exited so it can overwrite the code that is currently running.

Update packages are code-only. Nothing under a ``1. Excel`` / ``2. Output`` /
``4. data`` folder is ever shipped, and PRESERVE_PATHS below pins the handful of
in-tree files that hold live state (database, .env, admin-edited rule JSON).
Files the target has but the package does not are left untouched, so a bad
manifest can never wipe a user's workbooks.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import zipfile
from pathlib import Path

import requests

CURRENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = CURRENT_DIR.parent
VERSION_FILE = REPO_ROOT / "version.json"

DEFAULT_REPO = "NurulAqilahSaifulBahril/Commission"
GITHUB_API = "https://api.github.com"

# How long a successful check is reused before we hit the GitHub API again.
CHECK_TTL_SECONDS = 15 * 60

# Paths (relative to the install root) an update must never overwrite. These
# hold live state that belongs to this installation, not to the release.
PRESERVE_PATHS = [
    ".env",
    ".venv",
    "8. Web Dashboard/.env",
    "8. Web Dashboard/dashboard.db",
    "8. Web Dashboard/data",
    "8. Web Dashboard/dashboard.log",
    "8. Web Dashboard/special_cases.json",
    "8. Web Dashboard/factory_rates.json",
    "shell",  # the packaged Electron shell — never part of an OTA package, but
              # pinned here too so a future INCLUDE mistake can't wipe it out.
    "runtime",  # the bundled Python interpreter. Same reasoning as shell, and
                # the stakes are higher: remove it mid-update and the dashboard
                # has nothing left to restart itself with.
]

_LOG_FILE = CURRENT_DIR / "dashboard.log"


def _log(msg: str) -> None:
    try:
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[UPDATER] {msg}\n")
    except Exception:
        pass


# ── Version helpers ────────────────────────────────────────────────────────────
def _parse_version(value: str) -> tuple:
    """'v1.12.3' -> (1, 12, 3). Non-numeric tails sort below a plain release so
    a prerelease like 1.2.0-rc1 never outranks 1.2.0."""
    text = str(value or "0").strip().lstrip("vV")
    core = re.split(r"[-+]", text, maxsplit=1)
    nums = []
    for part in core[0].split("."):
        digits = re.sub(r"[^0-9]", "", part)
        nums.append(int(digits) if digits else 0)
    while len(nums) < 3:
        nums.append(0)
    # A suffix (rc/beta) makes it *older* than the same core version.
    return (tuple(nums[:3]), 0 if len(core) > 1 and core[1] else 1)


def read_local_version() -> dict:
    try:
        with open(VERSION_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("version"):
            return data
    except Exception:
        pass
    return {"version": "0.0.0", "channel": "stable", "repo": DEFAULT_REPO}


def current_version() -> str:
    return str(read_local_version().get("version") or "0.0.0")


def _repo_slug() -> str:
    return (
        os.environ.get("UPDATE_REPO")
        or read_local_version().get("repo")
        or DEFAULT_REPO
    ).strip()


def _github_headers() -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "CommissionDashboard-Updater",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("UPDATE_GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


# ── Progress state (single in-flight update per process) ──────────────────────
_state_lock = threading.Lock()
_state = {
    "phase": "idle",       # idle | downloading | verifying | extracting | restarting | error | done
    "message": "",
    "progress": 0,          # 0-100
    "target_version": None,
    "error": None,
    "started_at": None,
}

_check_cache = {"at": 0.0, "result": None}


def _set_state(**kwargs) -> None:
    with _state_lock:
        _state.update(kwargs)


def get_state() -> dict:
    with _state_lock:
        return dict(_state)


def is_busy() -> bool:
    return get_state()["phase"] in ("downloading", "verifying", "extracting", "restarting")


# ── Update detection ──────────────────────────────────────────────────────────
def _pick_asset(release: dict) -> dict | None:
    """The update payload asset — the code-only zip, not the installer exe."""
    assets = release.get("assets") or []
    for asset in assets:
        name = str(asset.get("name", "")).lower()
        if name.endswith(".zip") and "update" in name:
            return asset
    for asset in assets:
        if str(asset.get("name", "")).lower().endswith(".zip"):
            return asset
    return None


def _find_checksum(release: dict, asset_name: str) -> str | None:
    """Read SHA256SUMS.txt from the release, if the workflow published one."""
    for asset in release.get("assets") or []:
        if str(asset.get("name", "")).upper().startswith("SHA256SUMS"):
            try:
                url, headers = _asset_download(asset)
                resp = requests.get(url, headers=headers, timeout=30)
                resp.raise_for_status()
                for line in resp.text.splitlines():
                    parts = line.split()
                    if len(parts) >= 2 and parts[-1].lstrip("*") == asset_name:
                        return parts[0].lower()
            except Exception:
                _log("could not read SHA256SUMS: " + traceback.format_exc())
            return None
    return None


def check_for_update(force: bool = False) -> dict:
    """Ask GitHub for the latest release and compare it to the installed version."""
    now = time.time()
    if not force and _check_cache["result"] and now - _check_cache["at"] < CHECK_TTL_SECONDS:
        return dict(_check_cache["result"])

    local = current_version()
    result = {
        "current_version": local,
        "latest_version": None,
        "update_available": False,
        "release_notes": "",
        "release_url": "",
        "published_at": "",
        "asset_name": None,
        "asset_size": 0,
        "checked_at": now,
        "error": None,
    }

    try:
        resp = requests.get(
            f"{GITHUB_API}/repos/{_repo_slug()}/releases/latest",
            headers=_github_headers(),
            timeout=20,
        )
        if resp.status_code == 404:
            # A private repo also answers 404 when the request is unauthenticated,
            # so say which of the two it is rather than guessing wrong.
            if os.environ.get("GITHUB_TOKEN") or os.environ.get("UPDATE_GITHUB_TOKEN"):
                result["error"] = "No published release yet."
            else:
                result["error"] = (
                    "No release found. If the repository is private, add "
                    "UPDATE_GITHUB_TOKEN=<token with Contents:read> to the .env file."
                )
            return result
        resp.raise_for_status()
        release = resp.json()

        latest = str(release.get("tag_name") or release.get("name") or "").strip()
        asset = _pick_asset(release)

        result.update(
            latest_version=latest.lstrip("vV"),
            release_notes=(release.get("body") or "")[:4000],
            release_url=release.get("html_url") or "",
            published_at=release.get("published_at") or "",
            asset_name=asset.get("name") if asset else None,
            asset_size=asset.get("size", 0) if asset else 0,
        )
        if not asset:
            result["error"] = "Latest release has no update package attached."
            return result

        result["update_available"] = _parse_version(latest) > _parse_version(local)
        _check_cache.update(at=now, result=dict(result))
    except Exception as e:
        _log("check failed: " + traceback.format_exc())
        result["error"] = f"Could not reach GitHub: {e}"

    return result


# ── Download + stage ──────────────────────────────────────────────────────────
def _asset_download(asset: dict) -> tuple[str, dict]:
    """URL + headers for fetching a release asset.

    Always goes through the asset API endpoint rather than browser_download_url:
    that form works unauthenticated on a public repo *and* with a token on a
    private one, so flipping the repo's visibility does not break updates.
    """
    headers = _github_headers()
    headers["Accept"] = "application/octet-stream"
    return asset.get("url") or asset["browser_download_url"], headers


def _download(url: str, dest: Path, expected_size: int, headers: dict | None = None) -> None:
    with requests.get(url, headers=headers or _github_headers(), stream=True, timeout=60) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length") or expected_size or 0)
        done = 0
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = int(done * 70 / total)  # download owns 0-70% of the bar
                    _set_state(
                        progress=min(70, pct),
                        message=f"Downloading update… {done // 1048576} MB / {total // 1048576} MB",
                    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _flatten_single_root(folder: Path) -> Path:
    """GitHub's own zipball wraps everything in one directory; our built package
    does not. Accept either."""
    entries = [p for p in folder.iterdir() if p.name != "__MACOSX"]
    if len(entries) == 1 and entries[0].is_dir() and not (folder / "version.json").exists():
        return entries[0]
    return folder


def _relaunch_command() -> list[str]:
    """How to start the server again once files are swapped."""
    launcher = REPO_ROOT / "Launch Dashboard.bat"
    if launcher.exists():
        return [str(launcher)]
    venv_py = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    python = str(venv_py) if venv_py.exists() else sys.executable
    return [python, str(CURRENT_DIR / "app.py")]


def _run_update(release: dict) -> None:
    work_dir = Path(tempfile.mkdtemp(prefix="commission-update-"))
    try:
        asset = _pick_asset(release)
        if not asset:
            raise RuntimeError("Release has no update package attached.")

        asset_name = asset["name"]
        zip_path = work_dir / asset_name

        _set_state(phase="downloading", progress=1, message="Contacting GitHub…")
        url, headers = _asset_download(asset)
        _download(url, zip_path, asset.get("size", 0), headers)

        expected = _find_checksum(release, asset_name)
        if expected:
            _set_state(phase="verifying", progress=75, message="Verifying download…")
            actual = _sha256(zip_path)
            if actual != expected:
                raise RuntimeError("Checksum mismatch — the download was corrupted.")
        else:
            _log(f"no checksum published for {asset_name}; skipping verification")

        _set_state(phase="extracting", progress=82, message="Extracting update…")
        staging = work_dir / "staging"
        staging.mkdir()
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.namelist():
                # Refuse absolute paths and ../ traversal from the archive.
                target = (staging / member).resolve()
                if not str(target).startswith(str(staging.resolve())):
                    raise RuntimeError(f"Unsafe path in update package: {member}")
            zf.extractall(staging)
        payload = _flatten_single_root(staging)

        if not (payload / "8. Web Dashboard" / "app.py").exists():
            raise RuntimeError("Update package does not look like a dashboard build.")

        # Run the applier from outside the install tree — it is about to
        # overwrite the copy of itself that lives in the tree.
        applier_src = CURRENT_DIR / "apply_update.py"
        applier = work_dir / "apply_update.py"
        shutil.copy2(applier_src, applier)

        _set_state(
            phase="restarting",
            progress=92,
            message="Installing update and restarting the dashboard…",
        )

        cmd = [
            sys.executable,
            str(applier),
            "--staging", str(payload),
            "--target", str(REPO_ROOT),
            "--pid", str(os.getpid()),
            "--workdir", str(work_dir),
            "--relaunch", json.dumps(_relaunch_command()),
        ]
        creation = 0
        if os.name == "nt":
            creation = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008  # DETACHED_PROCESS
        subprocess.Popen(
            cmd,
            cwd=str(work_dir),
            creationflags=creation,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _log(f"handed off to apply_update.py (staging={payload})")

        # Give the browser a moment to receive the final status poll, then die
        # so the applier can replace our files.
        def _shutdown():
            time.sleep(2.5)
            os._exit(0)

        threading.Thread(target=_shutdown, daemon=True).start()
        _set_state(progress=95, message="Restarting… this page will reload automatically.")
    except Exception as e:
        _log("update failed: " + traceback.format_exc())
        _set_state(phase="error", error=str(e), message=f"Update failed: {e}", progress=0)
        shutil.rmtree(work_dir, ignore_errors=True)


def start_update() -> dict:
    """Kick off an update in the background. Returns the initial state."""
    if is_busy():
        return get_state()

    info = check_for_update(force=True)
    if info.get("error"):
        _set_state(phase="error", error=info["error"], message=info["error"], progress=0)
        return get_state()
    if not info.get("update_available"):
        _set_state(phase="idle", message="Already up to date.", progress=0, error=None)
        return get_state()

    resp = requests.get(
        f"{GITHUB_API}/repos/{_repo_slug()}/releases/latest",
        headers=_github_headers(),
        timeout=20,
    )
    resp.raise_for_status()
    release = resp.json()

    _set_state(
        phase="downloading",
        progress=0,
        message="Starting update…",
        error=None,
        target_version=info.get("latest_version"),
        started_at=time.time(),
    )
    threading.Thread(target=_run_update, args=(release,), daemon=True).start()
    return get_state()
