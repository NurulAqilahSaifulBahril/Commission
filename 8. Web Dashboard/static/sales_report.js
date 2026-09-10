(function () {
    "use strict";

    const MONTH_ABBR = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
        "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
    const MONTH_NAMES = ["January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December"];

    // `through` is the last month the report covers; null means "whatever
    // the ledger reaches", which is what the server picks on its own.
    const state = { year: "2026", through: null, data: null, search: "" };

    const yearSelect = document.getElementById("yearSelect");
    const periodSelect = document.getElementById("srPeriodSelect");
    const filterPanel = document.getElementById("srFilterPanel");
    const searchInput = document.getElementById("srAgentSearch");
    const clearSearchBtn = document.getElementById("srClearSearch");
    const matchNote = document.getElementById("srMatchNote");
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

    /** Comparable form of a name: lower case, punctuation and repeated spaces
     *  flattened. Lets "pua yee" find "ELYN PUA YEE LING" and keeps a stray
     *  double space or a "@" in a name from hiding a match. */
    function normalizeName(value) {
        return String(value == null ? "" : value)
            .toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
    }

    /** The agents the search box currently admits, in report order. An empty
     *  box admits everyone, so every caller can read from this one list rather
     *  than deciding for itself whether a filter is active. */
    function visibleAgents(data) {
        const q = normalizeName(state.search);
        if (!q) return data.agents;
        return data.agents.filter(a => normalizeName(a.agent).includes(q));
    }

    /** "1 agent" / "3 agents" -- filtering to a single person makes the
     *  bare plural show up constantly. */
    function plural(count, noun) {
        return `${count} ${noun}${count === 1 ? "" : "s"}`;
    }

    /** "600K" / "1.56M" — the headers name the bar, so they need the figure
     *  short enough to sit in a column heading. */
    function fmtThreshold(value) {
        const n = Number(value) || 0;
        if (n >= 1e6) return `${(n / 1e6).toFixed(2).replace(/\.?0+$/, "")}M`;
        if (n >= 1e3) return `${Math.round(n / 1e3)}K`;
        return fmtRM(n);
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
        // Computed from the visible agents rather than read from data.totals,
        // so the cards and the tables can never disagree while a search is on.
        // With an empty search box the two are the same sum.
        const shown = visibleAgents(data);
        const t = {
            cases: shown.reduce((s, a) => s + a.acc_noc, 0),
            sales: shown.reduce((s, a) => s + a.acc_ans, 0),
            agents: shown.length,
        };
        const internal = shown.filter(a => a.channel === "Internal");
        const outsource = shown.filter(a => a.channel === "Outsource");
        const qualified = shown.filter(a => a.qualified).length;
        cardsRow.innerHTML = [
            makeCard("Total Cases", String(t.cases), plural(t.agents, "agent")),
            makeCard("Total Net Sales", fmtCompact(t.sales), "net of EPP interest"),
            makeCard("Internal", fmtCompact(internal.reduce((s, a) => s + a.acc_ans, 0)),
                `${plural(internal.length, "agent")} · ${plural(internal.reduce((s, a) => s + a.acc_noc, 0), "case")}`),
            makeCard("Outsource", fmtCompact(outsource.reduce((s, a) => s + a.acc_ans, 0)),
                `${plural(outsource.length, "agent")} · ${plural(outsource.reduce((s, a) => s + a.acc_noc, 0), "case")}`),
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
            `<strong>How to read this against the printed report.</strong>
             These figures come from the live database, which is the authority. The printed
             report is a snapshot and will not agree with it: at June only the agents with no
             activity since reproduce it exactly, and the rest move in both directions as
             invoices are raised, edited, cancelled and paid.
             <b>RES / COM</b> — the property classifier returned Residential for ${res} of
             ${total} invoices (others: ${escapeHtml(others)}). It falls back to Residential
             whenever SEDA nem type, referral project type and the package fields are all
             silent, so most agents show a single RES line where the print splits them.
             <b>EP point</b> — 1 point per RM1 of sales, plus the campaign bonus: every third
             case invoiced inside the 1/5–10/6 window adds half a point per ringgit of that
             window's sales, so 3 cases give 0.5&times; and 9 give 1.5&times;. Under three cases
             earns nothing and the cell is left blank.
             <b>Case vintage</b> — the print splits those cases into before 31/12/25 and after
             1/1/26, and appears to pay the newer ones double. Nothing in the database identifies
             a case's vintage, so every case is counted on the before side and the after column
             stays empty. An agent whose cases are really the newer kind is understated.
             <b>Unpaid invoices</b> — an invoice counts only once a first payment is recorded.
             That keeps 608 of this year's 3,472 invoices in scope, so sales here are well below
             everything invoiced.`;
    }

    // The visible range grows month by month as the calendar moves past June
    // (see app.py:_sales_report_last_month); the EGA campaign split always
    // stays on the fixed split month (June) regardless of how far it extends.
    function lastMonth(data) { return data.last_month || 6; }
    function splitMonth(data) { return data.split_month || 6; }

    /** What the campaign column covers for the period on screen. The window
     *  is fixed at 1 May - 10 June, so a shorter period covers only part of it
     *  and one ending before May covers none. */
    function campaignLabel(data) {
        return data.campaign_label
            || `1/5 – ${data.split_day || 10}/${splitMonth(data)}`;
    }

    /** Whether the May multiplier columns belong on screen at all. The window
     *  opens on 1 May, so a period ending in April has nothing to put in them
     *  and four dashed columns would only take up room. */
    /** Whether the May multiplier columns belong on screen at all.
     *
     *  Two ways they do not. The period can end before the window opens on
     *  1 May, so there is nothing to show. Or it can run into July or beyond,
     *  where no multiplier is applied: it is a first-half device and an EGA
     *  one, so from a July period onwards the EP on screen is plain sales
     *  points and these columns would describe something no longer counted. */
    /** Whether June is split into 1-10 and 11-30.
     *
     *  That split exists only to measure the campaign window. With no
     *  multiplier applied -- any period running into July or beyond -- there is
     *  nothing to measure, so June reads as an ordinary month with one NOC and
     *  one ANS like every other. */
    function splitJune(data) {
        return data.multiplier_in_period !== false;
    }

    function showCampaign(data) {
        return data.campaign_in_period !== false
            && data.multiplier_in_period !== false;
    }

    /** One award column at a time, whichever the period is still deciding.
     *  EGA is settled on EP as at 30 June, so a period reaching July can no
     *  longer move it; ESA is what those later months are still playing for. */
    function showEga(data) {
        return data.show_ega !== undefined
            ? data.show_ega : lastMonth(data) <= splitMonth(data);
    }

    function showEsa(data) {
        return data.show_esa !== undefined
            ? data.show_esa : lastMonth(data) > splitMonth(data);
    }

    function headerFor(data, channel) {
        const lm = lastMonth(data);
        const sm = splitMonth(data);
        // The print carries both channels' bars in one heading, "600K/720K",
        // and the same heading sits over the Internal and Outsource tables. We
        // follow it rather than naming only this block's own bar, so the two
        // read the same side by side.
        const bars = (bag) => `${fmtThreshold((bag || {}).Internal)}/${fmtThreshold((bag || {}).Outsource)}`;
        const egaBar = bars(data.targets);
        const esaBar = bars(data.esa_targets);
        const monthCols = [];
        const subCols = [];
        for (let m = 1; m <= lm; m++) {
            if (m === sm && splitJune(data)) {
                monthCols.push(`<th colspan="3">${MONTH_ABBR[m - 1]}</th>`);
                subCols.push(`<th>NOC</th><th>1-${data.split_day || 10} ${MONTH_ABBR[m - 1]}</th>`
                    + `<th>${(data.split_day || 10) + 1}-30 ${MONTH_ABBR[m - 1]}</th>`);
            } else {
                monthCols.push(`<th colspan="2">${MONTH_ABBR[m - 1]}</th>`);
                subCols.push("<th>NOC</th><th>ANS</th>");
            }
        }
        return `<thead>
            <tr>
                <th rowspan="2" class="sr-no">NO</th>
                <th rowspan="2" class="sr-name">NAME</th>
                <th rowspan="2">TYPE</th>
                ${monthCols.join("")}
                <th colspan="2">TOTAL</th>
                <th colspan="2">ACC</th>
                ${showCampaign(data) ? `<th colspan="4" title="Cases invoiced in the ${escapeHtml(campaignLabel(data))} window, and those sales with the multiplier applied. Every third case adds another half point per ringgit, so 3 cases pay 1.5x the window's sales and 6 pay 2x. The after-1/1/26 pair is empty because nothing available identifies a case's vintage.">MAY MULTIPLIER POINT ${escapeHtml(campaignLabel(data))}</th>` : ""}
                <th rowspan="2" title="Cash received against this agent's invoices in the period shown. Sales are counted when invoiced, so this is how much of that has actually been paid.">COLLECTED<br>PAYMENT</th>
                <th rowspan="2">UP TO DATE<br>EP POINT</th>
                ${showEga(data) ? `<th rowspan="2" title="Balance to qualify for the EGA Hanoi trip. EGA closes at the end of June, so this is measured on EP as at 30 June and cannot be reached on later sales.">EGA - HANOI<br>Balance to Qualify<br>(${egaBar} EP Point Till 30Jun)</th>` : ""}
                ${showEsa(data) ? `<th rowspan="2" title="Balance to qualify for the ESA Chongqing trip. ESA runs to the end of December, so this is measured on the running EP.">ESA - CHONGQING<br>Balance to Qualify<br>(${esaBar} EP Point Till 31Dec)</th>` : ""}
            </tr>
            <tr>
                ${subCols.join("")}
                <th>NOC</th><th>ANS</th>
                <th>NOC</th><th>ANS</th>
                ${showCampaign(data) ? `<th>CASES</th><th>BEFORE<br>31/12/25</th>
                <th>CASES</th><th>AFTER 1/1/26</th>` : ""}
            </tr>
        </thead>`;
    }

    /** Case lists behind the NOC hovers, indexed so a cell can carry a number
     *  rather than a payload of its own. Rebuilt on every render. */
    const caseLists = [];

    /** Registers one NOC cell's cases and returns the attributes that let the
     *  hover panel find them again. An empty list registers nothing, so the
     *  cell stays inert instead of opening an empty panel. */
    function caseAttrs(cases, label) {
        const all = (cases || []).filter(Boolean);
        if (!all.length) return "";
        caseLists.push({ label, cases: all });
        // No class here: the cell template already carries one, and a second
        // class attribute is dropped by the parser. The selector below keys
        // off the data attribute instead.
        return ` data-cases="${caseLists.length - 1}"`;
    }

    /** The hover panel itself: one table of every payment behind the cases in
     *  the cell, newest first. A browser tooltip cannot draw a table and wraps
     *  long text into an unreadable block, so this is a panel of our own. */
    function caseTable(entry) {
        const rows = [];
        // The panel cannot be scrolled -- it ignores the mouse so it never
        // swallows a hover -- so a long month is trimmed rather than running
        // off the bottom where nobody can reach it.
        const shown = entry.cases.slice(0, 12);
        const more = entry.cases.length - shown.length;
        shown.forEach(c => {
            const pays = c.p || [];
            const span = Math.max(1, pays.length);
            const name = `<td class="sr-tip-cust" rowspan="${span}">${escapeHtml(c.n)}</td>`;
            if (!pays.length) {
                rows.push(`<tr>${name}<td colspan="3" class="sr-tip-none">`
                    + `no payment recorded in this period</td></tr>`);
                return;
            }
            pays.forEach((p, i) => {
                rows.push(`<tr>${i === 0 ? name : ""}`
                    + `<td class="numeric">${fmtRM(p.a)}</td>`
                    + `<td>${escapeHtml(p.d || "—")}</td>`
                    + `<td>${escapeHtml(p.m || "—")}</td></tr>`);
            });
        });
        if (more) {
            rows.push(`<tr><td colspan="4" class="sr-tip-none">`
                + `…and ${plural(more, "more case")}</td></tr>`);
        }
        return `<div class="sr-tip-head">${escapeHtml(entry.label)} —
                ${plural(entry.cases.length, "case")}</div>
            <table class="sr-tip-table">
                <thead><tr><th>Customer</th><th>Amount</th>
                    <th>Paid</th><th>Entered</th></tr></thead>
                <tbody>${rows.join("")}</tbody>
            </table>`;
    }

    /** Shows the panel beside the cell, flipped left or up when it would run
     *  off the screen. The table is wide, so the naive "always below right"
     *  placement puts the December columns' panels out of view. */
    function showCaseTip(cell) {
        const entry = caseLists[Number(cell.dataset.cases)];
        if (!entry) return;
        const tip = document.getElementById("srCaseTip");
        tip.innerHTML = caseTable(entry);
        tip.hidden = false;
        // Measured against documentElement rather than window.innerWidth: the
        // latter reads 0 in an embedded preview, which sent the clamp the wrong
        // way and pushed wide panels off the right edge. offsetWidth forces the
        // layout, so the size is the one this content actually takes.
        const box = cell.getBoundingClientRect();
        const w = tip.offsetWidth;
        const h = tip.offsetHeight;
        // A hidden or embedded frame can report a zero-sized viewport. Clamping
        // against that would throw the panel off screen, so fall back to
        // placing it at the cell and leave the clamping to a real layout.
        const vw = document.documentElement.clientWidth || 0;
        const vh = document.documentElement.clientHeight || 0;
        const pad = 8;
        let left = box.left;
        if (vw && left + w > vw - pad) left = vw - w - pad;
        if (left < pad) left = pad;
        let top = box.bottom + 6;
        if (vh && top + h > vh - pad) top = box.top - h - 6;
        if (top < pad) top = pad;
        tip.style.left = `${Math.round(left)}px`;
        tip.style.top = `${Math.round(top)}px`;
    }

    function hideCaseTip() {
        const tip = document.getElementById("srCaseTip");
        if (tip) tip.hidden = true;
    }

    function agentRows(agent, no, data) {
        const lm = lastMonth(data);
        const sm = splitMonth(data);
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
            for (let m = 1; m <= lm; m++) {
                const cell = t.months[m - 1] || { noc: 0, ans: 0 };
                if (m === sm && splitJune(data)) {
                    cells.push(`<td class="numeric"${caseAttrs(cell.custs, MONTH_ABBR[m - 1])}>${cell.noc}</td>`,
                        `<td class="numeric">${fmtRM(t.jun_early)}</td>`,
                        `<td class="numeric">${fmtRM(t.jun_late)}</td>`);
                } else {
                    cells.push(`<td class="numeric"${caseAttrs(cell.custs, MONTH_ABBR[m - 1])}>${cell.noc}</td>`,
                        `<td class="numeric">${fmtRM(cell.ans)}</td>`);
                }
            }
            const tier = Number(agent.bonus_tier) || 0;
            // The campaign opens on 1 May. A period that stops before then
            // earns no bonus at all, so both cells say so rather than printing
            // a case count beside a zero and letting it read as an earning.
            const noCampaign = data.campaign_in_period === false;
            // The print carries one figure here, the window's sales with the
            // multiplier already in them, which is what feeds EP. The raw
            // window sales are still on the row, in the May and 1-10 June
            // month columns, so nothing is lost by showing the multiplied one.
            const campaignPaid = (Number(agent.campaign_ans) || 0)
                + (Number(agent.campaign_bonus) || 0);
            const campaignTitle = noCampaign
                ? "The period on screen ends before the campaign opens on 1 May."
                : (tier
                    ? `${fmtRM(agent.campaign_ans)} of window sales, ${agent.campaign_cases} cases`
                      + ` giving a ${tier.toFixed(1)}x multiplier worth ${fmtRM(agent.campaign_bonus)}`
                    : `${fmtRM(agent.campaign_ans)} of window sales; under three cases, so no multiplier`);
            // Blank unless a multiplier is actually earned. Under three cases
            // there is nothing to multiply, and the window's own sales are
            // already on the row in the May and 1-10 June columns, so printing
            // them again here would read as an award that was not made. The
            // printed report leaves the cell at zero for the same reason.
            // The print splits these cases into two vintages and appears to pay
            // the newer one double. Nothing in the data identifies a case's
            // vintage -- it is not the customer's history, since every campaign
            // case here is a first-time customer -- so all of them sit on the
            // older side and this pair stays empty rather than guessing.
            const vintageTitle = "Not derivable yet: no field identifies a case's vintage, "
                + "so every campaign case is counted on the before-31/12/25 side.";
            const afterCell = `<span class="sr-muted">—</span>`;
            const campaignCell = (noCampaign || !tier)
                ? `<span class="sr-muted">—</span>`
                : fmtRM(campaignPaid);
            // Sales are counted when invoiced, so the share actually paid is
            // worth showing rather than making a reader divide two columns.
            const collected = Number(agent.collected) || 0;
            const pctPaid = agent.acc_ans ? Math.round(100 * collected / agent.acc_ans) : 0;
            const collectedCell = collected
                ? fmtRM(collected)
                : `<span class="sr-muted">—</span>`;
            const collectedTitle = agent.acc_ans
                ? `${fmtRM(collected)} received of ${fmtRM(agent.acc_ans)} invoiced, ${pctPaid}%`
                : "Nothing invoiced in this period";
            const epTitle = tier
                ? `${fmtRM(agent.base_ep)} sales + ${tier.toFixed(1)}x campaign bonus ${fmtRM(agent.campaign_bonus)}`
                : `${fmtRM(agent.base_ep)} sales, no campaign bonus`;
            cells.push(`<td class="numeric">${t.total_noc}</td>`,
                `<td class="numeric">${fmtRM(t.total_ans)}</td>`,
                // ACC merges RES and COM, so it belongs on the agent's last line
                `<td class="numeric">${last ? agent.acc_noc : ""}</td>`,
                `<td class="numeric sr-strong">${last ? fmtRM(agent.acc_ans) : ""}</td>`);
            if (showCampaign(data)) {
                cells.push(
                    `<td class="numeric"${first ? caseAttrs(agent.campaign_custs, "Campaign window") : ""} title="${escapeHtml(campaignTitle)}">${first ? agent.campaign_cases : ""}</td>`,
                    `<td class="numeric" title="${escapeHtml(campaignTitle)}">${first ? campaignCell : ""}</td>`,
                    `<td class="numeric" title="${escapeHtml(vintageTitle)}">${first ? afterCell : ""}</td>`,
                    `<td class="numeric" title="${escapeHtml(vintageTitle)}">${first ? afterCell : ""}</td>`);
            }
            cells.push(
                `<td class="numeric" title="${escapeHtml(collectedTitle)}">${first ? collectedCell : ""}</td>`,
                `<td class="numeric sr-strong" title="${escapeHtml(epTitle)}">${first ? fmtRM(agent.ep) : ""}</td>`);
            const awards = (showEga(data) ? 1 : 0) + (showEsa(data) ? 1 : 0);
            if (!first) {
                for (let k = 0; k < awards; k++) cells.push("<td></td>");
            } else {
                // Each award cell says one of two things: the label when the
                // bar is cleared, or how much EP is still missing. Same shape
                // the printed report uses.
                if (showEga(data)) {
                    const egaTitle = `EP as at 30 June ${fmtRM(agent.ep_jun)} against ${fmtRM(agent.ega_threshold)}`;
                    if (agent.ega_double) {
                        // Deliberately spelled out: the printed report shows
                        // this label on one agent only, whose figures satisfy
                        // both "twice the EGA bar" and "won EGA and ESA", so
                        // the print cannot tell the two readings apart. We take
                        // the first, and say so rather than leaving a reader to
                        // count one agent's win twice.
                        const doubleTitle = `${egaTitle} — at least twice the bar, so two trip tickets.`
                            + ` This is about EGA alone; whether they also won ESA is a separate question.`;
                        cells.push(`<td class="sr-qualified" title="${escapeHtml(doubleTitle)}">DOUBLE TICKET</td>`);
                    } else if (agent.qualified) {
                        cells.push(`<td class="sr-qualified" title="${escapeHtml(egaTitle)}">${escapeHtml(agent.status || "Qualifier")}</td>`);
                    } else {
                        cells.push(`<td class="numeric sr-short" title="${escapeHtml(egaTitle)}">${fmtRM(agent.balance)}</td>`);
                    }
                }
                if (showEsa(data)) {
                    // ESA is judged on plain sales points. The May multiplier is
                    // an EGA device and never counts toward it, whatever the
                    // period.
                    const esaEp = agent.esa_ep != null ? agent.esa_ep : agent.ep;
                    const esaTitle = `EP to date ${fmtRM(esaEp)} against `
                        + `${fmtRM(agent.esa_threshold)}, excluding the May multiplier`;
                    if (agent.esa_qualified) {
                        cells.push(`<td class="sr-qualified" title="${escapeHtml(esaTitle)}">${escapeHtml(agent.esa_status || "Qualifier")}</td>`);
                    } else {
                        cells.push(`<td class="numeric sr-short" title="${escapeHtml(esaTitle)}">${fmtRM(agent.esa_balance)}</td>`);
                    }
                }
            }
            return `<tr>${cells.join("")}</tr>`;
        }).join("");
    }

    function subtotalRow(members, data) {
        const lm = lastMonth(data);
        const sm = splitMonth(data);
        const cells = ['<td class="sr-no"></td>', '<td class="sr-name">SUBTOTAL</td>', "<td></td>"];
        const monthAns = m => members.reduce((sum, a) =>
            sum + a.types.reduce((s, t) => s + ((t.months[m] || {}).ans || 0), 0), 0);
        const monthNoc = m => members.reduce((sum, a) =>
            sum + a.types.reduce((s, t) => s + ((t.months[m] || {}).noc || 0), 0), 0);
        const sum = fn => members.reduce((s, a) => s + fn(a), 0);
        for (let m = 1; m <= lm; m++) {
            if (m === sm && splitJune(data)) {
                cells.push(`<td class="numeric">${monthNoc(m - 1)}</td>`,
                    `<td class="numeric">${fmtRM(sum(a => a.types.reduce((s, t) => s + t.jun_early, 0)))}</td>`,
                    `<td class="numeric">${fmtRM(sum(a => a.types.reduce((s, t) => s + t.jun_late, 0)))}</td>`);
            } else {
                cells.push(`<td class="numeric">${monthNoc(m - 1)}</td>`,
                    `<td class="numeric">${fmtRM(monthAns(m - 1))}</td>`);
            }
        }
        cells.push(`<td class="numeric">${sum(a => a.acc_noc)}</td>`,
            `<td class="numeric">${fmtRM(sum(a => a.acc_ans))}</td>`,
            `<td class="numeric">${sum(a => a.acc_noc)}</td>`,
            `<td class="numeric sr-strong">${fmtRM(sum(a => a.acc_ans))}</td>`);
        if (showCampaign(data)) {
            cells.push(`<td class="numeric">${sum(a => a.campaign_cases)}</td>`,
                `<td class="numeric">${fmtRM(sum(a => (Number(a.bonus_tier) || 0)
                    ? (a.campaign_ans || 0) + (a.campaign_bonus || 0) : 0))}</td>`,
                `<td class="numeric sr-muted">—</td>`,
                `<td class="numeric sr-muted">—</td>`);
        }
        // Collected, EP, then one blank per award column the header carries.
        cells.push(`<td class="numeric">${fmtRM(sum(a => a.collected || 0))}</td>`,
            "<td></td>");
        if (showEga(data)) cells.push("<td></td>");
        if (showEsa(data)) cells.push("<td></td>");
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
                <span class="row-count-badge">${plural(members.length, "agent")} ·
                    target RM ${fmtRM(target)} EP</span>
            </div>
            <div class="table-container">
                <table class="dashboard-table sr-table">
                    ${headerFor(data, members[0].channel)}
                    <tbody>${body}</tbody>
                </table>
            </div>
        </div>`;
    }

    function renderSections(data) {
        // The index the cells point into is rebuilt with the cells themselves,
        // so a stale entry can never outlive the row it described.
        caseLists.length = 0;
        hideCaseTip();
        const shown = visibleAgents(data);
        const pick = (channel, branch) => shown.filter(a =>
            a.channel === channel && (branch === null || a.branch === branch));
        const chunks = [];

        if (!shown.length) {
            sectionsBox.innerHTML = `<div class="card" style="padding:28px; text-align:center;">
                <p style="margin:0; color:var(--text-muted);">
                    No agent matches <strong>${escapeHtml(state.search)}</strong>.
                </p></div>`;
            return;
        }

        // The month columns are headed with two bare abbreviations that repeat
        // across the whole table, so say once what they hold rather than
        // leaving every reader to infer it.
        chunks.push(`<p class="sr-legend">
            <strong>NOC</strong> is the number of cases.
            <strong>ANS</strong> is net sales in RM, after EPP interest is taken out.
        </p>`);

        // Company-wide takes every visible agent, so it always has something
        // under it by the time we get past the no-matches return above.
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
        const sm = splitMonth(data);
        const head = ["SECTION", "CHANNEL", "NO", "NAME", "TYPE"];
        for (let m = 1; m <= lm; m++) {
            if (m === sm && splitJune(data)) {
                head.push(`${MONTH_ABBR[m - 1]} NOC`, `${MONTH_ABBR[m - 1]} 1-${data.split_day}`,
                    `${MONTH_ABBR[m - 1]} ${data.split_day + 1}-30`);
            } else {
                head.push(`${MONTH_ABBR[m - 1]} NOC`, `${MONTH_ABBR[m - 1]} ANS`);
            }
        }
        head.push("TOTAL NOC", "TOTAL ANS",
            "ACC NOC", "ACC ANS", "CAMPAIGN CASES", "CAMPAIGN ANS",
            "MULTIPLIER RATE", "CAMPAIGN BONUS", "SALES EP",
            "COLLECTED PAYMENT",
            "UP TO DATE EP POINT", "EP AT 30 JUN",
            "EGA THRESHOLD", "EGA BALANCE", "EGA STATUS",
            "ESA THRESHOLD", "ESA BALANCE", "ESA STATUS");

        const lines = [head];
        const push = (section, members) => members.forEach((a, i) => {
            a.types.forEach((t, ti) => {
                const row = [section, a.channel, ti === 0 ? i + 1 : "", ti === 0 ? a.agent : "", t.type];
                for (let m = 1; m <= lm; m++) {
                    const c = t.months[m - 1] || { noc: 0, ans: 0 };
                    if (m === sm && splitJune(data)) {
                        row.push(c.noc, t.jun_early.toFixed(2), t.jun_late.toFixed(2));
                    } else {
                        row.push(c.noc, c.ans.toFixed(2));
                    }
                }
                const last = ti === a.types.length - 1;
                row.push(t.total_noc, t.total_ans.toFixed(2),
                    last ? a.acc_noc : "", last ? a.acc_ans.toFixed(2) : "",
                    ti === 0 ? a.campaign_cases : "", ti === 0 ? a.campaign_ans.toFixed(2) : "",
                    ti === 0 ? (Number(a.bonus_tier) || 0).toFixed(1) : "",
                    ti === 0 ? (a.campaign_bonus || 0).toFixed(2) : "",
                    ti === 0 ? (a.base_ep || 0).toFixed(2) : "",
                    ti === 0 ? (a.collected || 0).toFixed(2) : "",
                    ti === 0 ? a.ep.toFixed(2) : "",
                    ti === 0 ? (a.ep_jun || 0).toFixed(2) : "",
                    ti === 0 ? (a.ega_threshold || 0).toFixed(2) : "",
                    ti === 0 && !a.qualified ? a.balance.toFixed(2) : "",
                    ti === 0 ? (a.ega_double ? "DOUBLE TICKET"
                        : (a.qualified ? (a.status || "Qualifier") : "")) : "",
                    ti === 0 ? (a.esa_threshold || 0).toFixed(2) : "",
                    ti === 0 && !a.esa_qualified ? (a.esa_balance || 0).toFixed(2) : "",
                    ti === 0 && a.esa_qualified ? (a.esa_status || "Qualifier") : "");
                lines.push(row);
            });
        });

        // Exports what is on screen. Filtering to one agent and then getting
        // the whole company back in the file would be the surprising result.
        const shown = visibleAgents(data);
        const byChannel = c => shown.filter(a => a.channel === c);
        push("Company-wide", byChannel("Internal"));
        push("Company-wide", byChannel("Outsource"));
        (data.branches || []).forEach(branch => {
            ["Internal", "Outsource"].forEach(c => {
                push(`${branch} Branch`, shown.filter(a => a.channel === c && a.branch === branch));
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

    /** Downloads the same report as a PDF. The file is built server-side from
     *  the same payload the page renders, so the two cannot drift; the agent
     *  search travels with it for the same reason the CSV honours it. */
    async function exportPdf() {
        const btn = document.getElementById("srExportPdf");
        if (!state.data || (btn && btn.disabled)) return;
        const label = btn ? btn.innerHTML : "";
        if (btn) {
            btn.disabled = true;
            btn.innerHTML = '<span class="icon">⏳</span> Building PDF…';
        }
        try {
            const url = `/api/sales-report/pdf?year=${encodeURIComponent(state.year)}`
                + `&month=${encodeURIComponent(state.through || "")}`
                + `&agent=${encodeURIComponent(state.search || "")}`;
            const res = await fetch(url);
            if (!res.ok) {
                const body = await res.json().catch(() => ({}));
                alert(body.message || body.error || "Failed to build the PDF.");
                return;
            }
            const blob = await res.blob();
            const objectUrl = URL.createObjectURL(blob);
            const link = document.createElement("a");
            link.href = objectUrl;
            link.download = `sales_report_${state.data.year}_jan_`
                + `${MONTH_ABBR[lastMonth(state.data) - 1].toLowerCase()}.pdf`;
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
            URL.revokeObjectURL(objectUrl);
        } catch (err) {
            console.error("Failed to download the sales report PDF:", err);
            alert("Failed to download the PDF: " + err.message);
        } finally {
            if (btn) {
                btn.disabled = false;
                btn.innerHTML = label;
            }
        }
    }

    /** Fills the period picker from the payload and marks the month on
     *  screen. Rebuilt on every load so a year with a different ledger length
     *  never leaves a month behind that has no data. */
    function syncPeriodSelect(data) {
        if (!periodSelect) return;
        const max = Number(data.max_month) || lastMonth(data);
        const options = [];
        for (let m = 1; m <= max; m++) {
            // The month alone, not the range. The report always starts in
            // January, so naming both ends here only repeats the header above
            // the table; the select's tooltip says what the month means.
            options.push(`<option value="${m}">${MONTH_NAMES[m - 1]}</option>`);
        }
        periodSelect.innerHTML = options.join("");
        periodSelect.value = String(lastMonth(data));
        state.through = Number(periodSelect.value);
    }

    function setBusy(busy) {
        loader.classList.toggle("hidden", !busy);
    }

    /** Re-render everything the search box affects. */
    function applySearch() {
        const data = state.data;
        if (clearSearchBtn) clearSearchBtn.hidden = !state.search;
        if (!data) {
            if (matchNote) matchNote.textContent = "";
            return;
        }
        renderCards(data);
        renderSections(data);
        if (matchNote) {
            const shown = visibleAgents(data).length;
            matchNote.textContent = state.search
                ? `Showing ${shown} of ${data.agents.length} agents`
                : "";
        }
    }

    // Matches the overview: a cold cache resolves itself within seconds of a
    // restart, so poll rather than stranding the user on a dead page.
    let retryTimer = null;
    const RETRY_MS = 5000;
    const MAX_RETRIES = 24;
    let retries = 0;

    // Each load takes a ticket. Changing the month again before the first
    // answer arrives makes that answer stale, and rendering it would leave the
    // wrong period on screen under the right label in the picker.
    let loadSeq = 0;

    async function loadReport(isRetry) {
        if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
        if (!isRetry) retries = 0;
        const seq = ++loadSeq;
        setBusy(true);
        emptyView.classList.add("hidden");
        try {
            const res = await fetch(`/api/sales-report?year=${encodeURIComponent(state.year)}`
                + `&month=${encodeURIComponent(state.through || "")}`);
            const data = await res.json().catch(() => ({}));
            if (seq !== loadSeq) return;      // overtaken; a newer period wins
            if (!res.ok) throw new Error(data.error || "Failed to load sales report");

            if (!data.ready) {
                state.data = null;
                cardsRow.innerHTML = "";
                sectionsBox.innerHTML = "";
                caveatBox.hidden = true;
                if (filterPanel) filterPanel.hidden = true;
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
            syncPeriodSelect(data);
            renderCaveat(data);
            if (filterPanel) filterPanel.hidden = false;
            // Draws the cards and the tables, honouring a search the user
            // typed before this reload finished.
            applySearch();

            document.getElementById("srPeriod").textContent =
                `${MONTH_NAMES[0]} – ${MONTH_NAMES[lastMonth(data) - 1]} ${data.year}`;
            document.getElementById("srSubHeader").textContent = data.scoped_to_agent
                ? `Cases and net sales — ${data.scoped_to_agent}`
                : "Cases and net sales per agent, with EGA qualification";
        } catch (err) {
            if (seq !== loadSeq) return;
            console.error("Sales report load failed:", err);
            document.getElementById("srEmptyMsg").textContent = err.message;
            emptyView.classList.remove("hidden");
        } finally {
            // The spinner belongs to the newest request, so an overtaken one
            // must not clear it while that request is still in flight.
            if (seq === loadSeq) setBusy(false);
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
        // Delegated, because the rows are replaced on every search keystroke
        // and per-cell listeners would have to be rebound each time.
        sectionsBox.addEventListener("mouseover", (e) => {
            const cell = e.target.closest("td[data-cases]");
            if (cell) showCaseTip(cell);
        });
        sectionsBox.addEventListener("mouseout", (e) => {
            const cell = e.target.closest("td[data-cases]");
            if (cell && !cell.contains(e.relatedTarget)) hideCaseTip();
        });
        // A panel anchored to a cell that has moved is worse than no panel.
        window.addEventListener("scroll", hideCaseTip, true);
        window.addEventListener("resize", hideCaseTip);

        state.year = yearSelect.value;
        yearSelect.addEventListener("change", () => {
            state.year = yearSelect.value;
            state.through = null;
            loadReport();
        });

        if (periodSelect) {
            periodSelect.addEventListener("change", () => {
                state.through = Number(periodSelect.value) || null;
                loadReport();
            });
        }

        document.getElementById("srExport").addEventListener("click", exportCsv);
        const pdfBtn = document.getElementById("srExportPdf");
        if (pdfBtn) pdfBtn.addEventListener("click", exportPdf);

        if (searchInput) {
            searchInput.addEventListener("input", () => {
                state.search = searchInput.value.trim();
                applySearch();
            });
            // Escape clears from the keyboard, matching the Clear button.
            searchInput.addEventListener("keydown", (e) => {
                if (e.key !== "Escape" || !searchInput.value) return;
                searchInput.value = "";
                state.search = "";
                applySearch();
            });
        }
        if (clearSearchBtn) {
            clearSearchBtn.addEventListener("click", () => {
                if (searchInput) searchInput.value = "";
                state.search = "";
                applySearch();
                if (searchInput) searchInput.focus();
            });
        }

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
