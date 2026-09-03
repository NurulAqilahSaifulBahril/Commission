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

// ── macOS: the install lives here, not next to the app ───────────────────────
//
// On Windows the shell sits inside the install folder and the dashboard files
// are its siblings. That shape cannot work on a Mac, and every macOS failure
// this app has had traces back to trying:
//
//   * Everything dragged out of a downloaded .dmg carries com.apple.quarantine.
//     Approving the .app ("Open anyway") approves the .app alone — the sibling
//     runtime/bin/python3 and its dylibs stay quarantined, so the window opens
//     and the server it spawns is killed on sight.
//   * App Translocation. macOS runs a quarantined app from a randomised
//     read-only copy under /private/var/folders, so siblings are nowhere near
//     __dirname. Finder only exempts an app from this when the user drags the
//     .app ITSELF; dragging a folder that contains it does not count — which is
//     exactly what the old disk image asked people to do.
//
// So the payload ships INSIDE the bundle (Contents/Resources/payload.tar.gz)
// and is unpacked here on first launch. The bundle travels with the app, so
// translocation is harmless; this directory is ours, so it is writable and
// nothing in it is quarantined. Python path resolution needs no change at all:
// every module derives its root from __file__, so it simply follows.
const MAC_ROOT = path.join(
  os.homedir(), "Library", "Application Support", "Commission Dashboard");

// Written by seedMacInstall() to record which bundle produced this tree. A
// re-seed happens only when the .app on disk is a different build, so an OTA
// update — which bumps version.json but not this — is never rolled back by it.
const MAC_SEED_STAMP = ".seeded-from";

// Live state a re-seed must carry across. Same paths updater.py pins in
// PRESERVE_PATHS, for the same reason: they are the user's, not the build's.
const MAC_PRESERVE = [
  ".env",
  "8. Web Dashboard/dashboard.db",
  "8. Web Dashboard/data",
  "8. Web Dashboard/special_cases.json",
  "8. Web Dashboard/factory_rates.json",
];

function run(cmd, args, opts = {}) {
  return new Promise((resolve) => {
    const child = spawn(cmd, args, { stdio: "ignore", ...opts });
    child.on("error", (err) => resolve({ ok: false, detail: err.message }));
    child.on("exit", (code, signal) => resolve({
      ok: code === 0,
      detail: signal ? `killed by ${signal}` : `exit code ${code}`,
    }));
  });
}

// rename() when it can, copy when it cannot. Same volume is the norm here (the
// stash is deliberately a sibling of the install), but a redirected Application
// Support folder on a managed Mac could land it elsewhere, and EXDEV must not
// be what loses someone their edited rules.
function move(from, to) {
  try {
    fs.renameSync(from, to);
  } catch (err) {
    if (err.code !== "EXDEV") throw err;
    fs.cpSync(from, to, { recursive: true });
    fs.rmSync(from, { recursive: true, force: true });
  }
}

// The payload shipped inside this bundle, or null when there is none — which
// means we are running from a source checkout (npm start), where the dashboard
// files are already on disk and findCommissionRoot() is the right answer.
function bundledPayload() {
  const res = process.resourcesPath;
  if (!res) return null;
  const archive = path.join(res, "payload.tar.gz");
  if (!fs.existsSync(archive)) return null;
  let build = "";
  try {
    build = fs.readFileSync(path.join(res, "payload-version.txt"), "utf8").trim();
  } catch {}
  return { archive, build };
}

// Where live state waits while tar writes over the tree. A sibling of the
// install so it is on the same volume: see the note in seedMacInstall().
const MAC_STASH = MAC_ROOT + ".seed-stash";

// Put stashed live state back and remove the stash. Safe to call at any point:
// it only ever moves files towards the install, never away from it.
function restoreStash() {
  if (!fs.existsSync(MAC_STASH)) return false;
  for (const rel of MAC_PRESERVE) {
    const from = path.join(MAC_STASH, rel);
    if (!fs.existsSync(from)) continue;
    const to = path.join(MAC_ROOT, rel);
    // Whatever is at the destination is the release's copy of a file that
    // belongs to this install; the stashed one wins.
    fs.rmSync(to, { recursive: true, force: true });
    fs.mkdirSync(path.dirname(to), { recursive: true });
    move(from, to);
  }
  fs.rmSync(MAC_STASH, { recursive: true, force: true });
  return true;
}

// Unpack the bundled payload into MAC_ROOT, first run and after an upgrade.
// Returns the install root, or throws with something worth showing a user.
async function seedMacInstall(payload, status) {
  // FIRST, before deciding anything else. A stash still on disk means a
  // previous seed died between moving live state out and putting it back, so
  // it holds the only copy of that install's database and edited rules.
  // Recovering it has to happen before the checks below, because an unpack
  // that died even earlier leaves no app.py — which would read as "fresh
  // install", skip the stash handling entirely, and the recovery step at the
  // end would then delete the lot.
  if (restoreStash()) {
    console.log("recovered live state from an interrupted setup");
  }

  let seededFrom = "";
  try {
    seededFrom = fs.readFileSync(path.join(MAC_ROOT, MAC_SEED_STAMP), "utf8").trim();
  } catch {}
  const haveApp = fs.existsSync(path.join(MAC_ROOT, "8. Web Dashboard", "app.py"));
  if (haveApp && seededFrom && seededFrom === payload.build) return MAC_ROOT;

  const upgrading = haveApp;
  status(upgrading
    ? "Updating the dashboard files. This takes a minute..."
    : "Setting up the dashboard for the first time. This takes a minute...");

  // Move live state aside. tar overwrites what the archive holds and leaves
  // everything else alone, and special_cases.json / factory_rates.json ARE in
  // the archive — so without this an upgrade would silently discard rules the
  // admin edited in the app.
  //
  // The stash is a sibling of MAC_ROOT rather than somewhere under /var/folders
  // so that it is on the same volume, which makes every move below a rename:
  // instant, and no second copy of "8. Web Dashboard/data" on disk. That folder
  // holds cached reports and can run to hundreds of MB, so copying it out and
  // back on every version upgrade is a cost worth not paying.
  if (upgrading) {
    fs.mkdirSync(MAC_STASH, { recursive: true });
    for (const rel of MAC_PRESERVE) {
      const from = path.join(MAC_ROOT, rel);
      if (!fs.existsSync(from)) continue;
      const to = path.join(MAC_STASH, rel);
      fs.mkdirSync(path.dirname(to), { recursive: true });
      move(from, to);
    }
  }

  fs.mkdirSync(MAC_ROOT, { recursive: true });
  const untar = await run("/usr/bin/tar", ["-xzf", payload.archive, "-C", MAC_ROOT]);
  if (!untar.ok) {
    throw new Error(
      `Could not unpack the dashboard files into\n${MAC_ROOT}\n\n${untar.detail}`);
  }

  restoreStash();

  // Belt and braces. Nothing we write here should carry the download flag, but
  // if anything ever does, macOS kills the bundled interpreter and the failure
  // looks like a broken Python rather than a quarantined one.
  await run("/usr/bin/xattr", ["-dr", "com.apple.quarantine", MAC_ROOT]);

  // A freshly unpacked runtime has no bytecode, and compiling it on first
  // import is what made the first launch on an Intel Mac take over a minute
  // — long enough that the shell gave up and killed the server as it came
  // up. Compile it here instead, where the loading screen already says the
  // setup is running. Best effort: a failure just means the slow first
  // import happens later, under the longer start allowance in launch().
  status("Preparing the dashboard for its first start...");
  await run(path.join(MAC_ROOT, "runtime", "bin", "python3"),
    ["-m", "compileall", "-q", "-j", "0", MAC_ROOT]);

  fs.writeFileSync(path.join(MAC_ROOT, MAC_SEED_STAMP), payload.build, "utf8");
  return MAC_ROOT;
}

// Where the install root is on Windows, and in a source checkout on any
// platform: walk up from this file until "8. Web Dashboard/app.py" appears.
//
//   Windows install: <root>/shell/resources/app.asar        -> 4 hops
//   Source checkout: <root>/10. Electron App/app/main.js    -> 3 hops
//   npm run dist:    .../app/dist/*/resources/app.asar      -> 7 hops
//
// A packaged macOS build never gets here — resolveRoot() answers with MAC_ROOT
// before this is called. 15 is simply well clear of the deepest case above.
function findCommissionRoot() {
  let dir = __dirname;
  for (let i = 0; i < 15; i++) {
    if (fs.existsSync(path.join(dir, "8. Web Dashboard", "app.py"))) return dir;
    const parent = path.dirname(dir);
    if (parent === dir) break;  // hit the filesystem root
    dir = parent;
  }
  return null;
}

// Packaged on macOS the answer is always MAC_ROOT; everywhere else — Windows,
// and a Mac source checkout — it is still found by walking up from __dirname.
async function resolveRoot(status) {
  if (os.platform() === "darwin") {
    const payload = bundledPayload();
    if (payload) return await seedMacInstall(payload, status);
  }
  return findCommissionRoot();
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
// assumed. seedMacInstall() strips this from the install root every time it
// runs, so a positive here means something re-applied it and the bundled
// interpreter is being killed for that reason rather than any other.
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

  // Progress on the loading screen. The first launch on a Mac unpacks a few
  // hundred MB before the server is even started, and a window that says
  // "Starting the dashboard..." for two minutes reads as a hang.
  const status = (msg) => {
    try {
      win.webContents.executeJavaScript(
        `(() => { const el = document.getElementById("status");` +
        ` if (el) el.textContent = ${JSON.stringify(msg)}; })()`
      );
    } catch {}
  };

  let root;
  try {
    root = await resolveRoot(status);
  } catch (err) {
    dialog.showErrorBox(
      "Commission Portal",
      "The Commission Portal could not set itself up.\n\n" + err.message +
        "\n\nSend this message to IT."
    );
    app.quit();
    return;
  }
  if (!root) {
    dialog.showErrorBox(
      "Commission Portal",
      os.platform() === "darwin"
        ? "This copy of the Commission Portal is missing its dashboard files.\n\n" +
          "It looks like it was built or copied incompletely. Download the " +
          ".dmg again from the Releases page and drag CommissionDashboard to " +
          "Applications."
        : "Could not find the dashboard files (8. Web Dashboard\\app.py) near " +
          "this app. Reinstall the Finance Commission Dashboard."
    );
    app.quit();
    return;
  }

  // The install root is ours and inside the user's home, so it is writable in
  // every normal case. A managed Mac with a redirected or locked Application
  // Support folder is the exception, and it fails in a way worth naming: the
  // dashboard keeps its database, .env and logs here.
  if (os.platform() === "darwin") {
    try {
      fs.accessSync(root, fs.constants.W_OK);
    } catch {
      dialog.showErrorBox(
        "Commission Portal",
        `The Portal cannot write to its own folder:\n\n${root}\n\n` +
          "It keeps its settings, database and logs there. Ask IT to check " +
          "the permissions on that folder."
      );
      app.quit();
      return;
    }
  }

  status("Starting the dashboard...");
  startFlask(root);
  // The server can take a while to listen: a cold runtime imports pandas and
  // friends, then reaches the database before it binds the port. The first
  // launch on an Intel Mac measured 63 s to the banner, and the old 60 s
  // budget declared it dead one second after it came up. Give it three
  // minutes, and tell the user it is still going rather than look hung.
  const SERVER_START_MS = 180_000;
  const stillGoing = setTimeout(() => status(
    "Still starting. The first start after an install can take a couple of minutes..."),
    20_000);
  const listening = await waitForPort(SERVER_START_MS);
  clearTimeout(stillGoing);
  // The page-load retries get their own budget, counted from when the port
  // opened. It used to share the port wait's deadline, so a slow start left
  // zero attempts and the shell blamed a server that was in fact running.
  const deadline = Date.now() + (listening ? 30_000 : 0);

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
      hint = os.platform() === "win32"
        ? "No Python environment was found — run \"Setup Environment.bat\" " +
          "in the install folder, then open this app again."
        : "The bundled Python is missing from\n" + root + "\n\n" +
          "The setup step did not finish. Quit the Portal, delete that " +
          "folder, and open the app again to rebuild it.";
    } else if (os.platform() === "darwin" && isQuarantined(root)) {
      // Checked, not inferred. macOS refuses to run quarantined programs.
      // Re-opening the app is the fix because seedMacInstall() strips the flag
      // on every launch — so if this is still true, it is being re-applied and
      // IT needs to know rather than the user retrying forever.
      hint = "macOS has quarantined the dashboard files, so it will not let " +
        "the bundled Python run.\n\n" + root + "\n\n" +
        "Quit the Portal and open it again. If this message comes back, " +
        "send it to IT.";
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
