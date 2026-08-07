/* Self-update UI.
 *
 * Shows the installed version in the sidebar footer, polls GitHub (through the
 * server) for a newer release, and drives the install → restart → reload cycle.
 * Only admins get the Install button; everyone else just sees that an update
 * is waiting.
 */
(function () {
    "use strict";

    const CHECK_INTERVAL_MS = 60 * 60 * 1000;   // hourly background re-check
    const POLL_INTERVAL_MS = 1500;              // while an update is running

    const el = (id) => document.getElementById(id);
    const group = el("updateGroup");
    const card = el("updateCard");
    const headline = el("updateHeadline");
    const sub = el("updateSub");
    const progress = el("updateProgress");
    const progressBar = el("updateProgressBar");
    const installBtn = el("updateInstallBtn");
    const notesLink = el("updateNotesLink");
    const versionLabel = el("appVersion");

    if (!group || !installBtn) return;

    let isAdmin = false;
    let latest = null;
    let pollTimer = null;

    async function getJSON(url, options) {
        const resp = await fetch(url, options || {});
        if (!resp.ok && resp.status !== 202 && resp.status !== 409) {
            throw new Error("HTTP " + resp.status);
        }
        return resp.json();
    }

    function show(state) {
        group.style.display = "";
        card.dataset.state = state;
    }

    // ── Version label ─────────────────────────────────────────────────────────
    async function loadVersion() {
        try {
            const info = await getJSON("/api/version");
            if (versionLabel) versionLabel.textContent = "v" + info.version;
        } catch (e) {
            /* footer label is cosmetic — stay quiet */
        }
    }

    async function loadRole() {
        try {
            const me = await getJSON("/api/me");
            isAdmin = me && me.role === "admin";
        } catch (e) {
            isAdmin = false;
        }
    }

    // ── Check ─────────────────────────────────────────────────────────────────
    async function check(force) {
        let info;
        try {
            info = await getJSON("/api/update/check" + (force ? "?force=1" : ""));
        } catch (e) {
            return;
        }
        latest = info;

        if (!info.update_available) {
            group.style.display = "none";
            return;
        }

        show("available");
        headline.textContent = "Version " + info.latest_version + " is available";
        sub.textContent = "You are on v" + info.current_version + ".";
        installBtn.style.display = "";
        installBtn.disabled = false;
        progress.style.display = "none";

        if (info.release_url) {
            notesLink.href = info.release_url;
            notesLink.style.display = "";
        } else {
            notesLink.style.display = "none";
        }
    }

    // ── Install ───────────────────────────────────────────────────────────────
    installBtn.addEventListener("click", async () => {
        if (!latest) return;
        const ok = window.confirm(
            "⚠️  Install version " + latest.latest_version + "?\n\n" +
            "The dashboard will:\n" +
            "  • Restart immediately (any unsaved work will be lost)\n" +
            "  • Download and apply the update\n" +
            "  • Reload this page automatically\n\n" +
            "Your database and Excel files are NOT affected."
        );
        if (!ok) return;

        installBtn.disabled = true;
        show("working");
        headline.textContent = "Updating to v" + latest.latest_version;
        progress.style.display = "";
        setProgress(1, "Starting update…");

        try {
            const state = await getJSON("/api/update/apply", { method: "POST" });
            if (state.phase === "error") {
                return fail(state.error || "Update failed.");
            }
            startPolling();
        } catch (e) {
            fail("Could not start the update: " + e.message);
        }
    });

    function setProgress(pct, message) {
        progressBar.style.width = Math.max(2, Math.min(100, pct)) + "%";
        sub.textContent = message || "";
    }

    function fail(message) {
        stopPolling();
        show("error");
        headline.textContent = "Update failed";
        sub.textContent = message;
        progress.style.display = "none";
        installBtn.disabled = false;
        installBtn.innerHTML = '<span class="icon">🔁</span> Retry Update';
    }

    function startPolling() {
        stopPolling();
        pollTimer = setInterval(pollOnce, POLL_INTERVAL_MS);
    }

    function stopPolling() {
        if (pollTimer) clearInterval(pollTimer);
        pollTimer = null;
    }

    async function pollOnce() {
        let state;
        try {
            state = await getJSON("/api/update/status");
        } catch (e) {
            // The server going unreachable is the expected end of an update —
            // it has exited so its files can be replaced. Wait for it to return.
            stopPolling();
            waitForRestart();
            return;
        }
        if (state.phase === "error") return fail(state.error || "Update failed.");
        setProgress(state.progress || 0, state.message || "Working…");
        if (state.phase === "restarting" && (state.progress || 0) >= 95) {
            stopPolling();
            waitForRestart();
        }
    }

    // ── Wait for the restarted server, then reload ────────────────────────────
    function waitForRestart() {
        show("working");
        headline.textContent = "Restarting dashboard";
        progress.style.display = "";
        setProgress(97, "Waiting for the dashboard to come back…");

        const deadline = Date.now() + 5 * 60 * 1000;
        const timer = setInterval(async () => {
            if (Date.now() > deadline) {
                clearInterval(timer);
                return fail("The dashboard did not restart. Check dashboard.log, or start it from the Start Menu.");
            }
            try {
                const resp = await fetch("/api/boot-id", { cache: "no-store" });
                if (!resp.ok) return;
                clearInterval(timer);
                setProgress(100, "Update complete — reloading…");
                // Drop the cached commission snapshot so the new build never
                // renders against data shaped by the old one.
                try { localStorage.removeItem("commissionCache"); } catch (e) {}
                setTimeout(() => window.location.reload(true), 800);
            } catch (e) {
                /* still down — keep waiting */
            }
        }, 2000);
    }

    // ── Boot ──────────────────────────────────────────────────────────────────
    (async function init() {
        await Promise.all([loadVersion(), loadRole()]);

        // If a previous page load kicked off an update, rejoin it.
        try {
            const state = await getJSON("/api/update/status");
            if (state.phase && !["idle", "done", "error"].includes(state.phase)) {
                show("working");
                headline.textContent = "Updating…";
                progress.style.display = "";
                installBtn.disabled = true;
                setProgress(state.progress || 0, state.message || "");
                startPolling();
                return;
            }
        } catch (e) { /* fall through to a normal check */ }

        check(false);
        setInterval(() => check(true), CHECK_INTERVAL_MS);
    })();
})();
