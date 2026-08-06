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

const PORT = 5001;
const DASHBOARD_URL = `http://127.0.0.1:${PORT}`;

// Dev: this file lives at <root>/10. Electron App/app/main.js.
// Packaged: the built shell is copied to <root>/shell/, putting the exe's
// resources at <root>/shell/resources/app*, so walk up until app.py appears.
function findCommissionRoot() {
  let dir = __dirname;
  // 8 levels: deployed is <root>/shell/resources/app.asar (4 hops), but the
  // dev build sits at <root>/10. Electron App/app/dist/win-unpacked/resources/
  // app.asar (7 hops) and should be launchable for testing too.
  for (let i = 0; i < 8; i++) {
    if (fs.existsSync(path.join(dir, "8. Web Dashboard", "app.py"))) return dir;
    dir = path.dirname(dir);
  }
  return null;
}

function freePort() {
  try {
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
  } catch {} // findstr exits 1 when nothing is listening — port already free
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

function startFlask(root) {
  freePort();

  const cachePkl = path.join(root, "8. Web Dashboard", "data", "dashboard_cache.pkl");
  try { fs.unlinkSync(cachePkl); } catch {}

  // The bundled runtime first: an install ships with its own interpreter and
  // dependencies, so nothing needs to be on the machine. .venv is the fallback
  // for installs made before the runtime was bundled, and for running from a
  // source checkout; bare "python" is the last resort.
  const candidates = [
    path.join(root, "runtime", "python.exe"),
    path.join(root, ".venv", "Scripts", "python.exe"),
  ];
  const python = candidates.find((p) => fs.existsSync(p)) || "python";

  flaskProc = spawn(python, [path.join(root, "8. Web Dashboard", "app.py")], {
    cwd: root,
    stdio: "ignore",
    windowsHide: true,
  });
  flaskProc.on("error", () => { flaskProc = null; });
}

function stopFlask() {
  if (flaskProc && !flaskProc.killed) {
    try { execSync(`taskkill /PID ${flaskProc.pid} /T /F`, { stdio: "ignore" }); } catch {}
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
    dialog.showErrorBox(
      "Commission Portal",
      "Could not find the dashboard files (8. Web Dashboard\\app.py) near this app. Reinstall the Finance Commission Dashboard."
    );
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
    dialog.showErrorBox(
      "Commission Portal",
      "The dashboard server did not start within 60 seconds.\n\n" +
        "If this is the first run, the Python environment may still be missing — run \"Setup Environment.bat\" in the install folder, then open this app again."
    );
    app.quit();
  }
}

app.whenReady().then(launch);
app.on("window-all-closed", () => app.quit());
app.on("before-quit", stopFlask);
app.on("quit", stopFlask);
