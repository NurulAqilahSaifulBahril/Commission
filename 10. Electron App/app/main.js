// Commission Portal — Electron shell around the local Flask dashboard.
//
// Replaces "Launch Dashboard.bat starts Flask minimized + opens Chrome" with
// a native window. Mirrors the bat's startup contract exactly (free the
// port, drop the computed-data cache, prefer the install's .venv python,
// wait for 5001 to answer) so the two launchers stay interchangeable.
// Flask code itself still self-updates via 8. Web Dashboard/updater.py.
const { app, BrowserWindow, dialog } = require("electron");
const { spawn, execSync } = require("child_process");
const fs = require("fs");
const net = require("net");
const path = require("path");
const os = require("os");

const PORT = 5001;
const DASHBOARD_URL = `http://127.0.0.1:${PORT}`;

// Dev: this file lives at <root>/10. Electron App/app/main.js.
// Packaged: the built shell is copied to <root>/shell/, putting the exe's
// resources at <root>/shell/resources/app*, so walk up until app.py appears.
function findCommissionRoot() {
  let dir = __dirname;
  // Walk up the directory tree looking for 8. Web Dashboard/app.py.
  // Deployed: exe is at <root>/shell/resources/app*, so 4-5 hops up to root.
  // Dev: app.asar is at <root>/10. Electron App/app/dist/*/resources/app.asar, so ~7 hops.
  // macOS packaged: app is at <root>/CommissionDashboard.app/Contents/Resources/ or similar.
  // Windows from DMG-like scenario: similar depth but different path structure.
  // Search up to 15 levels to handle macOS .app bundles and various deployments.
  for (let i = 0; i < 15; i++) {
    const dashboardPath = path.join(dir, "8. Web Dashboard", "app.py");
    if (fs.existsSync(dashboardPath)) {
      return dir;
    }
    // Also check if dir itself contains the 8. Web Dashboard folder (for when the
    // app is in a sibling folder scenario on macOS).
    const dashboardDir = path.join(dir, "8. Web Dashboard");
    if (fs.existsSync(dashboardDir) && fs.existsSync(path.join(dashboardDir, "app.py"))) {
      return dir;
    }
    dir = path.dirname(dir);
  }
  return null;
}

function freePort() {
  try {
    if (os.platform() === "win32") {
      const out = execSync(`netstat -ano | findstr ":${PORT} " | findstr "LISTENING"`, {
        shell: "cmd.exe",
        stdio: ["ignore", "pipe", "ignore"],
      }).toString();
      const pids = new Set(
        out.split(/\r?\n/).map((l) => l.trim().split(/\s+/).pop()).filter((p) => /^\d+$/.test(p))
      );
      for (const pid of pids) {
        try { execSync(`taskkill /PID ${pid} /F`, { stdio: "ignore" }); } catch {}
      }
    } else {
      // macOS/Linux: use lsof to find processes using the port
      try {
        const out = execSync(`lsof -i :${PORT} -t`, { stdio: "pipe" }).toString();
        const pids = out.trim().split(/\s+/).filter((p) => /^\d+$/.test(p));
        for (const pid of pids) {
          try { execSync(`kill -9 ${pid}`, { stdio: "ignore" }); } catch {}
        }
      } catch {}
    }
  } catch {}
}

function waitForPort(timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve) => {
    const attempt = () => {
      const socket = net.connect(PORT, "127.0.0.1");
      socket.once("connect", () => { socket.destroy(); resolve(true); });
      socket.once("error", () => {
        socket.destroy();
        if (Date.now() > deadline) resolve(false);
        else setTimeout(attempt, 500);
      });
    };
    attempt();
  });
}

let flaskProc = null;
// What actually went wrong launching the interpreter. Read only by the
// failure dialog.
let launchFailure = null;

function startFlask(root) {
  freePort();

  const cachePkl = path.join(root, "8. Web Dashboard", "data", "dashboard_cache.pkl");
  try { fs.unlinkSync(cachePkl); } catch {}

  // The bundled runtime first: an install ships with its own interpreter and
  // dependencies, so nothing needs to be on the machine. .venv is the fallback
  // for installs made before the runtime was bundled, and for running from a
  // source checkout; bare "python" is the last resort.
  let candidates;
  if (os.platform() === "win32") {
    candidates = [
      path.join(root, "runtime", "python.exe"),
      path.join(root, ".venv", "Scripts", "python.exe"),
    ];
  } else {
    // macOS/Linux
    candidates = [
      path.join(root, "runtime", "bin", "python3"),
      path.join(root, ".venv", "bin", "python3"),
    ];
  }
  const python = candidates.find((p) => fs.existsSync(p)) || "python3";

  // The server's own words, kept. With stdio "ignore" a crash-on-boot (missing
  // access keys, a broken .env) left nothing behind, so the timeout dialog
  // could only guess at the cause — and guessed wrong once the runtime was
  // bundled and "Python is missing" stopped being the likely explanation.
  let out = "ignore";
  try {
    out = fs.openSync(path.join(root, "8. Web Dashboard", "shell-startup.log"), "w");
  } catch {}
  launchFailure = null;
  flaskProc = spawn(python, [path.join(root, "8. Web Dashboard", "app.py")], {
    cwd: root,
    stdio: ["ignore", out, out],
    windowsHide: true,
  });
  // Both of these used to be discarded — the "error" handler threw the error
  // object away and nothing watched for "exit" at all. When the interpreter
  // failed to start it wrote nothing to the log, so the dialog had an empty
  // log and no error, and could do nothing but guess at a cause. It guessed
  // "macOS blocked it", which is only one of several things an empty log
  // means. Keep what actually happened and say that instead.
  flaskProc.on("error", (err) => {
    flaskProc = null;
    launchFailure = `could not run ${python}\n${err.code || ""} ${err.message}`.trim();
  });
  flaskProc.on("exit", (code, signal) => {
    if (signal) {
      launchFailure = `${python}\nwas killed by ${signal} before it could start.`;
    } else if (code) {
      launchFailure = `${python}\nexited immediately with code ${code}.`;
    }
  });
}

// True only when macOS has actually flagged the path — checked rather than
// assumed, so the app can tell "still quarantined, run the installer" apart
// from "not quarantined, so this is something else entirely".
function isQuarantined(target) {
  if (os.platform() !== "darwin") return false;
  try {
    execSync(`xattr -p com.apple.quarantine ${JSON.stringify(target)}`,
      { stdio: ["ignore", "pipe", "ignore"] });
    return true;
  } catch {
    return false;
  }
}

function startupLogTail(root, maxLines = 12) {
  try {
    const raw = fs.readFileSync(
      path.join(root, "8. Web Dashboard", "shell-startup.log"), "utf8");
    const lines = raw.trim().split(/\r?\n/);
    return lines.slice(-maxLines).join("\n");
  } catch {
    return "";
  }
}

function stopFlask() {
  if (flaskProc && !flaskProc.killed) {
    try {
      if (os.platform() === "win32") {
        execSync(`taskkill /PID ${flaskProc.pid} /T /F`, { stdio: "ignore" });
      } else {
        execSync(`kill -9 ${flaskProc.pid}`, { stdio: "ignore" });
      }
    } catch {}
  }
  flaskProc = null;
}

async function launch() {
  const win = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 1000,
    minHeight: 700,
    title: "Commission Portal",
    icon: path.join(__dirname, "assets", "icon.ico"),
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  });
  win.removeMenu();
  // Small-screen laptops otherwise open at a fixed 1400x900 that doesn't
  // fit, cutting off wide commission tables with no visible way to
  // maximize (no menu bar). Start maximized; Ctrl+=/-/0 below covers the
  // rest (zooming out to fit a wide table even when already maximized).
  win.maximize();
  await win.loadFile(path.join(__dirname, "loading.html"));

  win.webContents.on("before-input-event", (event, input) => {
    if (input.type !== "keyDown" || !(input.control || input.meta)) return;
    if (input.key === "=" || input.key === "+") {
      win.webContents.setZoomLevel(win.webContents.getZoomLevel() + 0.5);
      event.preventDefault();
    } else if (input.key === "-") {
      win.webContents.setZoomLevel(win.webContents.getZoomLevel() - 0.5);
      event.preventDefault();
    } else if (input.key === "0") {
      win.webContents.setZoomLevel(0);
      event.preventDefault();
    }
  });

  const root = findCommissionRoot();
  if (!root) {
    // On macOS the overwhelmingly likely cause is not a broken install but
    // App Translocation: a still-quarantined app (dragged out of the disk
    // image by hand instead of run through the installer) is executed from a
    // randomised read-only copy under /private/var/folders, so the sibling
    // "8. Web Dashboard" folder is nowhere near __dirname. The generic
    // "reinstall it" text sent people round the same loop, because dragging
    // it again reproduces the same state. Name the actual fix instead.
    const translocated =
      os.platform() === "darwin" &&
      (__dirname.includes("/AppTranslocation/") || __dirname.startsWith("/private/var/folders/"));
    const message = translocated
      ? "macOS is running this app from a temporary read-only copy, so it " +
        "cannot see the dashboard files next to it.\n\n" +
        "Open the downloaded .dmg again and run " +
        "\"Install Commission Dashboard.command\" instead of dragging the " +
        "app out. That puts it in your Applications folder and clears the " +
        "download quarantine that causes this."
      : os.platform() === "darwin"
      ? "Could not find the dashboard files (\"8. Web Dashboard/app.py\") " +
        "next to this app.\n\n" +
        "CommissionDashboard must stay inside the \"Commission Dashboard\" " +
        "folder — moving the app out on its own leaves it with nothing to " +
        "run. Open the downloaded .dmg again and run " +
        "\"Install Commission Dashboard.command\"."
      : "Could not find the dashboard files (8. Web Dashboard\\app.py) near this app. Reinstall the Finance Commission Dashboard.";
    dialog.showErrorBox("Commission Portal", message);
    app.quit();
    return;
  }

  startFlask(root);
  const deadline = Date.now() + 60_000;
  await waitForPort(60_000);

  // A bare TCP connect can succeed against the previous, still-dying
  // listener that freePort() just killed — loading then gets
  // ERR_CONNECTION_REFUSED. Retry until the page actually loads.
  let loaded = false;
  while (Date.now() < deadline) {
    try {
      await win.loadURL(DASHBOARD_URL);
      loaded = true;
      break;
    } catch {
      await new Promise((r) => setTimeout(r, 1000));
    }
  }
  if (!loaded) {
    // Say what actually happened, not a guess. The old text blamed a missing
    // Python environment, which stopped being the plausible cause the moment
    // the installer started bundling one — while the real first-run failure
    // (no access keys in .env) crashed the server silently and got
    // misdiagnosed. The server's own last words are in shell-startup.log.
    const tail = startupLogTail(root);
    // Was looking for python.exe on every platform, so on a Mac — where the
    // bundled interpreter is runtime/bin/python3 — this was always false and
    // the dialog always blamed a missing Python environment, whatever had
    // actually gone wrong. Check the paths startFlask() actually uses.
    const hasInterpreter = os.platform() === "win32"
      ? fs.existsSync(path.join(root, "runtime", "python.exe")) ||
        fs.existsSync(path.join(root, ".venv", "Scripts", "python.exe"))
      : fs.existsSync(path.join(root, "runtime", "bin", "python3")) ||
        fs.existsSync(path.join(root, ".venv", "bin", "python3"));
    let hint;
    if (tail.includes("PG_MIRROR_TOKEN") || tail.includes("PG_PROXY_TOKEN")) {
      hint = "The access keys are missing or wrong. Ask IT for your keys and " +
        "add them to the .env file in the install folder, then open this app again.";
    } else if (!hasInterpreter) {
      const setupCmd = os.platform() === "win32"
        ? "\"Setup Environment.bat\""
        : "the setup script";
      hint = `No Python environment was found — run ${setupCmd} ` +
        "in the install folder, then open this app again.";
    } else if (os.platform() === "darwin" && isQuarantined(root)) {
      // Checked, not inferred. macOS refuses to run quarantined programs, and
      // that is the one cause the app can both confirm and give an exact fix
      // for.
      hint = "macOS has this install quarantined, so it will not let the " +
        "bundled Python run.\n\n" +
        "Open the downloaded .dmg again and run " +
        "\"Install Commission Dashboard.command\". It clears the quarantine; " +
        "opening the app straight from the disk image, or dragging the folder " +
        "across by hand, leaves it in place.";
    } else if (launchFailure) {
      hint = "The bundled Python would not start.\n\n" + launchFailure +
        "\n\nSend this message to IT.";
    } else {
      hint = "Check \"8. Web Dashboard\\shell-startup.log\" and " +
        "\"8. Web Dashboard\\dashboard.log\" in the install folder, or send " +
        "them to IT.";
    }
    dialog.showErrorBox(
      "Commission Portal",
      "The dashboard server did not start.\n\n" + hint +
        (tail ? "\n\nServer output:\n" + tail : "")
    );
    app.quit();
  }
}

app.whenReady().then(launch);
app.on("window-all-closed", () => app.quit());
app.on("before-quit", stopFlask);
app.on("quit", stopFlask);
