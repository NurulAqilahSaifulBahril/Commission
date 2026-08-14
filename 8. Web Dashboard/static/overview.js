(function () {
    "use strict";

    const MONTH_NAMES = ["January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December"];

    // Same ramp the PDF's Payout by Type page uses, so the two read as one
    // report rather than two different-looking views of the same figures.
    const TYPE_COLORS = {
        basic: "#0E7C66",
        nfp: "#1B9A81",
        anp: "#33B79B",
        other: "#5CCEB4",
    };

    const state = { year: "2026", month: "8" };

    const yearSelect = document.getElementById("yearSelect");
    const monthSelect = document.getElementById("monthSelect");
    const cardsRow = document.getElementById("overviewCards");
    const loader = document.getElementById("overviewLoader");
    const emptyView = document.getElementById("overviewEmpty");

    function fmtRM(value) {
        const n = Number(value) || 0;
        return `RM ${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    }

    function fmtCompact(value) {
        const n = Number(value) || 0;
        if (Math.abs(n) >= 1e6) return `RM ${(n / 1e6).toFixed(2)}m`;
        if (Math.abs(n) >= 1e3) return `RM ${(n / 1e3).toFixed(1)}k`;
        return fmtRM(n);
    }

    function escapeHtml(str) {
        return String(str == null ? "" : str).replace(/[&<>"']/g, c => (
            { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
        ));
    }

    /** "+12.4% vs last month" with its own colour, or null when there is no
     *  prior month to compare against -- deliberately silent rather than
     *  showing a made-up 0%. */
    function deltaText(current, previous, mode) {
        if (previous === null || previous === undefined) return null;
        const cur = Number(current) || 0, prev = Number(previous) || 0;
        if (mode === "points") {
            const diff = cur - prev;
            if (Math.abs(diff) < 0.005) return { text: "No change vs last month", cls: "flat" };
            return {
                text: `${diff > 0 ? "+" : "−"}${Math.abs(diff).toFixed(2)} pts vs last month`,
                cls: diff > 0 ? "up" : "down",
            };
        }
        if (mode === "count") {
            const diff = cur - prev;
            if (diff === 0) return { text: "No change vs last month", cls: "flat" };
            return {
                text: `${diff > 0 ? "+" : "−"}${Math.abs(diff)} vs last month`,
                cls: diff > 0 ? "up" : "down",
            };
        }
        if (!prev) return { text: "No prior month on file", cls: "flat" };
        const pct = (cur - prev) / Math.abs(prev) * 100;
        if (Math.abs(pct) < 0.05) return { text: "No change vs last month", cls: "flat" };
        return {
            text: `${pct > 0 ? "+" : "−"}${Math.abs(pct).toFixed(1)}% vs last month`,
            cls: pct > 0 ? "up" : "down",
        };
    }

    function makeCard(label, value, delta) {
        const deltaHtml = delta
            ? `<span class="metric-delta ${delta.cls}">${escapeHtml(delta.text)}</span>`
            : "";
        return `<div class="card metric-card">
            <div class="metric-details">
                <span class="metric-label">${escapeHtml(label)}</span>
                <span class="metric-value">${escapeHtml(value)}</span>
                ${deltaHtml}
            </div>
        </div>`;
    }

    function renderCards(totals, prev) {
        const p = prev || {};
        cardsRow.innerHTML = [
            makeCard("Total Sales", fmtCompact(totals.sales), deltaText(totals.sales, prev ? p.sales : null)),
            makeCard("Total Commission", fmtRM(totals.total), deltaText(totals.total, prev ? p.total : null)),
            makeCard("Effective Rate", `${(Number(totals.effective_rate) || 0).toFixed(2)}%`,
                deltaText(totals.effective_rate, prev ? p.effective_rate : null, "points")),
            makeCard("Total Agents", String(totals.agents ?? 0),
                deltaText(totals.agents, prev ? p.agents : null, "count")),
            makeCard("Total Customers/ Invoices", String(totals.customers ?? 0),
                deltaText(totals.customers, prev ? p.customers : null, "count")),
            makeCard("Total Other Commission", fmtRM(totals.other_commission),
                deltaText(totals.other_commission, prev ? p.other_commission : null)),
            makeCard("Referral Fee", fmtRM(totals.referral_fee),
                deltaText(totals.referral_fee, prev ? p.referral_fee : null)),
        ].join("");
    }

    function renderPayoutByType(totals) {
        const segments = [
            { key: "basic", label: "Basic Commission", value: Number(totals.basic) || 0 },
            { key: "nfp", label: "Net Floor Price", value: Number(totals.nfp) || 0 },
            { key: "anp", label: "ANP Commission", value: Number(totals.anp) || 0 },
            { key: "other", label: "Other Commission", value: Number(totals.other_commission) || 0 },
        ];
        const sum = segments.reduce((a, s) => a + s.value, 0);
        const bar = document.getElementById("payoutBar");
        const legend = document.getElementById("payoutLegend");

        if (!sum) {
            bar.innerHTML = `<div class="payout-seg" style="width:100%; background:var(--border-color);"></div>`;
            legend.innerHTML = `<div class="payout-legend-row"><span>No commission this period</span></div>`;
            return;
        }

        bar.innerHTML = segments.filter(s => s.value > 0).map(s =>
            `<div class="payout-seg" title="${escapeHtml(s.label)}"
                  style="width:${(s.value / sum * 100).toFixed(2)}%; background:${TYPE_COLORS[s.key]};"></div>`
        ).join("");

        legend.innerHTML = segments.map(s => `
            <div class="payout-legend-row">
                <span class="payout-dot" style="background:${TYPE_COLORS[s.key]}"></span>
                <span class="payout-name">${escapeHtml(s.label)}</span>
                <span class="payout-pct">${sum ? (s.value / sum * 100).toFixed(1) : "0.0"}%</span>
                <strong class="payout-amt">${fmtRM(s.value)}</strong>
            </div>`).join("");
    }

    /** "+3" / "−2" / "NEW" against last month's position in the same combined
     *  Internal+Outsource leaderboard. Mirrors _rank_changes() server side. */
    function rankBadge(code) {
        const raw = String(code || "flat");
        if (raw === "new") return `<span class="rank-badge new">NEW</span>`;
        if (raw.startsWith("up:")) return `<span class="rank-badge up">+${escapeHtml(raw.split(":")[1])}</span>`;
        if (raw.startsWith("down:")) return `<span class="rank-badge down">−${escapeHtml(raw.split(":")[1])}</span>`;
        return `<span class="rank-badge flat">—</span>`;
    }

    function renderTopPerformers(rows) {
        const box = document.getElementById("topPerformers");
        if (!rows || !rows.length) {
            box.innerHTML = `<div class="top-row"><span>No agents with commission this period.</span></div>`;
            return;
        }
        box.innerHTML = rows.map((r, i) => `
            <div class="top-row">
                <span class="top-rank">${String(i + 1).padStart(2, "0")}</span>
                <span class="top-tag ${r.type === "Internal" ? "int" : "out"}">${r.type === "Internal" ? "INT" : "OUT"}</span>
                <span class="top-name">${escapeHtml(r.agent)}</span>
                <span class="top-amt">${fmtRM(r.total)}</span>
                ${rankBadge(r.rank_change)}
            </div>`).join("");
    }

    function setBusy(busy) {
        loader.classList.toggle("hidden", !busy);
    }

    // A cold cache (server just restarted, or a background rebuild is running)
    // resolves on its own within seconds, so poll rather than stranding the
    // user on a dead page they have to reload by hand.
    let retryTimer = null;
    const RETRY_MS = 5000;
    const MAX_RETRIES = 24;   // ~2 minutes, which covers a full prefetch
    let retries = 0;

    async function loadOverview(isRetry) {
        if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
        if (!isRetry) retries = 0;
        setBusy(true);
        emptyView.classList.add("hidden");
        try {
            const res = await fetch(`/api/overview?year=${encodeURIComponent(state.year)}&month=${encodeURIComponent(state.month)}`);
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.error || "Failed to load overview");

            if (!data.ready) {
                cardsRow.innerHTML = "";
                document.getElementById("payoutBar").innerHTML = "";
                document.getElementById("payoutLegend").innerHTML = "";
                document.getElementById("topPerformers").innerHTML = "";
                const keepTrying = retries < MAX_RETRIES;
                document.getElementById("overviewEmptyMsg").textContent = keepTrying
                    ? `${data.message || "Commission data is still being prepared."} Retrying…`
                    : (data.message || "Commission data is still being prepared.");
                emptyView.classList.remove("hidden");
                if (keepTrying) {
                    retries += 1;
                    retryTimer = setTimeout(() => loadOverview(true), RETRY_MS);
                }
                return;
            }

            renderCards(data.totals, data.prev_totals);
            renderPayoutByType(data.totals);
            renderTopPerformers(data.top_performers);

            document.getElementById("overviewPeriod").textContent =
                `${MONTH_NAMES[parseInt(state.month, 10) - 1]} ${state.year}`;
            document.getElementById("overviewSubHeader").textContent = data.scoped_to_agent
                ? `Sales & commission analysis — ${data.scoped_to_agent}`
                : "Sales & commission analysis — all agents";
        } catch (err) {
            console.error("Overview load failed:", err);
            document.getElementById("overviewEmptyMsg").textContent = err.message;
            emptyView.classList.remove("hidden");
        } finally {
            setBusy(false);
        }
    }

    async function initAccountBar() {
        try {
            const res = await fetch("/api/me");
            if (!res.ok) { window.location.href = "/login"; return; }
            const me = await res.json();
            const whoami = document.getElementById("accountWhoami");
            if (whoami) whoami.textContent = `${me.username} (${me.role})`;
            const adminLink = document.getElementById("adminLink");
            if (adminLink && me.role === "admin") adminLink.style.display = "flex";
        } catch (err) {
            console.error("Failed to load account info:", err);
        }
    }

    function init() {
        state.year = yearSelect.value;
        state.month = monthSelect.value;
        yearSelect.addEventListener("change", () => { state.year = yearSelect.value; loadOverview(); });
        monthSelect.addEventListener("change", () => { state.month = monthSelect.value; loadOverview(); });

        const logoutBtn = document.getElementById("logoutBtn");
        if (logoutBtn) {
            logoutBtn.addEventListener("click", async () => {
                await fetch("/logout", { method: "POST" });
                window.location.href = "/login";
            });
        }

        initAccountBar();
        loadOverview();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
