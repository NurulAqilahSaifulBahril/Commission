(function () {
    "use strict";

    const MONTH_NAMES = ["January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December"];
    const MONTH_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

    /** Reads the Basic Commission "Search Month/Year" free-text box into the
     *  same {yVal, mVal} shape the old separate Year/Month dropdowns produced
     *  -- either half blank means "all". Accepts a year alone ("2026"), a
     *  month alone (name, abbreviation, or number: "July" / "Jul" / "07" /
     *  "7"), or both together in either order ("July 2026", "2026 July",
     *  "2026-07", "07/2026"). */
    function parseMonthYearSearch(text) {
        const raw = String(text || "").trim();
        if (!raw) return { yVal: "", mVal: "" };

        const combined = raw.match(/^(\d{4})[\s\-\/](\d{1,2})$/) || raw.match(/^(\d{1,2})[\s\-\/](\d{4})$/);
        if (combined) {
            const [a, b] = [combined[1], combined[2]];
            const year = a.length === 4 ? a : b;
            const monthNum = parseInt(a.length === 4 ? b : a, 10);
            return {
                yVal: year,
                mVal: (monthNum >= 1 && monthNum <= 12) ? String(monthNum).padStart(2, "0") : ""
            };
        }

        let yVal = "";
        let mVal = "";
        raw.split(/[\s,\/\-]+/).filter(Boolean).forEach((tok) => {
            const low = tok.toLowerCase();
            if (!yVal && /^\d{4}$/.test(tok)) {
                yVal = tok;
                return;
            }
            if (!mVal) {
                const byFull = MONTH_NAMES.findIndex((m) => m.toLowerCase() === low);
                if (byFull !== -1) { mVal = String(byFull + 1).padStart(2, "0"); return; }
                const byShort = MONTH_SHORT.findIndex((m) => m.toLowerCase() === low);
                if (byShort !== -1) { mVal = String(byShort + 1).padStart(2, "0"); return; }
                if (/^(0?[1-9]|1[0-2])$/.test(tok)) { mVal = String(parseInt(tok, 10)).padStart(2, "0"); return; }
            }
        });
        return { yVal, mVal };
    }
    // The current role names, most senior first. Retired labels ("Senior",
    // "Executive", "OSA/OSA1", "OGM", "Regional Sales Director") are absent on
    // purpose: rows already saved under them keep their stored label and keep
    // pricing exactly as before — rewriting a stored role would reprice months
    // that have already paid out — but nothing new is created with them. The
    // pickers still offer a row's own legacy label so opening and saving it
    // can't quietly blank the role.
    const RATE_ROLES = {
        "Internal": ["Senior Branch Director", "Branch Sales Manager",
            "Sales Development Manager", "Sales Team Manager",
            "Senior Sales Consultant", "Sales Consultant",
            "Sales Senior", "Sales Executive"],
        "Outsource": ["OUM", "OSA"]
    };
    // Retired labels that no current name covers, so a filter can still reach
    // the rows holding them. "Senior", "Executive" and "OSA/OSA1" are not
    // listed: canonRole() below already makes the current name match them, and
    // repeating both spellings would just double up the filter's options.
    const LEGACY_ROLES = {
        "Internal": ["Regional Sales Director"],
        "Outsource": ["OGM"]
    };
    // Only types the calculation engine actually reads are "wired". Entries of
    // other types are stored (and audited) but ignored until their engine is wired.
    // Rule and Formula are display-only and derive from the commission type —
    // they are never typed, so a typo can't change how money is calculated.
    const RATE_TYPES = [
        { key: "Basic Commission", wired: true, unit: "%",
          rule: "Payout per milestone rules",
          ruleLines: [
              "Pre-Jul invoices: full payout at 100% payment",
              "Jul 2026+ invoices: 1st payment ≥5% pays RM300 (once per invoice)",
              "Jul 2026+ invoices: payment ≥75% pays balance (total − RM300)",
              "Referral / Safwan / Gan Lai Soon: only at 100% payment"
          ],
          formula: "(a − b) × z" },
        { key: "Production Bonus Rate", wired: false, unit: "%",
          rule: "Monthly production target", formula: "per scheme" },
        { key: "Net Floor Price Rate", wired: true, unit: "%",
          rule: "Tiered vs NFP (i / ii / iii)",
          ruleLines: [
              "i. Sales Price > Net Floor Price → positive rate",
              "ii. System Price > Sales Price → 100%",
              "iii. Sales Price < Net Floor Price → negative rate"
          ],
          formula: "(c − d) × z" },
        { key: "ANP Commission", wired: false, unit: "%",
          rule: "Average Net Price scheme", formula: "per scheme" },
        { key: "EGA/ESA Award", wired: false, unit: "RM",
          rule: "Award qualification criteria", formula: "per scheme" },
        { key: "Monthly Contest", wired: false, unit: "RM",
          rule: "Contest qualification criteria", formula: "per scheme" }
    ];
    let isAdmin = false;
    const CURRENT_YM = (function () {
        const now = new Date();
        return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
    })();

    async function api(path, options) {
        const res = await fetch(path, options);
        if (res.status === 401) {
            window.location.href = "/login?next=/data";
            throw new Error("Not authenticated");
        }
        return res;
    }

    function escapeHtml(str) {
        return String(str == null ? "" : str)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
    }

    function fmtMonth(s) {
        if (!s) return "";
        const str = String(s).trim();
        if (str.includes(" to ")) {
            return str.split(" to ").map(fmtMonth).join(" to ");
        }
        const m = /^(\d{4})-(\d{2})$/.exec(str);
        if (!m) return str;
        const y = m[1];
        const mi = parseInt(m[2], 10) - 1;
        return mi >= 0 && mi < 12 ? MONTH_SHORT[mi] + " " + y : str;
    }

    // An effective month is either a single month ("2026-07", meaning from then
    // onwards) or a closed range ("2026-07 to 2026-08", meaning those months
    // only). Mirrors split_effective_range() in basic_commission_rates.py —
    // both sides must read the two spellings the same way.
    const OPEN_ENDED = "9999-12";

    function splitEffRange(eff) {
        const s = String(eff || "").trim();
        if (!s) return ["", ""];
        const sep = s.includes(" to ") ? " to " : (s.includes("..") ? ".." : null);
        if (!sep) return [s, OPEN_ENDED];
        const parts = s.split(sep);
        const start = parts[0].trim();
        const end = (parts[1] || "").trim() || OPEN_ENDED;
        return [start, end];
    }

    /** The month a row starts applying — what "latest row wins" sorts on.
     *  Comparing the raw cell ranks "2026-07 to 2026-08" above a plain
     *  "2026-07" purely because it is the longer string. */
    function effStart(eff) { return splitEffRange(eff)[0]; }
    function effEnd(eff) { return splitEffRange(eff)[1]; }

    /** Does this row's Invoice Date govern `ym` ("YYYY-MM")? Mirrors
     *  effective_covers() in basic_commission_rates.py, so filtering by a month
     *  shows exactly the rows the commission engines would price that month
     *  with — an open-ended "2026-01" still counts for every later month.
     *  A row with no date covers nothing, so it drops out of a month filter;
     *  the needs-review banner still counts it, since that reads the unfiltered
     *  list. */
    function effectiveCoversMonth(eff, ym) {
        const [start, end] = splitEffRange(eff);
        if (!start || !ym) return false;
        return start <= ym && ym <= end;
    }

    /** "2026-12" -> "2027-01" */
    function nextYm(ym) {
        const m = /^(\d{4})-(\d{2})$/.exec(String(ym || "").trim());
        if (!m) return ym;
        let y = parseInt(m[1], 10);
        let mo = parseInt(m[2], 10) + 1;
        if (mo > 12) { mo = 1; y += 1; }
        return `${y}-${String(mo).padStart(2, "0")}`;
    }

    function typeInfo(typeValue) {
        const t = RATE_TYPES.find((t) => t.key === typeValue) || {};
        return { isRule: false, wired: !!t.wired, unit: t.unit || "%",
                 rule: t.rule || "", ruleFull: t.ruleFull || "",
                 ruleLines: t.ruleLines || (t.rule ? [t.rule] : []),
                 formula: t.formula || "" };
    }

    function ruleLinesHtml(lines) {
        if (!lines || !lines.length) return `<div class="rule-lines"><div>n/a</div></div>`;
        return `<div class="rule-lines">${lines.map((l) => `<div>• ${escapeHtml(l)}</div>`).join("")}</div>`;
    }

    function formatConditionHtml(condStr) {
        if (!condStr) return `<span style="color:var(--text-muted);">—</span>`;
        const lines = String(condStr).split("\n").map((s) => s.trim()).filter(Boolean);
        if (!lines.length) return `<span style="color:var(--text-muted);">—</span>`;
        if (lines.length === 1) return `<span>${escapeHtml(lines[0])}</span>`;
        return `<div class="rule-cell" style="display:flex; flex-direction:column; gap:2px; padding:2px 0;">${lines.map((l) => `<div>${escapeHtml(l)}</div>`).join("")}</div>`;
    }

    // ── Editable rule bullets (live inside the Rule column of Basic rows) ────
    // Rules are stored as effective-dated entries but edited in place: the
    // numbers inside the bullet sentences are inputs, synced across rows.

    let loadedRates = [];

    // Blank = a plain rate row, not a rule. Advance and Payout are Basic
    // Commission's own payout mechanics; kept as a list so a future scheme can
    // add its own without touching the grid code.
    const RULE_TYPE_OPTIONS = ["", "Advance", "Payout"];
    const PROPERTY_TYPES = ["", "Residential", "Commercial", "Shop Lot", "Factory", "NGO Project", "Government Project"];

    // The edit page is scoped to one commission type per visit, set by which
    // "Add / Edit entries" button opened it — no visible type switcher.
    let editScope = "Basic Commission";

    function currentScope() {
        return editScope;
    }

    function currentMonth() {
        const el = document.getElementById("filterMonth");
        return (el && el.value) ? el.value : CURRENT_YM;
    }

    // Among entries matching keyFn, the one whose effective_from is the latest
    // that is <= ym. Returns { key: entry }.
    function governing(entries, ym, keyFn) {
        const best = {};
        entries.forEach((e) => {
            if (String(e.effective_from) > ym) return;
            const k = keyFn(e);
            if (!best[k] || String(e.effective_from) > String(best[k].effective_from)) best[k] = e;
        });
        return best;
    }

    // The head above the two grids: formula + wired/not-wired badge for the scope.
    function renderRulesHead() {
        const scope = currentScope();
        const t = RATE_TYPES.find((x) => x.key === scope) || {};
        const badge = t.wired
            ? `<span class="wire-badge active">Active — used in calculation</span>`
            : `<span class="wire-badge pending">Not wired yet — stored only</span>`;
        document.getElementById("rulesHead").innerHTML =
            `<div class="rules-head" style="padding:12px 24px 0;">` +
            `<span><strong>Formula:</strong> <span class="formula-cell">${escapeHtml(t.formula || "—")}</span></span>${badge}</div>`;
        // Rule Type / Amount express Basic Commission's own payout mechanics, so
        // those two columns only appear inside that scope.
        const showRuleCols = scope === "Basic Commission";
        document.querySelectorAll(".unified-grid .rule-type-col").forEach((th) => {
            th.style.display = showRuleCols ? "" : "none";
        });
    }

    function ruleTypeOptionsHtml(sel) {
        return RULE_TYPE_OPTIONS.map((t) =>
            `<option value="${escapeHtml(t)}" ${t === sel ? "selected" : ""}>${escapeHtml(t || "(rate row)")}</option>`).join("");
    }

    function propertyOptionsHtml(sel) {
        return PROPERTY_TYPES.map((p) =>
            `<option value="${escapeHtml(p)}" ${p === sel ? "selected" : ""}>${escapeHtml(p || "(all)")}</option>`).join("");
    }

    // ── Properties Type: pick several ────────────────────────────────────────
    // A rate usually covers more than one property type, so this is a tag
    // picker rather than a single dropdown. Stored as a comma-separated list;
    // empty means "applies to all property types".

    function normProp(s) {
        if (!s) return "";
        const items = String(s).split(",").map((item) => {
            let str = item.trim().toLowerCase();
            if (str === "ngo") return "ngo project";
            if (str === "goverment" || str === "government") return "government project";
            return str;
        }).filter(Boolean);
        items.sort();
        return items.join(", ");
    }

    function parseList(v) {
        if (!v) return [];
        const rawItems = String(v).split(",").map((s) => s.trim()).filter(Boolean);
        return rawItems.map((trimmed) => {
            const norm = normProp(trimmed);
            const matched = PROPERTY_TYPES.find((p) => p && normProp(p) === norm);
            return matched || trimmed;
        });
    }

    function propertyPickerHtml(selected) {
        const chosen = parseList(selected);
        const chips = chosen.length
            ? chosen.map((p) => `<span class="ms-chip">${escapeHtml(p)}<button type="button" class="ms-x" data-val="${escapeHtml(p)}" title="Remove">×</button></span>`).join("")
            : `<span class="ms-empty">All types</span>`;
        const opts = PROPERTY_TYPES.filter(Boolean).map((p) =>
            `<label class="ms-opt"><input type="checkbox" value="${escapeHtml(p)}" ${
                chosen.some((c) => normProp(c) === normProp(p)) ? "checked" : ""}> ${escapeHtml(p)}</label>`).join("");
        return `<div class="ms e-prop" data-value="${escapeHtml(chosen.join(", "))}">
            <div class="ms-box" tabindex="0">${chips}<span class="ms-add">+</span></div>
            <div class="ms-menu" hidden>${opts}</div>
        </div>`;
    }

    // Wire one picker: chips, checkbox menu, and the data-value that readRow reads.
    function initPropertyPicker(root, enabled) {
        const box = root.querySelector(".ms-box");
        const menu = root.querySelector(".ms-menu");

        function sync() {
            let chosen = Array.from(menu.querySelectorAll("input:checked")).map((i) => i.value);
            root.dataset.value = chosen.join(", ");
            box.innerHTML = (chosen.length
                ? chosen.map((p) => `<span class="ms-chip">${escapeHtml(p)}<button type="button" class="ms-x" data-val="${escapeHtml(p)}" title="Remove">×</button></span>`).join("")
                : `<span class="ms-empty">All types</span>`) + `<span class="ms-add">+</span>`;
            bindChips();
        }
        function bindChips() {
            if (!enabled) return;
            box.querySelectorAll(".ms-x").forEach((btn) => {
                btn.addEventListener("click", (ev) => {
                    ev.stopPropagation();
                    const cb = menu.querySelector(`input[value="${CSS.escape(btn.dataset.val)}"]`);
                    if (cb) { cb.checked = false; sync(); }
                });
            });
        }
        if (!enabled) {
            box.style.opacity = "0.7";
            return;
        }
        box.addEventListener("click", (ev) => {
            ev.stopPropagation();
            menu.hidden = !menu.hidden;
        });
        box.addEventListener("keydown", (ev) => {
            if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); menu.hidden = !menu.hidden; }
        });
        menu.addEventListener("change", sync);
        menu.addEventListener("click", (ev) => { ev.stopPropagation(); });
        document.addEventListener("click", (ev) => {
            if (!root.contains(ev.target)) menu.hidden = true;
        });
        bindChips();
    }

    // How the profit-sharing figure combines with the rate. "+ rate" is what
    // the Factory path does today (rate + sharing); "× rate" multiplies instead.
    // "Per invoice" says the number is not fixed here — it is keyed in per deal
    // on the dashboard, and this row only records that the deal type earns it.
    const SHARING_MODES = ["", "+ rate", "× rate", "Per invoice"];

    function sharingModeOptionsHtml(sel) {
        return SHARING_MODES.map((m) =>
            `<option value="${escapeHtml(m)}" ${m === sel ? "selected" : ""}>${escapeHtml(m || "—")}</option>`).join("");
    }

    // "Override from": whose sales the override is earned on, e.g. a Senior
    // earning 0.25% on the Executives reporting to them.
    function overrideFromOptionsHtml(agentType, selected) {
        const roles = RATE_ROLES[agentType] || RATE_ROLES["Internal"];
        const opts = roles.slice();
        if (selected && !opts.includes(selected)) opts.push(selected);
        return `<option value="">(none)</option>` + opts.map((r) =>
            `<option value="${escapeHtml(r)}" ${r === selected ? "selected" : ""}>${escapeHtml(r)}</option>`).join("");
    }

    function agentTypeOptionsHtml(sel, includeAll) {
        const opts = includeAll ? ["", "Internal", "Outsource"] : ["Internal", "Outsource"];
        return opts.map((a) =>
            `<option value="${escapeHtml(a)}" ${a === sel ? "selected" : ""}>${escapeHtml(a || "All")}</option>`).join("");
    }

    function ruleCellHtml(info) {
        const title = info.ruleFull ? ` title="${escapeHtml(info.ruleFull)}"` : "";
        return `<span class="rule-cell"${title}>${escapeHtml(info.rule || "n/a")}</span>`;
    }

    function formulaCellHtml(info) {
        return `<span class="formula-cell">${escapeHtml(info.formula || "—")}</span>`;
    }

    async function loadMe() {
        const res = await api("/api/me");
        const me = await res.json();
        document.getElementById("accountWhoami").textContent = `${me.username} (${me.role})`;
        isAdmin = me.role === "admin";
        if (isAdmin) {
            ["editModeBtn", "nfpAddBtn", "nfpUploadBtn", "seedRolesBtn", "addRoleBtn", "showExcludedRolesBtn", "saveRolesBtn",
             "contestSaveBtn", "contestNewBtn", "contestAddRosterBtn",
             "anpSaveBtn", "anpAddTierBtn", "egaSaveBtn", "egaAddMonthBtn", "egaNewBtn", "pbSaveBtn",
             "egaCarryAddBtn", "egaCarrySaveBtn"].forEach((id) => {
                const el = document.getElementById(id);
                if (el) el.style.display = "";
            });
        } else {
            document.getElementById("viewOnlyNote").style.display = "block";
        }
        return me;
    }

    // ── VIEW MODE ────────────────────────────────────────────────────────────

    // Which of the four sources actually answered. "Data page" is the only one
    // that means "this is the value you entered"; anything else is a fallback
    // still supplying the number, and a row you have not taken control of yet.
    const SOURCE_LABELS = {
        unified: "Data page",
        legacy: "Legacy table",
        system: "Legacy table",
        sheet: "Sheet fallback",
        default: "Built-in default"
    };

    function sourceBadge(source) {
        const key = String(source || "default");
        return `<span class="source-badge ${escapeHtml(key)}" title="${escapeHtml(
            key === "unified" ? "Entered on this Data page — wired into the calculation"
                              : "Not from the Data page: a fallback is supplying this value")}">${
            escapeHtml(SOURCE_LABELS[key] || key)}</span>`;
    }

    // Scheme-wide payout mechanics, stated once above the table. Each line
    // carries its own source badge, so a rule that is only a built-in default
    // (because nothing has been entered for it) cannot be mistaken for data
    // you typed.
    function renderPayoutPanel(rules, month) {
        const panel = document.getElementById("payoutPanel");
        if (panel) {
            panel.style.display = "none";
            panel.innerHTML = "";
            return;
        }
        const monthName = MONTH_NAMES[Number(month) - 1] || "";
        const by = {};
        rules.forEach((r) => { by[r.rule_key] = r; });

        const lines = [];
        const push = (key, text) => {
            const r = by[key];
            lines.push({ text, source: r ? r.source : "default", present: !!r });
        };
        if (by.basic_payout_pre_july) {
            push("basic_payout_pre_july",
                `Pre-Jul invoices: full payout at ${by.basic_payout_pre_july.value}% payment`);
        }
        if (by.basic_commission_cap) {
            const c = by.basic_commission_cap;
            push("basic_commission_cap",
                `${monthName} invoices: 1st payment ≥${c.trigger_pct || 5}% → RM${c.value} (once per invoice)`);
        }
        if (by.basic_balance_payout_trigger) {
            push("basic_balance_payout_trigger",
                `${monthName} invoices: payment ≥${by.basic_balance_payout_trigger.value}% → balance`);
        }
        // Anything else entered as a standalone rule keeps its own label.
        const known = ["basic_payout_pre_july", "basic_commission_cap",
            "basic_balance_payout_trigger"];
        rules.forEach((r) => {
            if (known.includes(r.rule_key)) return;
            const label = String(r.label).replace(/\s*\((RM|%|months|yes\/no)\)\s*$/i, "");
            const valText = r.unit === "RM" ? "RM " + r.value
                : r.unit === "months" ? r.value + " months"
                : r.unit === "yesno" ? (Number(r.value) ? "Yes" : "No")
                : r.value + "%";
            lines.push({ text: `${label}: ${valText}`, source: r.source, present: true });
        });

        if (!lines.length) {
            panel.innerHTML = `<div class="section-hint" style="padding-top:0;">
                <strong>Payout rules:</strong> none entered for ${escapeHtml(monthName)} —
                the calculation is using its built-in defaults
                (RM300 advance at ≥5%, balance at ≥75%, 100% for pre-July invoices).
                Add them as rows with a Rule Type to take control of them.</div>`;
            return;
        }
        panel.innerHTML = `<div class="section-hint" style="padding-top:0;">
            <strong>Payout rules for ${escapeHtml(monthName)}</strong> — these apply to the
            whole scheme, not to any single rate below.
            <div class="rule-lines" style="max-width:none; margin-top:6px;">${
                lines.map((l) => `<div>• ${escapeHtml(l.text)} ${sourceBadge(l.source)}</div>`).join("")
            }</div></div>`;
    }

    const PAGE_SIZE = 100;
    // The Basic Commission table on the Data page pages at 15 rows so the card
    // stays a readable height; the edit/entries grid keeps PAGE_SIZE.
    const PREVIEW_PAGE_SIZE = 15;
    let previewPage = 1;
    let previewAllRows = [];  // built rows (DOM elements) after filter
    let editPage = 1;
    let editAllRows = [];     // built rows (DOM elements) for edit mode

    /** A row's Agent Name cell as a lowercased list — the picker is multi-select
     *  and stores every agent the rate applies to on one row. Mirrors
     *  _row_agent_list() in basic_commission_rates.py. */
    function splitAgents(v) {
        return String(v || "").toLowerCase().split(",").map((s) => s.trim()).filter(Boolean);
    }

    // "OSA/OSA1" is one picker entry but two stored values, so compare on a
    // normalised form and let a slash-joined option match either side.
    function normRole(s) {
        return String(s || "").toLowerCase().replace(/[^a-z0-9]/g, "");
    }

    // Two labels for the same tier have to compare equal everywhere: the
    // July-2026 names replaced "Senior"/"Executive"/"OSA/OSA1" but rows saved
    // under the old names were left alone, so filtering, de-duplicating and
    // drift detection all have to see through the rename. Mirrors
    // _HIERARCHY_ALIASES in basic_commission_rates.py, which does the same for
    // the rate lookup.
    const ROLE_ALIASES = {
        senior: "salessenior",
        salessenior: "salessenior",
        executive: "salesexecutive",
        salesexecutive: "salesexecutive",
        osa: "osaosa1",
        osa1: "osaosa1",
        osaosa1: "osaosa1",
    };

    function canonRole(s) {
        const k = normRole(s);
        return ROLE_ALIASES[k] || k;
    }

    /** Does this role belong to this agent type? A role names a rate table, so
     *  an Outsource role on an Internal row (or the reverse) matches no rate at
     *  all and the agent silently falls through to the hardcoded default.
     *  Unknown roles and blanks are not called wrong — only a role that
     *  demonstrably belongs to the OTHER type is. */
    function roleBelongsToType(role, agentType) {
        const key = canonRole(role);
        if (!key) return true;
        const inList = (t) => (RATE_ROLES[t] || []).concat(LEGACY_ROLES[t] || [])
            .some((x) => canonRole(x) === key);
        const other = agentType === "Internal" ? "Outsource"
                    : agentType === "Outsource" ? "Internal" : null;
        if (!other) return true;
        return inList(agentType) || !inList(other);
    }

    function roleMatches(rowRole, filterRole) {
        if (!filterRole) return true;
        const target = canonRole(rowRole);
        if (!target) return false;
        return canonRole(filterRole) === target
            || filterRole.split("/").some((part) => canonRole(part) === target);
    }

    /** Reads the Basic Commission "Search Agent Type/Role" free-text box into
     *  {agentTypeVal, roleVal} -- the same shape the old separate Agent Type
     *  and Role dropdowns produced, either half blank meaning "all". The word
     *  "Internal" or "Outsource" (standalone, case-insensitive) is read out as
     *  the Agent Type; whatever text is left over is matched against the known
     *  role names (current + legacy, both agent types) using the same
     *  canonRole() normalisation the rest of this page already relies on, so
     *  "OUM", "oum", and a role's retired label all still resolve. */
    function parseAgentTypeRoleSearch(text) {
        const raw = String(text || "").trim();
        if (!raw) return { agentTypeVal: "", roleVal: "" };

        let agentTypeVal = "";
        let rest = raw;
        const atypeMatch = raw.match(/\b(internal|outsource)\b/i);
        if (atypeMatch) {
            agentTypeVal = atypeMatch[1].toLowerCase() === "internal" ? "Internal" : "Outsource";
            rest = (raw.slice(0, atypeMatch.index) + raw.slice(atypeMatch.index + atypeMatch[0].length)).trim();
        }

        let roleVal = "";
        if (rest) {
            const allRoles = [...new Set(
                (RATE_ROLES.Internal || []).concat(RATE_ROLES.Outsource || [])
                    .concat(LEGACY_ROLES.Internal || []).concat(LEGACY_ROLES.Outsource || [])
            )];
            const target = canonRole(rest);
            roleVal = allRoles.find((r) => canonRole(r) === target)
                || allRoles.find((r) => target && canonRole(r).includes(target))
                || "";
        }
        return { agentTypeVal, roleVal };
    }

    /** Agent names come from the roles table (the same list the Roles section
     *  shows), narrowed by the Agent Type and Role filters and de-duplicated —
     *  an agent with role history has one row per role, not one name each. */
    function populatePreviewAgentNameFilter() {
        const sel = document.getElementById("previewAgentName");
        if (!sel) return;
        const parsed = parseAgentTypeRoleSearch(document.getElementById("previewAgentRoleSearch")?.value || "");
        const atype = parsed.agentTypeVal.toLowerCase();
        const role = parsed.roleVal;
        const names = [...new Set(rolesList
            .filter((r) => !atype || String(r.agent_type || "").toLowerCase() === atype)
            .filter((r) => roleMatches(r.hierarchy, role))
            .map((r) => String(r.agent || "").trim())
            .filter(Boolean))].sort((a, b) => a.localeCompare(b));
        const prev = sel.value;
        sel.innerHTML = `<option value="">All Agents</option>` + names.map((n) =>
            `<option value="${escapeHtml(n)}">${escapeHtml(n)}</option>`).join("");
        sel.value = names.includes(prev) ? prev : "";
    }

    /** The roles this agent holds — used so picking an agent still shows the
     *  role-level rates that govern them, not just rows naming them outright. */
    function rolesForAgentName(name) {
        const key = String(name || "").trim().toLowerCase();
        return rolesList
            .filter((r) => String(r.agent || "").trim().toLowerCase() === key)
            .map((r) => r.hierarchy)
            .filter(Boolean);
    }

    async function loadPreview() {
        // Basic and Net Floor Price are separate sections with separate cards,
        // so each carries its own Year/Month pair -- the one belonging to the
        // visible card is the one that decides which revision is resolved.
        const onNfp = activeDataSection === "nfp";
        let yVal, mVal;
        if (onNfp) {
            yVal = (document.getElementById("nfpPreviewYear")?.value || "").trim();
            mVal = (document.getElementById("nfpPreviewMonth")?.value || "").trim();
        } else {
            const parsed = parseMonthYearSearch(document.getElementById("previewMonthYearSearch")?.value || "");
            yVal = parsed.yVal;
            mVal = parsed.mVal;
        }
        const atRoleParsed = parseAgentTypeRoleSearch(document.getElementById("previewAgentRoleSearch")?.value || "");
        const agentTypeFilter = atRoleParsed.agentTypeVal.toLowerCase();
        const roleFilter = atRoleParsed.roleVal;
        const agentNameFilter = (document.getElementById("previewAgentName")?.value || "").trim();
        const agentNameRoles = agentNameFilter ? rolesForAgentName(agentNameFilter) : [];
        const propTypeFilter = (document.getElementById("previewPropertyType")?.value || "").toLowerCase();
        const month = (yVal && mVal) ? `${yVal}-${mVal}` : "all";

        const tbody = document.getElementById("previewBody");
        try {
            // Always refresh raw entries so newly added rows appear immediately
            const rawRes = await api("/api/commission-rates");
            loadedRates = (await rawRes.json()).map((r) => ({
                id: r.id,
                rate_type: r.rate_type || "Basic Commission",
                agent_type: r.agent_type || "", hierarchy: r.hierarchy || "", agent: r.agent || "",
                label: r.label || "", condition: r.condition || "",
                rate_pct: r.rate_pct || "", override_rate_pct: r.override_rate_pct || "",
            override_from: r.override_from || "",
                profit_sharing_rate_pct: r.profit_sharing_rate_pct || "",
                profit_sharing_mode: r.profit_sharing_mode || "",
                property_type: r.property_type || "", trigger_pct: r.trigger_pct || "",
                job_type: r.job_type || "", ev_type: r.ev_type || "",
                invoice_date_from: r.invoice_date_from || "", rule_type: r.rule_type || "",
                amount_rm: r.amount_rm || "",
                effective_from: r.effective_from, remarks: r.remarks || ""
            }));

            const res = await api(`/api/basic-rates/resolved?month=${encodeURIComponent(month)}`);
            const data = await res.json();
            renderPayoutPanel(data.rules || [], month);

            // Apply agent type, property type, year, month, and section filter
            const sectionRateTypeMap = {
                basic: "Basic Commission",
                nfp: "Net Floor Price Rate",
                anp: "ANP Commission",
                ega_esa: "EGA/ESA Award",
                production_bonus: "Production Bonus Rate",
                monthly_contest: "Monthly Contest"
            };
            const targetRateType = sectionRateTypeMap[activeDataSection] || "Basic Commission";

            let filteredRates = (data.rates || []).filter((r) => {
                if (targetRateType && r.rate_type && r.rate_type !== targetRateType && activeDataSection !== "basic") {
                    return false;
                }
                if (agentTypeFilter && (r.agent_type || "").toLowerCase() !== agentTypeFilter) {
                    return false;
                }
                if (roleFilter && !roleMatches(r.hierarchy, roleFilter)) {
                    return false;
                }
                if (agentNameFilter) {
                    // One row can name several agents ("A, B"), so this is a
                    // membership test, not a string compare.
                    const rowAgents = splitAgents(r.agent);
                    // A rate that names the agent, or a role-level rate (no agent
                    // named) for a role they hold — both govern this agent.
                    const named = rowAgents.includes(agentNameFilter.toLowerCase());
                    const viaRole = !rowAgents.length
                        && agentNameRoles.some((hr) => roleMatches(r.hierarchy, hr));
                    if (!named && !viaRole) return false;
                }
                if (propTypeFilter) {
                    const rProp = (r.property_type || "").toLowerCase();
                    if (!rProp.includes(propTypeFilter) && rProp !== "all types" && rProp !== "") {
                        return false;
                    }
                }
                const eff = String(r.effective_from || "");
                if (yVal && !eff.includes(yVal)) {
                    return false;
                }
                if (mVal && !eff.includes(`-${mVal}`) && !eff.includes(`/${mVal}`)) {
                    return false;
                }
                return true;
            });

            // Sort by Job Type (Solar, then EV), then EV Type, then month
            // descending so the latest row still floats within each group.
            filteredRates.sort((a, b) => {
                const jobA = normJob(a.job_type);
                const jobB = normJob(b.job_type);
                if (jobA !== jobB) {
                    // Solar Services first, EV Services after.
                    return jobA === "Solar Services" ? -1 : 1;
                }
                const evA = normEvType(a.ev_type);
                const evB = normEvType(b.ev_type);
                if (evA !== evB) return evA.localeCompare(evB);
                const effA = String(a.effective_from || "");
                const effB = String(b.effective_from || "");
                return effB.localeCompare(effA);
            });

            // Build all row elements
            previewAllRows = filteredRates.map((r) => {
                const rawEntry = month === "all" ? r : (loadedRates.find((e) =>
                    (e.rate_type || "Basic Commission") === "Basic Commission" &&
                    (e.agent_type || "").toLowerCase() === (r.agent_type || "").toLowerCase() &&
                    (e.hierarchy || "").toLowerCase() === (r.hierarchy || "").toLowerCase() &&
                    (e.agent || "").toLowerCase() === (r.agent || "").toLowerCase() &&
                    (normProp(e.property_type) === normProp(r.property_type)) &&
                    normJob(e.job_type) === normJob(r.job_type) &&
                    evKey(e) === evKey(r) &&
                    e.source !== "legacy" && e.source !== "default"
                ) || loadedRates.find((e) =>
                    (e.rate_type || "Basic Commission") === "Basic Commission" &&
                    (e.agent_type || "").toLowerCase() === (r.agent_type || "").toLowerCase() &&
                    (e.hierarchy || "").toLowerCase() === (r.hierarchy || "").toLowerCase() &&
                    (e.agent || "").toLowerCase() === (r.agent || "").toLowerCase() &&
                    (normProp(e.property_type) === normProp(r.property_type)) &&
                    normJob(e.job_type) === normJob(r.job_type) &&
                    evKey(e) === evKey(r)
                ) || loadedRates.find((e) =>
                    (e.rate_type || "Basic Commission") === "Basic Commission" &&
                    (e.agent_type || "").toLowerCase() === (r.agent_type || "").toLowerCase() &&
                    (e.hierarchy || "").toLowerCase() === (r.hierarchy || "").toLowerCase() &&
                    (e.agent || "").toLowerCase() === (r.agent || "").toLowerCase()
                ));
                const displayEff = r.effective_from || (rawEntry && rawEntry.effective_from ? rawEntry.effective_from : "");
                const ovrPct = r.override_rate_pct || (rawEntry && rawEntry.override_rate_pct) || "";
                const ovrFrom = r.override_from || (rawEntry && rawEntry.override_from) || "";
                const modalData = rawEntry ? Object.assign({}, rawEntry, r, {
                    override_rate_pct: ovrPct,
                    override_from: ovrFrom,
                    property_type: r.property_type || (rawEntry && rawEntry.property_type) || "",
                    job_type: normJob(r.job_type || (rawEntry && rawEntry.job_type)),
                    ev_type: normEvType(r.ev_type || (rawEntry && rawEntry.ev_type))
                }) : r;

                const roleLabel = r.agent ? `${r.hierarchy} — ${r.agent}` : (r.hierarchy || "(all roles)");
                const info = typeInfo("Basic Commission");
                const condStr = (rawEntry && rawEntry.condition) ? rawEntry.condition : (r.condition || (r.trigger_pct ? `Pays at ≥${r.trigger_pct}% payment` : ""));
                const cond = formatConditionHtml(condStr);
                let rateVal = r.rate_pct ? `${Number(r.rate_pct).toFixed(2)}%` : (r.amount_rm ? `RM ${r.amount_rm}` : "—");
                if (ovrPct) {
                    const ovrLines = formatOverrideRulesHtml(ovrPct, ovrFrom);
                    rateVal += `<div style="font-size:12px; color:#2563eb; margin-top:4px; font-weight:500; line-height:1.4;">Override: ${ovrLines}</div>`;
                }
                const cType = r.rule_type ? `Basic — ${r.rule_type}` : "Basic Commission";
                const propType = (r.property_type || (rawEntry && rawEntry.property_type) || "").trim();
                const propTypeDisplay = propType || "All";
                const evTypeDisplay = normEvType(r.ev_type || (rawEntry && rawEntry.ev_type));
                const jobTypeDisplay = normJob(r.job_type || (rawEntry && rawEntry.job_type)) + (evTypeDisplay ? ` (${evTypeDisplay})` : "");

                const tr = document.createElement("tr");
                tr.innerHTML = `
                    <td class="sm-cell">${escapeHtml(cType)}</td>
                    <td class="sm-cell">${escapeHtml(fmtMonth(displayEff) || "—")}</td>
                    <td class="sm-cell">${escapeHtml(r.agent_type || "All")}</td>
                    <td class="sm-cell">${escapeHtml(roleLabel)}</td>
                    <td class="sm-cell">${escapeHtml(propTypeDisplay)}</td>
                    <td class="sm-cell">${escapeHtml(jobTypeDisplay)}</td>
                    <td class="sm-cell">${rateVal}</td>
                    <td>${cond}</td>
                    <td>${formulaCellHtml(info)}</td>
                    <td>${sourceBadge(r.source)}</td>
                    <td>${isAdmin ? `<button class="btn btn-secondary row-edit-btn" title="Revise this rate">✏️</button>` : ""}</td>
                `;
                const btn = tr.querySelector(".row-edit-btn");
                if (btn) btn.addEventListener("click", () => openDataEditModal(modalData, "Basic Commission"));
                return tr;
            });

            previewPage = 1;
            renderPreviewPage();
            renderNfpPreview(data.nfp_rates || []);
        } catch (e) {
            if (tbody) tbody.innerHTML = `<tr><td colspan="11">Could not load values.</td></tr>`;
        }
    }

    function renderPreviewPage() {
        const tbody = document.getElementById("previewBody");
        const paginationEl = document.getElementById("previewPagination");
        const totalPages = Math.max(1, Math.ceil(previewAllRows.length / PREVIEW_PAGE_SIZE));
        previewPage = Math.max(1, Math.min(previewPage, totalPages));

        if (!tbody) return;   // the old Basic Commission rates grid was removed
        tbody.innerHTML = "";
        const start = (previewPage - 1) * PREVIEW_PAGE_SIZE;
        previewAllRows.slice(start, start + PREVIEW_PAGE_SIZE).forEach((tr) => tbody.appendChild(tr));

        // Render pagination controls
        if (!paginationEl) return;
        if (!previewAllRows.length) {
            paginationEl.innerHTML = "";
            return;
        }
        const info = `<span style="font-size:13px; color:var(--text-muted);">Page ${previewPage} of ${totalPages} (${previewAllRows.length} total rows)</span>`;
        if (totalPages <= 1) {
            paginationEl.innerHTML = info;
            return;
        }
        const prev = `<button class="btn btn-secondary" id="previewPrevBtn" ${previewPage === 1 ? "disabled" : ""} style="padding:3px 10px; font-size:12px;">‹ Prev</button>`;
        const next = `<button class="btn btn-secondary" id="previewNextBtn" ${previewPage === totalPages ? "disabled" : ""} style="padding:3px 10px; font-size:12px;">Next ›</button>`;
        paginationEl.innerHTML = prev + info + next;
        paginationEl.querySelector("#previewPrevBtn")?.addEventListener("click", () => { previewPage--; renderPreviewPage(); });
        paginationEl.querySelector("#previewNextBtn")?.addEventListener("click", () => { previewPage++; renderPreviewPage(); });
    }

    function renderEditPage() {
        const tbody = document.getElementById("entriesBody");
        const paginationEl = document.getElementById("editPagination");
        const totalPages = Math.max(1, Math.ceil(editAllRows.length / PAGE_SIZE));
        editPage = Math.max(1, Math.min(editPage, totalPages));

        tbody.innerHTML = "";
        const start = (editPage - 1) * PAGE_SIZE;
        editAllRows.slice(start, start + PAGE_SIZE).forEach((tr) => tbody.appendChild(tr));

        if (!paginationEl) return;
        if (!editAllRows.length) {
            paginationEl.innerHTML = "";
            return;
        }
        const info = `<span style="font-size:13px; color:var(--text-muted);">Page ${editPage} of ${totalPages} (${editAllRows.length} total rows)</span>`;
        if (totalPages <= 1) {
            paginationEl.innerHTML = info;
            return;
        }
        const prev = `<button class="btn btn-secondary" id="editPrevBtn" ${editPage === 1 ? "disabled" : ""} style="padding:3px 10px; font-size:12px;">‹ Prev</button>`;
        const next = `<button class="btn btn-secondary" id="editNextBtn" ${editPage === totalPages ? "disabled" : ""} style="padding:3px 10px; font-size:12px;">Next ›</button>`;
        paginationEl.innerHTML = prev + info + next;
        paginationEl.querySelector("#editPrevBtn")?.addEventListener("click", () => { editPage--; renderEditPage(); });
        paginationEl.querySelector("#editNextBtn")?.addEventListener("click", () => { editPage++; renderEditPage(); });
    }

    /** An NFP row's payout stages. `label` holds them as written text (the tier
     *  owns `condition` on these rows); the scalar columns are the fallback for
     *  a row saved before the stages had anywhere to live. */
    function nfpPayoutText(r) {
        const label = String(r.label || "").trim();
        if (label) return label;
        return formatPaymentRulesCondition(
            (r.trigger_pct || r.amount_rm)
                ? [{ trigger_pct: r.trigger_pct || "", rule_type: r.rule_type || "Payout", amount_rm: r.amount_rm || "" }]
                : []);
    }

    function renderNfpPreview(nfpRates) {
        const tbody = document.getElementById("nfpPreviewBody");
        if (!tbody) return;
        tbody.innerHTML = "";
        const countEl = document.getElementById("nfpPreviewCount");
        if (countEl) countEl.textContent = `${nfpRates.length} tier${nfpRates.length === 1 ? "" : "s"}`;
        if (!nfpRates.length) {
            tbody.innerHTML = `<tr><td colspan="10" style="color:var(--text-muted);">No NFP tier entries cover this month.</td></tr>`;
            return;
        }
        const info = typeInfo("Net Floor Price Rate");
        nfpRates.forEach((r) => {
            const roleLabel = r.agent ? `${r.hierarchy || "All"} — ${r.agent}` : (r.hierarchy || "All");
            const condTitle = r.condition ? ` title="${escapeHtml(r.condition)}"` : "";
            // A built-in default describes the engine's own tier and belongs to
            // no agent type, so there is nothing to revise -- taking control of
            // one means adding a real entry, which "Add entry" does.
            const isDefault = String(r.source || "") === "default";
            const tr = document.createElement("tr");
            tr.innerHTML = `
                <td class="sm-cell">Net Floor Price</td>
                <td class="sm-cell">${escapeHtml(fmtMonth(r.effective_from) || "—")}</td>
                <td class="sm-cell">${escapeHtml(r.agent_type)}</td>
                <td class="sm-cell">${escapeHtml(roleLabel)}</td>
                <td class="sm-cell">${Number(r.rate_pct).toFixed(2)}%</td>
                <td><span class="rule-cell"${condTitle}>${escapeHtml(r.condition || info.rule || "n/a")}</span></td>
                <td>${formatConditionHtml(nfpPayoutText(r))}</td>
                <td>${formulaCellHtml(info)}</td>
                <td>${sourceBadge(r.source)}</td>
                <td>${isAdmin && !isDefault ? `<button class="btn btn-secondary row-edit-btn" title="Revise this tier">✏️</button>` : ""}</td>
            `;
            const btn = tr.querySelector(".row-edit-btn");
            if (btn) btn.addEventListener("click", () => openDataEditModal(r, NFP_TYPE));
            tbody.appendChild(tr);
        });
    }

    // ── EDIT MODE ────────────────────────────────────────────────────────────

    function initMonthFilter() {
        const sel = document.getElementById("filterMonth");
        const now = new Date();
        sel.innerHTML = "";
        const optAll = document.createElement("option");
        optAll.value = "all";
        optAll.textContent = "All Months";
        optAll.selected = true;
        sel.appendChild(optAll);
        [2025, 2026].forEach((y) => {
            for (let m = 1; m <= 12; m++) {
                const opt = document.createElement("option");
                const ym = `${y}-${String(m).padStart(2, "0")}`;
                opt.value = ym;
                opt.textContent = `${MONTH_NAMES[m - 1]} ${y}`;
                sel.appendChild(opt);
            }
        });
        sel.addEventListener("change", () => refreshEditScope());
        const agentTypeFilterEl = document.getElementById("filterAgentType");
        if (agentTypeFilterEl && !agentTypeFilterEl.dataset.bound) {
            agentTypeFilterEl.dataset.bound = "true";
            agentTypeFilterEl.addEventListener("change", () => refreshEditScope());
        }
    }

    function typeOptionsHtml(selected) {
        return RATE_TYPES.map((t) =>
            `<option value="${escapeHtml(t.key)}" ${t.key === selected ? "selected" : ""}>${escapeHtml(t.key)}</option>`
        ).join("");
    }

    function roleOptionsHtml(agentType, selected) {
        const roles = RATE_ROLES[agentType] || RATE_ROLES["Internal"];
        let opts = roles.slice();
        if (selected && !opts.includes(selected)) opts.push(selected);
        return opts.map((r) =>
            `<option value="${escapeHtml(r)}" ${r === selected ? "selected" : ""}>${escapeHtml(r)}</option>`
        ).join("");
    }

    function fmtMonth(eff) {
        const s = String(eff || "").trim();
        if (!s) return "";
        if (s.toLowerCase() === "all" || s.toLowerCase() === "all months") return "All Months";
        // Handle range strings like "2025-01 to 2026-05"
        if (s.includes(" to ") || s.includes("..")) {
            const sep = s.includes(" to ") ? " to " : "..";
            const parts = s.split(sep);
            const fmt = (p) => {
                const m = /^(\d{4})-(\d{2})$/.exec(p.trim());
                return m ? `${MONTH_SHORT[parseInt(m[2], 10) - 1]} ${m[1]}` : p.trim();
            };
            return `${fmt(parts[0])} – ${fmt(parts[1] || parts[0])}`;
        }
        const m = /^(\d{4})-(\d{2})$/.exec(s);
        if (!m) return s;
        return `${MONTH_SHORT[parseInt(m[2], 10) - 1]} ${m[1]}`;
    }

    function prevMonth(eff) {
        const m = /^(\d{4})-(\d{2})$/.exec(String(eff || "").trim());
        if (!m) return "";
        let y = parseInt(m[1], 10), mo = parseInt(m[2], 10) - 1;
        if (mo === 0) { mo = 12; y -= 1; }
        return `${y}-${String(mo).padStart(2, "0")}`;
    }

    // Row identity for copy-on-write. Everything that makes a row a distinct
    // entry (not just a new value for an existing one) belongs in this key.
    function rateKeyOf(r) {
        return [r.agent_type, r.hierarchy, String(r.agent || "").toLowerCase(),
                String(r.condition || "").toLowerCase(),
                normProp(r.property_type),
                String(r.rule_type || "").toLowerCase(),
                String(r.label || "").toLowerCase()].join("|");
    }

    // Every editable field of a unified row, in one place, so rendering,
    // change-detection and saving can never drift apart.
    const ROW_FIELDS = [
        ["agent_type", ".e-agent-type"], ["hierarchy", ".e-role"], ["agent", ".e-agent"],
        ["rate_pct", ".e-value"], ["override_rate_pct", ".e-override"],
        ["override_from", ".e-override-from"],
        ["profit_sharing_rate_pct", ".e-sharing"], ["profit_sharing_mode", ".e-sharing-mode"],
        ["property_type", ".e-prop"],
        ["trigger_pct", ".e-trigger"], ["invoice_date_from", ".e-invoice-date"],
        ["rule_type", ".e-rule-type"], ["amount_rm", ".e-amount"]
    ];

    function readRow(tr) {
        const out = {};
        ROW_FIELDS.forEach(([field, sel]) => {
            const el = tr.querySelector(sel);
            // The property picker is a div holding its value in data-value;
            // everything else is a plain input/select.
            out[field] = !el ? ""
                : String(el.dataset && el.dataset.value !== undefined
                    ? el.dataset.value : (el.value || "")).trim();
        });
        return out;
    }

    // Render the unified grid for the currently-selected month. One row per
    // entry that governs the month; the month itself is a read-only cell.
    function renderMonth(filterEntry) {
        const ym = currentMonth();
        const scope = currentScope();
        const agentFilter = document.getElementById("filterAgentType").value;

        const scopeRows = loadedRates.filter((r) => (r.rate_type || "Basic Commission") === scope);
        let rowsToRender;
        if (ym === "all") {
            rowsToRender = scopeRows;
        } else {
            const gov = governing(scopeRows, ym, rateKeyOf);
            rowsToRender = Object.values(gov);
        }

        if (filterEntry && !filterEntry.isAdd && filterEntry.hierarchy !== undefined) {
            const matchesFilter = (e) => {
                if (filterEntry.agent_type && e.agent_type && e.agent_type !== filterEntry.agent_type) return false;
                if (filterEntry.hierarchy && e.hierarchy && e.hierarchy !== filterEntry.hierarchy) return false;
                if (filterEntry.agent !== undefined && (e.agent || "") !== (filterEntry.agent || "")) return false;
                if (filterEntry.condition !== undefined && (e.condition || "") !== (filterEntry.condition || "")) return false;
                return true;
            };
            let matched = rowsToRender.filter(matchesFilter);
            if (!matched.length) {
                matched = scopeRows.filter(matchesFilter);
            }
            if (!matched.length && filterEntry.hierarchy) {
                matched = [{
                    rate_type: scope,
                    agent_type: filterEntry.agent_type || "Internal",
                    hierarchy: filterEntry.hierarchy || "",
                    agent: filterEntry.agent || "",
                    condition: filterEntry.condition || "",
                    effective_from: ym
                }];
            }
            if (matched.length) {
                rowsToRender = matched;
            }
        }

        editAllRows = rowsToRender
            .filter((e) => !agentFilter || !e.agent_type || e.agent_type === agentFilter)
            .sort((a, b) => (a.agent_type < b.agent_type ? -1 : a.agent_type > b.agent_type ? 1 :
                a.hierarchy < b.hierarchy ? -1 : a.hierarchy > b.hierarchy ? 1 : 0))
            .map((e) => addUnifiedRow(e, ym));

        document.getElementById("entriesCountBadge").textContent =
            `${editAllRows.length} row${editAllRows.length === 1 ? "" : "s"} in ${fmtMonth(ym)}`;

        editPage = 1;
        renderEditPage();
    }

    // One row of the unified table. A row fills in only the columns it needs:
    // a plain rate, an override, a profit-sharing rate, a payout/advance rule —
    // or a rate that carries its own payment condition.
    function addUnifiedRow(e, ym) {
        const tbody = document.getElementById("entriesBody");
        const showRuleCols = currentScope() === "Basic Commission";
        const hide = showRuleCols ? "" : ` style="display:none;"`;
        const tr = document.createElement("tr");
        tr.classList.add("entry-row");
        if (e.id) tr.dataset.id = e.id;
        tr.dataset.remarks = e.remarks || "";
        tr.dataset.condition = e.condition || "";  // preserved (NFP tiers) though not shown
        tr.dataset.label = e.label || "";
        tr.dataset.origKey = rateKeyOf(e);
        tr.dataset.origRoleKey = [e.agent_type || "", e.hierarchy || "", String(e.agent || "").toLowerCase()].join("|");
        tr.dataset.origEff = e.effective_from || ym;
        const dis = isAdmin ? "" : "disabled";
        const num = (v) => escapeHtml(v != null && v !== "" ? v : "");
        tr.innerHTML = `
            <td class="sm-cell"><input type="month" class="e-month" value="${escapeHtml(e.effective_from || ym)}" ${dis}></td>
            <td class="sm-cell"><select class="e-agent-type" ${dis}>${agentTypeOptionsHtml(e.agent_type || "", true)}</select></td>
            <td class="sm-cell"><select class="e-role" ${dis}><option value="">(all)</option>${roleOptionsHtml(e.agent_type || "Internal", e.hierarchy || "")}</select></td>
            <td class="sm-cell"><input type="text" class="e-agent" value="${escapeHtml(e.agent || "")}" placeholder="(all agents)" ${dis}></td>
            <td class="sm-cell"><input type="number" class="e-value" step="0.05" value="${num(e.rate_pct)}" placeholder="e.g. 3.25" ${dis}></td>
            <td class="sm-cell"><input type="number" class="e-override" step="0.05" value="${num(e.override_rate_pct)}" placeholder="—" ${dis}></td>
            <td class="sm-cell"><select class="e-override-from" ${dis}>${overrideFromOptionsHtml(e.agent_type || "Internal", e.override_from || "")}</select></td>
            <td class="sm-cell"><input type="number" class="e-sharing" step="0.05" value="${num(e.profit_sharing_rate_pct)}" placeholder="—" ${dis}></td>
            <td class="sm-cell"><select class="e-sharing-mode" ${dis}>${sharingModeOptionsHtml(e.profit_sharing_mode || "")}</select></td>
            <td class="sm-cell e-prop-cell">${propertyPickerHtml(e.property_type || "")}</td>
            <td class="sm-cell"><input type="number" class="e-trigger" step="1" value="${num(e.trigger_pct)}" placeholder="—" ${dis}></td>
            <td class="sm-cell"><input type="month" class="e-invoice-date" value="${escapeHtml(e.invoice_date_from || "")}" ${dis}>
                <button type="button" class="btn-clear-date" title="Clear — apply to all payment dates" ${dis}>all dates</button></td>
            <td class="sm-cell rule-type-col"${hide}><select class="e-rule-type" ${dis}>${ruleTypeOptionsHtml(e.rule_type || "")}</select></td>
            <td class="sm-cell rule-type-col"${hide}><input type="number" class="e-amount" step="1" value="${num(e.amount_rm)}" placeholder="—" ${dis}></td>
            <td>${isAdmin ? `<button class="btn btn-danger btn-remove-row">✕</button>` : ""}</td>
        `;
        if (tr.dataset.remarks) tr.title = tr.dataset.remarks;
        // Amount (RM) is only meaningful for an Advance row.
        function syncAmountForType() {
            const amt = tr.querySelector(".e-amount");
            const isAdvance = tr.querySelector(".e-rule-type").value === "Advance";
            amt.disabled = !isAdvance || !isAdmin;
            amt.style.background = isAdvance ? "" : "#eef1f5";
            if (!isAdvance) amt.value = "";
        }
        tr.querySelector(".e-rule-type").addEventListener("change", syncAmountForType);
        syncAmountForType();
        // Invoice Date is optional: blank means the row applies to invoices of
        // every date, so give clearing it a one-click affordance.
        const clearDate = tr.querySelector(".btn-clear-date");
        if (clearDate) clearDate.addEventListener("click", () => {
            tr.querySelector(".e-invoice-date").value = "";
        });
        tr.querySelector(".e-agent-type").addEventListener("change", (ev) => {
            const atype = ev.target.value || "Internal";
            const cur = tr.querySelector(".e-role").value;
            tr.querySelector(".e-role").innerHTML =
                `<option value="">(all)</option>` + roleOptionsHtml(atype, cur);
            // Override From lists the same agent type's roles.
            const of = tr.querySelector(".e-override-from");
            of.innerHTML = overrideFromOptionsHtml(atype, of.value);
        });
        const removeBtn = tr.querySelector(".btn-remove-row");
        if (removeBtn) removeBtn.addEventListener("click", () => {
            if (tr.dataset.id) gridDeletedIds.add(String(tr.dataset.id));
            if (tr.dataset.origKey && tr.dataset.origEff) {
                gridDeletedKeys.add(tr.dataset.origKey + "@" + tr.dataset.origEff);
            }
            if (tr.dataset.origRoleKey && tr.dataset.origEff) {
                gridDeletedKeys.add(tr.dataset.origRoleKey + "@" + tr.dataset.origEff);
            }
            tr.remove();
            editAllRows = editAllRows.filter(r => r !== tr);
            renderEditPage();
        });
        initPropertyPicker(tr.querySelector(".e-prop"), isAdmin);
        return tr;
    }

    async function loadEntries() {
        const res = await api("/api/commission-rates");
        const rawRates = await res.json();
        // The roles table's Rate column reads the same rows; redraw it so a
        // rate saved anywhere on this page shows there at once.
        roleRateRows = rawRates;
        if (document.getElementById("rolesBody")) renderRoles();
        loadedRates = rawRates.map((r) => ({
            rate_type: r.rate_type || "Basic Commission",
            agent_type: r.agent_type || "", hierarchy: r.hierarchy || "", agent: r.agent || "",
            label: r.label || "", condition: r.condition || "",
            rate_pct: r.rate_pct || "", override_rate_pct: r.override_rate_pct || "",
            override_from: r.override_from || "",
            profit_sharing_rate_pct: r.profit_sharing_rate_pct || "",
            profit_sharing_mode: r.profit_sharing_mode || "",
            property_type: r.property_type || "", trigger_pct: r.trigger_pct || "",
            job_type: r.job_type || "", ev_type: r.ev_type || "",
            invoice_date_from: r.invoice_date_from || "", rule_type: r.rule_type || "",
            amount_rm: r.amount_rm || "",
            effective_from: r.effective_from, remarks: r.remarks || ""
        }));
        if (document.getElementById("editCard").style.display !== "none") {
            refreshEditScope();
        }
    }

    // The entry of `key` governing `ym`: latest effective_from that is <= ym.
    function governingAt(rows, ym, key) {
        let best = null;
        rows.forEach((e) => {
            if (rateKeyOf(e) !== key) return;
            if (String(e.effective_from) > ym) return;
            if (!best || String(e.effective_from) > String(best.effective_from)) best = e;
        });
        return best;
    }

    // Copy-on-write, per row. Each row carries its OWN Payment Date, so a row
    // can be entered for any month without leaving the current view — the
    // filtered month is only the default. History for months the grid does not
    // touch is preserved untouched.
    function collectEntries() {
        const ym = currentMonth();
        const scope = currentScope();
        const inScope = (e) => (e.rate_type || "Basic Commission") === scope;
        const otherScopes = loadedRates.filter((e) => !inScope(e));
        const scopeRows = loadedRates.filter(inScope);

        const gridRows = [];
        editAllRows.forEach((tr) => {
            const row = readRow(tr);
            // A row with nothing filled in at all is not an entry.
            const hasContent = row.rate_pct || row.override_rate_pct ||
                row.profit_sharing_rate_pct || row.trigger_pct || row.amount_rm;
            if (!hasContent) return;
            const monthEl = tr.querySelector(".e-month");
            row.rate_type = scope;
            row.condition = tr.dataset.condition || "";
            row.label = tr.dataset.label || "";
            row.effective_from = (monthEl && monthEl.value.trim()) || (ym === "all" ? CURRENT_YM : ym);
            row.remarks = tr.dataset.remarks || "edited via data grid";
            gridRows.push({ row, tr });
        });

        if (ym === "all") {
            return otherScopes.concat(gridRows.map((g) => g.row));
        }

        const claimedKeys = new Set(gridDeletedKeys);
        const claimedRoleKeys = new Set();

        gridRows.forEach(({ row, tr }) => {
            claimedKeys.add(rateKeyOf(row) + "@" + row.effective_from);
            const roleKey = [row.agent_type, row.hierarchy, String(row.agent || "").toLowerCase()].join("|") + "@" + row.effective_from;
            claimedRoleKeys.add(roleKey);

            if (tr.dataset && tr.dataset.origKey && tr.dataset.origEff) {
                claimedKeys.add(tr.dataset.origKey + "@" + tr.dataset.origEff);
            }
            if (tr.dataset && tr.dataset.origRoleKey && tr.dataset.origEff) {
                claimedRoleKeys.add(tr.dataset.origRoleKey + "@" + tr.dataset.origEff);
            }
        });

        const result = scopeRows.filter((e) => {
            if (e.effective_from === ym) return false;
            const eKey = rateKeyOf(e) + "@" + e.effective_from;
            if (claimedKeys.has(eKey)) return false;
            const eRoleKey = [e.agent_type, e.hierarchy, String(e.agent || "").toLowerCase()].join("|") + "@" + e.effective_from;
            if (claimedRoleKeys.has(eRoleKey)) return false;
            return true;
        });

        gridRows.forEach(({ row }) => {
            result.push(row);
        });

        return otherScopes.concat(result);
    }

    async function saveEntries() {
        const status = document.getElementById("saveStatus");
        status.className = "save-status";
        status.textContent = "Saving...";
        const entries = collectEntries();
        const res = await api("/api/commission-rates", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ entries })
        });
        if (!res.ok) {
            const d = await res.json().catch(() => ({}));
            status.className = "save-status err";
            status.textContent = d.error || "Save failed";
            return;
        }
        status.className = "save-status ok";
        status.textContent = "Saved. Commission cache cleared — dashboard will recompute.";
        await loadEntries();
        loadPreview();
        setTimeout(() => { exitEditMode(); status.textContent = ""; }, 900);
    }

    function nextMonth(ym) {
        const m = /^(\d{4})-(\d{2})$/.exec(String(ym || "").trim());
        if (!m) return "2026-08";
        let y = parseInt(m[1], 10), mo = parseInt(m[2], 10) + 1;
        if (mo === 13) { mo = 1; y += 1; }
        return `${y}-${String(mo).padStart(2, "0")}`;
    }

    function refreshEditScope(prefill) {
        document.getElementById("editTitle").textContent = currentScope();
        renderRulesHead();
        renderMonth(prefill);
    }

    async function enterEditMode(prefill) {
        document.getElementById("editCard").style.display = "";
        editScope = (prefill && prefill.type) || "Basic Commission";
        await loadEntries();
        document.getElementById("filterAgentType").value = (prefill && prefill.agent_type) || "";
        refreshEditScope(prefill);
        if (prefill && prefill.isAdd) {
            addUnifiedRow({ agent_type: prefill.agent_type || "Internal", hierarchy: "Executive" }, currentMonth());
            const rows = document.querySelectorAll("#entriesBody tr.entry-row");
            if (rows.length > 0) {
                const lastVal = rows[rows.length - 1].querySelector(".e-value");
                if (lastVal) lastVal.focus();
            }
        }
        document.getElementById("editCard").scrollIntoView({ behavior: "smooth", block: "start" });
    }

    function exitEditMode() {
        document.getElementById("editCard").style.display = "none";
    }

    // ── Agent roles & hierarchy ──────────────────────────────────────────────
    // Postgres supplies agent NAMES only — it is how a newly tagged agent shows
    // up here without being typed in. Everything else on a row (Agent Type,
    // Role, Reports To, Branch, IC No, Nick Name, Full Name, effective month)
    // lives in SQLite (loadedRoles) and is entered on this page. Since
    // 2026-09 eeAdmin's tags never prefill, overwrite or flag those fields.

    let loadedRoles = [];
    // Live Postgres agent names (from /api/agent-roles/pg-list): [{bubble_id, name}]
    let pgAgentsCache = [];
    // Saved rows kept out of the grid entirely — tombstones written by the
    // Delete button (hidden:1). A blocked-in-eeAdmin agent is NOT hidden by
    // this page any more: their saved row stays until deleted by hand. It is
    // never
    // rendered into rolesList (every branch of rebuildRolesList() below skips
    // hidden rows on purpose), so they must be written back on every save
    // exactly as loaded: a save replaces the whole table with whatever
    // collectRoles() serializes, which is rolesList plus this list. Without
    // this, a delete's tombstone only survives the ONE save that created it —
    // the very next unrelated save (editing any other row) silently drops it,
    // because by then a reload has already rebuilt rolesList without it. That
    // is the actual mechanism behind "I deleted them and they came back":
    // it was never really about the tombstone failing to write, but about it
    // not surviving the next save that came after.
    let suppressedRoles = [];
    let rolesList = [];
    let rolesPage = 1;
    const ROLES_PAGE_SIZE = 15;
    // The only values Branch may hold — named after the eeAdmin "team-" tags
    // they are derived from. Mirrors _BRANCH_TAG_KEYWORDS in app.py.
    const BRANCHES = ["Team-JB", "Team-Kluang", "Team-Klang", "Team-Seremban"];

    function allRoleNames() {
        return RATE_ROLES["Internal"].concat(RATE_ROLES["Outsource"]);
    }

    /** Sentence-case: "JOHN DOE" → "John Doe" */
    function toSentenceCase(name) {
        if (!name) return name;
        return String(name).trim().split(/\s+/).map((w) =>
            w.charAt(0).toUpperCase() + w.slice(1).toLowerCase()
        ).join(" ");
    }

    /** All saved rows for a Postgres agent — matched by bubble_id (a stable
     * link that survives renaming "Agent Name (from eeAdmin)") when present,
     * falling back to name-matching only for rows saved before pg_bubble_id
     * existed. Matching purely by name meant renaming that field and saving
     * looked broken: the next reload re-derived a fresh row under the
     * original Postgres name (since nothing tied the renamed row back to
     * that person), so the edit appeared to revert or silently duplicate.
     * An agent can also have more than one row on purpose — a role change is
     * a new effective-dated row, same as an override rate change on the
     * Basic Commission Data page — so this must never collapse to one row or
     * a history row silently disappears. */
    function savedRolesForPg(pg) {
        if (!pg) return [];
        if (pg.bubble_id) {
            const byId = loadedRoles.filter((r) => r.pg_bubble_id && r.pg_bubble_id === pg.bubble_id);
            if (byId.length) return byId;
        }
        const key = String(pg.name || "").trim().toLowerCase();
        return loadedRoles.filter((r) => !r.pg_bubble_id && String(r.agent || "").trim().toLowerCase() === key);
    }

    /** A row for an agent Postgres names but nobody has saved anything for.
     *  Only the name comes from Postgres; every other field is blank for an
     *  admin to fill in, and while the effective month is blank the row is
     *  never saved and never reaches a commission calculation. */
    function derivedRoleRow(pg) {
        return {
            agent:          toSentenceCase(pg.name),
            agent_type:     "",
            ic_no:          "",
            nick_name:      "",
            full_name:      "",
            branch:         "",
            effective_from: "",
            needs_date:     true,
            hierarchy:      "",
            reports_to:     "",
            pg_bubble_id:   pg.bubble_id || "",
        };
    }

    function rebuildRolesList() {
        // Rebuilt from scratch each time, alongside rolesList, so a row cannot
        // end up in both lists (shown and re-appended) or in neither (lost).
        // Every hidden row qualifies, not only blocked ones: a plain Delete
        // tombstone (hidden:1, not blocked) needs exactly the same write-back
        // treatment or it vanishes on the next unrelated save.
        suppressedRoles = loadedRoles.filter((r) => r.hidden);
        if (pgAgentsCache.length) {
            rolesList = [];
            pgAgentsCache.forEach((pg) => {
                const savedRows = savedRolesForPg(pg);
                const visibleRows = savedRows.filter((r) => !r.hidden);
                if (!savedRows.length) {
                    // Nobody has entered anything for this person yet — list
                    // the name so their details can be entered by hand.
                    rolesList.push(derivedRoleRow(pg));
                } else if (!visibleRows.length) {
                    // Every saved row for this person is a hidden tombstone —
                    // they were explicitly deleted. Postgres still lists them
                    // (that's exactly why a plain delete kept reappearing), so
                    // skip creating any row at all.
                } else {
                    // One row per saved effective-dated entry — this is what
                    // lets the same agent carry an old Role and a new Role as
                    // two separate rows instead of one overwriting the other.
                    // Every field is exactly what was saved: Postgres has no
                    // say over a row that exists.
                    visibleRows.forEach((r) => {
                        rolesList.push({
                            agent:          toSentenceCase(r.agent),
                            agent_type:     r.agent_type     || "",
                            package_type:   r.package_type   || "",
                            ic_no:          r.ic_no          || "",
                            nick_name:      r.nick_name      || "",
                            full_name:      r.full_name      || "",
                            branch:         r.branch         || "",
                            effective_from: r.effective_from || "",
                            needs_date:     !(r.effective_from || "").trim(),
                            hierarchy:      r.hierarchy      || "",
                            reports_to:     r.reports_to     || "",
                            // Heal legacy rows (saved before this field existed)
                            // by stamping the ID the moment they're re-saved.
                            pg_bubble_id:   r.pg_bubble_id || pg.bubble_id || "",
                        });
                    });
                }
            });
            // Add custom SQLite roles not in Postgres (incl. rows with no eeAdmin
            // agent name at all — e.g. pulled from the static Excel roster and
            // identified only by Full Name / Nick Name). A row already shown
            // above (matched by pg_bubble_id, or by name for un-stamped rows)
            // must not be repeated here just because its (possibly renamed)
            // agent text no longer matches any live Postgres name.
            const pgNames = new Set(pgAgentsCache.map(pg => pg.name.trim().toLowerCase()));
            const pgIds = new Set(pgAgentsCache.map(pg => pg.bubble_id).filter(Boolean));
            loadedRoles.forEach((r) => {
                const hasIdentity = r.agent || r.full_name || r.nick_name;
                const alreadyShown = r.pg_bubble_id
                    ? pgIds.has(r.pg_bubble_id)
                    : (r.agent && pgNames.has(r.agent.trim().toLowerCase()));
                // An agent eeAdmin no longer lists (left, or blocked) keeps
                // their saved rows: earlier months were priced with them, and
                // removing them is a decision for the Delete button, not a tag.
                if (hasIdentity && !alreadyShown && !r.hidden) {
                    rolesList.push({
                        agent:          toSentenceCase(r.agent),
                        agent_type:     r.agent_type     || "",
                        package_type:   r.package_type   || "",
                        ic_no:          r.ic_no          || "",
                        nick_name:      r.nick_name      || "",
                        full_name:      r.full_name      || "",
                        branch:         r.branch         || "",
                        effective_from: r.effective_from || "",
                        needs_date:     !(r.effective_from || "").trim(),
                        hierarchy:      r.hierarchy      || "",
                        reports_to:     r.reports_to     || "",
                        pg_bubble_id:   r.pg_bubble_id   || "",
                    });
                }
            });
        } else {
            rolesList = loadedRoles.filter((r) => !r.hidden).map((r) => ({
                agent:          toSentenceCase(r.agent),
                agent_type:     r.agent_type     || "",
                package_type:   r.package_type   || "",
                ic_no:          r.ic_no          || "",
                nick_name:      r.nick_name      || "",
                full_name:      r.full_name      || "",
                branch:         r.branch         || "",
                effective_from: r.effective_from || "",
                needs_date:     !(r.effective_from || "").trim(),
                hierarchy:      r.hierarchy      || "",
                reports_to:     r.reports_to     || "",
                pg_bubble_id:   r.pg_bubble_id   || "",
            }));
        }
        flagExpiringRoles();
        rolesList.sort((a, b) => String(a.agent || "").localeCompare(String(b.agent || "")));
        
        // Re-populate suggestion datalists. An agent with role-history rows
        // (e.g. an old Senior row and a new Sales Team Manager row) must only
        // suggest once in "Reports To" — not once per row.
        const nameList = [...new Set(rolesList.map(r => r.agent).filter(Boolean))];
        ensureDatalists(nameList);
        // The agent list arrives after the page renders, so the Agent Name
        // filter and the Add/Edit Entry picker have to be refilled here.
        populatePreviewAgentNameFilter();
        populateModalAgentOptions();
    }

    /** Mark agents whose newest row is a range that has run out.
     *
     *  A closed range stops governing after its last month — that is the point
     *  of it — but if nobody adds a follow-up row the agent quietly has no role
     *  at all from then on, and the engine falls back to the old hardcoded name
     *  matching without saying so. Only the newest row per agent is judged: an
     *  earlier range ending is just history, which is exactly what ranges are
     *  for. Open-ended single-month rows can never expire. */
    function flagExpiringRoles() {
        const newest = new Map();
        rolesList.forEach((r) => {
            r.role_expired = false;
            r.role_expiring = false;
            if (r.needs_date || !r.effective_from || r.hidden) return;
            const key = String(r.agent || r.full_name || r.nick_name || "").trim().toLowerCase();
            if (!key) return;
            const prev = newest.get(key);
            if (!prev || effStart(r.effective_from) > effStart(prev.effective_from)) {
                newest.set(key, r);
            }
        });
        const soon = nextYm(CURRENT_YM);
        newest.forEach((r) => {
            const end = effEnd(r.effective_from);
            if (end === OPEN_ENDED) return;
            if (end < CURRENT_YM) r.role_expired = true;
            else if (end <= soon) r.role_expiring = true;
        });
    }

    /**
     * PRIMARY LOADER — calls /api/agent-roles/pg-list, populates pgAgentsCache,
     * updates metric cards and re-renders the table immediately.
     */
    async function loadPgAgents(opts) {
        const quiet = !!(opts && opts.quiet);
        const statusEl = document.getElementById("pgAgentsStatus");
        if (statusEl && !quiet) { statusEl.className = "save-status"; statusEl.textContent = "Loading from Postgres…"; }
        try {
            const res  = await api("/api/agent-roles/pg-list");
            const data = await res.json();
            if (!res.ok) throw new Error(data.error || "Failed to load");
            pgAgentsCache = data.agents || [];
            if (statusEl && !quiet) {
                statusEl.className = "save-status ok";
                statusEl.textContent = `${pgAgentsCache.length} agent${pgAgentsCache.length === 1 ? "" : "s"} loaded from Postgres.`;
                setTimeout(() => { if (statusEl) statusEl.textContent = ""; }, 4000);
            }
            markRolesSynced();
            rebuildRolesList();
            renderRoles();
        } catch (e) {
            // Keep the last good data on a quiet background refresh: blanking a
            // table the user is reading because one poll failed is worse than
            // showing data that is a few minutes old and saying so.
            if (!quiet) pgAgentsCache = [];
            if (statusEl && !quiet) { statusEl.className = "save-status err"; statusEl.textContent = e.message; }
            if (quiet) setRolesSyncNote("last sync failed — showing earlier data");
        }
    }

    // ── Keeping the page current ─────────────────────────────────────────────
    // The pull is sub-second, so there is no cache or scheduler here; the page
    // simply re-asks. What it must never do is re-ask while someone is editing,
    // which would swap the rows out from under them.

    let rolesLastSynced = null;
    let rolesSyncTimer = null;
    const ROLES_SYNC_INTERVAL_MS = 5 * 60 * 1000;

    function markRolesSynced() {
        rolesLastSynced = new Date();
        setRolesSyncNote("");
    }

    function setRolesSyncNote(extra) {
        const el = document.getElementById("rolesSyncedAt");
        if (!el) return;
        if (!rolesLastSynced) { el.textContent = ""; return; }
        const hh = String(rolesLastSynced.getHours()).padStart(2, "0");
        const mm = String(rolesLastSynced.getMinutes()).padStart(2, "0");
        el.textContent = extra ? `Synced ${hh}:${mm} — ${extra}` : `Synced ${hh}:${mm}`;
    }

    /** True while something would be lost by replacing the rows underneath. */
    function rolesEditInProgress() {
        const modal = document.getElementById("agentRoleModal");
        return !!(modal && !modal.classList.contains("hidden"));
    }

    function maybeRefreshRoles() {
        if (document.hidden) return;
        if (rolesEditInProgress()) return;
        // Only when the roles section is the one on screen — a background poll
        // for a section nobody is looking at is pure noise.
        const section = document.querySelector('#dataSectionList li[data-section="roles"]');
        if (section && !section.classList.contains("active")) return;
        loadPgAgents({ quiet: true });
    }

    function startRolesAutoRefresh() {
        if (rolesSyncTimer) return;
        rolesSyncTimer = setInterval(maybeRefreshRoles, ROLES_SYNC_INTERVAL_MS);
        document.addEventListener("visibilitychange", () => {
            if (!document.hidden) maybeRefreshRoles();
        });
        window.addEventListener("focus", maybeRefreshRoles);
    }

    /** Update the Total Agents metric card. */
    /** One agent can legitimately have several rows (a role change is a new
     * effective-dated row, not a new agent) — count people, not rows. Rows
     * with no eeAdmin Agent Name (Excel-only entries) fall back to Full Name /
     * Nick Name as their identity. */
    function countDistinctAgents(rows) {
        const seen = new Set();
        rows.forEach((r) => {
            const key = String(r.agent || r.full_name || r.nick_name || "").trim().toLowerCase();
            if (key) seen.add(key);
        });
        return seen.size;
    }

    function updateRolesMetrics(agents, filteredRows) {
        const setVal = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
        const rowsToCount = filteredRows !== undefined ? filteredRows : agents;
        setVal("rolesTotalCount", countDistinctAgents(rowsToCount));
    }

    /** Load saved Role / Reports-To overrides from SQLite. */
    async function loadRoles() {
        const ratesLoaded = loadRoleRates(false);
        try {
            const res = await api("/api/agent-roles");
            loadedRoles = await res.json();
        } catch (e) {
            loadedRoles = [];
        }
        await ratesLoaded;
        rebuildRolesList();
        renderRoles();
    }

    // ── Rate column of the Agent Roles & Hierarchy table ─────────────────────
    // The Basic Commission rate each role row is paid at, read off the same
    // commission_rates rows the engine uses and picked the way
    // basic_commission_rates._scan_unified_rows picks: Basic Commission, the
    // role's own Package Type (see rolePackageFit), its Agent Type and Role, dated to cover the month, naming this
    // agent or nobody; then the most specific (agent named, then property
    // types named) and, among those, the latest start. Shown for the role's
    // last month up to today.
    let roleRateRows = [];

    async function loadRoleRates(render) {
        try {
            const res = await api("/api/commission-rates");
            roleRateRows = res.ok ? await res.json() : [];
        } catch (e) {
            roleRateRows = [];
        }
        if (render) renderRoles();
    }

    /** How a stored rate row fits a role row's Package Type: -1 not at all,
     *  0 loosely (a Solar rate on a Solar role, or an EV rate for every EV
     *  type on an EV role), 1 exactly (an EV rate for the role's own EV type).
     *  So an EV Charger role never shows a Solar role's rate or overrides. */
    function rolePackageFit(row, r) {
        const want = cardJob(r);
        const job = normJob(row.job_type);
        if (want.job_type === "Solar Services") return job === "Solar Services" ? 0 : -1;
        if (job !== "EV Services") return -1;
        const ev = normEvType(row.ev_type);
        if (want.ev_type && ev === want.ev_type) return 1;
        return ev ? -1 : 0;
    }

    /** {html, title} for a role row's Rate cell. */
    function roleRateCell(r) {
        if (r.needs_date || !String(r.effective_from || "").trim()) return { html: "—", title: "" };
        const { ym, best } = roleRateBest(r);
        return roleRateCellFrom(best, ym);
    }

    /** The rate row that prices each property type for a role row, as at the
     *  month the Rate cell shows: {ym, best: {property: {spec, start, rate,
     *  own, row}}}. Shared by the Rate and Payment Rules cells, so the two
     *  always read the same row. */
    function roleRateBest(r) {
        const [start, end] = splitEffRange(r.effective_from);
        // The month shown: today's, or the role's last month if it has ended,
        // or its first if it has not begun.
        const ym = CURRENT_YM > end ? end : (CURRENT_YM < start ? start : CURRENT_YM);
        const type = String(r.agent_type || "").trim().toLowerCase();
        const role = canonRole(r.hierarchy);
        const who = String(r.agent || "").trim().toLowerCase();
        const props = PROPERTY_TYPES.filter(Boolean);
        const best = {};
        roleRateRows.forEach((row) => {
            if ((row.rate_type || "Basic Commission") !== "Basic Commission") return;
            if (String(row.remarks || "").trim().toLowerCase() === "deleted") return;
            const fit = rolePackageFit(row, r);
            if (fit < 0) return;
            const rate = parseFloat(String(row.rate_pct || "").replace("%", ""));
            if (!Number.isFinite(rate)) return;
            const rowType = String(row.agent_type || "").trim().toLowerCase();
            if (rowType && rowType !== type) return;
            if (row.hierarchy && canonRole(row.hierarchy) !== role) return;
            if (!effectiveCoversMonth(row.effective_from, ym)) return;
            const agents = String(row.agent || "").split(",").map((a) => a.trim().toLowerCase()).filter(Boolean);
            if (agents.length && !agents.includes(who)) return;
            const rowProps = normProp(row.property_type).split(", ").filter(Boolean);
            const spec = (agents.length ? 4 : 0) + (fit ? 2 : 0) + (rowProps.length ? 1 : 0);
            const startYm = effStart(row.effective_from);
            props.forEach((prop) => {
                if (rowProps.length && !rowProps.includes(normProp(prop))) return;
                const cur = best[prop];
                if (!cur || spec > cur.spec || (spec === cur.spec && startYm > cur.start)) {
                    best[prop] = { spec, start: startYm, rate, own: agents.length > 0, row };
                }
            });
        });
        return { ym, best };
    }

    function roleRateCellFrom(best, ym) {
        const props = PROPERTY_TYPES.filter(Boolean);
        // Property types sharing a rate are grouped, "own" marking a group any
        // of whose types is priced by this agent's own rate. The cell shows the
        // everyday types -- Residential, Commercial, Shop Lot -- and the hover
        // lists every type, Factory / NGO / Government projects included.
        const MAIN = ["Residential", "Commercial", "Shop Lot"];
        const group = (list) => {
            const out = [];
            list.forEach((prop) => {
                const b = best[prop];
                if (!b) return;
                const g = out.find((x) => x.rate === b.rate);
                if (g) { g.props.push(prop); g.own = g.own || b.own; }
                else out.push({ rate: b.rate, own: b.own, props: [prop] });
            });
            return out;
        };
        const all = group(props);
        if (!all.length) {
            return { html: `<span style="color:var(--text-muted);">—</span>`,
                title: "No Basic Commission rate on the Data page covers this role; the calculation falls back to its built-in default." };
        }
        const main = group(MAIN);
        const shown = main.length ? main : all;
        const html = shown.length === 1
            ? escapeHtml(`${shown[0].rate}%`)
            : shown.map((g) => `<div style="white-space:nowrap;">${escapeHtml(`${g.rate}%`)} <span style="color:var(--text-muted); font-size:11px;">${escapeHtml(g.props.join(", "))}</span></div>`).join("");
        const title = `Basic Commission rate as at ${fmtMonth(ym)}:\n`
            + all.map((g) => `${g.rate}% — ${g.props.join(", ")}${g.own ? " (this agent's own rate)" : ""}`).join("\n");
        return { html, title };
    }

    /** {html, title} for a role row's Payment Rules cell: the payment stages
     *  on the same rate row the Rate cell reads. A stage stored once per
     *  override line is shown once; the advance comes first. Property types
     *  whose rows pay differently are listed apart, as for the rate. */
    function rolePaymentCell(r) {
        const dash = { html: `<span style="color:var(--text-muted);">—</span>`, title: "" };
        if (r.needs_date || !String(r.effective_from || "").trim()) return dash;
        const { ym, best } = roleRateBest(r);
        const linesOf = (row) => {
            const seen = new Set();
            return parsePaymentRulesFromEntry(row)
                .filter((x) => x.trigger_pct || x.amount_rm)
                .sort((a, b) => parseFloat(a.trigger_pct || 0) - parseFloat(b.trigger_pct || 0))
                .map((x) => x.rule_type === "Advance"
                    ? `Advance${x.amount_rm ? ` RM ${x.amount_rm}` : ""} at ≥${x.trigger_pct}%`
                    : `Pays at ≥${x.trigger_pct}%`)
                .filter((t) => !seen.has(t) && seen.add(t));
        };
        const groupBy = (list) => {
            const out = [];
            list.forEach((prop) => {
                const b = best[prop];
                if (!b) return;
                const lines = linesOf(b.row);
                const key = lines.join("|");
                const g = out.find((x) => x.key === key);
                if (g) g.props.push(prop);
                else out.push({ key, lines, props: [prop] });
            });
            return out;
        };
        const all = groupBy(PROPERTY_TYPES.filter(Boolean));
        if (!all.length) return dash;
        const main = groupBy(["Residential", "Commercial", "Shop Lot"]);
        const shown = main.length ? main : all;
        const block = (g) => g.lines.length
            ? g.lines.map((t) => `<div style="white-space:nowrap;">${escapeHtml(t)}</div>`).join("")
            : `<span style="color:var(--text-muted);">—</span>`;
        const html = shown.length === 1 ? block(shown[0])
            : shown.map((g) => `<div style="margin-bottom:3px;">${block(g)}<span style="color:var(--text-muted); font-size:11px;">${escapeHtml(g.props.join(", "))}</span></div>`).join("");
        const title = `Payment rules as at ${fmtMonth(ym)}:\n`
            + all.map((g) => `${g.lines.join(", ") || "none set"} — ${g.props.join(", ")}`).join("\n");
        return { html, title };
    }

    /** The Override Rate % and From Role on the Basic Commission rate that
     *  covers this role in its month, found the same way roleRateCell finds the
     *  rate: a rate for this agent beats one for the role, and then the latest
     *  start wins. Only rates for the role's own Package Type, as for the
     *  rate. Each override is one
     *  line, in the same order in both columns. */
    function roleOverrideCell(r) {
        const dash = `<span style="color:var(--text-muted);">—</span>`;
        if (r.needs_date || !String(r.effective_from || "").trim()) return { pct: dash, from: dash };
        const [start, end] = splitEffRange(r.effective_from);
        const ym = CURRENT_YM > end ? end : (CURRENT_YM < start ? start : CURRENT_YM);
        const type = String(r.agent_type || "").trim().toLowerCase();
        const role = canonRole(r.hierarchy);
        const who = String(r.agent || "").trim().toLowerCase();
        let top = null;
        let picks = [];
        roleRateRows.forEach((row) => {
            if ((row.rate_type || "Basic Commission") !== "Basic Commission") return;
            if (String(row.remarks || "").trim().toLowerCase() === "deleted") return;
            const fit = rolePackageFit(row, r);
            if (fit < 0) return;
            const pcts = String(row.override_rate_pct || "").split(",").map((s) => s.trim()).filter(Boolean);
            const froms = String(row.override_from || "").split(",").map((s) => s.trim()).filter(Boolean);
            if (!pcts.length && !froms.length) return;
            const rowType = String(row.agent_type || "").trim().toLowerCase();
            if (rowType && rowType !== type) return;
            if (row.hierarchy && canonRole(row.hierarchy) !== role) return;
            if (!effectiveCoversMonth(row.effective_from, ym)) return;
            const agents = String(row.agent || "").split(",").map((a) => a.trim().toLowerCase()).filter(Boolean);
            if (agents.length && !agents.includes(who)) return;
            const spec = (agents.length ? 2 : 0) + (fit ? 1 : 0);
            const startYm = effStart(row.effective_from);
            if (!top || spec > top.spec || (spec === top.spec && startYm > top.start)) {
                top = { spec, start: startYm };
                picks = [];
            }
            if (spec !== top.spec || startYm !== top.start) return;
            // Several overrides on one row pair up by position, as they do in
            // the rate's own Override Rate text; a lone value applies to each.
            const n = Math.max(pcts.length, froms.length);
            for (let i = 0; i < n; i++) {
                const pct = pcts[i] || pcts[0] || "";
                const from = froms[i] || froms[0] || "";
                if (!picks.some((p) => p.pct === pct && p.from === from)) picks.push({ pct, from });
            }
        });
        // Highest-ranked From Role first; each % stays with its From Role.
        const rankOf = overrideRankOf(type);
        picks.sort((a, b) => rankOf(a.from) - rankOf(b.from));
        if (!picks.length) return { pct: dash, from: dash };
        return {
            pct: picks.map((p) => `<div style="white-space:nowrap;">${p.pct ? escapeHtml(`${p.pct}%`) : "—"}</div>`).join(""),
            from: picks.map((p) => `<div style="white-space:nowrap;">${p.from ? escapeHtml(overrideFromLabel(p.from)) : "—"}</div>`).join(""),
        };
    }

    /** True when a row carries any of the amber/red warnings the grid can
     *  show below the Role badge — expired/soon to expire, or a Role that
     *  matches no rate row. An undated row is
     *  excluded on purpose: "⚠ Set effective date" is its own, expected,
     *  one-time housekeeping step (every freshly-tagged agent starts there),
     *  not a staleness signal worth mixing into the same count. */
    function rowNeedsReview(r) {
        if (r.needs_date || !String(r.effective_from || "").trim()) return false;
        return !!(r.role_expired || r.role_expiring
            || !roleBelongsToType(r.hierarchy, r.agent_type));
    }

    function updateRolesReviewBanner() {
        const banner = document.getElementById("rolesReviewBanner");
        const textEl = document.getElementById("rolesReviewBannerText");
        if (!banner || !textEl) return;
        const flagged = rolesList.filter(rowNeedsReview);
        if (!flagged.length) {
            banner.style.display = "none";
            return;
        }
        banner.style.display = "flex";
        textEl.textContent = `⚠ ${flagged.length} agent${flagged.length === 1 ? "" : "s"} `
            + `${flagged.length === 1 ? "has" : "have"} a role warning below —`;
    }

    /** Rows -> one block per person, each block oldest role first.
     *  Keyed like rowsForSamePerson(): the eeAdmin id when a row has one,
     *  otherwise the name. Blocks keep the order their person first appears in,
     *  which is the agent-name order rolesList is already sorted by. */
    function groupRolesByPerson(rows) {
        const keyOf = (r) => r.pg_bubble_id
            ? `id:${r.pg_bubble_id}`
            : `n:${String(r.agent || r.full_name || r.nick_name || "").trim().toLowerCase()}`;
        // An undated row has no start yet, so it sorts after the dated ones.
        const startOf = (r) => (r.needs_date || !String(r.effective_from || "").trim())
            ? "9999-99" : effStart(r.effective_from);
        // Roles over the same months then sit together, and among them the
        // packages run in list order: Solar, EV Charger, EV Installation,
        // EV Charger + EV Installation. A package not on the list goes last.
        const endOf = (r) => (r.needs_date || !String(r.effective_from || "").trim())
            ? "9999-99" : effEnd(r.effective_from);
        const pkgRank = (r) => {
            const i = PACKAGE_TYPES.indexOf(r.package_type || "Solar");
            return i < 0 ? PACKAGE_TYPES.length : i;
        };
        const cmp = (x, y) => (x < y ? -1 : x > y ? 1 : 0);
        const blocks = new Map();
        rows.forEach((r) => {
            const k = keyOf(r);
            if (!blocks.has(k)) blocks.set(k, []);
            blocks.get(k).push(r);
        });
        return [...blocks.values()].map((g) => g.slice().sort((a, b) =>
            cmp(startOf(a), startOf(b)) || cmp(endOf(a), endOf(b)) || (pkgRank(a) - pkgRank(b))));
    }

    // Highest rank first, per agent type (confirmed 2026-10-14). The retired
    // Regional Sales Director and OGM sit at the top of their type.
    const ROLE_RANK_ORDER = {
        "Internal": ["Regional Sales Director", "Senior Branch Director", "Branch Sales Manager",
            "Sales Development Manager", "Sales Team Manager", "Senior Sales Consultant",
            "Sales Consultant", "Sales Senior", "Sales Executive"],
        "Outsource": ["OGM", "OUM", "OSA"]
    };
    const AGENT_TYPE_ORDER = ["Internal", "Outsource"];

    /** Rank of an override's From Role within its agent type: lower is higher.
     *  A From Role not on the list, or none (all roles), comes last. */
    function overrideRankOf(agentType) {
        const list = ROLE_RANK_ORDER[String(agentType || "").toLowerCase() === "outsource" ? "Outsource" : "Internal"];
        // canonRole() turns a name into a key ("salesdevelopmentmanager"), so
        // both sides go through it before they are compared.
        const keys = list.map(canonRole);
        return (from) => {
            const i = keys.indexOf(canonRole(splitOverrideFrom(from).role));
            return i < 0 ? list.length : i;
        };
    }

    /** An override's From Role may name one person in brackets --
     *  "Sales Team Manager (Ng Zhan Yi)": the override comes from that agent
     *  only, not from everyone in the role. Stored with the eeAdmin agent name,
     *  which stays put when a nick name changes. {role, agent}; agent is "" for
     *  a plain role. */
    function splitOverrideFrom(value) {
        const v = String(value || "").trim();
        const m = /^(.*?)\s*\(([^()]+)\)$/.exec(v);
        return m ? { role: m[1].trim(), agent: m[2].trim() } : { role: v, agent: "" };
    }

    /** What a From Role reads as: the person by nick name, as the team knows
     *  them -- "Sales Team Manager (Louis Ng)". */
    function overrideFromLabel(value) {
        const { role, agent } = splitOverrideFrom(value);
        if (!agent) return role;
        const key = agent.toLowerCase();
        const row = rolesList.find((x) => String(x.agent || "").trim().toLowerCase() === key && x.nick_name);
        return `${role} (${(row && row.nick_name) || agent})`;
    }

    /** The people a role card's override can name: everyone of the card's
     *  Agent Type who held one of `roles` at any time in the card's dates,
     *  bar the agent being edited. [{value, label}], highest role first. */
    function overridePersonOptions(p, roles) {
        const me = String(document.getElementById("roleModalAgent")?.value || "").trim().toLowerCase();
        const type = String(p.agent_type || "Internal").toLowerCase();
        const pEnd = p.present || !p.to ? OPEN_ENDED : p.to;
        const rankOf = overrideRankOf(p.agent_type || "Internal");
        const seen = new Set();
        const out = [];
        rolesList.forEach((x) => {
            if (x.needs_date || !String(x.effective_from || "").trim()) return;
            const agent = String(x.agent || "").trim();
            if (!agent || agent.toLowerCase() === me) return;
            if (String(x.agent_type || "").toLowerCase() !== type) return;
            if (!x.hierarchy || !roles.includes(x.hierarchy)) return;
            const [s, e] = splitEffRange(x.effective_from);
            if (p.from && (s > pEnd || e < p.from)) return;
            const value = `${x.hierarchy} (${agent})`;
            if (seen.has(value)) return;
            seen.add(value);
            out.push({ value, label: overrideFromLabel(value) });
        });
        return out.sort((a, b) => (rankOf(a.value) - rankOf(b.value)) || a.label.localeCompare(b.label));
    }

    /** Where a person's block sits: by their latest roles (every dated role
     *  starting in their most recent start month) -- Internal before
     *  Outsource, highest rank first, then by name. Among those latest roles
     *  the highest-ranked one counts, so a Role of "All" on an EV package
     *  never pulls a Branch Sales Manager down. An unknown role or type goes
     *  after the known ones. */
    function personRankKey(group) {
        const dated = group.filter((r) => !r.needs_date && String(r.effective_from || "").trim());
        const latestStart = dated.reduce((m, r) => (effStart(r.effective_from) > m ? effStart(r.effective_from) : m), "");
        const latest = dated.length ? dated.filter((r) => effStart(r.effective_from) === latestStart) : [group[group.length - 1]];
        const keyOf = (r) => {
            const type = AGENT_TYPE_ORDER.includes(r.agent_type) ? r.agent_type : "";
            const typeIdx = type ? AGENT_TYPE_ORDER.indexOf(type) : AGENT_TYPE_ORDER.length;
            const ranks = (ROLE_RANK_ORDER[type] || []).map(canonRole);
            const i = ranks.indexOf(canonRole(r.hierarchy));
            return [typeIdx, i === -1 ? 99 : i];
        };
        const [typeIdx, roleIdx] = latest.map(keyOf)
            .sort((a, b) => (a[1] - b[1]) || (a[0] - b[0]))[0];
        // The name the table shows: Full Name, else the eeAdmin name.
        const fullName = (group.find((r) => String(r.full_name || "").trim()) || {}).full_name;
        const name = String(fullName || group[0].agent || group[0].nick_name || "").trim().toLowerCase();
        return [typeIdx, roleIdx, name];
    }

    function compareByRank(a, b) {
        const ka = personRankKey(a);
        const kb = personRankKey(b);
        if (ka[0] !== kb[0]) return ka[0] - kb[0];
        if (ka[1] !== kb[1]) return ka[1] - kb[1];
        return ka[2].localeCompare(kb[2]);
    }

    /**
     * Render the table.
     */
    function renderRoles() {
        const tbody      = document.getElementById("rolesBody");
        tbody.innerHTML  = "";

        const filterEl   = document.getElementById("rolesAgentTypeFilter");
        const filterType = filterEl ? filterEl.value.toLowerCase() : "";
        const searchEl   = document.getElementById("rolesSearchInput");
        const searchQuery = searchEl ? searchEl.value.toLowerCase().trim() : "";
        const reviewOnlyEl = document.getElementById("rolesNeedsReviewFilter");
        const reviewOnly  = !!(reviewOnlyEl && reviewOnlyEl.checked);
        const monthEl    = document.getElementById("rolesMonthFilter");
        const monthQuery = monthEl ? String(monthEl.value || "").trim() : "";
        const monthClearBtn = document.getElementById("rolesMonthClearBtn");
        if (monthClearBtn) monthClearBtn.style.display = monthQuery ? "inline-flex" : "none";

        updateRolesReviewBanner();
        renderExcludedRoles();

        let filtered = rolesList;
        if (monthQuery) {
            filtered = filtered.filter(r => effectiveCoversMonth(r.effective_from, monthQuery));
        }
        if (filterType) {
            filtered = filtered.filter(r => String(r.agent_type || "").toLowerCase().includes(filterType));
        }
        if (searchQuery) {
            filtered = filtered.filter(r =>
                String(r.agent || "").toLowerCase().includes(searchQuery) ||
                String(r.full_name || "").toLowerCase().includes(searchQuery) ||
                String(r.nick_name || "").toLowerCase().includes(searchQuery)
            );
        }
        if (reviewOnly) {
            filtered = filtered.filter(rowNeedsReview);
        }

        // One table entry per person, not per saved row: an agent who held
        // role A and then role B is a single agent, so their roles read as one
        // block. The saved data is untouched -- each role is still its own
        // agent_roles row; only the grid groups them. Filtering above stays
        // row-level, so a month or type filter shows just the roles that match.
        const groups = groupRolesByPerson(filtered).sort(compareByRank);
        const total = groups.length;
        const totalPages = Math.max(1, Math.ceil(total / ROLES_PAGE_SIZE));
        rolesPage = Math.max(1, Math.min(rolesPage, totalPages));

        const start = (rolesPage - 1) * ROLES_PAGE_SIZE;
        const pageGroups = groups.slice(start, start + ROLES_PAGE_SIZE);

        pageGroups.forEach((group) => {
            const span = group.length;
            // The person's own fields, from whichever role has them filled in.
            const first = group[0];
            const shared = (k) => first[k] || (group.find((x) => x[k]) || {})[k] || "";
            const groupTrs = [];

            group.forEach((r, gi) => {
                const tr = document.createElement("tr");
                tr.className = "role-row";
                // A row that only carries a Postgres name has no effective month
                // until somebody enters one. Until then the row is never written
                // to the database and every commission lookup skips it, so it has
                // to read as an open to-do, not as a date.
                const needsDate = !!r.needs_date || !String(r.effective_from || "").trim();
                // A range that has run out (or is about to) on the agent's newest
                // row leaves them with no role from then on, so it has to be
                // visible here rather than only showing up as a wrong payout.
                const expiryHtml = r.role_expired
                    ? `<div style="margin-top:3px; font-size:10px; color:#b91c1c; font-weight:600;" title="This is this agent's newest row and its range ended in ${escapeHtml(fmtMonth(effEnd(r.effective_from)))}. From the month after that they have no role here at all, and the calculation falls back to the old hardcoded name matching. Add a follow-up role to cover the later months.">⚠ ended — no role after ${escapeHtml(fmtMonth(effEnd(r.effective_from)))}</div>`
                    : (r.role_expiring
                        ? `<div style="margin-top:3px; font-size:10px; color:#b45309;" title="This is this agent's newest row and its range ends soon. Add a follow-up role before then, or the agent drops back to the old hardcoded name matching.">ends ${escapeHtml(fmtMonth(effEnd(r.effective_from)))} — needs a follow-up role</div>`
                        : "");
                // "To Present" is stored as a bare start month, so the cell would
                // otherwise read "Jun 2026" -- indistinguishable from a row that
                // applies to that one month only. Spelling out "→ Present" makes
                // the open end visible, matching the Present tick in the popup;
                // closed ranges already print both ends and need nothing added.
                // (Open-ended rows can never be expiring, so this never collides
                // with expiryHtml -- see flagExpiringRoles().)
                const isOpenEnded = effEnd(r.effective_from) === OPEN_ENDED;
                const presentHtml = isOpenEnded
                    ? ` <span style="color:#047857; font-weight:600;" title="Present — this role applies from ${escapeHtml(fmtMonth(effStart(r.effective_from)))} onwards, including every future month. Untick Present in the popup to set an end month.">→ Present</span>`
                    : "";
                const monthCell = needsDate
                    ? `<span title="Only this agent's name came from eeAdmin. Nothing is saved and no commission calculation uses this row until you set the month and fill in the details here." style="color:#b45309; font-weight:600;">⚠ Set effective date</span>`
                    : escapeHtml(fmtMonth(r.effective_from)) + presentHtml + expiryHtml;
                // "Sales" is stored on rows seeded before this was tightened. It names
                // no rate table, so RATE_ROLES quietly priced it as Internal — shown as
                // unresolved rather than rewritten, since changing a saved type would
                // reprice periods that already paid out.
                const displayAtype = r.agent_type === "Sales"
                    ? "⚠ Sales (needs review)"
                    : (r.agent_type || "—");
                const displayRole = r.hierarchy || "—";
                const rateCell = roleRateCell(r);
                const payCell = rolePaymentCell(r);
                const ovrCell = roleOverrideCell(r);
                // The Role cell is shaded by Agent Type: Outsource light blue,
                // Internal light purple; anything else stays plain.
                const roleCellBg = r.agent_type === "Outsource" ? "#e0f2fe"
                    : r.agent_type === "Internal" ? "#ede9fe" : "";
                const displayBranch = r.branch || "—";
                // A cell spanning each run of this person's consecutive roles
                // that share `keyOf`, the way the name cell spans the whole
                // person; "" on the rows a span above already covers. A change
                // of value starts a new cell, so every change stays visible.
                const runCell = (keyOf, inner, style) => {
                    if (gi > 0 && keyOf(group[gi - 1]) === keyOf(r)) return "";
                    let run = 1;
                    while (gi + run < span && keyOf(group[gi + run]) === keyOf(r)) run++;
                    return `<td class="sm-cell"${run > 1 ? ` rowspan="${run}"` : ""}${style ? ` style="${style}"` : ""}>${inner}</td>`;
                };
                const branchCell = runCell((x) => x.branch || "—", escapeHtml(displayBranch));
                // Invoice Date and Agent Type merge the same way. An undated row
                // never merges: each one is its own to-do. Its expiry note is
                // part of the key, so a flagged range is never hidden in a span.
                const monthKey = (x) => (x.needs_date || !String(x.effective_from || "").trim())
                    ? `undated:${group.indexOf(x)}`
                    : `${x.effective_from}|${x.role_expired ? 1 : 0}${x.role_expiring ? 1 : 0}`;
                const monthTd = runCell(monthKey, monthCell);
                const atypeTd = runCell((x) => x.agent_type || "", escapeHtml(displayAtype));

                // Kept, not deleted: the row holds an effective date somebody
                // entered, and past months were priced with it.
                // A role from the other agent type matches no rate row, so the
                // agent quietly falls through to the hardcoded default. Loud on
                // purpose — this is a wrong number, not untidy data.
                const mismatchHtml = !roleBelongsToType(r.hierarchy, r.agent_type)
                    ? `<div style="margin-top:3px; font-size:10px; color:#b91c1c; font-weight:600;" title="${escapeHtml(r.hierarchy)} is a ${r.agent_type === "Internal" ? "Outsource" : "Internal"} role but this row is ${escapeHtml(r.agent_type || "—")}. No rate row matches that combination, so this agent falls back to the hardcoded default rate. Fix the Role or the Agent Type.">⚠ not a ${escapeHtml(r.agent_type || "—")} role — no rate matches</div>`
                    : "";
                // The Role cell spans each run of this person's consecutive
                // roles with the same Role and Agent Type (its shading and
                // warning follow both), the way Branch does.
                const roleCell = runCell((x) => `${x.agent_type || ""}|${x.hierarchy || "—"}`,
                    escapeHtml(displayRole) + mismatchHtml, roleCellBg ? `background:${roleCellBg};` : "");

                // Person-level cells are drawn once, on the block's first line,
                // spanning every role the person has. Agent Name (from eeAdmin),
                // Nick Name and IC No. are kept off the table (they stay in the
                // Edit popup; the names also in the search); with no Full Name
                // entered, the eeAdmin name stands in, greyed, so no row is left
                // nameless.
                const fullName = shared("full_name");
                const nameCell = fullName
                    ? escapeHtml(fullName)
                    : (first.agent
                        ? `<span style="color:var(--text-muted);" title="No Full Name entered — showing the eeAdmin name. Add the Full Name with Edit.">${escapeHtml(first.agent)}</span>`
                        : "—");
                const personCells = gi === 0 ? `
                    <td class="sm-cell" rowspan="${span}" style="font-weight:600;">${nameCell}</td>` : "";
                const editCell = gi === 0 ? `
                    <td rowspan="${span}" style="white-space:nowrap;">
                        ${isAdmin ? `<button class="btn btn-secondary row-edit-btn" title="${span > 1 ? `Edit ${span} roles` : "Edit agent details"}" style="padding:4px 8px; font-size:12px;">✏️ Edit</button>` : ""}
                    </td>` : "";
                tr.innerHTML = `
                    ${personCells}
                    ${monthTd}
                    ${atypeTd}
                    ${roleCell}
                    <td class="sm-cell">${escapeHtml(r.package_type || "Solar")}</td>
                    <td class="sm-cell" title="${escapeHtml(rateCell.title)}">${rateCell.html}</td>
                    <td class="sm-cell" title="${escapeHtml(payCell.title)}">${payCell.html}</td>
                    <td class="sm-cell">${ovrCell.pct}</td>
                    <td class="sm-cell">${ovrCell.from}</td>
                    ${branchCell}
                    ${editCell}
                `;

                if (gi === 0) {
                    // Opens the whole person; the newest role is the one the
                    // modal highlights, since that is the one in force today.
                    tr.querySelector(".row-edit-btn")?.addEventListener("click", () => {
                        openAgentRoleModal(group[group.length - 1]);
                    });
                }

                tbody.appendChild(tr);
                groupTrs.push(tr);
            });

            // A black outline round each person's block, so two neighbours do
            // not read as one agent with several roles. Drawn on the cells (a
            // border on a row does not show in this table): the first row's
            // top, the bottom of every cell that reaches the last row --
            // merged Role and Branch cells included -- and the outer sides.
            const edge = "2px solid #111111";
            groupTrs.forEach((tr, i) => {
                const cells = [...tr.cells];
                cells.forEach((td) => {
                    if (i === 0) td.style.borderTop = edge;
                    if (i + (td.rowSpan || 1) >= span) td.style.borderBottom = edge;
                });
            });
            const firstCells = groupTrs[0] ? [...groupTrs[0].cells] : [];
            if (firstCells.length) {
                firstCells[0].style.borderLeft = edge;
                firstCells[firstCells.length - 1].style.borderRight = edge;
            }
        });

        const isFiltered = !!(filterType || searchQuery);
        const totalAgentCount = countDistinctAgents(rolesList);
        document.getElementById("rolesCount").textContent = isFiltered
            ? `${countDistinctAgents(filtered)} of ${totalAgentCount} agent${totalAgentCount === 1 ? "" : "s"} (filtered)`
            : `${totalAgentCount} agent${totalAgentCount === 1 ? "" : "s"}`;

        if (!filtered.length) {
            const msg = !rolesList.length
                ? `Loading agents from Postgres… click "🔄 Refresh from Postgres" if nothing appears.`
                : `No agents match the current filter.`;
            tbody.innerHTML = `<tr><td colspan="11" style="color:var(--text-muted); text-align:center;">${msg}</td></tr>`;
        }

        // Render pagination controls
        const paginationEl = document.getElementById("rolesPagination");
        if (paginationEl) {
            if (!filtered.length) {
                paginationEl.innerHTML = "";
            } else {
                const info = `<span style="font-size:13px; color:var(--text-muted); margin: 0 10px;">Page ${rolesPage} of ${totalPages} (${total} agent${total === 1 ? "" : "s"})</span>`;
                if (totalPages <= 1) {
                    paginationEl.innerHTML = info;
                } else {
                    const prev = `<button class="btn btn-secondary" id="rolesPrevBtn" ${rolesPage === 1 ? "disabled" : ""} style="padding:3px 10px; font-size:12px;">‹ Prev</button>`;
                    const next = `<button class="btn btn-secondary" id="rolesNextBtn" ${rolesPage === totalPages ? "disabled" : ""} style="padding:3px 10px; font-size:12px;">Next ›</button>`;
                    paginationEl.innerHTML = prev + info + next;
                    paginationEl.querySelector("#rolesPrevBtn")?.addEventListener("click", () => { rolesPage--; renderRoles(); });
                    paginationEl.querySelector("#rolesNextBtn")?.addEventListener("click", () => { rolesPage++; renderRoles(); });
                }
            }
        }

        updateRolesMetrics(rolesList, isFiltered ? filtered : rolesList);
    }

    // Reports To suggests existing agent names; Branch suggests known branches.
    function ensureDatalists(names) {
        let dl = document.getElementById("agentNameList");
        if (!dl) {
            dl = document.createElement("datalist");
            dl.id = "agentNameList";
            document.body.appendChild(dl);
        }
        dl.innerHTML = names.map((n) => `<option value="${escapeHtml(n)}"></option>`).join("");
        let bl = document.getElementById("branchList");
        if (!bl) {
            bl = document.createElement("datalist");
            bl.id = "branchList";
            document.body.appendChild(bl);
            bl.innerHTML = BRANCHES.map((b) => `<option value="${escapeHtml(b)}"></option>`).join("");
        }
    }

    let editingAgentObj = null;

    // ── eeAdmin name search (Add Agent only) ─────────────────────────────────
    // The hierarchy list itself comes from /api/agent-roles/pg-list, which
    // keeps only people tagged internal/outsource/sales. Someone whose
    // agent_type was never set in eeAdmin is therefore invisible there — and
    // is exactly who you open this modal to add. So the search below calls
    // /api/agent-roles/pg-search, which drops that tag filter, and remembers
    // the eeAdmin id of whatever you pick so the saved row links to the real
    // record instead of being matched by name forever after.

    // { bubble_id, name } of the search hit currently chosen, or null.
    let pickedPgAgent = null;
    let agentSearchTimer = null;
    let agentSearchSeq = 0;

    /** Coloured one-liner under the Agent Name box; "" hides it. */
    function setAgentSearchStatus(text, tone) {
        const el = document.getElementById("roleModalAgentStatus");
        if (!el) return;
        el.textContent = text || "";
        el.style.display = text ? "" : "none";
        el.style.color = tone === "ok" ? "var(--success-color, #15803d)"
            : tone === "warn" ? "#b45309"
            : "var(--text-muted)";
    }

    function hideAgentSuggest() {
        const box = document.getElementById("roleModalAgentSuggest");
        if (box) { box.style.display = "none"; box.innerHTML = ""; }
    }

    /** Put the Agent Name box into edit-a-row mode (locked, greyed) or
     *  add-a-row mode (typeable, searches eeAdmin). */
    function setAgentNameMode(editable) {
        const input = document.getElementById("roleModalAgent");
        if (!input) return;
        input.readOnly = !editable;
        input.style.background = editable ? "" : "var(--bg-muted, #f1f3f5)";
        input.style.color = editable ? "" : "var(--text-muted)";
        input.style.cursor = editable ? "" : "not-allowed";
        input.placeholder = editable ? "Type a name to search eeAdmin…" : "";
        input.title = editable
            ? "Type at least 2 letters to search every eeAdmin record, including people with no agent type set."
            : "Fixed — sourced from eeAdmin (Postgres) and cannot be edited here. Use Full Name / Nick Name to customise what's displayed.";
        hideAgentSuggest();
        setAgentSearchStatus(editable
            ? "Type at least 2 letters to search eeAdmin. Untagged people are included."
            : "");
    }

    function renderAgentSuggest(list, term) {
        const box = document.getElementById("roleModalAgentSuggest");
        if (!box) return;
        if (!list.length) {
            hideAgentSuggest();
            setAgentSearchStatus(`No eeAdmin record matches "${term}". You can still save this as a manual row.`, "warn");
            return;
        }
        box.innerHTML = list.map((a, i) => {
            const type = a.agent_type ? a.agent_type : "no agent type set";
            return `<div class="pg-suggest-item" data-idx="${i}" style="padding:7px 12px; cursor:pointer; font-size:13px; line-height:1.35;">
                        <div style="font-weight:600;">${escapeHtml(a.name)}</div>
                        <div style="font-size:11px; color:var(--text-muted);">${escapeHtml(type)}</div>
                    </div>`;
        }).join("");
        box.querySelectorAll(".pg-suggest-item").forEach((el) => {
            el.addEventListener("mouseenter", () => { el.style.background = "var(--bg-hover, #f1f3f5)"; });
            el.addEventListener("mouseleave", () => { el.style.background = ""; });
            el.addEventListener("mousedown", (ev) => {
                // mousedown, not click: the input's blur handler closes this
                // list, and blur fires first on a click.
                ev.preventDefault();
                choosePgAgent(list[Number(el.dataset.idx)]);
            });
        });
        box.style.display = "";
        setAgentSearchStatus(`${list.length} match${list.length === 1 ? "" : "es"} in eeAdmin — pick one.`);
    }

    function choosePgAgent(a) {
        pickedPgAgent = { bubble_id: a.bubble_id || "", name: a.name || "" };
        const input = document.getElementById("roleModalAgent");
        if (input) input.value = a.name || "";
        hideAgentSuggest();
        setAgentSearchStatus(
            a.agent_type
                ? `Linked to eeAdmin (${a.agent_type}).`
                : "Linked to eeAdmin. This person has no agent type there, so pick one below.",
            "ok");
    }

    /** One search. Returns [] and shows the reason when the lookup fails —
     *  a dead proxy must not look like "this person does not exist". */
    async function searchPgAgents(term) {
        const mySeq = ++agentSearchSeq;
        try {
            const res = await api(`/api/agent-roles/pg-search?q=${encodeURIComponent(term)}`);
            const data = await res.json();
            if (mySeq !== agentSearchSeq) return null;  // a newer keystroke won
            if (!res.ok) {
                setAgentSearchStatus(data.error || "eeAdmin search failed.", "warn");
                return null;
            }
            return data.agents || [];
        } catch (e) {
            if (mySeq !== agentSearchSeq) return null;
            setAgentSearchStatus(`eeAdmin search failed: ${e.message}`, "warn");
            return null;
        }
    }

    document.getElementById("roleModalAgent")?.addEventListener("input", () => {
        const input = document.getElementById("roleModalAgent");
        if (!input || input.readOnly) return;
        // Editing the text after picking someone means the link no longer
        // describes what is in the box.
        if (pickedPgAgent && input.value.trim().toLowerCase() !== pickedPgAgent.name.toLowerCase()) {
            pickedPgAgent = null;
        }
        const term = input.value.trim();
        clearTimeout(agentSearchTimer);
        if (term.length < 2) {
            hideAgentSuggest();
            setAgentSearchStatus("Type at least 2 letters to search eeAdmin. Untagged people are included.");
            return;
        }
        setAgentSearchStatus("Searching eeAdmin…");
        agentSearchTimer = setTimeout(async () => {
            const list = await searchPgAgents(term);
            if (list) renderAgentSuggest(list, term);
        }, 300);
    });

    document.getElementById("roleModalAgent")?.addEventListener("blur", () => {
        setTimeout(hideAgentSuggest, 150);
    });


    // ── Roles over time ──────────────────────────────────────────────────────
    // One agent can hold several roles in turn (role A Jan–Mar, role B Apr–Jun).
    // Each is its own agent_roles row -- Invoice Date range, Agent Type, Role,
    // Reports To and Branch together -- and this modal edits all of a person's
    // rows at once. Name, Full Name, Nick Name and IC No. belong to the person,
    // so they are typed once and written onto every row on save.

    let rolePeriods = [];     // working copies, one per card; `src` is the rolesList row behind it
    let editingGroup = [];    // the rolesList rows this person had when the modal opened
    let rolePeriodUid = 0;
    let focusPeriodUid = null;

    /** "2026-04" -> "2026-03" */
    function prevYm(ym) {
        const m = /^(\d{4})-(\d{2})$/.exec(String(ym || "").trim());
        if (!m) return ym;
        let y = parseInt(m[1], 10);
        let mo = parseInt(m[2], 10) - 1;
        if (mo < 1) { mo = 12; y -= 1; }
        return `${y}-${String(mo).padStart(2, "0")}`;
    }

    /** Every row saved for the same person as `r`: by eeAdmin id when it has
     *  one, otherwise by name -- the same key savedRolesForPg() falls back to. */
    function rowsForSamePerson(r) {
        if (!r) return [];
        const nameKey = (x) => String(x.agent || x.full_name || x.nick_name || "").trim().toLowerCase();
        const rows = r.pg_bubble_id
            ? rolesList.filter((x) => x.pg_bubble_id === r.pg_bubble_id)
            : rolesList.filter((x) => !x.pg_bubble_id && nameKey(x) === nameKey(r));
        if (!rows.includes(r)) rows.push(r);
        return rows;
    }

    function periodFromRow(r) {
        const needsDate = !!r.needs_date || !String(r.effective_from || "").trim();
        const [from, to] = splitEffRange(needsDate ? "" : r.effective_from);
        const open = needsDate || to === OPEN_ENDED;
        return {
            uid: ++rolePeriodUid, src: r,
            from: from || "", to: open ? "" : to, present: open,
            package_type: r.package_type || "Solar",
            agent_type: r.agent_type || "", hierarchy: r.hierarchy || "",
            reports_to: r.reports_to || "", branch: r.branch || "",
            rates: [], oRules: [],
        };
    }

    function compareFrom(a, b) {
        // Undated cards sort last so a half-filled one stays at the bottom.
        if (!a.from && !b.from) return 0;
        if (!a.from) return 1;
        if (!b.from) return -1;
        return a.from < b.from ? -1 : a.from > b.from ? 1 : 0;
    }

    /** Edit Agent Details lists the Solar roles first, then the EV ones, each
     *  group by start month; EV roles starting together run in package list
     *  order (EV Charger, EV Installation, EV Charger + EV Installation). */
    function compareSolarThenEv(a, b) {
        const isEv = (p) => (p.package_type || "Solar") !== "Solar";
        const rank = (p) => {
            const i = PACKAGE_TYPES.indexOf(p.package_type || "Solar");
            return i < 0 ? PACKAGE_TYPES.length : i;
        };
        return (isEv(a) - isEv(b)) || compareFrom(a, b) || (rank(a) - rank(b));
    }

    /** Keep the ranges from colliding after `changed` was added or its From
     *  moved: an open-ended role of the same Package Type that began earlier
     *  is closed the month before `changed` starts, so an invoice dated in the
     *  earlier months still reads the earlier role. Any role may be ticked
     *  Present, so ticking it never closes another role and `changed` itself
     *  is never unticked; overlaps that leaves are kept as they are.
     *  Only runs on an edit -- merely opening an agent never rewrites ranges. */
    function autoCloseAround(changed) {
        if (!changed || !changed.from) return;
        const pkg = (x) => x.package_type || "Solar";
        rolePeriods.forEach((p) => {
            if (p === changed || !p.present || !p.from || pkg(p) !== pkg(changed)) return;
            if (p.from < changed.from) {
                p.present = false;
                p.to = prevYm(changed.from);
            }
        });
    }

    function addRolePeriod() {
        syncRolePeriodsFromDom();
        const latest = rolePeriods.filter((p) => p.from).sort(compareFrom).pop();
        // This month, unless the latest role already starts then or later --
        // the new unit must never begin on top of the one before it.
        let from = CURRENT_YM;
        if (latest && latest.from >= from) from = nextYm(latest.from);
        const p = {
            uid: ++rolePeriodUid, src: null, from, to: "", present: true,
            package_type: latest ? latest.package_type : "Solar",
            agent_type: latest ? latest.agent_type : "",
            hierarchy: latest ? latest.hierarchy : "",
            reports_to: latest ? latest.reports_to : "",
            branch: latest ? latest.branch : "",
            rates: [], oRules: [],
        };
        rolePeriods.push(p);
        autoCloseAround(p);
        focusPeriodUid = p.uid;
        renderRolePeriods();
    }

    /** Copy what is typed in the cards back into rolePeriods, so a field that
     *  has not blurred yet (Reports To, mid-typing) is still saved. */
    function syncRolePeriodsFromDom() {
        document.querySelectorAll("#roleModalPeriods [data-uid]").forEach((card) => {
            const p = rolePeriods.find((x) => x.uid === Number(card.dataset.uid));
            if (!p) return;
            card.querySelectorAll("[data-f]").forEach((el) => {
                const f = el.dataset.f;
                if (f === "present") p.present = el.checked;
                else p[f] = String(el.value || "").trim();
            });
            if (p.present) p.to = "";
            syncOwnRatesFromCard(card, p);
        });
    }

    /** {overlaps, gaps} as readable sentences, from the ranges as they stand. */
    function rolePeriodIssues() {
        const list = rolePeriods
            .filter((p) => p.from && (p.present || (p.to && p.to >= p.from)))
            .map((p) => ({ p, s: p.from, e: p.present ? OPEN_ENDED : p.to }))
            .sort((a, b) => (a.s < b.s ? -1 : a.s > b.s ? 1 : 0));
        const name = (x) => `${x.p.hierarchy || "(no role)"} (${fmtMonth(x.s)} to ${x.e === OPEN_ENDED ? "Present" : fmtMonth(x.e)})`;
        const overlaps = [];
        const gaps = [];
        for (let i = 0; i < list.length; i++) {
            for (let j = i + 1; j < list.length; j++) {
                // Two roles starting the same month (e.g. Jul to Sep beside Jul
                // to Present) are allowed side by side, whatever their ends;
                // only roles starting in different months warn when they overlap.
                if (list[j].s === list[i].s) continue;
                if (list[j].s <= list[i].e) overlaps.push(`${name(list[i])} overlaps ${name(list[j])}`);
            }
        }
        let reach = list.length ? list[0].e : "";
        for (let k = 1; k < list.length; k++) {
            if (reach !== OPEN_ENDED && list[k].s > nextYm(reach)) {
                const a = nextYm(reach);
                const b = prevYm(list[k].s);
                gaps.push(a === b ? `no role in ${fmtMonth(a)}` : `no role from ${fmtMonth(a)} to ${fmtMonth(b)}`);
            }
            if (list[k].e > reach) reach = list[k].e;
        }
        return { overlaps, gaps };
    }

    function updateRolePeriodWarnings() {
        const el = document.getElementById("roleModalPeriodWarnings");
        if (!el) return;
        // Overlapping roles are not flagged (user, 2026-10-20): an agent
        // routinely holds roles side by side. Only gaps are.
        const { gaps } = rolePeriodIssues();
        const lines = gaps.map((t) => `⚠ Gap: ${t}`);
        el.style.display = lines.length ? "" : "none";
        el.innerHTML = lines.map(escapeHtml).join("<br>");
    }

    /** The roles as a table. A role takes one row per rate of its own (at least
     *  one row); its cells -- Invoice Date, Agent Type, Role, Branch, Reports
     *  To -- span those rows, and each row holds one rate: Override Rate % and
     *  From Role, Rate %, Property and Payment Rules. Every row carries its
     *  role's data-uid, and a rate row also its rate's data-rate, so
     *  syncRolePeriodsFromDom() and the handlers below find both from any cell. */
    function renderRolePeriods() {
        const host = document.getElementById("roleModalPeriods");
        if (!host) return;
        const cell = "padding:5px 5px; border-bottom:1px solid #e2e8f0; vertical-align:top; white-space:nowrap;";
        const ctl = "padding:5px 7px; font-size:12.5px; border:1px solid var(--border-color); border-radius:6px; box-sizing:border-box;";
        const th = "padding:6px 5px; text-align:left; font-size:11.5px; font-weight:600; color:var(--text-muted); border-bottom:1px solid #cbd5e1; vertical-align:bottom; line-height:1.25;";
        const tiny = "padding:2px 7px; font-size:11px;";
        const muted = "font-size:11.5px; color:var(--text-muted);";
        const opt = (v, cur, text) => `<option value="${escapeHtml(v)}"${v === cur ? " selected" : ""}>${escapeHtml(text === undefined ? v : text)}</option>`;
        const ready = !agentRateLoad.loading && !agentRateLoad.error;
        const propList = PROPERTY_TYPES.filter(Boolean);

        const lineCells = (p, line, j) => {
            // An override pair and a payment stage are always on show, blank
            // when there is none, so each column can be typed into directly.
            if (!line.oRules.length) line.oRules.push({ override_rate_pct: "", override_from: "" });
            if (!line.pRules.length) line.pRules.push({ trigger_pct: "", rule_type: "Payout", amount_rm: "" });
            // Shown once, on the first rate of the role: a repeat shows "as above".
            const overrideOnly = isOverrideOnlyLine(p, j);
            const roleOpts = getOverrideRolesForAgentType(p.agent_type || "Internal");
            // Each override is one row of both columns, so they line up. "+"
            // adds another, as with payment stages; "✕" drops one but the last.
            const last = line.oRules.length - 1;
            const oPct = line.oRules.map((o, k) => `
                <div data-orule="${k}" style="height:30px; display:flex; align-items:center; gap:3px; white-space:nowrap;">
                    <input type="number" data-o="pct" step="0.05" value="${escapeHtml(o.override_rate_pct || "")}" placeholder="%" style="${ctl} width:48px;">
                    ${line.oRules.length > 1 ? `<button type="button" class="btn btn-secondary" data-act="rm-orule" data-k="${k}" style="${tiny}" title="Remove this override">✕</button>` : ""}
                    ${k === last ? `<button type="button" class="btn btn-secondary" data-act="add-orule" style="${tiny}" title="Add another override, e.g. from a different role">+</button>` : ""}
                </div>`).join("");
            // Below the roles, one choice per person who held one of them in
            // this role's dates: the override then comes from that agent only.
            const people = overridePersonOptions(p, roleOpts.filter(Boolean));
            const oFrom = line.oRules.map((o, k) => {
                const from = o.override_from || "";
                const known = roleOpts.includes(from) || people.some((x) => x.value === from);
                const roleList = known ? roleOpts : roleOpts.concat([from]);
                const personHtml = people.length
                    ? `<optgroup label="A specific person">${people.map((x) => opt(x.value, from, x.label)).join("")}</optgroup>`
                    : "";
                // A person's name needs the room; a plain role keeps the narrow box.
                const w = splitOverrideFrom(from).agent ? 176 : 104;
                return `
                <div data-orule="${k}" style="height:30px; display:flex; align-items:center; gap:3px; white-space:nowrap;">
                    <select data-o="from" title="${escapeHtml(overrideFromLabel(from) || "(all roles)")}" style="${ctl} width:${w}px;">${roleList.map((x) => opt(x, from, x ? overrideFromLabel(x) : "(all roles)")).join("")}${personHtml}</select>
                </div>`;
            }).join("");
            const props = propList.concat(line.property.filter((x) => !propList.includes(x)));
            const stages = line.pRules.map((r, k) => `
                <div data-prule="${k}" style="height:30px; display:flex; align-items:center; gap:3px; white-space:nowrap;">
                    <span style="${muted}">≥</span>
                    <input type="number" data-p="trigger_pct" step="1" value="${escapeHtml(r.trigger_pct || "")}" placeholder="%" style="${ctl} width:46px; font-weight:600;">
                    <span style="${muted}">%</span>
                    <select data-p="rule_type" style="${ctl} width:auto;">
                        <option value="Payout"${r.rule_type !== "Advance" ? " selected" : ""}>Payout</option>
                        <option value="Advance"${r.rule_type === "Advance" ? " selected" : ""}>Advance</option>
                    </select>
                    <span style="${muted}">RM</span>
                    <input type="number" data-p="amount_rm" step="10" value="${escapeHtml(r.amount_rm || "")}" placeholder="—" style="${ctl} width:44px;">
                    <button type="button" class="btn btn-secondary" data-act="rm-prule" data-k="${k}" style="${tiny}">✕</button>
                    ${k === line.pRules.length - 1 ? `<button type="button" class="btn btn-secondary" data-act="add-prule" style="${tiny}" title="Add another payment stage">+</button>` : ""}
                </div>`).join("");
            const ovr = `
                <td style="${cell}">${oPct}</td>
                <td style="${cell}">${oFrom || `<span style="${muted}">—</span>`}</td>`;
            // Payment Rules sits after the role's Reports To cell, so it is
            // returned apart from Rate % and Property.
            const pay = overrideOnly ? `
                <td style="${cell}"></td>` : `
                <td style="${cell}">
                    <div style="min-height:${line.oRules.length * 30}px;">${stages}</div>
                </td>`;
            const rate = overrideOnly ? `
                <td style="${cell}"></td>
                <td style="${cell}"></td>` : `
                <td style="${cell}"><div style="min-height:${line.oRules.length * 30}px;"><input type="number" data-rf="rate_pct" step="0.05" value="${escapeHtml(line.rate_pct)}" placeholder="role's" title="Blank = the role's own rate from Basic Commission" style="${ctl} width:54px;"></div></td>
                <td style="${cell}">
                    <details data-sum="prop"${line.propOpen ? " open" : ""} style="min-width:120px;">
                        <summary title="${escapeHtml(propertySummary(line.property))}" style="cursor:pointer; font-size:12px; line-height:30px; white-space:nowrap; max-width:132px; overflow:hidden; text-overflow:ellipsis;">${escapeHtml(propertySummary(line.property))}</summary>
                        <div style="display:flex; flex-direction:column; gap:3px; margin-top:4px;">
                            ${props.map((x) => `<label style="display:flex; align-items:center; gap:5px; font-size:12px; cursor:pointer; white-space:nowrap;"><input type="checkbox" data-rp value="${escapeHtml(x)}"${line.property.includes(x) ? " checked" : ""} style="width:auto; margin:0; flex:0 0 auto;"> ${escapeHtml(x)}</label>`).join("")}
                            <span style="${muted}">None ticked = all property types</span>
                        </div>
                    </details>
                </td>`;
            return { rate, pay, ovr };
        };

        const body = rolePeriods.map((p, i) => {
            const roleOpts = allRoleNames().slice();
            // A retired label stays selectable on the row that holds it, so
            // opening and saving cannot blank a role that is pricing invoices.
            if (p.hierarchy && !roleOpts.includes(p.hierarchy)) roleOpts.push(p.hierarchy);
            const branchOpts = BRANCHES.slice();
            if (p.branch && !branchOpts.includes(p.branch)) branchOpts.push(p.branch);
            const packageOpts = PACKAGE_TYPES.slice();
            if (p.package_type && !packageOpts.includes(p.package_type)) packageOpts.push(p.package_type);
            // Once the rates are loaded every role shows at least one rate row,
            // blank and ready to type into: left blank it saves nothing and the
            // role's own rate applies.
            if (ready && !p.rates.length) p.rates.push(ownRateLine(null));
            const lines = ready ? p.rates : [];
            const span = Math.max(1, lines.length);
            const bg = p.uid === focusPeriodUid ? "background:#eef2ff;" : "";
            const rs = span > 1 ? ` rowspan="${span}"` : "";
            const roleCells = `
                <td${rs} style="${cell} font-weight:600; font-size:12px; white-space:nowrap;">${i + 1}${p.src ? "" : ` <span style="color:#6366f1;">(new)</span>`}</td>
                <td${rs} style="${cell} white-space:nowrap;">
                    <div style="display:flex; align-items:center; gap:5px; height:30px;">
                        <input type="month" data-f="from" value="${escapeHtml(p.from)}" title="From" style="${ctl} width:122px; padding-left:4px; padding-right:2px;">
                        <span style="${muted}">to</span>
                        <input type="month" data-f="to" value="${escapeHtml(p.to)}" style="${ctl} width:122px; padding-left:4px; padding-right:2px;${p.present ? " opacity:0.5;" : ""}" title="${p.present ? "To — no end month: the role applies to this month and every later one. Pick a month here if it ended." : "To — the last month this role applies to."}">
                        <label style="display:flex; align-items:center; gap:4px; font-size:12px; font-weight:600; cursor:pointer;"><input type="checkbox" data-f="present" ${p.present ? "checked" : ""} style="width:auto; margin:0; flex:0 0 auto;"> Present</label>
                    </div>
                </td>
                <td${rs} style="${cell}">
                    <select data-f="package_type" style="${ctl} width:178px; padding-left:4px; padding-right:2px;">
                        ${packageOpts.map((x) => opt(x, p.package_type || "Solar")).join("")}
                    </select>
                </td>
                <td${rs} style="${cell}">
                    <select data-f="agent_type" style="${ctl} width:92px;">
                        ${opt("", p.agent_type, "(needs review)")}
                        ${opt("Internal", p.agent_type)}
                        ${opt("Outsource", p.agent_type)}
                        ${p.agent_type === "Sales" ? opt("Sales", p.agent_type, "Sales (needs review - no rate table)") : ""}
                    </select>
                </td>
                <td${rs} style="${cell}">
                    <select data-f="hierarchy" style="${ctl} width:140px;">
                        ${opt("", p.hierarchy, "(none)")}
                        ${opt(ALL_ROLES, p.hierarchy)}
                        ${roleOpts.filter((x) => x !== ALL_ROLES).map((x) => opt(x, p.hierarchy)).join("")}
                    </select>
                </td>
                <td${rs} style="${cell}">
                    <select data-f="branch" style="${ctl} width:100px;">
                        ${opt("", p.branch, "—")}
                        ${branchOpts.map((x) => opt(x, p.branch)).join("")}
                    </select>
                </td>`;
            const roleActions = `
                <td${rs} style="${cell} text-align:right;"><div style="display:flex; gap:4px; align-items:center; height:30px; white-space:nowrap;">
                    ${rolePeriods.length > 1 ? `<button type="button" class="btn btn-secondary" data-act="remove" style="${tiny}">Remove</button>` : ""}
                </div></td>`;
            // Reports To belongs to the role, not to a rate, so like Branch it
            // spans the role's rate rows; it drives the override attribution.
            const reportsCell = `
                <td${rs} style="${cell}">
                    <input type="text" data-f="reports_to" list="agentNameList" value="${escapeHtml(p.reports_to || "")}" placeholder="—" title="Who this agent reports to in this role" style="${ctl} width:88px;">
                </td>`;
            if (!lines.length) {
                const msg = agentRateLoad.loading ? "Loading this agent's rates…"
                    : `Could not load this agent's rates (${escapeHtml(agentRateLoad.error)}) — they are left as they are when you save.`;
                return `<tr data-uid="${p.uid}" style="${bg}">${roleCells}<td colspan="2" style="${cell} ${muted} vertical-align:middle;">${msg}</td>${reportsCell}<td colspan="3" style="${cell}"></td>${roleActions}</tr>`;
            }
            // Rate %, Property, the role's Reports To, Payment Rules, then the
            // rate's Override Rate % and From Role.
            return lines.map((line, j) => {
                const c = lineCells(p, line, j);
                return `
            <tr data-uid="${p.uid}" data-rate="${line.uid}" style="${bg}">${j === 0 ? roleCells : ""}${c.rate}${j === 0 ? reportsCell : ""}${c.pay}${c.ovr}${j === 0 ? roleActions : ""}</tr>`;
            }).join("");
        }).join("");

        // Number boxes here go without their up/down arrows: the arrows
        // covered the digits (100 read as 10) and typing is how they are filled.
        host.innerHTML = `
            <style>
                #roleModalPeriods input[type=number]::-webkit-inner-spin-button,
                #roleModalPeriods input[type=number]::-webkit-outer-spin-button { -webkit-appearance:none; margin:0; }
                #roleModalPeriods input[type=number] { -moz-appearance:textfield; appearance:textfield; }
            </style>
            <div style="overflow-x:auto; width:fit-content; max-width:100%; border:1px solid var(--border-color); border-radius:8px;">
                <table style="width:auto; border-collapse:collapse; font-size:12.5px;">
                    <thead><tr>
                        <th style="${th}">#</th>
                        <th style="${th}">Invoice Date (From – To)</th>
                        <th style="${th}">Package Type</th>
                        <th style="${th}">Agent Type</th>
                        <th style="${th}">Role</th>
                        <th style="${th}">Branch</th>
                        <th style="${th}">Rate %</th>
                        <th style="${th}">Property</th>
                        <th style="${th}">Reports To</th>
                        <th style="${th}">Payment Rules</th>
                        <th style="${th}">Override Rate %</th>
                        <th style="${th}">From Role</th>
                        <th style="${th}"></th>
                    </tr></thead>
                    <tbody>${body}</tbody>
                </table>
            </div>`;
        // Again a frame later: the body's scrollbar may only appear once the
        // new rows are laid out, and it takes width from the table.
        syncAgentFieldWidths();
        requestAnimationFrame(syncAgentFieldWidths);
        renderOtherAgentRates();
        updateRolePeriodWarnings();
        if (focusPeriodUid !== null) {
            const row = host.querySelector(`[data-uid="${focusPeriodUid}"]`);
            if (row && row.scrollIntoView) row.scrollIntoView({ block: "nearest" });
        }
    }

    function propertySummary(props) {
        return props.length ? props.join(", ") : "All property types";
    }

    function paymentSummary(pRules) {
        const r = pRules.filter((x) => x.trigger_pct || x.amount_rm);
        if (!r.length) return "Role's rule";
        return r.map((x) => x.rule_type === "Advance"
            ? `Advance${x.amount_rm ? ` RM ${x.amount_rm}` : ""} at ≥${x.trigger_pct}%`
            : `Pay at ≥${x.trigger_pct}%`).join(" · ");
    }

    document.getElementById("roleModalAddPeriodBtn")?.addEventListener("click", addRolePeriod);

    document.getElementById("roleModalPeriods")?.addEventListener("change", (ev) => {
        const el = ev.target;
        const card = el.closest && el.closest("[data-uid]");
        const f = el.dataset && el.dataset.f;
        if (!card || !f) return;
        const p = rolePeriods.find((x) => x.uid === Number(card.dataset.uid));
        if (!p) return;
        syncRolePeriodsFromDom();
        // Typing an end month is a clear statement that the role ended, so it
        // unticks Present for you rather than leaving the field dead.
        if (f === "to" && p.to) p.present = false;
        if (f === "present" && p.present) p.to = "";
        if (f === "from") autoCloseAround(p);
        if (f === "from" || f === "to" || f === "present" || f === "agent_type") renderRolePeriods();
        else updateRolePeriodWarnings();
    });

    // A Rate %, property or payment change can make a later rate repeat the
    // first one (or stop repeating it), so the card is redrawn. Remembered open
    // lists come back open.
    document.getElementById("roleModalPeriods")?.addEventListener("change", (ev) => {
        const el = ev.target;
        if (!el.dataset || (el.dataset.rp === undefined && !el.dataset.p && el.dataset.rf === undefined)) return;
        const row = el.closest("[data-rate]");
        if (!row) return;
        syncRolePeriodsFromDom();
        renderRolePeriods();
    });

    // Remember which property / payment lists are open across redraws.
    document.getElementById("roleModalPeriods")?.addEventListener("toggle", (ev) => {
        const d = ev.target;
        const row = d && d.closest && d.closest("[data-rate]");
        if (!row || !d.dataset || !d.dataset.sum) return;
        const p = rolePeriods.find((x) => x.uid === Number(row.dataset.uid));
        const line = p && p.rates.find((x) => x.uid === Number(row.dataset.rate));
        if (!line) return;
        if (d.dataset.sum === "prop") line.propOpen = d.open;
        else line.payOpen = d.open;
    }, true);

    document.getElementById("roleModalPeriods")?.addEventListener("click", (ev) => {
        const btn = ev.target.closest && ev.target.closest("[data-act='remove']");
        if (!btn) return;
        const card = btn.closest("[data-uid]");
        syncRolePeriodsFromDom();
        const idx = rolePeriods.findIndex((x) => x.uid === Number(card.dataset.uid));
        if (idx < 0 || rolePeriods.length < 2) return;
        // Asked every time: after a removal the next role slides up under the
        // pointer, so a second click (or a double-click) would quietly take
        // that one too -- and Save then deletes its own rates with it.
        const p = rolePeriods[idx];
        const when = `${fmtMonth(p.from) || "no start"} to ${p.present ? "Present" : (fmtMonth(p.to) || "?")}`;
        if (!confirm(`Remove role ${idx + 1}: ${p.hierarchy || "(no role)"}, ${p.package_type || "Solar"}, ${when}?\n\nIts own rates are deleted with it when you press Save Changes. Cancel keeps it.`)) return;
        rolePeriods.splice(idx, 1);
        renderRolePeriods();
    });

    // ── This agent's own rates ───────────────────────────────────────────────
    // Each rate row of a role is one of the agent's own Basic Commission rows
    // for that role, over exactly the role's Invoice Date range: Property,
    // Rate %, Payment Rules and its Override Rules. They are ordinary
    // commission_rates rows with this agent alone in the Agent cell, which the
    // engine already ranks above the role's own rate
    // (basic_commission_rates._scan_unified_rows), so Rate % and Payment Rules
    // change this agent's commission. A rate always follows its role's dates.
    // A row naming several agents, or naming this one over dates that are not
    // one of their roles' exactly, stays on the Basic Commission page and is
    // listed under the table, read-only.

    // Mirrors COMMISSION_RATE_FIELDS in db.py -- the columns a rates save writes.
    const COMMISSION_RATE_FIELDS = ["rate_type", "agent_type", "hierarchy", "agent", "label",
        "rate_pct", "override_rate_pct", "override_from",
        "profit_sharing_rate_pct", "profit_sharing_mode",
        "property_type", "job_type", "ev_type", "trigger_pct", "invoice_date_from",
        "rule_type", "amount_rm", "condition", "effective_from", "remarks"];
    const OWN_RATE_REMARK = "set on Agent Roles & Hierarchy";

    // {managed, others} once loaded; {loading} while the request is out; {error}
    // when it failed -- then nothing about rates is written on save.
    let agentRateLoad = { managed: [], others: [] };
    let agentRateToken = 0;
    let agentRateUid = 0;

    function rateRowFromApi(r) {
        const out = { id: r.id };
        COMMISSION_RATE_FIELDS.forEach((k) => { out[k] = r[k] == null ? "" : String(r[k]); });
        if (!out.rate_type) out.rate_type = "Basic Commission";
        return out;
    }

    function ratePayload(r) {
        const out = {};
        COMMISSION_RATE_FIELDS.forEach((k) => { out[k] = r[k] == null ? "" : String(r[k]); });
        return out;
    }

    function periodEff(p) {
        return p.present ? p.from : `${p.from} to ${p.to}`;
    }

    function normEffText(s) {
        return String(s || "").trim().toLowerCase().replace(/\s+/g, " ");
    }

    function rateRowAgents(r) {
        return String(r.agent || "").split(",").map((a) => a.trim().toLowerCase()).filter(Boolean);
    }

    function isLiveBasicRow(r) {
        return (r.rate_type || "Basic Commission") === "Basic Commission"
            && String(r.remarks || "").trim().toLowerCase() !== "deleted";
    }

    /** A row naming this agent and nobody else -- the rows a role can own. */
    function isOwnRateRow(r, agentKey) {
        const agents = rateRowAgents(r);
        return isLiveBasicRow(r) && agents.length === 1 && agents[0] === agentKey;
    }

    /** The Job Type and EV Type a role card's rates are saved under, from its
     *  Package Type: Solar (or blank) is Solar Services, "EV" an EV rate for
     *  every EV type, and an EV subtype an EV rate for that type only -- the
     *  same split the engine reads an invoice's role by. */
    function cardJob(p) {
        const pkg = String(p.package_type || "").trim();
        if (!pkg.toLowerCase().startsWith("ev")) return { job_type: "Solar Services", ev_type: "" };
        return { job_type: "EV Services", ev_type: normEvType(pkg) };
    }

    /** Whether a stored rate row is priced for this card's package. A row
     *  with no Job Type is a Solar row, as the engine reads it. */
    function rowFitsCardJob(r, p) {
        const want = cardJob(p);
        const job = String(r.job_type || "").trim();
        if (want.job_type === "Solar Services") return job === "" || job === "Solar Services";
        return job === "EV Services" && normEvType(r.ev_type) === want.ev_type;
    }

    /** The Agent Type a card's rates are saved with. A Role of "All" holds for
     *  every role, Internal and Outsource alike, so its rates name neither. */
    function cardRateAgentType(p) {
        return String(p.hierarchy || "").trim().toLowerCase() === "all" ? "" : p.agent_type;
    }

    /** A rate row: from its stored row, or a new empty one. */
    function ownRateLine(src) {
        return {
            uid: ++agentRateUid, src: src || null,
            property: src ? String(src.property_type || "").split(",").map((x) => x.trim()).filter(Boolean) : [],
            rate_pct: src ? String(src.rate_pct || "").trim() : "",
            pRules: src ? parsePaymentRulesFromEntry(src) : [],
            oRules: src ? parseOverrideRulesFromEntry(src).slice() : [],
            propOpen: false, payOpen: false,
        };
    }

    /** A rate row with nothing typed or ticked in it -- it saves nothing. */
    function isBlankLine(line) {
        return !String(line.rate_pct || "").trim() && !line.property.length
            && !line.pRules.some((r) => r.trigger_pct || r.amount_rm)
            && !line.oRules.some((o) => o.override_rate_pct || o.override_from);
    }

    /** A rate's Rate %, property and payment rules as one string, to tell two
     *  rates of one role apart. */
    function ownRateKey(line) {
        return JSON.stringify([
            parseFloat(line.rate_pct),
            line.property.slice().sort(),
            line.pRules.filter((r) => r.trigger_pct || r.amount_rm)
                .map((r) => [r.trigger_pct, r.rule_type || "Payout", r.amount_rm || ""]),
        ]);
    }

    /** A later rate of a role that repeats the first rate's Rate %, property and
     *  payment rules and carries overrides of its own. It shows and saves only
     *  its overrides: the first rate already holds the rest. */
    function isOverrideOnlyLine(p, j) {
        if (j < 1 || !p.rates.length) return false;
        const line = p.rates[j];
        if (!line.oRules.some((o) => o.override_rate_pct || o.override_from)) return false;
        // Saved with no Rate %, property or payment rules: only its overrides.
        if (!String(line.rate_pct || "").trim() && !line.property.length
            && !line.pRules.some((r) => r.trigger_pct || r.amount_rm)) return true;
        const first = p.rates[0];
        if (!String(first.rate_pct || "").trim()) return false;
        return ownRateKey(first) === ownRateKey(line);
    }

    /** A role card's Role of "All" holds for whichever role the agent is on.
     *  Its own rates are saved with a blank Role, which every rate lookup
     *  already reads as "(all roles)". */
    const ALL_ROLES = "All";

    /** A role card's Package Type choices; a value not on the list stays
     *  selectable on the row that holds it. */
    const PACKAGE_TYPES = ["Solar", "EV Charger", "EV Installation", "EV Charger + EV Installation"];
    function rateRoleOf(p) {
        return String(p.hierarchy || "").trim().toLowerCase() === "all" ? "" : p.hierarchy;
    }

    /** Everything about a rate the modal can change, as one string: its own
     *  fields plus its role's dates, Package Type, Agent Type and Role. */
    function ownLineState(line, p) {
        return JSON.stringify([
            periodEff(p), line.property.slice().sort(), line.rate_pct,
            line.pRules.filter((r) => r.trigger_pct || r.amount_rm)
                .map((r) => [r.trigger_pct, r.rule_type || "Payout", r.amount_rm || ""]),
            cardJob(p), p.agent_type, p.hierarchy,
            line.oRules.filter((x) => x.override_rate_pct || x.override_from)
                .map((x) => [x.override_rate_pct, x.override_from]),
        ]);
    }

    async function loadAgentOwnRates(agentName) {
        const token = ++agentRateToken;
        const key = String(agentName || "").trim().toLowerCase();
        if (agentName === null || !key) {
            agentRateLoad = { managed: [], others: [] };
            return;
        }
        agentRateLoad = { loading: true };
        renderRolePeriods();
        let all;
        try {
            const res = await api("/api/commission-rates");
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            all = (await res.json()).map(rateRowFromApi);
        } catch (e) {
            if (token !== agentRateToken) return;
            syncRolePeriodsFromDom();
            agentRateLoad = { error: String((e && e.message) || e) };
            renderRolePeriods();
            return;
        }
        if (token !== agentRateToken) return;
        syncRolePeriodsFromDom();
        // The blank row a role was drawn with before its rates arrived gives
        // way to the rates themselves (a role left with none gets it back).
        rolePeriods.forEach((p) => { p.rates = p.rates.filter((line) => !isBlankLine(line)); });
        const managed = [];
        const others = [];
        all.forEach((r) => {
            if (!isLiveBasicRow(r) || !rateRowAgents(r).includes(key)) return;
            // Its role: the same role, package and Agent Type over exactly the
            // same dates -- what saveAgentOwnRates() writes for that card.
            const p = isOwnRateRow(r, key) && rolePeriods.find((q) => q.from
                && canonRole(rateRoleOf(q)) === canonRole(r.hierarchy)
                && rowFitsCardJob(r, q)
                && (!r.agent_type || String(q.agent_type).toLowerCase() === String(r.agent_type).toLowerCase())
                && normEffText(periodEff(q)) === normEffText(r.effective_from));
            if (!p) { others.push(r); return; }
            managed.push(r);
            p.rates.push(ownRateLine(r));
        });
        // What each loaded rate looked like, so an untouched one is saved as is.
        rolePeriods.forEach((p) => p.rates.forEach((line) => {
            if (line.src) line.srcState = ownLineState(line, p);
        }));
        agentRateLoad = { managed, others };
        renderRolePeriods();
    }

    /** Read a role's rate rows back into its period. Only once the rates are
     *  loaded: before that the table has none to read. Idempotent, since
     *  syncRolePeriodsFromDom() calls it once per table row of the role. */
    function syncOwnRatesFromCard(card, p) {
        if (agentRateLoad.loading || agentRateLoad.error) return;
        const host = document.getElementById("roleModalPeriods");
        if (!host) return;
        const rows = [...host.querySelectorAll(`tr[data-uid="${p.uid}"][data-rate]`)];
        p.rates = rows.map((el) => {
            const uid = Number(el.dataset.rate);
            const line = p.rates.find((x) => x.uid === uid) || ownRateLine(null);
            // An "as above" rate has no Rate %, property or payment inputs: it
            // keeps the values it was loaded with.
            if (el.querySelector("[data-rf='rate_pct']")) {
                line.property = [...el.querySelectorAll("[data-rp]")].filter((c) => c.checked).map((c) => c.value);
                line.rate_pct = String(el.querySelector("[data-rf='rate_pct']").value || "").trim();
                line.pRules = [...el.querySelectorAll("[data-prule]")].map((row) => ({
                    trigger_pct: String(row.querySelector("[data-p='trigger_pct']").value || "").trim(),
                    rule_type: row.querySelector("[data-p='rule_type']").value || "Payout",
                    amount_rm: String(row.querySelector("[data-p='amount_rm']").value || "").trim(),
                }));
            }
            const pcts = [...el.querySelectorAll("[data-o='pct']")];
            const froms = [...el.querySelectorAll("[data-o='from']")];
            line.oRules = pcts.map((inp, k) => ({
                override_rate_pct: String(inp.value || "").trim(),
                override_from: froms[k] ? froms[k].value || "" : "",
            }));
            return line;
        });
    }

    /** Rows naming this agent that no role owns, so they are not lost from view. */
    function renderOtherAgentRates() {
        const el = document.getElementById("roleModalOtherRates");
        if (!el) return;
        const others = agentRateLoad.others || [];
        if (!others.length) { el.style.display = "none"; el.innerHTML = ""; return; }
        const lines = others.map((r) => {
            const n = rateRowAgents(r).length;
            const why = n > 1 ? `shared with ${n - 1} other agent${n > 2 ? "s" : ""}`
                : `its dates or package are not exactly one of this agent's ${r.hierarchy || ""} roles`
                    + (r.job_type === "EV Services" ? ` (${normEvType(r.ev_type) || "EV"})` : "");
            return `<li>${escapeHtml(r.hierarchy || "(any role)")} · ${escapeHtml(r.effective_from || "")} · `
                + `${escapeHtml(r.property_type || "all property types")} · <strong>${escapeHtml(r.rate_pct || "—")}%</strong>`
                + ` <span style="color:var(--text-muted);">(${escapeHtml(why)})</span></li>`;
        }).join("");
        el.style.display = "";
        el.innerHTML = `<div style="font-weight:600; margin-bottom:4px;">Other Basic Commission rates naming this agent — edit these on the Basic Commission page:</div>`
            + `<ul style="margin:0; padding-left:18px;">${lines}</ul>`;
    }

    /** The first problem with the roles' rates as a sentence, or "". */
    function ownRatesProblem(agentName) {
        if (agentRateLoad.loading || agentRateLoad.error) return "";
        for (let i = 0; i < rolePeriods.length; i++) {
            const p = rolePeriods[i];
            const tag = rolePeriods.length > 1 ? `Role ${i + 1}: ` : "";
            // A row left wholly blank is no rate at all: it saves nothing.
            const filled = p.rates.filter((line) => !isBlankLine(line));
            if (filled.length && !agentName) {
                return "Rates are kept against the Agent Name (from eeAdmin), so enter it first.";
            }
            for (let j = 0; j < p.rates.length; j++) {
                const line = p.rates[j];
                if (isBlankLine(line)) continue;
                const lt = `${tag}${p.rates.length > 1 ? `Rate ${j + 1}: ` : ""}`;
                if (!isOverrideOnlyLine(p, j) && (line.rate_pct === "" || !Number.isFinite(parseFloat(line.rate_pct)))) {
                    return `${lt}enter the Rate %.`;
                }
                if (line.oRules.some((o) => o.override_from && !o.override_rate_pct)) {
                    return `${lt}an override has a From Role but no Override Rate %.`;
                }
                if (line.pRules.some((r) => !r.trigger_pct && r.amount_rm)) {
                    return `${lt}a payment stage has an Amount but no Payment ≥ %.`;
                }
                // Two rates of one role on the same property would tie, and the
                // engine would pick between them by row order. None ticked
                // means every type, which a specific one outranks. An override-
                // only rate repeats the first one's property, so it is not one.
                if (isOverrideOnlyLine(p, j)) continue;
                for (let k = 0; k < j; k++) {
                    if (isBlankLine(p.rates[k])) continue;
                    const a = line.property, b = p.rates[k].property;
                    if ((!a.length && !b.length) || a.some((x) => b.includes(x))) {
                        return `${lt}it covers the same property type as Rate ${k + 1} of this role. Give each rate its own property types.`;
                    }
                }
            }
        }
        return "";
    }

    /** Write the roles' rates over the rows they were loaded from. Every other
     *  row goes back exactly as the server has it. */
    async function saveAgentOwnRates(agentName, periods, load) {
        if (!load || load.loading || load.error) return;
        const newRows = [];
        periods.forEach((p) => {
            p.rates.forEach((line, j) => {
                // Cleared (or never filled in): no row. A cleared stored rate
                // is then tombstoned below like a removed one.
                if (isBlankLine(line)) return;
                // An override-only rate saves no Rate %, property or payment
                // rules: the first rate of the role holds them.
                const overrideOnly = isOverrideOnlyLine(p, j);
                // Untouched, it goes back exactly as stored: rebuilding it would
                // reword its condition text and mark a change nobody made.
                if (!overrideOnly && line.src && ownLineState(line, p) === line.srcState) {
                    newRows.push(ratePayload(line.src));
                    return;
                }
                const pRules = overrideOnly ? [] : line.pRules.filter((r) => r.trigger_pct || r.amount_rm);
                const rankOf = overrideRankOf(p.agent_type);
                const o = line.oRules.filter((x) => x.override_rate_pct || x.override_from)
                    .sort((a, b) => rankOf(a.override_from) - rankOf(b.override_from));
                const base = line.src ? ratePayload(line.src) : {};
                newRows.push(Object.assign(base, cardJob(p), {
                    rate_type: "Basic Commission",
                    agent_type: cardRateAgentType(p),
                    hierarchy: rateRoleOf(p),
                    agent: agentName,
                    rate_pct: overrideOnly ? "" : line.rate_pct,
                    // Position for position: dropping a blank "(all roles)" would
                    // pair the next override's From Role with this one's rate.
                    override_rate_pct: o.map((x) => x.override_rate_pct).join(", "),
                    override_from: o.some((x) => x.override_from) ? o.map((x) => x.override_from).join(", ") : "",
                    property_type: overrideOnly ? "" : line.property.join(", "),
                    trigger_pct: pRules.map((r) => r.trigger_pct).filter(Boolean).join(", "),
                    rule_type: pRules.length > 1 ? "Multi-stage" : ((pRules[0] || {}).rule_type || ""),
                    amount_rm: (pRules.find((r) => r.rule_type === "Advance") || {}).amount_rm || "",
                    condition: formatPaymentRulesCondition(pRules),
                    effective_from: periodEff(p),
                    remarks: line.src ? line.src.remarks : OWN_RATE_REMARK,
                }));
            });
        });
        const sortKey = (r) => JSON.stringify(ratePayload(r));
        const before = load.managed.map(sortKey).sort();
        const after = newRows.map(sortKey).sort();
        if (before.length === after.length && before.every((x, i) => x === after[i])) return;

        const status = document.getElementById("rolesStatus");
        status.className = "save-status";
        status.textContent = "Saving rates...";
        const res = await api("/api/commission-rates");
        if (!res.ok) {
            status.className = "save-status err";
            status.textContent = "Roles saved, but the rates could not be loaded to save.";
            return;
        }
        const fresh = (await res.json()).map(rateRowFromApi);
        // The rows this modal loaded are replaced; a row someone else changed
        // since then no longer matches and is kept as they left it.
        const loadedKeys = new Set(load.managed.map(sortKey));
        const kept = fresh.filter((r) => !loadedKeys.has(sortKey(r)));
        // A rate taken away leaves a tombstone, as the Basic Commission page's
        // own Delete does, so the resolved view does not fall back to an
        // older copy of it.
        const newKeys = new Set(newRows.map(fullEntryKey));
        const tombstones = load.managed
            .filter((r) => !newKeys.has(fullEntryKey(r)))
            .map((r) => Object.assign(ratePayload(r), { rate_pct: "", remarks: "deleted" }));
        const saveRes = await api("/api/commission-rates", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ entries: kept.map(ratePayload).concat(newRows, tombstones) })
        });
        if (!saveRes.ok) {
            const d = await saveRes.json().catch(() => ({}));
            status.className = "save-status err";
            status.textContent = "Roles saved, but the rates failed: " + (d.error || `HTTP ${saveRes.status}`);
            return;
        }
        status.className = "save-status ok";
        status.textContent = "Saved, with this agent's rates. Commission cache cleared.";
        // The roles grid's Rate % and Override columns read these rates, so
        // reload them here or they keep showing the values from before the save.
        await loadRoleRates(true);
        await loadEntries();
        loadPreview();
        setTimeout(() => { status.textContent = ""; }, 2500);
    }

    document.getElementById("roleModalPeriods")?.addEventListener("click", (ev) => {
        const btn = ev.target.closest && ev.target.closest("[data-act]");
        const act = btn && btn.dataset.act;
        if (!act || act === "remove") return;
        const row = btn.closest("[data-uid]");
        syncRolePeriodsFromDom();
        const p = row && rolePeriods.find((x) => x.uid === Number(row.dataset.uid));
        if (!p) return;
        const line = row.dataset.rate && p.rates.find((x) => x.uid === Number(row.dataset.rate));
        const k = Number(btn.dataset.k);
        if (act === "add-prule" && line) { line.pRules.push({ trigger_pct: "", rule_type: "Payout", amount_rm: "" }); line.payOpen = true; }
        else if (act === "rm-prule" && line) { line.pRules.splice(k, 1); line.payOpen = true; }
        else if (act === "add-orule" && line) { line.oRules.push({ override_rate_pct: "", override_from: "" }); }
        else if (act === "rm-orule" && line && line.oRules.length > 1) { line.oRules.splice(k, 1); }
        else return;
        focusPeriodUid = p.uid;
        renderRolePeriods();
    });

    function openAgentRoleModal(r) {
        editingAgentObj = r;
        const modal = document.getElementById("agentRoleModal");
        if (!modal) return;

        if (r) {
            editingGroup = rowsForSamePerson(r);
            rolePeriods = editingGroup.map(periodFromRow).sort(compareSolarThenEv);
            const clicked = rolePeriods.find((p) => p.src === r);
            focusPeriodUid = rolePeriods.length > 1 && clicked ? clicked.uid : null;
            const needsDate = !!r.needs_date || !String(r.effective_from || "").trim();
            // The person's own fields live on every row; take the clicked
            // row's, falling back to a sibling for one that was never filled in.
            const shared = (k) => r[k] || (editingGroup.find((x) => x[k]) || {})[k] || "";
            document.getElementById("agentRoleModalTitle").textContent = "Edit Agent Details";
            // The name of an existing row is eeAdmin's to change, not this
            // modal's — searching would only invite retargeting the row at a
            // different person by accident.
            pickedPgAgent = r.pg_bubble_id ? { bubble_id: r.pg_bubble_id, name: r.agent || "" } : null;
            setAgentNameMode(false);
            document.getElementById("roleModalAgent").value = r.agent || "";
            document.getElementById("roleModalFullName").value = shared("full_name");
            document.getElementById("roleModalNickName").value = shared("nick_name");
            document.getElementById("roleModalIcNo").value = shared("ic_no");
            // Nothing to delete yet — an undated row exists only because
            // Postgres lists the name, and it is re-derived on every load.
            // Date it here to keep it.
            document.getElementById("roleModalDeleteBtn").style.display =
                (needsDate && editingGroup.length === 1) ? "none" : "";
            setRoleModalNote(needsDate
                ? "Only this agent's name came from eeAdmin. Fill in the details and set the effective month to save the row — until then no commission calculation uses it."
                : "");
        } else {
            editingGroup = [];
            rolePeriods = [{
                uid: ++rolePeriodUid, src: null, from: CURRENT_YM, to: "", present: true,
                agent_type: "", hierarchy: "", reports_to: "", branch: "",
                rates: [], oRules: [],
            }];
            focusPeriodUid = null;
            document.getElementById("agentRoleModalTitle").textContent = "Add Agent Details";
            document.getElementById("roleModalAgent").value = "";
            document.getElementById("roleModalFullName").value = "";
            document.getElementById("roleModalNickName").value = "";
            document.getElementById("roleModalIcNo").value = "";
            document.getElementById("roleModalDeleteBtn").style.display = "none";
            setRoleModalNote("");
            pickedPgAgent = null;
            setAgentNameMode(true);
            setTimeout(() => document.getElementById("roleModalAgent")?.focus(), 50);
        }

        renderRolePeriods();
        modal.classList.remove("hidden");
        // The table has no width while the modal is hidden, so the boxes
        // above it are matched to it once it shows.
        syncAgentFieldWidths();
        loadAgentOwnRates(r ? (r.agent || "") : null);
    }

    /** Agent Name, Full Name, Nick Name and IC No. -- and the Roles over time
     *  heading, its explanation and the notes under the table -- run exactly
     *  as wide as the Roles over time table, which takes only the width it
     *  needs. The table's own box is left alone, so it can still grow back
     *  when the window does. */
    function syncAgentFieldWidths() {
        const host = document.getElementById("roleModalPeriods");
        const table = host && host.querySelector(":scope > div");
        const inner = table && table.querySelector("table");
        if (!inner || !inner.offsetWidth) return;
        // The window itself is as wide as the table plus its own padding and
        // scrollbar, never wider than the screen.
        const box = host.closest(".modal-content");
        if (box) {
            // Everything round the table: the window's padding, borders and
            // scrollbar, measured rather than assumed.
            const extra = box.offsetWidth - host.clientWidth;
            const tableW = inner.offsetWidth + (table.offsetWidth - table.clientWidth);
            // A few pixels spare, so rounding or a wider scrollbar cannot tip
            // it into side-scrolling.
            box.style.width = `min(100%, ${Math.ceil(tableW + extra) + 6}px)`;
        }
        const w = table.offsetWidth;
        if (!w) return;
        ["roleModalAgent", "roleModalFullName", "roleModalNickName", "roleModalIcNo"].forEach((id) => {
            const row = document.getElementById(id)?.closest(".form-row");
            if (row) row.style.maxWidth = `${w}px`;
        });
        [...host.parentElement.children].forEach((el) => {
            if (el !== host) el.style.maxWidth = `${w}px`;
        });
    }
    window.addEventListener("resize", () => {
        if (!document.getElementById("agentRoleModal")?.classList.contains("hidden")) syncAgentFieldWidths();
    });

    /** One-line explanation shown above the modal's buttons; "" hides it. */
    function setRoleModalNote(text) {
        const el = document.getElementById("roleModalNote");
        if (!el) return;
        el.textContent = text;
        el.style.display = text ? "" : "none";
    }

    // Modal Event Bindings
    document.getElementById("roleModalSaveBtn")?.addEventListener("click", async () => {
        syncRolePeriodsFromDom();
        const agentName = document.getElementById("roleModalAgent").value.trim();
        const fullName = document.getElementById("roleModalFullName").value.trim();
        const nickNameCheck = document.getElementById("roleModalNickName").value.trim();
        if (!agentName && !fullName && !nickNameCheck) {
            alert("Enter at least one of Agent Name (from eeAdmin), Full Name, or Nick Name.");
            return;
        }

        // Adding a person: settle what this name points at in eeAdmin before
        // anything is written. Typing a name in full and pressing Save without
        // touching the dropdown is the normal case, so an exact match is
        // adopted silently rather than being treated as unmatched.
        if (!editingAgentObj && agentName) {
            const dup = rolesList.find((x) =>
                String(x.agent || "").trim().toLowerCase() === agentName.toLowerCase());
            if (dup) {
                alert(`${dup.agent} is already on this page.\n\nOpen that agent with Edit and use + Add role to give them another role from a later month.`);
                return;
            }
            if (!pickedPgAgent || pickedPgAgent.name.toLowerCase() !== agentName.toLowerCase()) {
                const hits = await searchPgAgents(agentName);
                const exact = (hits || []).find((a) =>
                    String(a.name || "").trim().toLowerCase() === agentName.toLowerCase());
                if (exact) {
                    choosePgAgent(exact);
                } else if (hits === null) {
                    // The lookup itself failed, so we do not know either way.
                    if (!confirm(`Could not reach eeAdmin to check "${agentName}".\n\nSave the row anyway? It will be a manual entry with no eeAdmin link.`)) return;
                    pickedPgAgent = null;
                } else if (!confirm(`"${agentName}" was not found in eeAdmin.\n\nSave it anyway as a manual row? Check the spelling first — a typo here creates a second copy of a person who is already there.`)) {
                    return;
                }
            }
        }

        // Every role needs its own dates, never defaulted: an effective month
        // decides which invoices this role prices, so falling back to "this
        // month" would quietly backdate or postdate a role nobody chose a date for.
        if (!rolePeriods.length) {
            alert("Keep at least one role.\n\nTo remove the agent altogether, use Delete Agent.");
            return;
        }
        for (let i = 0; i < rolePeriods.length; i++) {
            const p = rolePeriods[i];
            const tag = rolePeriods.length > 1 ? `Role ${i + 1}: ` : "";
            if (!p.from) {
                alert(`${tag}Enter the month this role starts From.\n\nIt decides which months are calculated with it, so it has to be entered here.`);
                return;
            }
            if (!p.present) {
                if (!p.to) {
                    alert(`${tag}Enter the month this role ends in the To field.\n\nIf it has not ended, tick Present instead — the role then applies to every later month.`);
                    return;
                }
                if (p.from > p.to) {
                    alert(`${tag}The range runs backwards: ${fmtMonth(p.from)} is after ${fmtMonth(p.to)}.\n\nSwap them so From is the earlier month.`);
                    return;
                }
            }
            if (!roleBelongsToType(p.hierarchy, p.agent_type)) {
                alert(`${tag}"${p.hierarchy}" is not a ${p.agent_type} role.\n\nRate rows are looked up by Agent Type AND Role together, so this combination matches nothing and the agent would silently fall back to the hardcoded default rate.\n\nPick a ${p.agent_type} role, or change the Agent Type.`);
                return;
            }
        }

        const ratesProblem = ownRatesProblem(agentName);
        if (ratesProblem) {
            alert(ratesProblem);
            return;
        }

        // Gaps are allowed but never silent: a month nobody covers falls back
        // to the old hardcoded name matching. Overlaps are not asked about
        // (user, 2026-10-20); where two roles cover one month the later start
        // wins, as before.
        const { gaps } = rolePeriodIssues();
        if (gaps.length) {
            if (!confirm("Months with no role:\n• " + gaps.join("\n• ")
                + "\n\nInvoices dated in those months fall back to the old hardcoded name matching."
                + "\n\nSave anyway?")) return;
        }

        const nickName = nickNameCheck;
        const icNo = document.getElementById("roleModalIcNo").value.trim();
        // A person added here is linked to eeAdmin by the id of whoever was
        // picked in the search; rows keyed by id survive a rename in eeAdmin. A
        // manual row saved past the "not found" warning has none and stays
        // matched by name. An existing person keeps the id they already had.
        const bubbleId = editingAgentObj
            ? (editingAgentObj.pg_bubble_id || "")
            : ((pickedPgAgent && pickedPgAgent.name.toLowerCase() === agentName.toLowerCase())
                ? pickedPgAgent.bubble_id : "");

        const kept = new Set();
        rolePeriods.forEach((p) => {
            const fields = {
                effective_from: p.present ? p.from : `${p.from} to ${p.to}`,
                needs_date: false,
                // The person's own fields, identical on every role.
                agent: agentName,
                full_name: fullName,
                nick_name: nickName,
                ic_no: icNo,
                package_type: p.package_type || "Solar",
                agent_type: p.agent_type,
                hierarchy: p.hierarchy,
                reports_to: p.reports_to,
                branch: p.branch,
            };
            if (p.src) {
                Object.assign(p.src, fields);
                kept.add(p.src);
            } else {
                const row = Object.assign({ pg_bubble_id: bubbleId }, fields);
                rolesList.push(row);
                kept.add(row);
            }
        });
        // A role removed from the cards leaves the table; the person's other
        // rows are still there, so no tombstone is needed.
        rolesList = rolesList.filter((x) => !editingGroup.includes(x) || kept.has(x));

        const ratePeriods = rolePeriods.slice();
        const rateLoad = agentRateLoad;
        document.getElementById("agentRoleModal").classList.add("hidden");
        await saveRoles();
        await saveAgentOwnRates(agentName, ratePeriods, rateLoad);
    });

    document.getElementById("roleModalDeleteBtn")?.addEventListener("click", async () => {
        if (!editingAgentObj) return;
        // Delete Agent removes the person, so every role they hold goes with
        // them -- not just the one whose Edit button opened this.
        const group = editingGroup.length ? editingGroup : [editingAgentObj];
        const prompt1 = group.length > 1
            ? `Are you sure you want to delete ${editingAgentObj.agent} and all ${group.length} of their roles?`
            : `Are you sure you want to delete ${editingAgentObj.agent}?`;
        if (!confirm(prompt1)) return;

        // A Postgres-linked agent reappears on the very next reload — Postgres
        // itself was never asked to remove them — unless a hidden tombstone
        // row is kept for them. Matched by pg_bubble_id when this row has
        // one; a saved row can still lack it (seeded before pg_bubble_id
        // existed, or never healed because the live eeAdmin name didn't
        // match), so the name is the fallback key here too — the same
        // fallback savedRolesForPg() already uses to find a saved row in the
        // first place. Skipping the tombstone whenever there was no ID is
        // exactly what let a deleted agent come back with nothing to stop it.
        const bubbleId = editingAgentObj.pg_bubble_id || "";
        const nameKey = String(editingAgentObj.agent || "").trim().toLowerCase();
        rolesList = rolesList.filter((r) => !group.includes(r));
        // A row identified only by Full Name / Nick Name (no eeAdmin Agent
        // Name — a pure Excel entry) has nothing in Postgres to ever
        // regenerate it from, so there is nothing to tombstone against.
        if (nameKey) {
            const reason = (prompt(
                `Optional: why is ${editingAgentObj.agent} being removed?\n`
                + `(e.g. "not an agent — office staff", "left the company", "duplicate entry")\n\n`
                + `Leave blank to skip — Cancel still deletes.`, ""
            ) || "").trim();
            rolesList.push({
                agent: editingAgentObj.agent,
                pg_bubble_id: bubbleId,
                hidden: true,
                effective_from: CURRENT_YM,
                agent_type: "", hierarchy: "", reports_to: "", branch: "",
                ic_no: "", nick_name: "", full_name: "",
                remarks: reason ? `excluded: ${reason}` : "",
            });
        }
        document.getElementById("agentRoleModal").classList.add("hidden");
        await saveRoles();
    });

    const closeRoleModal = () => {
        document.getElementById("agentRoleModal")?.classList.add("hidden");
    };
    document.getElementById("roleModalCancelBtn")?.addEventListener("click", closeRoleModal);
    document.getElementById("closeAgentRoleModalBtn")?.addEventListener("click", closeRoleModal);

    // Mirrors AGENT_ROLE_FIELDS in db.py — the columns a save writes.
    const AGENT_ROLE_FIELDS = ["effective_from", "agent", "package_type", "agent_type", "hierarchy",
        "reports_to", "branch", "remarks", "start_date", "ic_no", "nick_name",
        "full_name", "pg_bubble_id", "hidden"];

    /** Rows for blocked agents, written back exactly as they were loaded. They
     *  are off-screen, so nobody edited them — re-deriving or re-labelling them
     *  would record a change that never happened. */
    function suppressedRolePayloads() {
        return suppressedRoles.map((r) => {
            const out = {};
            AGENT_ROLE_FIELDS.forEach((k) => { out[k] = r[k]; });
            return out;
        });
    }

    function collectRoles() {
        return rolesList.map(r => ({
            effective_from: (r.effective_from || "").trim(),
            agent: (r.agent || "").trim(),
            full_name: (r.full_name || "").trim(),
            nick_name: (r.nick_name || "").trim(),
            package_type: r.package_type || "",
            agent_type: r.agent_type,
            hierarchy: r.hierarchy,
            reports_to: r.reports_to,
            branch: r.branch,
            ic_no: r.ic_no,
            pg_bubble_id: r.pg_bubble_id || "",
            hidden: r.hidden ? 1 : 0,
            // A reason typed into the delete prompt travels with the row (set
            // when the tombstone was pushed); fall back to the generic note
            // for older tombstones that predate the prompt.
            remarks: r.hidden
                ? (r.remarks || "deleted via roles grid (kept as a tombstone so Postgres stops re-adding this agent)")
                : "edited via roles grid"
        }))
            .filter(r => r.agent || r.full_name || r.nick_name)
            // An undated row is a name Postgres lists that nobody has dated
            // yet. It must never be written: the old `|| CURRENT_YM` stamped
            // it with this month on the next unrelated save, which both
            // invented an effective date nobody chose and let an unreviewed
            // row reach the calculation. It is re-derived from the Postgres
            // name list on every load until someone fills it in.
            .filter(r => r.effective_from || r.hidden)
            // Appended last and unfiltered: a save replaces the whole table, so
            // anything left out here is deleted. A blocked agent is hidden, not
            // deleted — their role history still prices the months they worked.
            .concat(suppressedRolePayloads());
    }

    async function saveRoles() {
        const status = document.getElementById("rolesStatus");
        status.className = "save-status";
        status.textContent = "Saving...";
        const res = await api("/api/agent-roles", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ entries: collectRoles() })
        });
        if (!res.ok) {
            const d = await res.json().catch(() => ({}));
            status.className = "save-status err";
            status.textContent = d.error || "Save failed";
            return;
        }
        status.className = "save-status ok";
        status.textContent = "Saved. Commission cache cleared.";
        await loadRoles();
        setTimeout(() => { status.textContent = ""; }, 2000);
    }

    async function seedRoles() {
        const status = document.getElementById("rolesStatus");
        status.className = "save-status";
        status.textContent = "Pulling agents from Postgres...";
        try {
            const res = await api("/api/agent-roles/seed", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({})
            });
            const d = await res.json();
            if (!res.ok) throw new Error(d.error || "Seed failed");
            status.className = "save-status ok";
            status.textContent = `Added ${d.added} agent(s); ${d.total} listed. ` +
                `Now fill in each new agent's details.`;
            await loadRoles();
        } catch (e) {
            status.className = "save-status err";
            status.textContent = e.message;
        }
    }

    // ── EGA carry-over cases ─────────────────────────────────────────────────
    // Cases closed before the scheme cutoff. Only the COUNT matters: it picks
    // the multiplier applied to the agent's 1 May – 10 Jun campaign sales on
    // the Sales Report. Held here because nothing in eeAdmin reproduces the
    // counts the printed report uses.

    let egaCarryRows = [];

    /** Every third case adds another half point per ringgit. Mirrors
     *  _campaign_bonus_tier() in app.py — keep the two in step. */
    function carryTier(cases) {
        const n = parseInt(String(cases || "0").replace(/[^0-9]/g, ""), 10) || 0;
        return Math.floor(n / 3) * 0.5;
    }

    function egaCarryYear() {
        const el = document.getElementById("egaCarryYear");
        return String((el && el.value) || "2026").trim();
    }

    async function loadEgaCarryover() {
        try {
            const res = await api("/api/ega-carryover");
            egaCarryRows = await res.json();
            if (!Array.isArray(egaCarryRows)) egaCarryRows = [];
        } catch (e) {
            egaCarryRows = [];
        }
        renderEgaCarry();
    }

    function renderEgaCarry() {
        const body = document.getElementById("egaCarryBody");
        if (!body) return;
        const year = egaCarryYear();
        const mine = egaCarryRows.filter(r => String(r.year || "").trim() === year);
        body.innerHTML = "";
        if (!mine.length) {
            body.innerHTML = `<tr><td colspan="5" style="text-align:center; padding:18px; color:var(--text-muted);">`
                + `No carry-over cases recorded for ${escapeHtml(year)}. `
                + `Every agent scores EP from their sales alone until a row is added here.</td></tr>`;
            return;
        }
        mine.forEach((r) => {
            const tr = document.createElement("tr");
            const tier = carryTier(r.cases);
            tr.innerHTML = `
                <td><input type="text" list="agentNameList" value="${escapeHtml(r.agent || "")}"
                     data-f="agent" style="width:100%;" ${isAdmin ? "" : "disabled"}></td>
                <td><input type="number" min="0" step="1" value="${escapeHtml(r.cases || "")}"
                     data-f="cases" style="width:100%;" ${isAdmin ? "" : "disabled"}></td>
                <td><input type="number" min="0" step="0.01" value="${escapeHtml(r.sales || "")}"
                     data-f="sales" style="width:100%;" ${isAdmin ? "" : "disabled"}></td>
                <td class="carry-tier" style="font-weight:600;"
                    title="Multiplier applied to this agent's 1 May – 10 Jun campaign sales.">
                    ${tier ? tier.toFixed(1) + "&times;" : "—"}</td>
                <td>${isAdmin ? `<button class="btn btn-secondary carry-del" style="padding:4px 8px; font-size:12px;">✕</button>` : ""}</td>`;
            tr.querySelectorAll("input").forEach((inp) => {
                inp.addEventListener("input", () => {
                    r[inp.dataset.f] = inp.value;
                    if (inp.dataset.f === "cases") {
                        const cell = tr.querySelector(".carry-tier");
                        const t = carryTier(inp.value);
                        cell.innerHTML = t ? t.toFixed(1) + "&times;" : "—";
                    }
                });
            });
            tr.querySelector(".carry-del")?.addEventListener("click", () => {
                egaCarryRows = egaCarryRows.filter(x => x !== r);
                renderEgaCarry();
            });
            body.appendChild(tr);
        });
    }

    async function saveEgaCarryover() {
        const status = document.getElementById("egaCarryStatus");
        if (status) { status.className = "save-status"; status.textContent = "Saving..."; }
        // Every year is sent, not just the one on screen: the save replaces the
        // whole table, so omitting the others would delete them.
        const entries = egaCarryRows
            .map(r => ({ year: String(r.year || "").trim(), agent: String(r.agent || "").trim(),
                         cases: String(r.cases || "").trim(), sales: String(r.sales || "").trim() }))
            .filter(r => r.year && r.agent);
        try {
            const res = await api("/api/ega-carryover", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ entries })
            });
            const d = await res.json();
            if (!res.ok) throw new Error(d.error || "Save failed");
            if (status) {
                status.className = "save-status ok";
                status.textContent = "Saved. Sales Report EP recalculated.";
                setTimeout(() => { status.textContent = ""; }, 3000);
            }
            await loadEgaCarryover();
        } catch (e) {
            if (status) { status.className = "save-status err"; status.textContent = e.message; }
        }
    }

    document.getElementById("egaCarryAddBtn")?.addEventListener("click", () => {
        egaCarryRows.push({ year: egaCarryYear(), agent: "", cases: "", sales: "" });
        renderEgaCarry();
    });
    document.getElementById("egaCarrySaveBtn")?.addEventListener("click", saveEgaCarryover);
    document.getElementById("egaCarryYear")?.addEventListener("input", renderEgaCarry);

    // ── NFP price list viewer ────────────────────────────────────────────────

    let nfpPricesCache = null;

    /** `wantMonth` ("07") preselects a month — the report page passes the month
     *  it is showing so the embedded list opens on the same one. */
    async function showNfpList(wantMonth) {
        document.getElementById("nfpListCard").style.display = "";
        if (nfpPricesCache === null) {
            // The list has real network latency now (Supabase, not local SQLite),
            // so show something immediately rather than leaving the card blank
            // while the fetch is in flight.
            document.getElementById("nfpListTables").innerHTML =
                '<p class="section-hint">Loading price list…</p>';
            let fetchError = null;
            try {
                const res = await api("/api/nfp-prices");
                nfpPricesCache = await res.json();
                if (!Array.isArray(nfpPricesCache)) {
                    fetchError = nfpPricesCache?.error || "Unexpected response from server.";
                    nfpPricesCache = [];
                }
            } catch (e) {
                fetchError = e?.message || "Could not reach the server.";
                nfpPricesCache = [];
            }
            if (fetchError) {
                document.getElementById("nfpListTables").innerHTML =
                    `<p class="section-hint" style="color:#b91c1c;">Failed to load price list: ${escapeHtml(fetchError)}. Try closing and reopening this list.</p>`;
                return;
            }
            const months = Array.from(new Set(nfpPricesCache.map((p) => p.month))).sort().reverse();
            const sel = document.getElementById("nfpListMonth");
            sel.innerHTML = "";
            months.forEach((ym) => {
                const opt = document.createElement("option");
                opt.value = ym;
                opt.textContent = fmtMonth(ym) || ym;
                sel.appendChild(opt);
            });
            // Default to the requested month, else the month selected above, else
            // whatever month the Basic Commission search box currently names.
            const nfpMonthVal = document.getElementById("nfpPreviewMonth")?.value || "";
            const basicMonthVal = parseMonthYearSearch(document.getElementById("previewMonthYearSearch")?.value || "").mVal;
            const pick = String(wantMonth || nfpMonthVal || basicMonthVal || "").padStart(2, "0");
            const viewYm = `2026-${pick}`;
            if (months.includes(viewYm)) sel.value = viewYm;
        }
        renderNfpList();
        document.getElementById("nfpListCard").scrollIntoView({ behavior: "smooth", block: "start" });
    }

    // Power System per price row: within a month, a 650W table whose panel counts
    // start at 10+ is the three-phase table (same rule the engine uses); everything
    // else is single phase.
    function computeNfpPhases(rows) {
        const groups = {};
        rows.forEach((p) => {
            const gk = `${p.month}|${p.panel_rating}|${p.table_no}`;
            if (!groups[gk]) groups[gk] = { minPanels: Infinity, count: 0 };
            groups[gk].minPanels = Math.min(groups[gk].minPanels, p.panels);
        });
        const tablesPerRating = {};
        Object.keys(groups).forEach((gk) => {
            const [month, rating] = gk.split("|");
            const rk = `${month}|${rating}`;
            tablesPerRating[rk] = (tablesPerRating[rk] || 0) + 1;
        });
        const phase = {};
        Object.keys(groups).forEach((gk) => {
            const [month, rating] = gk.split("|");
            const multi = tablesPerRating[`${month}|${rating}`] > 1;
            phase[gk] = (rating === "650" && multi && groups[gk].minPanels >= 10) ? "three" : "single";
        });
        return phase;
    }

    function fmtRM(v) {
        return v != null ? Number(v).toLocaleString("en-MY", { minimumFractionDigits: 2 }) : "—";
    }

    // Sentence case with common acronyms kept uppercase; Chinese text unaffected.
    function sentenceCase(s) {
        let out = String(s).toLowerCase();
        out = out.charAt(0).toUpperCase() + out.slice(1);
        out = out.replace(/\btng\b/gi, "TNG").replace(/\bcny(\d*)\b/gi, (m, d) => "CNY" + d)
                 .replace(/\brm\b/gi, "RM").replace(/\bapr\b/gi, "APR").replace(/\bmar\b/gi, "MAR")
                 .replace(/\bfeb\b/gi, "FEB").replace(/\bsep\b/gi, "Sep")
                 .replace(/\bmid-autumn\b/gi, "Mid-Autumn");
        return out;
    }

    function fmtColValue(label, v) {
        if (v == null || v === "") return "—";
        if (typeof v === "number") {
            if (label.toLowerCase().includes("percentage")) {
                return v <= 1 ? `${Math.round(v * 100)}%` : `${v}%`;
            }
            return v.toLocaleString("en-MY", { minimumFractionDigits: 2 });
        }
        return String(v);
    }

    function rowColumns(p) {
        if (p.columns_json) {
            try { return JSON.parse(p.columns_json); } catch (e) { /* fall through */ }
        }
        return [
            ["Package selling price", p.package_price],
            ["Nett price after discount", p.final_price],
            ["Roadshow TNG", p.tng_rebate],
            ["Nett price with TNG after discount", p.final_with_tng]
        ].filter((c) => c[1] != null);
    }

    function renderNfpList() {
        const month = document.getElementById("nfpListMonth").value;
        const rating = document.getElementById("nfpListRating").value;
        const system = document.getElementById("nfpListSystem").value;
        const inverter = document.getElementById("nfpListInverter").value;
        const container = document.getElementById("nfpListTables");
        container.innerHTML = "";

        document.getElementById("nfpListSubtitle").textContent = fmtMonth(month) || month || "";

        const rows = (nfpPricesCache || []);
        const phaseFallback = computeNfpPhases(rows);
        const phaseOf = (p) => p.power_system || phaseFallback[`${p.month}|${p.panel_rating}|${p.table_no}`] || "single";
        const invOf = (p) => (p.inverter_type || "string").toLowerCase();

        const visible = rows.filter((p) => {
            if (month && p.month !== month) return false;
            if (rating && String(p.panel_rating) !== rating) return false;
            if (system && phaseOf(p) !== system) return false;
            if (inverter && invOf(p) !== inverter) return false;
            return true;
        });

        // One sub-table per Excel table — never merge different layouts.
        const groups = new Map();
        visible.forEach((p) => {
            const key = `${p.panel_rating}|${p.table_no}|${invOf(p)}`;
            if (!groups.has(key)) groups.set(key, []);
            groups.get(key).push(p);
        });
        const groupKeys = Array.from(groups.keys()).sort((a, b) => {
            const [ra, ta, ia] = a.split("|");
            const [rb, tb, ib] = b.split("|");
            return (Number(rb) - Number(ra)) || (Number(ta) - Number(tb)) || ia.localeCompare(ib);
        });

        groupKeys.forEach((key) => {
            const groupRows = groups.get(key);
            groupRows.sort((a, b) => a.panels - b.panels);
            const first = groupRows[0];
            const labels = rowColumns(first).map((c) => c[0]);

            const title = [
                `${first.panel_rating}W`,
                phaseOf(first) === "three" ? "Three phase" : "Single phase",
                invOf(first) === "hybrid" ? "Hybrid package" : "String package"
            ].join(" · ");
            const titleEl = document.createElement("div");
            titleEl.className = "nfp-group-title";
            titleEl.textContent = title;
            container.appendChild(titleEl);

            const wrap = document.createElement("div");
            wrap.className = "table-container";
            const bodyHtml = groupRows.map((p) => {
                const byLabel = {};
                rowColumns(p).forEach(([label, v]) => { byLabel[label] = v; });
                return `<tr><td class="sm-cell">${escapeHtml(p.panels)}</td>` +
                    labels.map((l) => `<td>${escapeHtml(fmtColValue(l, byLabel[l] != null ? byLabel[l] : null))}</td>`).join("") +
                    `</tr>`;
            }).join("");
            wrap.innerHTML = `
                <table class="dashboard-table" style="table-layout: auto;">
                    <thead><tr><th>No. panels</th>${labels.map((l) => `<th>${escapeHtml(sentenceCase(l))}</th>`).join("")}</tr></thead>
                    <tbody>${bodyHtml}</tbody>
                </table>
            `;
            container.appendChild(wrap);
        });

        document.getElementById("nfpListCount").textContent = `${visible.length} rows`;
        if (!visible.length) {
            container.innerHTML = `<div class="section-hint" style="padding-bottom:16px;">No price rows for this selection.</div>`;
        }
    }

    // ── Wiring ───────────────────────────────────────────────────────────────

    document.getElementById("editModeBtn")?.addEventListener("click", () => openAddModal("Basic Commission"));
    document.getElementById("nfpAddBtn")?.addEventListener("click", () => openAddModal("Net Floor Price Rate"));
    document.getElementById("nfpListBtn")?.addEventListener("click", () => showNfpList());
    document.getElementById("nfpListCloseBtn")?.addEventListener("click", () => {
        const card = document.getElementById("nfpListCard");
        if (card) card.style.display = "none";
    });
    document.getElementById("nfpListMonth")?.addEventListener("change", renderNfpList);
    document.getElementById("nfpListRating")?.addEventListener("change", renderNfpList);
    document.getElementById("nfpListSystem")?.addEventListener("change", renderNfpList);
    document.getElementById("nfpListInverter")?.addEventListener("change", renderNfpList);

    // ── Inline Add Row & Modal Edit Entry ─────────────────────────────────────
    let editingRawEntry = null;

    function addInlineRow(type) {
        const isBasic = type === "Basic Commission";
        const tbody = document.getElementById(isBasic ? "previewBody" : "nfpPreviewBody");
        if (!tbody) return;   // the Net Floor Price Commission table was removed
        const activeMonthNum = document.getElementById("previewMonth").value || "5";
        const defaultYm = `2026-${String(activeMonthNum).padStart(2, "0")}`;
        
        const tr = document.createElement("tr");
        tr.className = "inline-add-row";
        tr.style.backgroundColor = "#f0fdf4";
        
        if (isBasic) {
            tr.innerHTML = `
                <td class="sm-cell">${escapeHtml(type)}</td>
                <td class="sm-cell"><input type="month" class="inline-eff" value="${defaultYm}" style="width:115px; font-size:12px;"></td>
                <td class="sm-cell"><select class="inline-atype" style="font-size:12px;"><option value="Internal">Internal</option><option value="Outsource">Outsource</option></select></td>
                <td class="sm-cell"><select class="inline-role" style="font-size:12px;">${roleOptionsHtml("Internal", "")}</select></td>
                <td class="sm-cell"><input type="text" class="inline-agent" placeholder="(all agents)" style="width:110px; font-size:12px;"></td>
                <td class="sm-cell"><input type="number" class="inline-rate" step="0.05" placeholder="e.g. 3.25" style="width:75px; font-size:12px;"></td>
                <td><span style="color:var(--text-muted);">—</span></td>
                <td><span class="formula-cell">(a − b) × z</span></td>
                <td><span class="source-badge unified">Data page</span></td>
                <td style="white-space:nowrap;">
                    <button class="btn btn-primary inline-save-btn" style="padding:2px 8px; font-size:11px;">Save</button>
                    <button class="btn btn-secondary inline-cancel-btn" style="padding:2px 6px; font-size:11px;">✕</button>
                </td>
            `;
        } else {
            tr.innerHTML = `
                <td class="sm-cell">${escapeHtml(type)}</td>
                <td class="sm-cell"><select class="inline-atype" style="font-size:12px;"><option value="Internal">Internal</option><option value="Outsource">Outsource</option></select></td>
                <td class="sm-cell"><select class="inline-role" style="font-size:12px;">${roleOptionsHtml("Internal", "")}</select></td>
                <td class="sm-cell"><input type="number" class="inline-rate" step="0.05" placeholder="e.g. 25.0" style="width:75px; font-size:12px;"></td>
                <td><span style="color:var(--text-muted);">—</span></td>
                <td><span class="formula-cell">(c − d) × z</span></td>
                <td><span class="source-badge unified">Data page</span></td>
                <td style="white-space:nowrap;">
                    <button class="btn btn-primary inline-save-btn" style="padding:2px 8px; font-size:11px;">Save</button>
                    <button class="btn btn-secondary inline-cancel-btn" style="padding:2px 6px; font-size:11px;">✕</button>
                </td>
            `;
        }

        const atypeSel = tr.querySelector(".inline-atype");
        const roleSel = tr.querySelector(".inline-role");
        atypeSel.addEventListener("change", () => {
            roleSel.innerHTML = roleOptionsHtml(atypeSel.value, "");
        });

        tr.querySelector(".inline-cancel-btn").addEventListener("click", () => tr.remove());
        tr.querySelector(".inline-save-btn").addEventListener("click", async () => {
            const effDate = isBasic ? tr.querySelector(".inline-eff").value : defaultYm;
            const agentType = atypeSel.value || "Internal";
            const hierarchy = roleSel.value || "";
            const agent = isBasic ? (tr.querySelector(".inline-agent").value.trim() || "") : "";
            const ratePct = parseFloat(tr.querySelector(".inline-rate").value);

            if (isNaN(ratePct)) {
                alert("Please enter a valid rate %.");
                return;
            }

            const newEntry = {
                rate_type: type,
                agent_type: agentType,
                hierarchy: hierarchy,
                agent: agent,
                rate_pct: ratePct,
                effective_from: effDate,
                remarks: "Added inline via Data page"
            };

            await saveSingleEntry(newEntry);
        });

        tbody.appendChild(tr);
        const rateInput = tr.querySelector(".inline-rate");
        if (rateInput) rateInput.focus();
    }

    // ── Agent Name chip picker (Add/Edit Entry modal) ────────────────────────
    // Multi-select, same interaction as the Property Type chips. Selecting
    // several agents saves one entry per agent rather than one row holding a
    // comma list — every consumer of `agent` (rate resolution, rateKeyOf,
    // the preview's agent filter) matches a single name exactly.

    // Lowercased name -> display name, so a pick survives the list being
    // re-filtered by Agent Type / Role / search.
    let modalAgentSelected = new Map();

    /** Distinct agent names from the roles table, narrowed by the modal's own
     *  Agent Type and Role, de-duplicated case-insensitively — an agent with
     *  several role-history rows, or listed in both Postgres and SQLite, must
     *  appear exactly once. Anything already selected is always included, so
     *  opening an old entry can never silently drop its agent. */
    function modalAgentCandidates() {
        const atype = (document.getElementById("modalAgentType")?.value || "").trim().toLowerCase();
        const role = (document.getElementById("modalRole")?.value || "").trim();
        const seen = new Map();
        rolesList
            .filter((r) => !atype || String(r.agent_type || "").toLowerCase() === atype)
            .filter((r) => roleMatches(r.hierarchy, role))
            .forEach((r) => {
                const name = String(r.agent || "").trim();
                if (name && !seen.has(name.toLowerCase())) seen.set(name.toLowerCase(), name);
            });
        modalAgentSelected.forEach((name, key) => {
            if (!seen.has(key)) seen.set(key, name);
        });
        return [...seen.entries()].sort((a, b) => a[1].localeCompare(b[1]));
    }

    function updateModalAgentReadout() {
        const readout = document.getElementById("modalAgentSelectedText");
        if (!readout) return;
        const picked = [...modalAgentSelected.values()];
        if (!picked.length) {
            readout.textContent = "(Applies to All)";
            readout.style.color = "#64748b";
        } else if (picked.length <= 3) {
            readout.textContent = "(Selected: " + picked.join(", ") + ")";
            readout.style.color = "#2563eb";
        } else {
            readout.textContent = `(${picked.length} agents selected)`;
            readout.style.color = "#2563eb";
        }
    }

    function renderModalAgentChips() {
        const container = document.getElementById("modalAgentChipsContainer");
        if (!container) return;
        const query = (document.getElementById("modalAgentChipSearch")?.value || "").trim().toLowerCase();
        // Selected chips stay visible and sort first, so a pick never scrolls
        // out of reach behind a search term that no longer matches it.
        const entries = modalAgentCandidates().filter(([key]) =>
            modalAgentSelected.has(key) || !query || key.includes(query));
        entries.sort((a, b) => {
            const sa = modalAgentSelected.has(a[0]) ? 0 : 1;
            const sb = modalAgentSelected.has(b[0]) ? 0 : 1;
            return sa !== sb ? sa - sb : a[1].localeCompare(b[1]);
        });

        if (!entries.length) {
            container.innerHTML = `<span class="agent-chips-empty">${
                query ? "No agents match that search." : "No agents for this Agent Type / Role."
            }</span>`;
            updateModalAgentReadout();
            return;
        }

        container.innerHTML = entries.map(([key, name]) => {
            const on = modalAgentSelected.has(key);
            return `<span class="prop-chip${on ? " selected" : ""}" data-val="${escapeHtml(name)}">${
                on ? "✓" : "+"} ${escapeHtml(name)}</span>`;
        }).join("");

        container.querySelectorAll(".prop-chip").forEach((chip) => {
            chip.addEventListener("click", () => {
                const name = chip.getAttribute("data-val");
                const key = name.toLowerCase();
                if (modalAgentSelected.has(key)) modalAgentSelected.delete(key);
                else modalAgentSelected.set(key, name);
                renderModalAgentChips();
            });
        });
        updateModalAgentReadout();
    }

    /** `keep` seeds the selection when a modal opens: the saved entry's agent
     *  (one name), or "" for a fresh Add. Omit it to re-render in place. */
    function populateModalAgentOptions(keep) {
        if (keep !== undefined) {
            modalAgentSelected = new Map();
            String(keep || "").split(",").map((s) => s.trim()).filter(Boolean)
                .forEach((n) => modalAgentSelected.set(n.toLowerCase(), n));
            const search = document.getElementById("modalAgentChipSearch");
            if (search) search.value = "";
        }
        renderModalAgentChips();
    }

    function getSelectedModalAgents() {
        return [...modalAgentSelected.values()];
    }

    // A Net Floor Price row is a tier, not a rate on a sale: it has no property
    // type, no override and no profit sharing, so those blocks are taken off the
    // form rather than left there to be filled in and ignored. Payment stages DO
    // apply -- but `condition` is spoken for by the tier on these rows, so their
    // written form goes to `label` instead.
    const NFP_TYPE = "Net Floor Price Rate";

    /** "Solar Services" or "EV Services". A row saved before Job Type existed
     *  has none and reads as Solar Services, because every such row was
     *  written for solar -- the same default the commission engine applies. */
    function normJob(value) {
        const v = String(value || "").trim().toLowerCase();
        return v.startsWith("ev") ? "EV Services" : "Solar Services";
    }

    const EV_TYPES = ["EV Charger", "EV Installation", "EV Charger + EV Installation"];

    /** One of EV_TYPES, or "" (meaning "all EV types" / not yet narrowed).
     *  Only meaningful when Job Type is EV Services. */
    function normEvType(value) {
        const v = String(value || "").trim().toLowerCase();
        return EV_TYPES.find((t) => t.toLowerCase() === v) || "";
    }

    /** Shows/hides the EV Type row based on the modal's current Job Type
     *  value -- called on modal open and whenever Job Type changes. */
    function updateEvTypeRowVisibility() {
        const row = document.getElementById("modalEvTypeRow");
        if (!row) return;
        const jobEl = document.getElementById("modalJobType");
        const jobRow = document.getElementById("modalJobTypeRow");
        const jobVisible = jobRow && jobRow.style.display !== "none";
        row.style.display = (jobVisible && jobEl && jobEl.value === "EV Services") ? "" : "none";
    }

    function applyModalTypeVisibility(type) {
        const isNfp = type === NFP_TYPE;
        [["modalNfpTierRow", isNfp],
         ["modalJobTypeRow", type === "Basic Commission"],
         ["modalPropertyRow", !isNfp],
         ["modalProfitSharingRow", !isNfp],
         ["modalOverrideRulesRow", !isNfp]].forEach(([id, show]) => {
            const el = document.getElementById(id);
            if (el) el.style.display = show ? "" : "none";
        });
        updateEvTypeRowVisibility();
        const rateEl = document.getElementById("modalRatePct");
        if (rateEl) rateEl.placeholder = isNfp ? "e.g. 25" : "e.g. 3.25";
    }

    /** Match a stored tier back to one of the three options. Compares on the
     *  comparison itself ("sales price > net floor price") so the descriptive
     *  tail can be reworded without orphaning rows already saved -- including
     *  the "i." / "ii." / "iii." numerals these options used to carry. */
    function setNfpTierValue(condition) {
        const sel = document.getElementById("modalNfpTier");
        if (!sel) return;
        const core = (s) => String(s || "").toLowerCase()
            .replace(/^\s*(iii|ii|i)\.\s*/, "")
            .split(/[—(]/)[0]
            .replace(/\s+/g, " ")
            .trim();
        const want = core(condition);
        const match = want
            ? Array.from(sel.options).find((o) => core(o.value) === want)
            : null;
        sel.value = match ? match.value : sel.options[0].value;
    }

    function openAddModal(type) {
        // Open the Edit Entry modal with blank defaults for a new entry
        const blankEntry = {
            rate_type: type,
            effective_from: CURRENT_YM,
            agent_type: "Internal",
            hierarchy: "",
            agent: "",
            property_type: "",
            rate_pct: "",
            override_rate_pct: "",
            override_from: "",
            profit_sharing_rate_pct: "",
            profit_sharing_mode: "",
            rule_type: "",
            trigger_pct: "",
            amount_rm: "",
            invoice_date_from: "",
            remarks: ""
        };
        editingRawEntry = null;   // null = new entry, saveSingleEntry will push not update
        const modal = document.getElementById("dataEditModal");
        if (!modal) return;

        const titleEl = document.getElementById("dataModalTitle");
        if (titleEl) titleEl.textContent = `Add Entry — ${type}`;

        const typeEl = document.getElementById("modalType");
        if (typeEl) typeEl.value = type;
        applyModalTypeVisibility(type);
        setNfpTierValue("");
        const jobEl = document.getElementById("modalJobType");
        if (jobEl) jobEl.value = "Solar Services";
        const evEl = document.getElementById("modalEvType");
        if (evEl) evEl.value = "";
        updateEvTypeRowVisibility();

        // Reset Invoice Month to single / current month
        const modeSelEl = document.getElementById("modalMonthMode");
        if (modeSelEl) modeSelEl.value = "single";
        const singleBox = document.getElementById("singleMonthBox");
        const rangeBox = document.getElementById("rangeMonthBox");
        if (singleBox) singleBox.style.display = "flex";
        if (rangeBox) rangeBox.style.display = "none";
        const effEl = document.getElementById("modalEffDate");
        if (effEl) effEl.value = CURRENT_YM;

        // Reset agent fields
        const atypeEl = document.getElementById("modalAgentType");
        if (atypeEl) atypeEl.value = "Internal";
        const roleSel = document.getElementById("modalRole");
        if (roleSel) {
            roleSel.innerHTML = `<option value="">(all)</option>` + roleOptionsHtml("Internal", "");
            roleSel.value = "";
        }
        populateModalAgentOptions("");

        // Clear all property chips
        setSelectedPropChips("");

        // Reset rates
        const rateEl = document.getElementById("modalRatePct");
        if (rateEl) rateEl.value = "";
        const ovrRateEl = document.getElementById("modalOverrideRatePct");
        if (ovrRateEl) ovrRateEl.value = "";
        const ofSel = document.getElementById("modalOverrideFrom");
        if (ofSel) ofSel.innerHTML = overrideFromOptionsHtml("Internal", "");
        const psRateEl = document.getElementById("modalProfitSharingRatePct");
        if (psRateEl) psRateEl.value = "";

        // Reset payment rules to one blank row
        setPaymentRulesInList([]);

        const invDateEl = document.getElementById("modalInvoiceDate");
        if (invDateEl) invDateEl.value = "";
        const remEl = document.getElementById("modalRemarks");
        if (remEl) remEl.value = "";

        modal.classList.remove("hidden");
        // Add mode: hide Delete, label Save as "Add Entry"
        const delBtn = document.getElementById("modalDeleteBtn");
        if (delBtn) delBtn.style.display = "none";
        const saveBtn = document.getElementById("modalSaveBtn");
        if (saveBtn) saveBtn.textContent = "Add Entry";
    }

    function openDataEditModal(r, type) {
        editingRawEntry = r;
        const modal = document.getElementById("dataEditModal");
        if (!modal) return;

        const titleEl = document.getElementById("dataModalTitle");
        if (titleEl) titleEl.textContent = `Edit Entry — ${type}`;

        const typeEl = document.getElementById("modalType");
        if (typeEl) typeEl.value = type;
        applyModalTypeVisibility(type);
        setNfpTierValue(r.condition);
        const jobEl = document.getElementById("modalJobType");
        if (jobEl) jobEl.value = normJob(r.job_type);
        const evEl = document.getElementById("modalEvType");
        if (evEl) evEl.value = normEvType(r.ev_type);
        updateEvTypeRowVisibility();

        const modeSelEl = document.getElementById("modalMonthMode");
        const singleBox = document.getElementById("singleMonthBox");
        const rangeBox = document.getElementById("rangeMonthBox");
        const effStr = (r.effective_from || CURRENT_YM).toString();
        if (effStr.includes(" to ") || effStr.includes("..")) {
            const parts = effStr.split(/ to |\.\./);
            if (modeSelEl) modeSelEl.value = "range";
            if (singleBox) singleBox.style.display = "none";
            if (rangeBox) rangeBox.style.display = "flex";
            const fromEl = document.getElementById("modalEffDateFrom");
            if (fromEl) fromEl.value = parts[0].trim();
            const toEl = document.getElementById("modalEffDateTo");
            if (toEl) toEl.value = parts[1] ? parts[1].trim() : parts[0].trim();
        } else {
            if (modeSelEl) modeSelEl.value = "single";
            if (singleBox) singleBox.style.display = "flex";
            if (rangeBox) rangeBox.style.display = "none";
            const effEl = document.getElementById("modalEffDate");
            if (effEl) effEl.value = effStr;
        }

        const atypeEl = document.getElementById("modalAgentType");
        if (atypeEl) atypeEl.value = r.agent_type || "Internal";
        
        const roleSel = document.getElementById("modalRole");
        if (roleSel) {
            roleSel.innerHTML = `<option value="">(all)</option>` + roleOptionsHtml(r.agent_type || "Internal", r.hierarchy || "");
            roleSel.value = r.hierarchy || "";
        }

        populateModalAgentOptions(r.agent || "");

        setSelectedPropChips(r.property_type || "");

        const rateEl = document.getElementById("modalRatePct");
        if (rateEl) rateEl.value = r.rate_pct != null && r.rate_pct !== "" ? r.rate_pct : "";

        setOverrideRulesInList(parseOverrideRulesFromEntry(r));

        const psRateEl = document.getElementById("modalProfitSharingRatePct");
        if (psRateEl) psRateEl.value = r.profit_sharing_rate_pct != null && r.profit_sharing_rate_pct !== "" ? r.profit_sharing_rate_pct : "";
        
        const modeSel = document.getElementById("modalSharingMode");
        if (modeSel) {
            modeSel.innerHTML = sharingModeOptionsHtml(r.profit_sharing_mode || "");
            modeSel.value = r.profit_sharing_mode || "";
        }

        // On an NFP row the payout stages live in `label`, so the parser is
        // pointed at that instead of the tier sitting in `condition`.
        setPaymentRulesInList(parsePaymentRulesFromEntry(
            type === NFP_TYPE ? Object.assign({}, r, { condition: r.label || "" }) : r));

        const invDateEl = document.getElementById("modalInvoiceDate");
        if (invDateEl) invDateEl.value = r.invoice_date_from || "";

        const remEl = document.getElementById("modalRemarks");
        if (remEl) remEl.value = r.remarks || "";

        modal.classList.remove("hidden");
        // Edit mode: show Delete, restore Save label
        const delBtn = document.getElementById("modalDeleteBtn");
        if (delBtn) delBtn.style.display = "";
        const saveBtn = document.getElementById("modalSaveBtn");
        if (saveBtn) saveBtn.textContent = "Save Changes";
    }

    function closeDataEditModal() {
        editingRawEntry = null;
        const modal = document.getElementById("dataEditModal");
        if (modal) modal.classList.add("hidden");
    }

    function updatePropChipsReadout() {
        const readout = document.getElementById("modalPropertySelectedText");
        if (!readout) return;
        const selected = getSelectedPropChips();
        if (!selected.length) {
            readout.textContent = "(Applies to All)";
            readout.style.color = "#64748b";
        } else {
            readout.textContent = "(Selected: " + selected.join(", ") + ")";
            readout.style.color = "#2563eb";
        }
    }

    // Property Chip Skill Tags
    function initPropChips() {
        const container = document.getElementById("modalPropertyChipsContainer");
        if (!container) return;
        const chips = container.querySelectorAll(".prop-chip");
        chips.forEach((chip) => {
            chip.addEventListener("click", () => {
                const val = chip.getAttribute("data-val");
                if (val === "All Types") {
                    chips.forEach((c) => {
                        c.classList.remove("selected");
                        c.textContent = "+ " + c.getAttribute("data-val");
                    });
                    chip.classList.add("selected");
                    chip.textContent = "✓ All Types";
                } else {
                    const allChip = container.querySelector('.prop-chip[data-val="All Types"]');
                    if (allChip) {
                        allChip.classList.remove("selected");
                        allChip.textContent = "+ All Types";
                    }
                    chip.classList.toggle("selected");
                    if (chip.classList.contains("selected")) {
                        chip.textContent = "✓ " + val;
                    } else {
                        chip.textContent = "+ " + val;
                    }
                }

                updatePropChipsReadout();

                const selectedVals = getSelectedPropChips();
                const hasSpecial = selectedVals.some((v) => {
                    const vl = v.toLowerCase();
                    return vl === "factory" || vl === "ngo" || vl.includes("goverment") || vl.includes("government");
                });
                if (hasSpecial) {
                    const psInput = document.getElementById("modalProfitSharingRatePct");
                    if (psInput && (!psInput.value || psInput.value === "—")) {
                        psInput.value = "x";
                    }
                }
            });
        });
    }

    function getSelectedPropChips() {
        const chips = document.querySelectorAll("#modalPropertyChipsContainer .prop-chip.selected");
        const vals = Array.from(chips).map((c) => c.getAttribute("data-val")).filter((v) => v !== "All Types");
        return vals;
    }

    function setSelectedPropChips(propStr) {
        const container = document.getElementById("modalPropertyChipsContainer");
        if (!container) return;
        const rawVals = (propStr || "").split(",").map((s) => s.trim()).filter(Boolean);
        const normVals = rawVals.map(normProp);
        const chips = container.querySelectorAll(".prop-chip");
        const isAll = !normVals.length;

        chips.forEach((chip) => {
            const val = chip.getAttribute("data-val");
            if (val === "All Types") {
                if (isAll) {
                    chip.classList.add("selected");
                    chip.textContent = "✓ All Types";
                } else {
                    chip.classList.remove("selected");
                    chip.textContent = "+ All Types";
                }
            } else {
                const normV = normProp(val);
                const isSelected = normVals.includes(normV);
                if (isSelected && !isAll) {
                    chip.classList.add("selected");
                    chip.textContent = "✓ " + val;
                } else {
                    chip.classList.remove("selected");
                    chip.textContent = "+ " + val;
                }
            }
        });

        updatePropChipsReadout();
    }

    /** `keep` is the value the dropdown is currently showing. A rule written
     *  against a retired label ("Senior", "OSA/OSA1", …) has to keep it as an
     *  option, or rebuilding the list drops the selection back to "(all roles)"
     *  and widens a rule that was deliberately scoped to one role. */
    function getOverrideRolesForAgentType(agentType, keep) {
        const atype = agentType || document.getElementById("modalAgentType")?.value || "Internal";
        const roles = (RATE_ROLES[atype] || []).slice();
        if (keep && !roles.includes(keep)) roles.push(keep);
        return [""].concat(roles);
    }

    function refreshOverrideFromDropdowns() {
        const rows = document.querySelectorAll("#overrideRulesList .orule-row");
        rows.forEach((r) => {
            const sel = r.querySelector(".orule-from");
            if (!sel) return;
            const currentVal = sel.value;
            const availableRoles = getOverrideRolesForAgentType(null, currentVal);
            sel.innerHTML = availableRoles.map((role) =>
                `<option value="${escapeHtml(role)}" ${role === currentVal ? "selected" : ""}>${role ? escapeHtml(role) : "(all roles)"}</option>`
            ).join("");
        });
    }

    // Override Rules Adder (Multiple Override Rates & From Roles)
    function addOverrideRuleRow(pct = "", fromRole = "") {
        const list = document.getElementById("overrideRulesList");
        if (!list) return;
        const row = document.createElement("div");
        row.className = "orule-row";
        row.style.display = "flex";
        row.style.alignItems = "center";
        row.style.gap = "10px";

        const availableRoles = getOverrideRolesForAgentType(null, fromRole);

        const roleOptsHtml = availableRoles.map((role) =>
            `<option value="${escapeHtml(role)}" ${role === fromRole ? "selected" : ""}>${role ? escapeHtml(role) : "(all roles)"}</option>`
        ).join("");

        row.innerHTML = `
            <span style="font-size:12px; font-weight:600; color:var(--text-muted);">Override Rate:</span>
            <input type="number" class="orule-pct" step="0.05" value="${escapeHtml(pct)}" placeholder="e.g. 0.5" style="width:90px; padding:6px 8px; font-size:12.5px; border:1px solid var(--border-color); border-radius:6px;">
            <span style="font-size:12px; font-weight:600; color:var(--text-muted);">% &nbsp;|&nbsp; Override From Role:</span>
            <select class="orule-from" style="padding:6px 8px; font-size:12.5px; border:1px solid var(--border-color); border-radius:6px; min-width:140px;">
                ${roleOptsHtml}
            </select>
            <button type="button" class="btn btn-danger btn-sm orule-remove-btn" style="padding:2px 8px; font-size:11px; margin-left:auto;">✕</button>
        `;
        row.querySelector(".orule-remove-btn").addEventListener("click", () => row.remove());
        list.appendChild(row);
    }

    function getOverrideRulesFromList() {
        const rows = document.querySelectorAll("#overrideRulesList .orule-row");
        const rules = [];
        rows.forEach((r) => {
            const pct = r.querySelector(".orule-pct").value.trim();
            const fromRole = r.querySelector(".orule-from").value.trim();
            if (pct || fromRole) {
                rules.push({ override_rate_pct: pct, override_from: fromRole });
            }
        });
        return rules;
    }

    function parseOverrideRulesFromEntry(r) {
        if (r.oRules && Array.isArray(r.oRules) && r.oRules.length) return r.oRules;
        const pctStr = String(r.override_rate_pct || "").trim();
        const fromStr = String(r.override_from || "").trim();
        if (pctStr.includes(",") || fromStr.includes(",")) {
            const pcts = pctStr.split(",").map((s) => s.trim());
            const froms = fromStr.split(",").map((s) => s.trim());
            const maxLen = Math.max(pcts.length, froms.length);
            const rules = [];
            for (let i = 0; i < maxLen; i++) {
                rules.push({
                    override_rate_pct: pcts[i] || pcts[0] || "",
                    override_from: froms[i] || froms[0] || ""
                });
            }
            return rules;
        }
        if (pctStr || fromStr) {
            return [{ override_rate_pct: pctStr, override_from: fromStr }];
        }
        return [];
    }

    function setOverrideRulesInList(rules) {
        const list = document.getElementById("overrideRulesList");
        if (list) list.innerHTML = "";
        if (rules && rules.length > 0 && (rules[0].override_rate_pct || rules[0].override_from)) {
            rules.forEach((r) => addOverrideRuleRow(r.override_rate_pct || "", r.override_from || ""));
        } else {
            addOverrideRuleRow("", "");
        }
    }

    function formatOverrideRulesHtml(pctStr, fromStr) {
        const pStr = String(pctStr == null ? "" : pctStr).trim();
        const fStr = String(fromStr == null ? "" : fromStr).trim();
        if (!pStr && !fStr) return `<span style="color:var(--text-muted);">—</span>`;
        if (pStr.includes(",") || fStr.includes(",")) {
            const pcts = pStr.split(",").map((s) => s.trim());
            const froms = fStr.split(",").map((s) => s.trim());
            const maxLen = Math.max(pcts.length, froms.length);
            const lines = [];
            for (let i = 0; i < maxLen; i++) {
                const p = pcts[i] || pcts[0] || "";
                const f = froms[i] || froms[0] || "";
                if (p || f) {
                    lines.push(`<span style="font-weight:600; color:#1e293b;">${escapeHtml(p ? p + "%" : "")}</span> <span style="color:#475569; font-size:12px;">${escapeHtml(f ? "(from " + f + ")" : "")}</span>`);
                }
            }
            return `<div style="display:flex; flex-direction:column; gap:2px; padding:2px 0;">${lines.map((l) => `<div>${l}</div>`).join("")}</div>`;
        }
        return `<span><strong style="font-weight:600; color:#1e293b;">${escapeHtml(pStr ? pStr + "%" : "")}</strong> <span style="color:#475569; font-size:12px;">${escapeHtml(fStr ? "(from " + fStr + ")" : "")}</span></span>`;
    }

    // Payment Rules Adder
    function addPaymentRuleRow(pct = "", ruleType = "Payout", amt = "") {
        const list = document.getElementById("paymentRulesList");
        if (!list) return;
        const row = document.createElement("div");
        row.className = "prule-row";
        row.innerHTML = `
            <span style="font-size:12px; font-weight:600; color:var(--text-muted);">Payment ≥</span>
            <input type="number" class="prule-pct" step="1" value="${escapeHtml(pct)}" placeholder="e.g. 75" style="width:90px; padding:6px 8px; font-size:12.5px; border:1px solid var(--border-color); border-radius:6px;">
            <span style="font-size:12px; font-weight:600; color:var(--text-muted);">% &nbsp;|&nbsp; Rule:</span>
            <select class="prule-type" style="padding:6px 8px; font-size:12.5px; border:1px solid var(--border-color); border-radius:6px; min-width:100px;">
                <option value="Payout" ${ruleType === "Payout" ? "selected" : ""}>Payout</option>
                <option value="Advance" ${ruleType === "Advance" ? "selected" : ""}>Advance</option>
            </select>
            <span style="font-size:12px; font-weight:600; color:var(--text-muted);">Amount (RM):</span>
            <input type="number" class="prule-amount" step="10" value="${escapeHtml(amt)}" placeholder="—" style="width:100px; padding:6px 8px; font-size:12.5px; border:1px solid var(--border-color); border-radius:6px;">
            <button type="button" class="btn btn-danger btn-sm prule-remove-btn" style="padding:2px 8px; font-size:11px; margin-left:auto;">✕</button>
        `;
        row.querySelector(".prule-remove-btn").addEventListener("click", () => row.remove());
        list.appendChild(row);
    }

    function getPaymentRulesFromList() {
        const rows = document.querySelectorAll("#paymentRulesList .prule-row");
        const rules = [];
        rows.forEach((r) => {
            const pct = r.querySelector(".prule-pct").value.trim();
            const rtype = r.querySelector(".prule-type").value;
            const amt = r.querySelector(".prule-amount").value.trim();
            if (pct || amt) {
                rules.push({ trigger_pct: pct, rule_type: rtype, amount_rm: amt });
            }
        });
        return rules;
    }

    function formatPaymentRulesCondition(pRules) {
        if (!pRules || !pRules.length) return "";
        const parts = [];
        pRules.forEach((r) => {
            const pct = r.trigger_pct ? `≥ ${r.trigger_pct}%` : "";
            if (r.rule_type === "Advance" && r.amount_rm) {
                parts.push(`Advance RM ${r.amount_rm} at ${pct}`);
            } else if (r.rule_type === "Advance") {
                parts.push(`Advance at ${pct}`);
            } else if (r.rule_type === "Payout") {
                parts.push(`Pays at ${pct} payment`);
            } else {
                parts.push(`${pct} payment`);
            }
        });
        return parts.join("\n");
    }

    function formatConditionHtml(condStr) {
        if (!condStr) return `<span style="color:var(--text-muted);">—</span>`;
        let lines = [];
        if (condStr.includes("\n")) {
            lines = condStr.split("\n").map((s) => s.trim()).filter(Boolean);
        } else if (condStr.includes(", ")) {
            lines = condStr.split(", ").map((s) => s.trim()).filter(Boolean);
        } else {
            lines = [condStr.trim()];
        }

        if (lines.length > 1) {
            const lineHtml = lines.map((line) => `<div style="white-space:nowrap; line-height:1.4;">${escapeHtml(line)}</div>`).join("");
            return `<div class="rule-cell" style="display:flex; flex-direction:column; gap:2px; padding:2px 0; background:transparent;">${lineHtml}</div>`;
        }
        return `<span class="rule-cell" title="${escapeHtml(condStr)}">${escapeHtml(condStr)}</span>`;
    }

    function parsePaymentRulesFromEntry(r) {
        if (r.pRules && Array.isArray(r.pRules) && r.pRules.length) return r.pRules;
        const cond = String(r.condition || "");
        if (cond.includes("Advance") || cond.includes("advance") || cond.includes("Pays") || cond.includes("balance") || cond.includes(",") || cond.includes("\n")) {
            const rules = [];
            const parts = cond.split(/[\n,]\s*/);
            parts.forEach((p) => {
                const mAdv = /(?:Advance|advance|RM)\s*(?:RM)?\s*(\d+)\s+at\s+≥?\s*([\d\.]+)%/i.exec(p) || /RM\s*(\d+)\s+advance\s+at\s+≥?\s*([\d\.]+)%/i.exec(p);
                if (mAdv) {
                    rules.push({ trigger_pct: mAdv[2], rule_type: "Advance", amount_rm: mAdv[1] });
                    return;
                }
                const mAdvSimple = /(?:Advance|advance)\s+at\s+≥?\s*([\d\.]+)%/i.exec(p);
                if (mAdvSimple) {
                    rules.push({ trigger_pct: mAdvSimple[1], rule_type: "Advance", amount_rm: r.amount_rm || "300" });
                    return;
                }
                const mBal = /(?:Pays|balance|payout)\s+at\s+≥?\s*([\d\.]+)%/i.exec(p);
                if (mBal) {
                    rules.push({ trigger_pct: mBal[1], rule_type: "Payout", amount_rm: "" });
                    return;
                }
                const mPct = /≥?\s*([\d\.]+)%/i.exec(p);
                if (mPct) {
                    rules.push({ trigger_pct: mPct[1], rule_type: "Payout", amount_rm: "" });
                }
            });
            if (rules.length) return rules;
        }
        if (r.trigger_pct || r.rule_type || r.amount_rm) {
            return [{ trigger_pct: r.trigger_pct || "", rule_type: r.rule_type || "Payout", amount_rm: r.amount_rm || "" }];
        }
        return [];
    }

    function setPaymentRulesInList(rules) {
        const list = document.getElementById("paymentRulesList");
        if (list) list.innerHTML = "";
        if (rules && rules.length > 0 && (rules[0].trigger_pct || rules[0].rule_type || rules[0].amount_rm)) {
            rules.forEach((r) => addPaymentRuleRow(r.trigger_pct || "", r.rule_type || "Payout", r.amount_rm || ""));
        } else {
            addPaymentRuleRow("", "Payout", "");
        }
    }

    /** EV Type only distinguishes rows once Job Type is EV Services -- a Solar
     *  row's ev_type cell (if any, e.g. stray data) never affects identity. */
    function evKey(e) {
        return normJob(e.job_type) === "EV Services" ? normEvType(e.ev_type) : "";
    }

    function isSameRawEntry(e, target) {
        if (target.id && e.id && String(e.id) === String(target.id)) return true;
        const norm = (v) => String(v || "").trim().toLowerCase();
        const rType = norm(e.rate_type || "Basic Commission") === norm(target.rate_type || "Basic Commission");
        const aType = norm(e.agent_type) === norm(target.agent_type);
        const hier = norm(e.hierarchy) === norm(target.hierarchy);
        const ag = norm(e.agent) === norm(target.agent);
        const eff = norm(e.effective_from) === norm(target.effective_from);
        const prop = normProp(e.property_type) === normProp(target.property_type);
        const job = normJob(e.job_type) === normJob(target.job_type);
        const ev = evKey(e) === evKey(target);
        // NFP tiers differ only by condition (see fullEntryKey), so without this
        // deleting one tier would take the other two with it.
        const cond = norm(e.rate_type) !== norm(NFP_TYPE)
            || norm(e.condition) === norm(target.condition);
        return rType && aType && hier && ag && eff && prop && job && ev && cond;
    }
    function fullEntryKey(e) {
        const norm = (v) => String(v || "").trim().toLowerCase();
        const type = norm(e.rate_type || "Basic Commission");
        return [
            type,
            norm(e.agent_type),
            norm(e.hierarchy),
            norm(e.agent),
            normProp(e.property_type),
            evKey(e),
            normJob(e.job_type),
            norm(e.effective_from),
            // The three NFP tiers are one role, one month and one property type
            // apart from their condition, so without it they collapse into a
            // single row: saving tier ii would silently overwrite tier i.
            type === norm(NFP_TYPE) ? norm(e.condition) : ""
        ].join("|");
    }

    async function saveSingleEntry(newOrUpdatedEntry) {
        const res = await api("/api/commission-rates");
        let currentEntries = (await res.json()).map((r) => ({
            id: r.id,
            rate_type: r.rate_type || "Basic Commission",
            agent_type: r.agent_type || "", hierarchy: r.hierarchy || "", agent: r.agent || "",
            label: r.label || "", condition: r.condition || "",
            rate_pct: r.rate_pct || "", override_rate_pct: r.override_rate_pct || "",
            override_from: r.override_from || "",
            profit_sharing_rate_pct: r.profit_sharing_rate_pct || "",
            profit_sharing_mode: r.profit_sharing_mode || "",
            property_type: r.property_type || "", trigger_pct: r.trigger_pct || "",
            job_type: r.job_type || "", ev_type: r.ev_type || "",
            invoice_date_from: r.invoice_date_from || "", rule_type: r.rule_type || "",
            amount_rm: r.amount_rm || "",
            effective_from: r.effective_from, remarks: r.remarks || ""
        }));

        let idx = -1;
        if (editingRawEntry && editingRawEntry.id) {
            idx = currentEntries.findIndex((e) => String(e.id) === String(editingRawEntry.id));
        }
        if (idx === -1) {
            idx = currentEntries.findIndex((e) => fullEntryKey(e) === fullEntryKey(newOrUpdatedEntry));
        }

        if (idx !== -1) {
            currentEntries[idx] = Object.assign({}, currentEntries[idx], newOrUpdatedEntry);
        } else {
            currentEntries.push(newOrUpdatedEntry);
        }

        // Deduplicate exact duplicate rows only (same role, agent, property_type, effective_from)
        const seenKeys = new Set();
        currentEntries = currentEntries.filter((e) => {
            if (String(e.remarks || "").toLowerCase() === "deleted") return true;
            const k = fullEntryKey(e);
            if (seenKeys.has(k)) return false;
            seenKeys.add(k);
            return true;
        });

        const saveRes = await api("/api/commission-rates", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ entries: currentEntries })
        });

        if (saveRes.ok) {
            await loadEntries();
            loadPreview();
            closeDataEditModal();
        } else {
            alert("Failed to save entry");
        }
    }

    async function deleteModalEntry() {
        if (!editingRawEntry) return;
        if (!confirm("Are you sure you want to delete this entry?")) return;

        const res = await api("/api/commission-rates");
        const rawJson = await res.json();
        let currentEntries = rawJson.map((r) => ({
            id: r.id,
            rate_type: r.rate_type || "Basic Commission",
            agent_type: r.agent_type || "", hierarchy: r.hierarchy || "", agent: r.agent || "",
            label: r.label || "", condition: r.condition || "",
            rate_pct: r.rate_pct || "", override_rate_pct: r.override_rate_pct || "",
            override_from: r.override_from || "",
            profit_sharing_rate_pct: r.profit_sharing_rate_pct || "",
            profit_sharing_mode: r.profit_sharing_mode || "",
            property_type: r.property_type || "", trigger_pct: r.trigger_pct || "",
            job_type: r.job_type || "", ev_type: r.ev_type || "",
            invoice_date_from: r.invoice_date_from || "", rule_type: r.rule_type || "",
            amount_rm: r.amount_rm || "",
            effective_from: r.effective_from, remarks: r.remarks || ""
        }));

        const isMatch = (e) => {
            if (editingRawEntry.id && e.id && String(e.id) === String(editingRawEntry.id)) return true;
            return isSameRawEntry(e, editingRawEntry);
        };

        currentEntries = currentEntries.filter((e) => !isMatch(e));

        // Always push a tombstone entry so basic_rates_resolved_api suppresses fallbacks for deleted item
        currentEntries.push({
            rate_type: editingRawEntry.rate_type || "Basic Commission",
            agent_type: editingRawEntry.agent_type || "",
            hierarchy: editingRawEntry.hierarchy || "",
            agent: editingRawEntry.agent || "",
            property_type: editingRawEntry.property_type || "",
            job_type: editingRawEntry.job_type || "",
            ev_type: editingRawEntry.ev_type || "",
            effective_from: editingRawEntry.effective_from || CURRENT_YM,
            // Keeps an NFP tombstone on its own tier instead of standing in for
            // all three.
            condition: editingRawEntry.condition || "",
            rate_pct: "",
            remarks: "deleted"
        });

        const saveRes = await api("/api/commission-rates", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ entries: currentEntries })
        });

        if (saveRes.ok) {
            await loadEntries();
            loadPreview();
            closeDataEditModal();
        } else {
            alert("Failed to delete entry");
        }
    }

    initPropChips();

    const addOverrideRuleBtn = document.getElementById("addOverrideRuleBtn");
    if (addOverrideRuleBtn) {
        addOverrideRuleBtn.addEventListener("click", () => addOverrideRuleRow("", ""));
    }

    const addPaymentRuleBtn = document.getElementById("addPaymentRuleBtn");
    if (addPaymentRuleBtn) {
        addPaymentRuleBtn.addEventListener("click", () => addPaymentRuleRow("", "Payout", ""));
    }

    const modalMonthModeSel = document.getElementById("modalMonthMode");
    if (modalMonthModeSel) {
        modalMonthModeSel.addEventListener("change", () => {
            const mode = modalMonthModeSel.value;
            const singleBox = document.getElementById("singleMonthBox");
            const rangeBox = document.getElementById("rangeMonthBox");
            if (mode === "range") {
                if (singleBox) singleBox.style.display = "none";
                if (rangeBox) rangeBox.style.display = "flex";
            } else {
                if (singleBox) singleBox.style.display = "flex";
                if (rangeBox) rangeBox.style.display = "none";
            }
        });
    }

    const modalAgentTypeSel = document.getElementById("modalAgentType");
    if (modalAgentTypeSel) {
        modalAgentTypeSel.addEventListener("change", () => {
            const atype = modalAgentTypeSel.value || "Internal";
            const roleEl = document.getElementById("modalRole");
            if (roleEl) roleEl.innerHTML = `<option value="">(all)</option>` + roleOptionsHtml(atype, "");
            populateModalAgentOptions("");
            refreshOverrideFromDropdowns();
        });
    }

    // Narrowing the Role narrows the agents offered with it.
    const modalRoleSel = document.getElementById("modalRole");
    if (modalRoleSel) {
        modalRoleSel.addEventListener("change", () => populateModalAgentOptions());
    }

    const modalAgentChipSearch = document.getElementById("modalAgentChipSearch");
    if (modalAgentChipSearch) {
        modalAgentChipSearch.addEventListener("input", renderModalAgentChips);
    }

    const modalJobTypeEl = document.getElementById("modalJobType");
    if (modalJobTypeEl) modalJobTypeEl.addEventListener("change", updateEvTypeRowVisibility);

    const closeDataEditModalBtn = document.getElementById("closeDataEditModalBtn");
    if (closeDataEditModalBtn) closeDataEditModalBtn.addEventListener("click", closeDataEditModal);
    const modalCancelBtn = document.getElementById("modalCancelBtn");
    if (modalCancelBtn) modalCancelBtn.addEventListener("click", closeDataEditModal);

    const modalSaveBtn = document.getElementById("modalSaveBtn");
    if (modalSaveBtn) {
        modalSaveBtn.addEventListener("click", async () => {
            const getVal = (id) => {
                const el = document.getElementById(id);
                return el ? el.value : "";
            };
            const monthMode = getVal("modalMonthMode");
            let effFrom = CURRENT_YM;
            if (monthMode === "range") {
                const f = getVal("modalEffDateFrom");
                const t = getVal("modalEffDateTo");
                effFrom = f && t ? `${f} to ${t}` : (f || t || CURRENT_YM);
            } else {
                effFrom = getVal("modalEffDate") || CURRENT_YM;
            }

            const ratePct = getVal("modalRatePct").trim();

            // Rate % is required to appear in the table — warn and stop if missing
            if (!ratePct) {
                alert("Rate % is required. Please enter a rate before saving.");
                document.getElementById("modalRatePct")?.focus();
                return;
            }

            const oRules = getOverrideRulesFromList();
            // Position for position, so each rate keeps its own From Role; dropping
            // a blank "(all roles)" would shift the next one onto this rate.
            const primaryOverrideRate = oRules.map((r) => r.override_rate_pct).join(", ");
            const primaryOverrideFrom = oRules.some((r) => r.override_from)
                ? oRules.map((r) => r.override_from).join(", ") : "";

            const pRules = getPaymentRulesFromList();
            const formattedCond = formatPaymentRulesCondition(pRules);
            const primaryTrigger = pRules.map((r) => r.trigger_pct).filter(Boolean).join(", ");
            const primaryAdvance = (pRules.find((r) => r.rule_type === "Advance") || {}).amount_rm || "";
            const primaryRuleType = pRules.length > 1 ? "Multi-stage" : ((pRules[0] || {}).rule_type || "");

            // Rate type: prefer saved entry's type, then modal hidden field, then default
            const rateType = (editingRawEntry && editingRawEntry.rate_type)
                || getVal("modalType")
                || "Basic Commission";

            // NFP rows carry their tier in `condition` and their payout stages
            // in `label`. The Basic-only inputs are hidden for them, so whatever
            // those held is left out rather than saved as stale values on a row
            // that has no use for them.
            if (rateType === NFP_TYPE) {
                await saveSingleEntry({
                    rate_type: rateType,
                    effective_from: effFrom,
                    agent_type: getVal("modalAgentType") || "Internal",
                    hierarchy: getVal("modalRole") || "",
                    agent: getSelectedModalAgents().join(", "),
                    property_type: "",
                    rate_pct: ratePct,
                    override_rate_pct: "", override_from: "", oRules: [],
                    profit_sharing_rate_pct: "", profit_sharing_mode: "",
                    rule_type: primaryRuleType,
                    trigger_pct: primaryTrigger,
                    amount_rm: primaryAdvance,
                    condition: getVal("modalNfpTier"),
                    label: formattedCond,
                    pRules: pRules,
                    invoice_date_from: getVal("modalInvoiceDate"),
                    remarks: getVal("modalRemarks").trim()
                });
                return;
            }

            const updated = {
                rate_type: rateType,
                effective_from: effFrom,
                agent_type: getVal("modalAgentType") || "Internal",
                hierarchy: getVal("modalRole") || "",
                // Every selected agent on ONE row, comma-joined — the Python
                // resolver treats the cell as a list (_row_agent_list), so a
                // shared rate stays a single entry instead of N near-duplicates.
                agent: getSelectedModalAgents().join(", "),
                property_type: getSelectedPropChips().join(", "),
                // Only Basic Commission rows are split by job; any other type
                // keeps whatever it already had.
                job_type: rateType === "Basic Commission"
                    ? (getVal("modalJobType") || "Solar Services")
                    : ((editingRawEntry && editingRawEntry.job_type) || ""),
                // EV Type only applies once Job Type is EV Services; blank means
                // "all EV types" (a general EV rate a specific row can override).
                ev_type: (rateType === "Basic Commission" && getVal("modalJobType") === "EV Services")
                    ? (getVal("modalEvType") || "")
                    : ((editingRawEntry && editingRawEntry.ev_type) || ""),
                rate_pct: ratePct,
                override_rate_pct: primaryOverrideRate,
                override_from: primaryOverrideFrom,
                oRules: oRules,
                profit_sharing_rate_pct: getVal("modalProfitSharingRatePct"),
                profit_sharing_mode: getVal("modalSharingMode"),
                rule_type: primaryRuleType,
                trigger_pct: primaryTrigger,
                amount_rm: primaryAdvance,
                condition: formattedCond,
                pRules: pRules,
                invoice_date_from: getVal("modalInvoiceDate"),
                remarks: getVal("modalRemarks").trim()
            };

            await saveSingleEntry(updated);
        });
    }

    const modalDeleteBtn = document.getElementById("modalDeleteBtn");
    if (modalDeleteBtn) modalDeleteBtn.addEventListener("click", deleteModalEntry);

    // ── NFP price list upload (xlsx, parse → preview → confirm) ─────────────

    const nfpUploadBtn = document.getElementById("nfpUploadBtn");
    if (nfpUploadBtn) {
        nfpUploadBtn.addEventListener("click", () => {
            document.getElementById("nfpUploadInput")?.click();
        });
    }

    const nfpUploadInput = document.getElementById("nfpUploadInput");
    if (nfpUploadInput) {
        nfpUploadInput.addEventListener("change", async (ev) => {
            const file = ev.target.files[0];
            ev.target.value = "";
            if (!file) return;
            const box = document.getElementById("nfpUploadPreview");
            if (!box) return;
            box.style.display = "";
            box.textContent = `Reading ${file.name}...`;
            const form = new FormData();
            form.append("file", file);
            let data;
            try {
                const res = await api("/api/nfp-prices/upload?mode=preview", { method: "POST", body: form });
                data = await res.json();
                if (!res.ok) throw new Error(data.error || "Upload failed");
            } catch (e) {
                box.innerHTML = `<span style="color:#dc2626;">${escapeHtml(e.message)}</span>`;
                return;
            }
            const sheetLines = data.summary.map((s) => {
                const tables = s.tables.map((t) => `${t.rating} ×${t.rows} rows`).join(", ");
                return `<li><strong>${escapeHtml(s.sheet)}</strong> → ${s.months.map(fmtMonth).join(", ")} (${escapeHtml(tables)})</li>`;
            }).join("");
            box.innerHTML = `
                <strong>Preview of ${escapeHtml(file.name)}:</strong>
                <ul style="margin:8px 0 8px 20px;">${sheetLines}</ul>
                ${data.total_rows} price rows for ${data.months.map(fmtMonth).join(", ")} —
                will replace <strong>${data.replaces}</strong> existing rows in those months
                (other months are untouched).<br><br>
                <button class="btn btn-primary" id="nfpUploadConfirm">Confirm upload</button>
                <button class="btn btn-secondary" id="nfpUploadCancel">Cancel</button>
            `;
            document.getElementById("nfpUploadCancel")?.addEventListener("click", () => {
                box.style.display = "none";
                box.innerHTML = "";
            });
            document.getElementById("nfpUploadConfirm")?.addEventListener("click", async () => {
                box.textContent = "Uploading...";
                try {
                    const res = await api("/api/nfp-prices/upload?mode=confirm", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ token: data.token })
                    });
                    const result = await res.json();
                    if (!res.ok) throw new Error(result.error || "Confirm failed");
                    box.innerHTML = `<span style="color:#16a34a;">Uploaded ${result.rows} rows for ` +
                        `${result.months.map(fmtMonth).join(", ")} (replaced ${result.replaced}). ` +
                        `Commission cache cleared.</span>`;
                    nfpPricesCache = null;
                    showNfpList();
                } catch (e) {
                    box.innerHTML = `<span style="color:#dc2626;">${escapeHtml(e.message)}</span>`;
                }
            });
        });
    }

    const addEntryBtn = document.getElementById("addEntryBtn");
    if (addEntryBtn) {
        addEntryBtn.addEventListener("click", () => {
            addUnifiedRow({ agent_type: "Internal", hierarchy: "Executive" }, currentMonth());
            const rows = document.querySelectorAll("#entriesBody tr.entry-row");
            if (rows.length) rows[rows.length - 1].querySelector(".e-value")?.focus();
        });
    }

    const addRoleBtn = document.getElementById("addRoleBtn");
    if (addRoleBtn) {
        addRoleBtn.addEventListener("click", () => {
            openAgentRoleModal(null);
        });
    }
    function initPreviewFilters() {
        ["previewPropertyType", "previewAgentName",
         "nfpPreviewYear", "nfpPreviewMonth"].forEach((id) => {
            const el = document.getElementById(id);
            if (el) el.addEventListener("change", loadPreview);
        });
        // Free-text search re-filters as you type rather than on blur/change,
        // debounced so a fast typist doesn't fire a request per keystroke.
        const monthYearSearchEl = document.getElementById("previewMonthYearSearch");
        if (monthYearSearchEl) {
            let searchTimer;
            monthYearSearchEl.addEventListener("input", () => {
                clearTimeout(searchTimer);
                searchTimer = setTimeout(loadPreview, 300);
            });
        }
        // Agent Type/Role drives Agent Name, so it rebuilds that dropdown
        // before re-filtering, same debounce as the month/year search box.
        const agentRoleSearchEl = document.getElementById("previewAgentRoleSearch");
        if (agentRoleSearchEl) {
            let searchTimer;
            agentRoleSearchEl.addEventListener("input", () => {
                clearTimeout(searchTimer);
                searchTimer = setTimeout(() => {
                    populatePreviewAgentNameFilter();
                    loadPreview();
                }, 300);
            });
        }
        populatePreviewAgentNameFilter();
    }

    const saveRolesBtn = document.getElementById("saveRolesBtn");
    if (saveRolesBtn) saveRolesBtn.addEventListener("click", saveRoles);
    const seedRolesBtn = document.getElementById("seedRolesBtn");
    if (seedRolesBtn) seedRolesBtn.addEventListener("click", seedRoles);

    // Agent type filter and search for roles table
    const rolesAgentTypeFilter = document.getElementById("rolesAgentTypeFilter");
    if (rolesAgentTypeFilter) rolesAgentTypeFilter.addEventListener("change", () => {
        rolesPage = 1;
        renderRoles();
    });

    const rolesSearchInput = document.getElementById("rolesSearchInput");
    if (rolesSearchInput) {
        rolesSearchInput.addEventListener("input", () => {
            rolesPage = 1;
            renderRoles();
        });
    }

    const rolesMonthFilter = document.getElementById("rolesMonthFilter");
    if (rolesMonthFilter) rolesMonthFilter.addEventListener("change", () => {
        rolesPage = 1;
        renderRoles();
    });

    const rolesMonthClearBtn = document.getElementById("rolesMonthClearBtn");
    if (rolesMonthClearBtn) rolesMonthClearBtn.addEventListener("click", () => {
        if (rolesMonthFilter) rolesMonthFilter.value = "";
        rolesPage = 1;
        renderRoles();
    });

    const rolesNeedsReviewFilter = document.getElementById("rolesNeedsReviewFilter");
    if (rolesNeedsReviewFilter) rolesNeedsReviewFilter.addEventListener("change", () => {
        rolesPage = 1;
        renderRoles();
    });

    const rolesReviewBannerLink = document.getElementById("rolesReviewBannerLink");
    if (rolesReviewBannerLink) rolesReviewBannerLink.addEventListener("click", (ev) => {
        ev.preventDefault();
        if (rolesNeedsReviewFilter) rolesNeedsReviewFilter.checked = true;
        rolesPage = 1;
        renderRoles();
        document.getElementById("rolesCard")?.scrollIntoView({ behavior: "smooth", block: "start" });
    });

    // ── Excluded agents (tombstones) ────────────────────────────────────────
    function renderExcludedRoles() {
        const tbody = document.getElementById("excludedRolesBody");
        const countEl = document.getElementById("excludedRolesCount");
        if (!tbody) return;
        const hidden = loadedRoles.filter((r) => r.hidden);
        if (countEl) countEl.textContent = `(${hidden.length})`;
        tbody.innerHTML = "";
        if (!hidden.length) {
            tbody.innerHTML = `<tr><td colspan="3" style="color:var(--text-muted);">None.</td></tr>`;
            return;
        }
        hidden.forEach((r) => {
            const tr = document.createElement("tr");
            const name = r.agent || r.full_name || r.nick_name || "—";
            const reason = (r.remarks || "").replace(/^excluded:\s*/i, "").trim()
                || "no reason given";
            tr.innerHTML = `
                <td class="sm-cell" style="font-weight:600;">${escapeHtml(name)}</td>
                <td class="sm-cell">${escapeHtml(reason)}</td>
                <td><button class="btn btn-secondary row-restore-btn" style="padding:4px 8px; font-size:12px;">↩ Restore</button></td>
            `;
            tr.querySelector(".row-restore-btn")?.addEventListener("click", () => restoreExcludedAgent(r));
            tbody.appendChild(tr);
        });
    }

    /** Removes a tombstone so the agent regenerates fresh from Postgres (or
     *  simply stops being listed, if Postgres no longer tags them either) —
     *  the same state they would be in had they never been deleted. */
    async function restoreExcludedAgent(r) {
        const name = r.agent || r.full_name || r.nick_name || "this agent";
        if (!confirm(`Restore ${name}? They will show up again next load as a name-only row — you will need to fill in their details and date the row again.`)) return;
        const bid = String(r.pg_bubble_id || "");
        const nameKey = String(r.agent || "").trim().toLowerCase();
        loadedRoles = loadedRoles.filter((row) => {
            if (!row.hidden) return true;
            return bid
                ? row.pg_bubble_id !== bid
                : !(!row.pg_bubble_id && String(row.agent || "").trim().toLowerCase() === nameKey);
        });
        rebuildRolesList();
        renderRoles();
        renderExcludedRoles();
        await saveRoles();
    }

    const showExcludedRolesBtn = document.getElementById("showExcludedRolesBtn");
    if (showExcludedRolesBtn) showExcludedRolesBtn.addEventListener("click", () => {
        const panel = document.getElementById("excludedRolesPanel");
        if (!panel) return;
        const showing = panel.style.display !== "none";
        panel.style.display = showing ? "none" : "block";
        if (!showing) renderExcludedRoles();
    });

    // Refresh from Postgres button
    const refreshPgAgentsBtn = document.getElementById("refreshPgAgentsBtn");
    if (refreshPgAgentsBtn) refreshPgAgentsBtn.addEventListener("click", async () => {
        await loadPgAgents();
        renderRoles(); // re-render to pick up any new IC numbers
    });

    const saveEntriesBtn = document.getElementById("saveEntriesBtn");
    if (saveEntriesBtn) saveEntriesBtn.addEventListener("click", saveEntries);
    const cancelEditBtn = document.getElementById("cancelEditBtn");
    if (cancelEditBtn) {
        cancelEditBtn.addEventListener("click", () => {
            const status = document.getElementById("saveStatus");
            if (status) status.textContent = "";
            exitEditMode();
        });
    }
    const logoutBtn = document.getElementById("logoutBtn");
    if (logoutBtn) {
        logoutBtn.addEventListener("click", async () => {
            await fetch("/logout", { method: "POST" });
            window.location.href = "/login";
        });
    }

    // ── MONTHLY CONTEST RULES ────────────────────────────────────────────────

    const CONTEST_RULE_IDS = [
        "full_rate_cap", "above_cap_pct", "activity_bonus_points", "target_bonus_points",
        "rank1_award", "rank2_award", "rank3_award", "achievement_bonus",
        "gb1_award", "gb2_award", "gb3_award", "gb_min_cases", "gb_min_sales",
        "fast_start_gift", "fast_start_slots", "fast_start_unpaid"
    ];

    function contestNum(value) {
        const n = parseFloat(String(value ?? "").replace(/,/g, "").trim());
        return Number.isFinite(n) ? n : 0;
    }

    function renderContestTeams(teams) {
        const body = document.getElementById("contestTeamsBody");
        if (!body) return;
        const dis = isAdmin ? "" : "disabled";
        body.innerHTML = teams.map((t) => `
            <tr data-team="${escapeHtml(t.team)}">
                <td style="font-weight:600;">${escapeHtml(t.team)}</td>
                <td class="sm-cell"><input type="text" class="ct-branch" value="${escapeHtml(t.branch || "")}" ${dis}></td>
                <td class="sm-cell"><input type="text" class="ct-captain" value="${escapeHtml(t.captain || "")}" ${dis}></td>
                <td class="sm-cell"><input type="number" class="ct-target" value="${escapeHtml(t.original_target || "0")}" ${dis}></td>
                <td class="sm-cell"><input type="number" class="ct-handicap" value="${escapeHtml(t.handicap || "0")}" ${dis}></td>
                <td class="baseline-cell"></td>
            </tr>
        `).join("");
        body.querySelectorAll(".ct-target, .ct-handicap").forEach((el) => {
            el.addEventListener("input", refreshContestBaselines);
        });
        refreshContestBaselines();
    }

    // The baseline is derived, never typed: a handicap exists precisely to lift
    // every team to one common baseline, so six baselines that are not equal
    // means a target or handicap was mistyped. Flagging it here is much cheaper
    // than discovering it after the awards are published.
    function refreshContestBaselines() {
        const rows = Array.from(document.querySelectorAll("#contestTeamsBody tr"));
        if (!rows.length) return;
        const baselines = rows.map((tr) =>
            contestNum(tr.querySelector(".ct-target")?.value) +
            contestNum(tr.querySelector(".ct-handicap")?.value)
        );
        const counts = new Map();
        baselines.forEach((b) => counts.set(b, (counts.get(b) || 0) + 1));
        let common = baselines[0];
        counts.forEach((count, value) => {
            if (count > (counts.get(common) || 0)) common = value;
        });

        const odd = [];
        rows.forEach((tr, i) => {
            const cell = tr.querySelector(".baseline-cell");
            const mismatch = baselines[i] !== common;
            cell.textContent = baselines[i].toLocaleString("en-MY", { maximumFractionDigits: 2 });
            cell.classList.toggle("mismatch", mismatch);
            if (mismatch) odd.push(tr.dataset.team);
        });

        const warn = document.getElementById("contestBaselineWarn");
        if (!warn) return;
        if (odd.length) {
            warn.style.display = "block";
            warn.textContent = `${odd.join(", ")} ${odd.length === 1 ? "does" : "do"} not reach the same ranking baseline as the other teams `
                + `(${common.toLocaleString("en-MY")}). Check the original target and handicap unless this is intended.`;
        } else {
            warn.style.display = "none";
        }
    }

    // Every configured month, newest first, as returned by the list endpoint.
    // The landing table and the popup both read from this, so opening a row
    // costs no round-trip.
    let contestSets = [];

    function contestMonthLabel(month) {
        const [y, m] = String(month || "").split("-");
        const names = ["January", "February", "March", "April", "May", "June",
                       "July", "August", "September", "October", "November", "December"];
        const name = names[parseInt(m, 10) - 1];
        return name ? `${name} ${y}` : month;
    }

    function contestRM(value) {
        return `RM ${contestNum(value).toLocaleString("en-MY", { maximumFractionDigits: 0 })}`;
    }

    // One baseline shared by all six teams is the normal case; anything else is
    // worth surfacing on the landing row rather than hiding inside the popup.
    function contestBaselineSummary(teams) {
        const values = (teams || []).map((t) => contestNum(t.original_target) + contestNum(t.handicap));
        if (!values.length) return "—";
        const unique = Array.from(new Set(values));
        if (unique.length === 1) return contestRM(unique[0]);
        return `Mixed (${unique.sort((a, b) => a - b).map((v) => contestRM(v)).join(", ")})`;
    }

    function contestUpdatedLabel(entry) {
        if (!entry.updated_at) return "—";
        const when = new Date(entry.updated_at);
        const stamp = Number.isNaN(when.getTime())
            ? entry.updated_at
            : when.toLocaleDateString("en-MY", { day: "2-digit", month: "short", year: "numeric" });
        return entry.updated_by ? `${stamp} · ${entry.updated_by}` : stamp;
    }

    function renderContestList() {
        const body = document.getElementById("contestListBody");
        if (!body) return;

        if (!contestSets.length) {
            body.innerHTML = `<tr><td colspan="8" style="text-align:center; color:var(--text-muted); padding:28px 0;">
                No monthly contest set up yet. Use “New monthly contest” to add one.</td></tr>`;
            return;
        }

        body.innerHTML = contestSets.map((entry, i) => {
            const r = entry.rules || {};
            const activity = Number(r.activity_bonus_enabled)
                ? `On · ${contestNum(r.activity_bonus_points).toLocaleString("en-MY")} pts`
                : "Off";
            const slots = String(r.fast_start_slots || "").trim();
            const fastStart = [r.fast_start_gift || "—", slots ? `${slots} slots` : "slots not set"].join(" · ");
            return `
                <tr class="contest-row" data-index="${i}" style="cursor:pointer;">
                    <td style="font-weight:600;">${escapeHtml(contestMonthLabel(entry.month))}</td>
                    <td>${escapeHtml(contestBaselineSummary(entry.teams))}</td>
                    <td>${escapeHtml(activity)}</td>
                    <td>${escapeHtml(contestNum(r.target_bonus_points).toLocaleString("en-MY"))} pts</td>
                    <td>${escapeHtml([r.rank1_award, r.rank2_award, r.rank3_award].map((v) => contestRM(v)).join(" / "))}</td>
                    <td>${escapeHtml([r.gb1_award, r.gb2_award, r.gb3_award].map((v) => contestRM(v)).join(" / "))}</td>
                    <td>${escapeHtml(fastStart)}</td>
                    <td style="color:var(--text-muted); font-size:12px;">${escapeHtml(contestUpdatedLabel(entry))}</td>
                </tr>`;
        }).join("");

        body.querySelectorAll(".contest-row").forEach((tr) => {
            tr.addEventListener("click", () => {
                const entry = contestSets[Number(tr.dataset.index)];
                if (entry) openContestModal(entry);
            });
        });
    }

    // A 404 here means the route does not exist, which in practice means the
    // Flask process is older than the static files it just served — it starts
    // with use_reloader=False, so a code change needs a restart. Saying that is
    // far more useful than relaying Flask's bare "Not found".
    function contestApiError(res, data, fallback) {
        if (res.status === 404) {
            return "The dashboard server is running an older version of app.py. "
                 + "Restart it (start_dashboard.bat) to pick up the Monthly Contest API.";
        }
        return data?.error || fallback;
    }

    async function loadContestList() {
        const status = document.getElementById("contestListStatus");
        if (status) { status.className = "save-status"; status.textContent = "Loading..."; }
        try {
            const res = await api("/api/contest-rules/list");
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(contestApiError(res, data, "Could not load the monthly contests"));
            contestSets = data.sets || [];
            renderContestList();
            if (status) status.textContent = "";
        } catch (err) {
            contestSets = [];
            renderContestList();
            if (status) { status.className = "save-status err"; status.textContent = err.message; }
        }
    }

    // Agent names offered in the roster picker, taken from Agent Roles &
    // Hierarchy so the contest cannot invent an agent who is not on the payroll.
    // A datalist rather than a select: someone not yet in the roster can still
    // be typed in, instead of blocking the month's setup.
    let contestAgentNames = null;

    async function ensureContestAgentList() {
        if (contestAgentNames) return contestAgentNames;
        try {
            const res = await api("/api/agent-roles");
            const rows = await res.json();
            const names = new Set();
            (rows || []).forEach(r => {
                const nick = String(r.nick_name || "").trim();
                const agent = String(r.agent || "").trim();
                if (nick) names.add(nick);
                else if (agent) names.add(agent);
            });
            contestAgentNames = Array.from(names).sort((a, b) => a.localeCompare(b));
        } catch {
            contestAgentNames = [];
        }
        let list = document.getElementById("contestAgentList");
        if (!list) {
            list = document.createElement("datalist");
            list.id = "contestAgentList";
            document.body.appendChild(list);
        }
        list.innerHTML = contestAgentNames.map(n => `<option value="${escapeHtml(n)}"></option>`).join("");
        return contestAgentNames;
    }

    // An agent can close several cases in a month, so the Sales Price cell holds
    // a list of amounts rather than one. Stored as JSON so a figure typed with
    // thousands separators is never split into two cases.
    function parseSalesPrices(raw) {
        if (Array.isArray(raw)) return raw.map(v => String(v));
        if (!raw) return [];
        try {
            const parsed = JSON.parse(raw);
            return Array.isArray(parsed) ? parsed.map(v => String(v)) : [];
        } catch {
            return String(raw).split(",").map(v => v.trim()).filter(Boolean);
        }
    }

    function rosterPriceInputsHtml(prices, dis) {
        const list = prices.length ? prices : [""];
        return `<div class="r-price-list" style="display:flex; flex-direction:column; gap:4px;">
            ${list.map(v => `<div style="display:flex; gap:4px; align-items:center;">
                <input type="text" class="r-price" value="${escapeHtml(v)}" placeholder="0.00" ${dis}>
                ${dis ? "" : `<button class="btn btn-secondary btn-drop-price" title="Remove this amount"
                    style="padding:2px 7px; font-size:11px; line-height:1.2;">−</button>`}
            </div>`).join("")}
        </div>
        ${dis ? "" : `<button class="btn btn-secondary btn-add-price" style="margin-top:4px; padding:2px 9px; font-size:11px;">+ Amount</button>`}`;
    }

    function bindRosterPriceCell(cell) {
        cell.querySelector(".btn-add-price")?.addEventListener("click", () => {
            const values = Array.from(cell.querySelectorAll(".r-price")).map(i => i.value);
            cell.innerHTML = rosterPriceInputsHtml(values.concat([""]), "");
            bindRosterPriceCell(cell);
        });
        cell.querySelectorAll(".btn-drop-price").forEach(btn => {
            btn.addEventListener("click", () => {
                const values = Array.from(cell.querySelectorAll(".r-price")).map(i => i.value);
                const idx = Array.from(cell.querySelectorAll(".btn-drop-price")).indexOf(btn);
                values.splice(idx, 1);
                cell.innerHTML = rosterPriceInputsHtml(values, "");
                bindRosterPriceCell(cell);
            });
        });
    }

    function renderContestRoster(roster, teamNames) {
        const body = document.getElementById("contestRosterBody");
        if (!body) return;
        const dis = isAdmin ? "" : "disabled";
        const teams = teamNames && teamNames.length ? teamNames : [];
        const rows = (roster || []);
        if (!rows.length) {
            body.innerHTML = `<tr><td colspan="4" style="text-align:center; color:var(--text-muted); padding:18px 0;">
                No agents listed yet — this month has no eligible candidates.</td></tr>`;
            return;
        }
        body.innerHTML = rows.map(r => `
            <tr>
                <td class="sm-cell"><select class="r-team" ${dis}>
                    ${teams.map(t => `<option value="${escapeHtml(t)}" ${t === r.team ? "selected" : ""}>${escapeHtml(t)}</option>`).join("")}
                </select></td>
                <td class="sm-cell"><input type="text" class="r-agent" list="contestAgentList" value="${escapeHtml(r.agent || "")}" ${dis}></td>
                <td class="r-prices">${rosterPriceInputsHtml(parseSalesPrices(r.sales_prices), dis)}</td>
                <td>${isAdmin ? `<button class="btn btn-danger btn-remove-roster">✕</button>` : ""}</td>
            </tr>`).join("");
        body.querySelectorAll(".btn-remove-roster").forEach(btn => {
            btn.addEventListener("click", () => { btn.closest("tr").remove(); refreshRosterEmptyState(); });
        });
        body.querySelectorAll(".r-prices").forEach(bindRosterPriceCell);
    }

    function currentRosterRows() {
        return Array.from(document.querySelectorAll("#contestRosterBody tr"))
            .filter(tr => tr.querySelector(".r-agent"))
            .map(tr => ({
                team: tr.querySelector(".r-team")?.value || "",
                agent: tr.querySelector(".r-agent")?.value.trim() || "",
                sales_prices: Array.from(tr.querySelectorAll(".r-price"))
                    .map(i => i.value.replace(/,/g, "").trim())
                    .filter(Boolean)
            }));
    }

    function refreshRosterEmptyState() {
        const body = document.getElementById("contestRosterBody");
        if (body && !body.querySelector("tr")) renderContestRoster([], contestTeamNames());
    }

    function contestTeamNames() {
        return Array.from(document.querySelectorAll("#contestTeamsBody tr"))
            .map(tr => tr.dataset.team)
            .filter(Boolean);
    }

    function applyContestPayload(payload) {
        renderContestTeams(payload.teams || []);
        const rules = payload.rules || {};
        CONTEST_RULE_IDS.forEach((key) => {
            const el = document.getElementById(`cr_${key}`);
            if (el) el.value = rules[key] ?? "";
        });
        const toggle = document.getElementById("cr_activity_bonus_enabled");
        if (toggle) toggle.checked = !!Number(rules.activity_bonus_enabled);

        ensureContestAgentList();
        renderContestRoster(payload.roster || [],
                            (payload.teams || []).map(t => t.team));

        if (!isAdmin) {
            document.querySelectorAll("#contestModal .contest-fields input, #contestModal .contest-fields select, #cr_activity_bonus_enabled")
                .forEach((el) => { el.disabled = true; });
        }

        const badge = document.getElementById("contestSavedBadge");
        if (badge) {
            badge.textContent = payload.saved ? "Saved" : "New — showing defaults";
            badge.className = `source-badge ${payload.saved ? "unified" : "system"}`;
        }

        const monthInput = document.getElementById("contestMonth");
        if (monthInput) {
            monthInput.value = payload.month || "";
            // Editing an existing month must not silently create a second one,
            // so the month is fixed once saved.
            monthInput.disabled = !isAdmin || !!payload.saved;
        }

        const title = document.getElementById("contestModalTitle");
        if (title) {
            title.textContent = payload.saved
                ? `Monthly Contest — ${contestMonthLabel(payload.month)}`
                : "New monthly contest";
        }

        // Copy-from lists the months that already have a rule set, so setting up
        // a new month is: pick last month, adjust two handicaps, save.
        const sel = document.getElementById("contestCopyFrom");
        const btn = document.getElementById("contestCopyBtn");
        const others = contestSets.map((s) => s.month).filter((m) => m !== payload.month);
        if (sel && btn) {
            sel.innerHTML = others.map((m) => `<option value="${m}">${contestMonthLabel(m)}</option>`).join("");
            const show = isAdmin && others.length > 0;
            sel.style.display = show ? "" : "none";
            btn.style.display = show ? "" : "none";
        }
    }

    function contestMonthValue() {
        return document.getElementById("contestMonth")?.value || "";
    }

    function closeContestModal() {
        document.getElementById("contestModal")?.classList.add("hidden");
    }

    function openContestModal(entry) {
        const status = document.getElementById("contestStatus");
        if (status) { status.className = "save-status"; status.textContent = ""; }
        applyContestPayload(entry);
        document.getElementById("contestModal")?.classList.remove("hidden");
    }

    // The first month that has no rule set yet, starting from this month and
    // looking forward — that is almost always the one being set up.
    function nextUnconfiguredMonth() {
        const taken = new Set(contestSets.map((s) => s.month));
        const now = new Date();
        for (let i = 0; i < 24; i++) {
            const d = new Date(now.getFullYear(), now.getMonth() + i, 1);
            const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
            if (!taken.has(key)) return key;
        }
        return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
    }

    async function newContest() {
        const month = nextUnconfiguredMonth();
        try {
            const res = await api(`/api/contest-rules?month=${encodeURIComponent(month)}`);
            const payload = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(contestApiError(res, payload, "Could not load the defaults"));
            openContestModal(payload);
        } catch (err) {
            const status = document.getElementById("contestListStatus");
            if (status) { status.className = "save-status err"; status.textContent = err.message; }
        }
    }

    function copyContestRulesFrom() {
        const from = document.getElementById("contestCopyFrom")?.value;
        const status = document.getElementById("contestStatus");
        const source = contestSets.find((s) => s.month === from);
        if (!source) return;
        // Keep the month being edited; take only the values.
        applyContestPayload({
            month: contestMonthValue(),
            saved: false,
            rules: source.rules,
            teams: source.teams
        });
        if (status) {
            status.className = "save-status";
            status.textContent = `Copied from ${contestMonthLabel(from)}. Not saved yet.`;
        }
    }

    function collectContestRules() {
        const rules = {};
        CONTEST_RULE_IDS.forEach((key) => {
            rules[key] = document.getElementById(`cr_${key}`)?.value ?? "";
        });
        rules.activity_bonus_enabled = document.getElementById("cr_activity_bonus_enabled")?.checked ? 1 : 0;

        const teams = Array.from(document.querySelectorAll("#contestTeamsBody tr")).map((tr) => ({
            team: tr.dataset.team,
            branch: tr.querySelector(".ct-branch")?.value ?? "",
            captain: tr.querySelector(".ct-captain")?.value ?? "",
            original_target: tr.querySelector(".ct-target")?.value ?? "0",
            handicap: tr.querySelector(".ct-handicap")?.value ?? "0"
        }));
        const roster = currentRosterRows().filter(r => r.agent);
        return { rules, teams, roster };
    }

    async function saveContestRules() {
        const status = document.getElementById("contestStatus");
        const month = contestMonthValue();
        if (!month) {
            status.className = "save-status err";
            status.textContent = "Pick a month first";
            return;
        }
        status.className = "save-status";
        status.textContent = "Saving...";
        const { rules, teams, roster } = collectContestRules();
        try {
            const res = await api("/api/contest-rules", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ month, rules, teams, roster })
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(contestApiError(res, data, "Save failed"));
            status.className = "save-status ok";
            status.textContent = `Saved ${contestMonthLabel(month)}.`;
            await loadContestList();
            setTimeout(closeContestModal, 700);
        } catch (err) {
            status.className = "save-status err";
            status.textContent = err.message;
        }
    }

    function initContestRules() {
        document.getElementById("contestNewBtn")?.addEventListener("click", newContest);
        document.getElementById("contestSaveBtn")?.addEventListener("click", saveContestRules);
        document.getElementById("contestCopyBtn")?.addEventListener("click", copyContestRulesFrom);
        document.getElementById("contestAddRosterBtn")?.addEventListener("click", () => {
            const teams = contestTeamNames();
            renderContestRoster(currentRosterRows().concat([{ team: teams[0] || "", agent: "", sales_prices: [] }]), teams);
        });
        document.getElementById("contestModalCloseBtn")?.addEventListener("click", closeContestModal);
        document.getElementById("contestCancelBtn")?.addEventListener("click", closeContestModal);
        // Clicking the backdrop closes, same as the other modals on this page.
        document.getElementById("contestModal")?.addEventListener("click", (e) => {
            if (e.target.id === "contestModal") closeContestModal();
        });
    }

    // ── ANP / EGA-ESA / PRODUCTION BONUS RULES ───────────────────────────────
    // All three are one rule set per period, so they share a loader and saver;
    // only the field lists and the optional child table differ.
    const RULE_SECTIONS = {
        anp: {
            card: "anpCard", endpoint: "/api/anp-rules", keyName: "effective_from",
            keyKind: "month_range",
            keyInputFrom: "anpPeriodFrom", keyInputTo: "anpPeriodTo", keyInputPresent: "anpPeriodPresent",
            badge: "anpSavedBadge", status: "anpStatus", saveBtn: "anpSaveBtn",
            prefix: "anp_", fields: ["min_paid", "excluded_payment_ids", "invoice_overrides"],
            childKey: "tiers", childBody: "anpTiersBody", addBtn: "anpAddTierBtn",
            childFields: [
                { name: "from_amount", type: "number" },
                { name: "to_amount", type: "number" },
                { name: "commission_rm", type: "number" }
            ]
        },
        ega: {
            card: "egaCard", endpoint: "/api/ega-rules", keyName: "year",
            keyKind: "year_range",
            keyInputFrom: "egaYearFrom", keyInputTo: "egaYearTo", keyInputPresent: "egaYearPresent",
            badge: "egaSavedBadge", status: "egaStatus", saveBtn: "egaSaveBtn",
            extraKey: "agent_type", extraInput: "egaAgentType",
            prefix: "ega_", fields: ["ega_threshold", "esa_threshold", "factory_from",
                                     "factory_first_block", "factory_balance_rate", "factory_min_panels"],
            childKey: "months", childBody: "egaMonthsBody", addBtn: "egaAddMonthBtn",
            childFields: [
                { name: "month", type: "number" },
                { name: "ep_threshold", type: "number" },
                { name: "label", type: "text" }
            ],
            // This one lives in a modal over a landing list, so a save has to
            // refresh what is behind it and step out of the way.
            afterSave: async () => { await loadEgaList(); closeEgaModal(); }
        },
        pb: {
            card: "pbCard", endpoint: "/api/production-bonus-rules", keyName: "effective_from",
            keyKind: "month_range",
            keyInputFrom: "pbPeriodFrom", keyInputTo: "pbPeriodTo", keyInputPresent: "pbPeriodPresent",
            badge: "pbSavedBadge", status: "pbStatus", saveBtn: "pbSaveBtn",
            prefix: "pb_", fields: ["min_paid", "property_types",
                                    "oum_team_target", "oum_personal_target", "oum_rate_pct",
                                    "ogm_team_target", "ogm_osa_rate_pct", "ogm_oum_rate_pct",
                                    "stage1_pct", "stage2_pct", "stage3_pct"]
        }
    };

    /** Keep the To field matched to the Present checkbox, same behaviour as
     *  the Agent Roles & Hierarchy modal (its Present tick). Works for
     *  both a month range (ANP) and a year range (EGA/ESA). */
    function syncMonthRangePresent(toId, presentId) {
        const present = document.getElementById(presentId);
        const to = document.getElementById(toId);
        if (!present || !to) return;
        if (present.checked) to.value = "";
        to.style.opacity = present.checked ? "0.5" : "";
        to.title = present.checked
            ? "No end — this applies from here onward. Pick an end point here if it stopped."
            : "The last period this applies to.";
    }

    function defaultRangeFromValue(keyKind) {
        const now = new Date();
        return keyKind === "year_range"
            ? String(now.getFullYear())
            : `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
    }

    function ruleKeyValue(cfg) {
        if (cfg.keyKind === "month_range" || cfg.keyKind === "year_range") {
            const fromEl = document.getElementById(cfg.keyInputFrom);
            const toEl = document.getElementById(cfg.keyInputTo);
            const presentEl = document.getElementById(cfg.keyInputPresent);
            if (!fromEl) return "";
            if (!fromEl.value) {
                fromEl.value = defaultRangeFromValue(cfg.keyKind);
                if (presentEl) presentEl.checked = true;
                syncMonthRangePresent(cfg.keyInputTo, cfg.keyInputPresent);
            }
            const present = !!presentEl?.checked;
            const to = (toEl?.value || "").trim();
            if (present || !to) return fromEl.value;
            return `${fromEl.value} to ${to}`;
        }
        const el = document.getElementById(cfg.keyInput);
        if (!el) return "";
        if (!el.value) {
            const now = new Date();
            el.value = cfg.keyKind === "year"
                ? String(now.getFullYear())
                : `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
        }
        return el.value;
    }

    function renderRuleChildRows(cfg, rows) {
        const body = document.getElementById(cfg.childBody);
        if (!body) return;
        const dis = isAdmin ? "" : "disabled";
        body.innerHTML = (rows || []).map(r => `
            <tr>
                ${cfg.childFields.map(f => `<td class="sm-cell"><input type="${f.type}" class="c-${f.name}" value="${escapeHtml(r[f.name] ?? "")}" ${dis}></td>`).join("")}
                <td>${isAdmin ? `<button class="btn btn-danger btn-remove-row">✕</button>` : ""}</td>
            </tr>`).join("");
        body.querySelectorAll(".btn-remove-row").forEach(btn => {
            btn.addEventListener("click", () => btn.closest("tr").remove());
        });
    }

    // The three staging percentages pay out one bonus over three years, so they
    // have to add up to the whole bonus — anything else silently over- or
    // under-pays every eligible agent.
    function refreshPbStageWarning() {
        const warn = document.getElementById("pbStageWarn");
        if (!warn) return;
        const total = ["pb_stage1_pct", "pb_stage2_pct", "pb_stage3_pct"]
            .reduce((a, id) => a + (parseFloat(document.getElementById(id)?.value) || 0), 0);
        const rounded = Math.round(total * 100) / 100;
        if (Math.abs(rounded - 100) < 0.001) {
            warn.style.display = "none";
        } else {
            warn.style.display = "block";
            warn.textContent = `Payout stages total ${rounded}%, not 100%. `
                + `As entered, every eligible agent would be paid ${rounded > 100 ? "more" : "less"} than the bonus they earned.`;
        }
    }

    function applyRulePayload(cfg, payload) {
        const rules = payload.rules || {};
        cfg.fields.forEach(f => {
            const el = document.getElementById(cfg.prefix + f);
            if (el) { el.value = rules[f] ?? ""; el.disabled = !isAdmin; }
        });
        if (cfg.childKey) renderRuleChildRows(cfg, payload[cfg.childKey]);

        const badge = document.getElementById(cfg.badge);
        if (badge) {
            const scope = payload.agent_type ? ` (${payload.agent_type})` : "";
            badge.textContent = (payload.saved ? "Saved" : "Not set up — showing defaults") + scope;
            badge.className = `source-badge ${payload.saved ? "unified" : "system"}`;
        }
        if (cfg.prefix === "pb_") refreshPbStageWarning();
    }

    async function loadRuleSection(key) {
        const cfg = RULE_SECTIONS[key];
        const status = document.getElementById(cfg.status);
        const period = ruleKeyValue(cfg);
        if (status) { status.className = "save-status"; status.textContent = "Loading..."; }
        try {
            let url = `${cfg.endpoint}?${cfg.keyName}=${encodeURIComponent(period)}`;
            if (cfg.extraKey) {
                const extra = document.getElementById(cfg.extraInput)?.value || "";
                url += `&${cfg.extraKey}=${encodeURIComponent(extra)}`;
            }
            const res = await api(url);
            const payload = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(contestApiError(res, payload, "Could not load the rules"));
            applyRulePayload(cfg, payload);
            if (status) status.textContent = "";
        } catch (err) {
            if (status) { status.className = "save-status err"; status.textContent = err.message; }
        }
    }

    async function saveRuleSection(key) {
        const cfg = RULE_SECTIONS[key];
        const status = document.getElementById(cfg.status);
        const period = ruleKeyValue(cfg);
        status.className = "save-status";
        status.textContent = "Saving...";

        const rules = {};
        cfg.fields.forEach(f => { rules[f] = document.getElementById(cfg.prefix + f)?.value ?? ""; });
        const body = { rules };
        body[cfg.keyName] = period;
        if (cfg.extraKey) body[cfg.extraKey] = document.getElementById(cfg.extraInput)?.value || "";
        if (cfg.childKey) {
            body[cfg.childKey] = Array.from(document.querySelectorAll(`#${cfg.childBody} tr`)).map(tr => {
                const row = {};
                cfg.childFields.forEach(f => { row[f.name] = tr.querySelector(`.c-${f.name}`)?.value ?? ""; });
                return row;
            });
        }
        try {
            const res = await api(cfg.endpoint, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body)
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(contestApiError(res, data, "Save failed"));
            status.className = "save-status ok";
            status.textContent = `Saved ${period}.`;
            await loadRuleSection(key);
            if (cfg.afterSave) await cfg.afterSave();
            setTimeout(() => { status.textContent = ""; }, 2500);
        } catch (err) {
            status.className = "save-status err";
            status.textContent = err.message;
        }
    }

    // ── EGA / ESA AWARD LANDING LIST ─────────────────────────────────────────
    // The rule form shows one agent type at a time, so Internal and Outsource
    // can be saved over each other with nothing on screen to show it. This list
    // puts them side by side, with who touched each one last.
    const EGA_MONTH_ABBR = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    const EGA_AGENT_TYPES = ["internal", "outsource"];
    let egaRuleSets = [];

    /** 350000 -> "350k", 1200000 -> "1.2M". The ladder is six entries wide, so
     *  full figures would push Last Updated off the row. */
    function egaShortEp(value) {
        const n = Number(String(value ?? "").replace(/,/g, ""));
        if (!Number.isFinite(n) || n === 0) return "—";
        if (Math.abs(n) >= 1e6) return `${(n / 1e6).toFixed(2).replace(/\.?0+$/, "")}M`;
        if (Math.abs(n) >= 1e3) return `${(n / 1e3).toFixed(1).replace(/\.?0+$/, "")}k`;
        return String(n);
    }

    function egaFullEp(value) {
        const n = Number(String(value ?? "").replace(/,/g, ""));
        return Number.isFinite(n) && n !== 0 ? n.toLocaleString("en-MY") : "—";
    }

    /** "Feb 350k · Mar 400k · …" for one half of the ladder. `wantEsa` picks the
     *  side, matching how the award scripts split these rows: a label naming ESA
     *  is an ESA row, everything else is EGA. */
    function egaLadderSummary(months, wantEsa) {
        const parts = (months || [])
            .filter((m) => (String(m.label || "").toUpperCase().includes("ESA")) === wantEsa)
            .sort((a, b) => Number(a.month) - Number(b.month))
            .map((m) => `${EGA_MONTH_ABBR[Number(m.month)] || m.month} ${egaShortEp(m.ep_threshold)}`);
        return parts.length ? parts.join(" · ") : "—";
    }

    function egaSchemeLabel(entry) {
        const type = String(entry.agent_type || "");
        return `${type.charAt(0).toUpperCase()}${type.slice(1)} ${entry.year}`;
    }

    /** Saved sets, plus a placeholder for any agent type missing in the current
     *  year. A scheme that was never set up is exactly the thing worth seeing —
     *  it is running on built-in defaults nobody chose. */
    function egaListRows() {
        const rows = egaRuleSets.slice();
        const year = String(new Date().getFullYear());
        EGA_AGENT_TYPES.forEach((type) => {
            const exists = rows.some((r) => r.year === year && r.agent_type === type);
            if (!exists) rows.push({ year, agent_type: type, saved: false, rules: {}, months: [] });
        });
        return rows.sort((a, b) => (b.year.localeCompare(a.year))
            || a.agent_type.localeCompare(b.agent_type));
    }

    function renderEgaList() {
        const body = document.getElementById("egaListBody");
        if (!body) return;
        const rows = egaListRows();

        body.innerHTML = rows.map((entry, i) => {
            const r = entry.rules || {};
            const stamp = entry.saved
                ? escapeHtml(contestUpdatedLabel(entry))
                : `<span style="color:var(--text-muted);">Not set up — using defaults</span>`;
            return `
                <tr class="ega-row" data-index="${i}" style="cursor:pointer;">
                    <td style="font-weight:600;">${escapeHtml(egaSchemeLabel(entry))}</td>
                    <td>${escapeHtml(egaFullEp(r.ega_threshold))}</td>
                    <td>${escapeHtml(egaFullEp(r.esa_threshold))}</td>
                    <td>${escapeHtml(egaLadderSummary(entry.months, false))}</td>
                    <td>${escapeHtml(egaLadderSummary(entry.months, true))}</td>
                    <td style="color:var(--text-muted); font-size:12px;">${stamp}</td>
                </tr>`;
        }).join("");

        body.querySelectorAll(".ega-row").forEach((tr) => {
            tr.addEventListener("click", () => {
                const entry = rows[Number(tr.dataset.index)];
                if (entry) openEgaModal(entry);
            });
        });
    }

    async function loadEgaList() {
        const status = document.getElementById("egaListStatus");
        if (status) { status.className = "save-status"; status.textContent = "Loading..."; }
        try {
            const res = await api("/api/ega-rule-sets");
            const data = await res.json().catch(() => ([]));
            if (!res.ok) throw new Error(contestApiError(res, data, "Could not load the rule sets"));
            egaRuleSets = Array.isArray(data) ? data : [];
            renderEgaList();
            if (status) status.textContent = "";
        } catch (err) {
            if (status) { status.className = "save-status err"; status.textContent = err.message; }
        }
    }

    /** Point the form's key inputs at `entry`, then let the shared loader fetch
     *  and populate it exactly as it did when these fields lived on the card. */
    function openEgaModal(entry) {
        const status = document.getElementById("egaStatus");
        if (status) { status.className = "save-status"; status.textContent = ""; }

        const [from, to] = String(entry.year || "").split(" to ");
        const fromEl = document.getElementById("egaYearFrom");
        const toEl = document.getElementById("egaYearTo");
        const presentEl = document.getElementById("egaYearPresent");
        if (fromEl) fromEl.value = (from || "").trim();
        if (toEl) toEl.value = (to || "").trim();
        if (presentEl) presentEl.checked = !to;
        syncMonthRangePresent("egaYearTo", "egaYearPresent");

        const typeEl = document.getElementById("egaAgentType");
        if (typeEl) typeEl.value = entry.agent_type || "internal";

        const title = document.getElementById("egaModalTitle");
        if (title) {
            title.textContent = entry.saved
                ? `EGA / ESA — ${egaSchemeLabel(entry)}`
                : `EGA / ESA — ${egaSchemeLabel(entry)} (new)`;
        }

        document.getElementById("egaModal")?.classList.remove("hidden");
        loadRuleSection("ega");
    }

    function closeEgaModal() {
        document.getElementById("egaModal")?.classList.add("hidden");
    }

    function initEgaRuleSets() {
        document.getElementById("egaModalCloseBtn")?.addEventListener("click", closeEgaModal);
        document.getElementById("egaCancelBtn")?.addEventListener("click", closeEgaModal);
        document.getElementById("egaNewBtn")?.addEventListener("click", () => {
            // Whichever agent type has no set for this year is almost always the
            // one being added; fall back to Internal when both exist.
            const year = String(new Date().getFullYear());
            const missing = EGA_AGENT_TYPES.find(
                (t) => !egaRuleSets.some((r) => r.year === year && r.agent_type === t));
            openEgaModal({ year, agent_type: missing || "internal", saved: false, rules: {}, months: [] });
        });
    }

    function initRuleSections() {
        Object.keys(RULE_SECTIONS).forEach(key => {
            const cfg = RULE_SECTIONS[key];
            if (cfg.keyKind === "month_range" || cfg.keyKind === "year_range") {
                const presentEl = document.getElementById(cfg.keyInputPresent);
                presentEl?.addEventListener("change", () => {
                    syncMonthRangePresent(cfg.keyInputTo, cfg.keyInputPresent);
                    loadRuleSection(key);
                });
                syncMonthRangePresent(cfg.keyInputTo, cfg.keyInputPresent);
                [cfg.keyInputFrom, cfg.keyInputTo].forEach(id => {
                    document.getElementById(id)?.addEventListener("change", () => loadRuleSection(key));
                });
            } else {
                document.getElementById(cfg.keyInput)?.addEventListener("change", () => loadRuleSection(key));
            }
            if (cfg.extraInput) {
                document.getElementById(cfg.extraInput)?.addEventListener("change", () => loadRuleSection(key));
            }
            document.getElementById(cfg.saveBtn)?.addEventListener("click", () => saveRuleSection(key));
            if (cfg.addBtn) {
                document.getElementById(cfg.addBtn)?.addEventListener("click", () => {
                    const blank = {};
                    cfg.childFields.forEach(f => { blank[f.name] = ""; });
                    const existing = Array.from(document.querySelectorAll(`#${cfg.childBody} tr`)).map(tr => {
                        const row = {};
                        cfg.childFields.forEach(f => { row[f.name] = tr.querySelector(`.c-${f.name}`)?.value ?? ""; });
                        return row;
                    });
                    renderRuleChildRows(cfg, existing.concat([blank]));
                });
            }
        });
        ["pb_stage1_pct", "pb_stage2_pct", "pb_stage3_pct"].forEach(id => {
            document.getElementById(id)?.addEventListener("input", refreshPbStageWarning);
        });
    }

    let activeDataSection = "roles";

    function initDataSectionList() {
        const list = document.getElementById("dataSectionList");
        if (!list) return;
        const items = list.querySelectorAll("li");
        items.forEach((item) => {
            item.addEventListener("click", () => {
                items.forEach((el) => el.classList.remove("active"));
                item.classList.add("active");
                activeDataSection = item.dataset.section || "roles";
                updateDataSectionView();
            });
        });
    }

    function updateDataSectionView() {
        const pageTitle = document.getElementById("pageTitle");
        const rolesCard = document.getElementById("rolesCard");
        const rolesMetricsRow = document.getElementById("rolesMetricsRow");
        const viewCard = document.getElementById("viewCard");
        const nfpCard = document.getElementById("nfpCard");
        const nfpListCard = document.getElementById("nfpListCard");
        const viewCardTitle = document.querySelector("#viewCard .card-header h3");

        // "roles" is shown as Basic Commission: the old Basic Commission
        // rates page was removed and rates are entered per agent here.
        const titles = {
            roles: "Basic Commission",
            nfp: "Net Floor Price",
            anp: "ANP Commission",
            ega_esa: "EGA/ESA Award",
            production_bonus: "Production Bonus",
            monthly_contest: "Monthly Contest"
        };

        if (pageTitle) {
            pageTitle.textContent = titles[activeDataSection] || titles.roles;
        }

        const isRoles = activeDataSection === "roles";
        const isContest = activeDataSection === "monthly_contest";
        const isNfp = activeDataSection === "nfp";
        // Sections with their own rules form, keyed by section id.
        const RULE_CARD_BY_SECTION = { anp: "anp", ega_esa: "ega", production_bonus: "pb" };
        const ruleKey = RULE_CARD_BY_SECTION[activeDataSection] || null;
        Object.keys(RULE_SECTIONS).forEach(k => {
            const el = document.getElementById(RULE_SECTIONS[k].card);
            if (el) el.style.display = (ruleKey === k) ? "block" : "none";
        });
        // The EGA/ESA form is a modal, so it sits outside the card the loop
        // above just hid — leaving the section would strand it over the page.
        if (ruleKey !== "ega") closeEgaModal();

        // Carry-over cases belong to the EGA scheme but are per agent, not per
        // agent type, so they sit on the section page rather than in the
        // per-scheme modal.
        const carryCard = document.getElementById("egaCarryCard");
        if (carryCard) carryCard.style.display = (ruleKey === "ega") ? "block" : "none";

        if (rolesCard) {
            rolesCard.style.display = isRoles ? "block" : "none";
        }

        if (rolesMetricsRow) {
            rolesMetricsRow.style.display = isRoles ? "flex" : "none";
        }

        // The contest is configured with its own form, not the shared rates
        // preview grid, so it replaces viewCard rather than sitting beside it.
        const contestCard = document.getElementById("contestCard");
        if (contestCard) {
            contestCard.style.display = isContest ? "block" : "none";
        }

        // The shared rates grid carries the Basic section (and any future
        // section without a card of its own); NFP has its own tier table.
        if (viewCard) {
            viewCard.style.display = (isRoles || isContest || isNfp || ruleKey) ? "none" : "block";
        }

        if (nfpCard) {
            nfpCard.style.display = isNfp ? "block" : "none";
        }

        // The price list is opened on demand, so switching section closes it.
        if (nfpListCard) {
            nfpListCard.style.display = "none";
        }

        if (viewCardTitle) {
            viewCardTitle.textContent = titles[activeDataSection] || "Commission Rates";
        }

        if (isContest) {
            loadContestList();
        } else if (ruleKey === "ega") {
            // EGA/ESA lands on its list; the rule form loads when a row opens.
            loadEgaList();
        } else if (ruleKey) {
            loadRuleSection(ruleKey);
        } else if (!isRoles) {
            loadPreview();
        }
    }

    // A `?section=` param (e.g. from the report page's "View Net Floor Price
    // List" button) lands directly on that Data section instead of the
    // default Agent Roles view.
    const urlParams = new URLSearchParams(window.location.search);
    let wantSection = urlParams.get("section");
    // "basic_nfp" was one section before Basic and Net Floor Price were split
    // apart. Links minted before the split (and any bookmark) still arrive with
    // the old id, so send them to whichever half they were actually after.
    if (wantSection === "basic_nfp") {
        wantSection = urlParams.get("showNfpList") ? "nfp" : "roles";
    }
    // The old Basic Commission page is gone; its name now belongs to "roles".
    if (wantSection === "basic") wantSection = "roles";

    initPreviewFilters();
    initDataSectionList();
    initContestRules();
    initEgaRuleSets();
    initRuleSections();
    if (wantSection) {
        const list = document.getElementById("dataSectionList");
        const item = list?.querySelector(`li[data-section="${wantSection}"]`);
        if (item) {
            list.querySelectorAll("li").forEach((el) => el.classList.remove("active"));
            item.classList.add("active");
            activeDataSection = wantSection;
        }
    }
    updateDataSectionView();

    loadMe().then(() => {
        // Load saved roles from SQLite, then load live Postgres agents in parallel.
        // pgAgentsCache is populated by loadPgAgents and used by addRoleRow to
        // show IC numbers without an extra round-trip per row.
        loadRoles();
        loadPgAgents();
        loadEgaCarryover();
        startRolesAutoRefresh();
        // updateDataSectionView() already ran, before isAdmin was known, so a
        // deep link straight to the contest section would have rendered its
        // rows without the admin controls. Re-render now that the role is in.
        if (activeDataSection === "monthly_contest") loadContestList();
        const reloadKey = { anp: "anp", ega_esa: "ega", production_bonus: "pb" }[activeDataSection];
        // Same reason as the contest list: the first render ran before isAdmin
        // was known, so the "New rule set" button was hidden on a deep link.
        if (reloadKey === "ega") loadEgaList();
        else if (reloadKey) loadRuleSection(reloadKey);
        if (urlParams.get("showNfpList")) {
            showNfpList(urlParams.get("month") || "");
        }
    }).catch((err) => {
        console.error("loadMe error:", err);
    });
})();
