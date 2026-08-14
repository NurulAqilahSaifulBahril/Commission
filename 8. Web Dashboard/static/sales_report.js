(function () {
    "use strict";

    const MONTH_ABBR = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
        "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
    const MONTH_NAMES = ["January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December"];

    const state = { year: "2026", data: null };

    const yearSelect = document.getElementById("yearSelect");
    const cardsRow = document.getElementById("srCards");
    const sectionsBox = document.getElementById("srSections");
    const caveatBox = document.getElementById("srCaveat");
    const loader = document.getElementById("srLoader");
    const emptyView = document.getElementById("srEmpty");

    function fmtRM(value) {
        return (Number(value) || 0).toLocaleString(undefined, {
            minimumFractionDigits: 2, maximumFractionDigits: 2,
        });
    }

    function fmtCompact(value) {
        const n = Number(value) || 0;
        if (Math.abs(n) >= 1e6) return `RM ${(n / 1e6).toFixed(2)}m`;
        if (Math.abs(n) >= 1e3) return `RM ${(n / 1e3).toFixed(1)}k`;
        return `RM ${fmtRM(n)}`;
    }

    function escapeHtml(str) {
        return String(str == null ? "" : str).replace(/[&<>"']/g, c => (
            { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
        ));
    }

    function makeCard(label, value, note) {
        return `<div class="card metric-card">
            <div class="metric-details">
                <span class="metric-label">${escapeHtml(label)}</span>
                <span class="metric-value">${escapeHtml(value)}</span>
                ${note ? `<span class="metric-delta flat">${escapeHtml(note)}</span>` : ""}
            </div>
        </div>`;
    }

    function renderCards(data) {
        const t = data.totals;
        const internal = data.agents.filter(a => a.channel === "Internal");
        const outsource = data.agents.filter(a => a.channel === "Outsource");
        const qualified = data.agents.filter(a => a.qualified).length;
        cardsRow.innerHTML = [
            makeCard("Total Cases", String(t.cases), `${t.agents} agents`),
            makeCard("Total Net Sales", fmtCompact(t.sales), "net of EPP interest"),
            makeCard("Internal", fmtCompact(internal.reduce((s, a) => s + a.acc_ans, 0)),
                `${internal.length} agents · ${internal.reduce((s, a) => s + a.acc_noc, 0)} cases`),
            makeCard("Outsource", fmtCompact(outsource.reduce((s, a) => s + a.acc_ans, 0)),
                `${outsource.length} agents · ${outsource.reduce((s, a) => s + a.acc_noc, 0)} cases`),
            makeCard("Qualified", `${qualified} / ${t.agents}`,
                `RM ${fmtRM(data.targets.Internal)} internal · RM ${fmtRM(data.targets.Outsource)} outsource`),
        ].join("");
    }

    /** The property classifier defaults to Residential when SEDA nem type,
     *  referral project type and the package fields are all silent, so the
     *  RES/COM split reads much thinner here than on the printed report.
     *  Saying so beats letting someone read it as a data loss. */
    function renderCaveat(data) {
        const mix = data.property_mix || {};
        const total = Object.values(mix).reduce((a, b) => a + b, 0);
        const res = mix.Residential || 0;
        if (!total || res < total * 0.9) { caveatBox.hidden = true; return; }
        const others = Object.entries(mix)
            .filter(([k]) => k !== "Residential")
            .map(([k, v]) => `${v} ${k}`).join(", ") || "none";
        caveatBox.hidden = false;
        caveatBox.innerHTML =
            `<strong>Three columns differ from the printed report.</strong>
             <b>RES / COM</b> — the property classifier returned Residential for ${res} of
             ${total} invoices (others: ${escapeHtml(others)}). It falls back to Residential
             whenever SEDA nem type, referral project type and the package fields are all
             silent, so most agents show a single RES line where the print splits them.
             <b>Campaign window</b> — the print divides 1/5–10/6 into cases before 31/12/25 and
             after 1/1/26; every start date in Agent Roles is empty, so that split is not
             derivable and the window is shown as one figure. <b>EP point</b> — 1 point per RM1
             of sales price, so EP equals accumulated sales; the May multiplier is applied in
             the spreadsheet only and is not modelled anywhere in the pipeline.`;
    }

    function lastMonth(data) { return data.last_month || 6; }

    function headerFor(data) {
        const lm = lastMonth(data);
        const monthCols = [];
        for (let m = 1; m < lm; m++) monthCols.push(`<th colspan="2">${MONTH_ABBR[m - 1]}</th>`);
        const subCols = [];
        for (let m = 1; m < lm; m++) subCols.push("<th>NOC</th><th>ANS</th>");
        return `<thead>
            <tr>
                <th rowspan="2" class="sr-no">NO</th>
                <th rowspan="2" class="sr-name">NAME</th>
                <th rowspan="2">TYPE</th>
                ${monthCols.join("")}
                <th colspan="3">${MONTH_ABBR[lm - 1]}</th>
                <th colspan="2">TOTAL</th>
                <th colspan="2">ACC</th>
                <th colspan="2">1/5 – ${data.split_day || 10}/${lm}</th>
                <th rowspan="2">UP TO DATE<br>EP POINT</th>
                <th rowspan="2">BALANCE TO<br>QUALIFY</th>
            </tr>
            <tr>
                ${subCols.join("")}
                <th>NOC</th><th>1-${data.split_day || 10}</th><th>${(data.split_day || 10) + 1}-30</th>
                <th>NOC</th><th>ANS</th>
                <th>NOC</th><th>ANS</th>
                <th>CASES</th><th>ANS</th>
            </tr>
        </thead>`;
    }

    function agentRows(agent, no, data) {
        const lm = lastMonth(data);
        const types = agent.types.length ? agent.types : [{
            type: "RES", months: [], jun_early: 0, jun_late: 0, total_noc: 0, total_ans: 0,
        }];
        return types.map((t, i) => {
            const first = i === 0;
            const last = i === types.length - 1;
            const cells = [
                `<td class="sr-no">${first ? no : ""}</td>`,
                `<td class="sr-name">${first ? escapeHtml(agent.agent) : ""}</td>`,
                `<td class="sr-type">${escapeHtml(t.type)}</td>`,
            ];
            for (let m = 0; m < lm - 1; m++) {
                const cell = t.months[m] || { noc: 0, ans: 0 };
                cells.push(`<td class="numeric">${cell.noc}</td>`,
                    `<td class="numeric">${fmtRM(cell.ans)}</td>`);
            }
            const junCell = t.months[lm - 1] || { noc: 0, ans: 0 };
            cells.push(`<td class="numeric">${junCell.noc}</td>`,
                `<td class="numeric">${fmtRM(t.jun_early)}</td>`,
                `<td class="numeric">${fmtRM(t.jun_late)}</td>`,
                `<td class="numeric">${t.total_noc}</td>`,
                `<td class="numeric">${fmtRM(t.total_ans)}</td>`,
                // ACC merges RES and COM, so it belongs on the agent's last line
                `<td class="numeric">${last ? agent.acc_noc : ""}</td>`,
                `<td class="numeric sr-strong">${last ? fmtRM(agent.acc_ans) : ""}</td>`,
                `<td class="numeric">${first ? agent.campaign_cases : ""}</td>`,
                `<td class="numeric">${first ? fmtRM(agent.campaign_ans) : ""}</td>`,
                `<td class="numeric sr-strong">${first ? fmtRM(agent.ep) : ""}</td>`);
            if (!first) {
                cells.push("<td></td>");
            } else if (agent.qualified) {
                cells.push(`<td class="sr-qualified">${escapeHtml(agent.status || "Qualified")}</td>`);
            } else {
                cells.push(`<td class="numeric sr-short">${fmtRM(agent.balance)}</td>`);
            }
            return `<tr>${cells.join("")}</tr>`;
        }).join("");
    }

    function subtotalRow(members, data) {
        const lm = lastMonth(data);
        const cells = ['<td class="sr-no"></td>', '<td class="sr-name">SUBTOTAL</td>', "<td></td>"];
        const monthAns = m => members.reduce((sum, a) =>
            sum + a.types.reduce((s, t) => s + ((t.months[m] || {}).ans || 0), 0), 0);
        const monthNoc = m => members.reduce((sum, a) =>
            sum + a.types.reduce((s, t) => s + ((t.months[m] || {}).noc || 0), 0), 0);
        for (let m = 0; m < lm - 1; m++) {
            cells.push(`<td class="numeric">${monthNoc(m)}</td>`,
                `<td class="numeric">${fmtRM(monthAns(m))}</td>`);
        }
        const sum = fn => members.reduce((s, a) => s + fn(a), 0);
        cells.push(`<td class="numeric">${monthNoc(lm - 1)}</td>`,
            `<td class="numeric">${fmtRM(sum(a => a.types.reduce((s, t) => s + t.jun_early, 0)))}</td>`,
            `<td class="numeric">${fmtRM(sum(a => a.types.reduce((s, t) => s + t.jun_late, 0)))}</td>`,
            `<td class="numeric">${sum(a => a.acc_noc)}</td>`,
            `<td class="numeric">${fmtRM(sum(a => a.acc_ans))}</td>`,
            `<td class="numeric">${sum(a => a.acc_noc)}</td>`,
            `<td class="numeric sr-strong">${fmtRM(sum(a => a.acc_ans))}</td>`,
            `<td class="numeric">${sum(a => a.campaign_cases)}</td>`,
            `<td class="numeric">${fmtRM(sum(a => a.campaign_ans))}</td>`,
            "<td></td>", "<td></td>");
        return `<tr class="sr-subtotal">${cells.join("")}</tr>`;
    }

    function renderBlock(title, members, data) {
        if (!members.length) return "";
        const target = data.targets[members[0].channel];
        let body = "";
        members.forEach((a, i) => { body += agentRows(a, i + 1, data); });
        body += subtotalRow(members, data);
        return `<div class="card table-card sr-block">
            <div class="card-header">
                <h3>${escapeHtml(title)}</h3>
                <span class="row-count-badge">${members.length} agents ·
                    target RM ${fmtRM(target)} EP</span>
            </div>
            <div class="table-container">
                <table class="dashboard-table sr-table">
                    ${headerFor(data)}
                    <tbody>${body}</tbody>
                </table>
            </div>
        </div>`;
    }

    function renderSections(data) {
        const pick = (channel, branch) => data.agents.filter(a =>
            a.channel === channel && (branch === null || a.branch === branch));
        const chunks = [];

        chunks.push('<h3 class="sr-section-title">Company-wide</h3>');
        chunks.push(renderBlock("Internal Sales Agent", pick("Internal", null), data));
        chunks.push(renderBlock("Outsource Sales Agent", pick("Outsource", null), data));

        (data.branches || []).forEach(branch => {
            const internal = pick("Internal", branch);
            const outsource = pick("Outsource", branch);
            if (!internal.length && !outsource.length) return;
            chunks.push(`<h3 class="sr-section-title">${escapeHtml(branch)} Branch</h3>`);
            chunks.push(renderBlock("Internal Sales Agent", internal, data));
            chunks.push(renderBlock("Outsource Sales Agent", outsource, data));
        });

        sectionsBox.innerHTML = chunks.join("");
    }

    function exportCsv() {
        const data = state.data;
        if (!data) return;
        const lm = lastMonth(data);
        const head = ["SECTION", "CHANNEL", "NO", "NAME", "TYPE"];
        for (let m = 1; m < lm; m++) head.push(`${MONTH_ABBR[m - 1]} NOC`, `${MONTH_ABBR[m - 1]} ANS`);
        head.push(`${MONTH_ABBR[lm - 1]} NOC`, `${MONTH_ABBR[lm - 1]} 1-${data.split_day}`,
            `${MONTH_ABBR[lm - 1]} ${data.split_day + 1}-30`, "TOTAL NOC", "TOTAL ANS",
            "ACC NOC", "ACC ANS", "CAMPAIGN CASES", "CAMPAIGN ANS",
            "UP TO DATE EP POINT", "BALANCE TO QUALIFY", "STATUS");

        const lines = [head];
        const push = (section, members) => members.forEach((a, i) => {
            a.types.forEach((t, ti) => {
                const row = [section, a.channel, ti === 0 ? i + 1 : "", ti === 0 ? a.agent : "", t.type];
                for (let m = 0; m < lm - 1; m++) {
                    const c = t.months[m] || { noc: 0, ans: 0 };
                    row.push(c.noc, c.ans.toFixed(2));
                }
                const jc = t.months[lm - 1] || { noc: 0 };
                const last = ti === a.types.length - 1;
                row.push(jc.noc, t.jun_early.toFixed(2), t.jun_late.toFixed(2),
                    t.total_noc, t.total_ans.toFixed(2),
                    last ? a.acc_noc : "", last ? a.acc_ans.toFixed(2) : "",
                    ti === 0 ? a.campaign_cases : "", ti === 0 ? a.campaign_ans.toFixed(2) : "",
                    ti === 0 ? a.ep.toFixed(2) : "",
                    ti === 0 && !a.qualified ? a.balance.toFixed(2) : "",
                    ti === 0 && a.qualified ? a.status : "");
                lines.push(row);
            });
        });

        const byChannel = c => data.agents.filter(a => a.channel === c);
        push("Company-wide", byChannel("Internal"));
        push("Company-wide", byChannel("Outsource"));
        (data.branches || []).forEach(branch => {
            ["Internal", "Outsource"].forEach(c => {
                push(`${branch} Branch`, data.agents.filter(a => a.channel === c && a.branch === branch));
            });
        });

        const csv = lines.map(r => r.map(v => {
            const s = String(v == null ? "" : v);
            return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
        }).join(",")).join("\r\n");

        const url = URL.createObjectURL(new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8;" }));
        const link = document.createElement("a");
        link.href = url;
        link.download = `sales_report_${data.year}_jan_${MONTH_ABBR[lm - 1].toLowerCase()}.csv`;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        URL.revokeObjectURL(url);
    }

    function setBusy(busy) {
        loader.classList.toggle("hidden", !busy);
    }

    // Matches the overview: a cold cache resolves itself within seconds of a
    // restart, so poll rather than stranding the user on a dead page.
    let retryTimer = null;
    const RETRY_MS = 5000;
    const MAX_RETRIES = 24;
    let retries = 0;

    async function loadReport(isRetry) {
        if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
        if (!isRetry) retries = 0;
        setBusy(true);
        emptyView.classList.add("hidden");
        try {
            const res = await fetch(`/api/sales-report?year=${encodeURIComponent(state.year)}`);
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.error || "Failed to load sales report");

            if (!data.ready) {
                state.data = null;
                cardsRow.innerHTML = "";
                sectionsBox.innerHTML = "";
                caveatBox.hidden = true;
                const keepTrying = retries < MAX_RETRIES;
                document.getElementById("srEmptyMsg").textContent = keepTrying
                    ? `${data.message || "Sales data is still being prepared."} Retrying…`
                    : (data.message || "Sales data is still being prepared.");
                emptyView.classList.remove("hidden");
                if (keepTrying) {
                    retries += 1;
                    retryTimer = setTimeout(() => loadReport(true), RETRY_MS);
                }
                return;
            }

            state.data = data;
            renderCards(data);
            renderCaveat(data);
            renderSections(data);

            document.getElementById("srPeriod").textContent =
                `${MONTH_NAMES[0]} – ${MONTH_NAMES[lastMonth(data) - 1]} ${data.year}`;
            document.getElementById("srSubHeader").textContent = data.scoped_to_agent
                ? `Cases and net sales — ${data.scoped_to_agent}`
                : "Cases and net sales per agent, with EGA qualification";
        } catch (err) {
            console.error("Sales report load failed:", err);
            document.getElementById("srEmptyMsg").textContent = err.message;
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
        yearSelect.addEventListener("change", () => {
            state.year = yearSelect.value;
            loadReport();
        });

        document.getElementById("srExport").addEventListener("click", exportCsv);

        const logoutBtn = document.getElementById("logoutBtn");
        if (logoutBtn) {
            logoutBtn.addEventListener("click", async () => {
                await fetch("/logout", { method: "POST" });
                window.location.href = "/login";
            });
        }

        initAccountBar();
        loadReport();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
