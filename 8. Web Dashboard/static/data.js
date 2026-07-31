(function () {
    "use strict";

    const MONTH_NAMES = ["January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December"];
    const MONTH_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    const RATE_ROLES = {
        "Internal": ["Executive", "Senior", "Regional Sales Director", "Senior Branch Director",
            "Branch Sales Manager", "Sales Development Manager", "Sales Team Manager",
            "Senior Sales Consultant", "Sales Consultant"],
        "Outsource": ["OGM", "OUM", "OSA/OSA1"]
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
        { key: "Net Floor Price Rate", wired: false, unit: "%",
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
        const el = document.getElementById("previewMonth") || document.getElementById("filterMonth");
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
            ["editModeBtn", "nfpUploadBtn", "seedRolesBtn", "addRoleBtn", "saveRolesBtn",
             "contestSaveBtn", "contestNewBtn", "contestAddRosterBtn",
             "anpSaveBtn", "anpAddTierBtn", "egaSaveBtn", "egaAddMonthBtn", "pbSaveBtn"].forEach((id) => {
                const el = document.getElementById(id);
                if (el) el.style.display = "";
            });
        } else {
            document.getElementById("viewOnlyNote").style.display = "block";
        }
        return me;
    }

    // ── VIEW MODE ────────────────────────────────────────────────────────────

    function initPreviewMonth() {
        const sel = document.getElementById("previewMonth");
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
                opt.value = `${y}-${String(m).padStart(2, "0")}`;
                opt.textContent = `${MONTH_NAMES[m - 1]} ${y}`;
                sel.appendChild(opt);
            }
        });
        sel.addEventListener("change", loadPreview);

        // Agent type filter
        const agentTypeSel = document.getElementById("previewAgentType");
        if (agentTypeSel) agentTypeSel.addEventListener("change", loadPreview);
    }

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

    function roleMatches(rowRole, filterRole) {
        if (!filterRole) return true;
        const target = normRole(rowRole);
        if (!target) return false;
        return normRole(filterRole) === target
            || filterRole.split("/").some((part) => normRole(part) === target);
    }

    /** Role options follow the Agent Type filter; with no type picked, both
     *  sets are offered. Keeps the current pick when it is still valid. */
    function populatePreviewRoleFilter() {
        const sel = document.getElementById("previewRole");
        if (!sel) return;
        const atype = (document.getElementById("previewAgentType")?.value || "").trim();
        const roles = atype
            ? (RATE_ROLES[atype] || [])
            : [...new Set(RATE_ROLES["Internal"].concat(RATE_ROLES["Outsource"]))];
        const prev = sel.value;
        sel.innerHTML = `<option value="">All Roles</option>` + roles.map((r) =>
            `<option value="${escapeHtml(r)}">${escapeHtml(r)}</option>`).join("");
        sel.value = roles.includes(prev) ? prev : "";
    }

    /** Agent names come from the roles table (the same list the Roles section
     *  shows), narrowed by the Agent Type and Role filters and de-duplicated —
     *  an agent with role history has one row per role, not one name each. */
    function populatePreviewAgentNameFilter() {
        const sel = document.getElementById("previewAgentName");
        if (!sel) return;
        const atype = (document.getElementById("previewAgentType")?.value || "").trim().toLowerCase();
        const role = (document.getElementById("previewRole")?.value || "").trim();
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
        const yVal = (document.getElementById("previewYear")?.value || "").trim();
        const mVal = (document.getElementById("previewMonth")?.value || "").trim();
        const agentTypeFilter = (document.getElementById("previewAgentType")?.value || "").toLowerCase();
        const roleFilter = (document.getElementById("previewRole")?.value || "").trim();
        const agentNameFilter = (document.getElementById("previewAgentName")?.value || "").trim();
        const agentNameRoles = agentNameFilter ? rolesForAgentName(agentNameFilter) : [];
        const propTypeFilter = (document.getElementById("previewPropertyType")?.value || "").toLowerCase();
        const month = (yVal && mVal) ? `${yVal}-${mVal}` : "all";

        const tbody = document.getElementById("previewBody");
        try {
            // Always refresh raw entries so newly added rows appear immediately
            const rawRes = await api("/api/commission-rates");
            loadedRates = (await rawRes.json()).map((r) => ({
                rate_type: r.rate_type || "Basic Commission",
                agent_type: r.agent_type || "", hierarchy: r.hierarchy || "", agent: r.agent || "",
                label: r.label || "", condition: r.condition || "",
                rate_pct: r.rate_pct || "", override_rate_pct: r.override_rate_pct || "",
                profit_sharing_rate_pct: r.profit_sharing_rate_pct || "",
                profit_sharing_mode: r.profit_sharing_mode || "",
                property_type: r.property_type || "", trigger_pct: r.trigger_pct || "",
                invoice_date_from: r.invoice_date_from || "", rule_type: r.rule_type || "",
                amount_rm: r.amount_rm || "",
                effective_from: r.effective_from, remarks: r.remarks || ""
            }));

            const res = await api(`/api/basic-rates/resolved?month=${encodeURIComponent(month)}`);
            const data = await res.json();
            renderPayoutPanel(data.rules || [], month);

            // Apply agent type, property type, year, month, and section filter
            const sectionRateTypeMap = {
                basic_nfp: "Basic Commission",
                anp: "ANP Commission",
                ega_esa: "EGA/ESA Award",
                production_bonus: "Production Bonus Rate",
                monthly_contest: "Monthly Contest"
            };
            const targetRateType = sectionRateTypeMap[activeDataSection] || "Basic Commission";

            let filteredRates = (data.rates || []).filter((r) => {
                if (targetRateType && r.rate_type && r.rate_type !== targetRateType && activeDataSection !== "basic_nfp") {
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

            // Sort by month descending: latest at the top
            filteredRates.sort((a, b) => {
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
                    e.source !== "legacy" && e.source !== "default"
                ) || loadedRates.find((e) =>
                    (e.rate_type || "Basic Commission") === "Basic Commission" &&
                    (e.agent_type || "").toLowerCase() === (r.agent_type || "").toLowerCase() &&
                    (e.hierarchy || "").toLowerCase() === (r.hierarchy || "").toLowerCase() &&
                    (e.agent || "").toLowerCase() === (r.agent || "").toLowerCase() &&
                    (normProp(e.property_type) === normProp(r.property_type))
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
                    property_type: r.property_type || (rawEntry && rawEntry.property_type) || ""
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

                const tr = document.createElement("tr");
                tr.innerHTML = `
                    <td class="sm-cell">${escapeHtml(cType)}</td>
                    <td class="sm-cell">${escapeHtml(fmtMonth(displayEff) || "—")}</td>
                    <td class="sm-cell">${escapeHtml(r.agent_type || "All")}</td>
                    <td class="sm-cell">${escapeHtml(roleLabel)}</td>
                    <td class="sm-cell">${escapeHtml(propTypeDisplay)}</td>
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
            tbody.innerHTML = `<tr><td colspan="10">Could not load values.</td></tr>`;
        }
    }

    function renderPreviewPage() {
        const tbody = document.getElementById("previewBody");
        const paginationEl = document.getElementById("previewPagination");
        const totalPages = Math.max(1, Math.ceil(previewAllRows.length / PREVIEW_PAGE_SIZE));
        previewPage = Math.max(1, Math.min(previewPage, totalPages));

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

    function renderNfpPreview(nfpRates) {
        // The Net Floor Price Commission card was removed from this page, so
        // there is nothing to draw into. Kept as a no-op guard rather than
        // deleted, since loadPreview() still receives nfp_rates from the API.
        const tbody = document.getElementById("nfpPreviewBody");
        if (!tbody) return;
        tbody.innerHTML = "";
        if (!nfpRates.length) {
            tbody.innerHTML = `<tr><td colspan="8" style="color:var(--text-muted);">No NFP tier entries cover this month.</td></tr>`;
            return;
        }
        const info = typeInfo("Net Floor Price Rate");
        nfpRates.forEach((r) => {
            const roleLabel = r.agent ? `${r.hierarchy || "All"} — ${r.agent}` : (r.hierarchy || "All");
            const condTitle = r.condition ? ` title="${escapeHtml(r.condition)}"` : "";
            const tr = document.createElement("tr");
            tr.innerHTML = `
                <td class="sm-cell">Net Floor Price</td>
                <td class="sm-cell">${escapeHtml(r.agent_type)}</td>
                <td class="sm-cell">${escapeHtml(roleLabel)}</td>
                <td class="sm-cell">${Number(r.rate_pct).toFixed(2)}%</td>
                <td><span class="rule-cell"${condTitle}>${escapeHtml(r.condition || info.rule || "n/a")}</span></td>
                <td>${formulaCellHtml(info)}</td>
                <td>${sourceBadge(r.source)}</td>
                <td>${isAdmin ? `<button class="btn btn-secondary row-edit-btn" title="Revise this tier">✏️</button>` : ""}</td>
            `;
            const btn = tr.querySelector(".row-edit-btn");
            if (btn) btn.addEventListener("click", () => openDataEditModal(r, "Net Floor Price Rate"));
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
        loadedRates = (await res.json()).map((r) => ({
            rate_type: r.rate_type || "Basic Commission",
            agent_type: r.agent_type || "", hierarchy: r.hierarchy || "", agent: r.agent || "",
            label: r.label || "", condition: r.condition || "",
            rate_pct: r.rate_pct || "", override_rate_pct: r.override_rate_pct || "",
            profit_sharing_rate_pct: r.profit_sharing_rate_pct || "",
            profit_sharing_mode: r.profit_sharing_mode || "",
            property_type: r.property_type || "", trigger_pct: r.trigger_pct || "",
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
    // Postgres is the LIVE source for agent names/types/IC/branch.
    // SQLite (loadedRoles) stores Role and Reports-To overrides only.

    let loadedRoles = [];
    // Live Postgres agent data (from /api/agent-roles/pg-list)
    let pgAgentsCache = [];
    let rolesList = [];
    let rolesPage = 1;
    const ROLES_PAGE_SIZE = 15;
    const BRANCHES = ["JB Sales Branch", "Kluang Sales Branch", "Klang Sales Branch", "Seremban Sales Branch"];

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

    function rebuildRolesList() {
        if (pgAgentsCache.length) {
            rolesList = [];
            pgAgentsCache.forEach((pg) => {
                const savedRows = savedRolesForPg(pg);
                const visibleRows = savedRows.filter((r) => !r.hidden);
                if (!savedRows.length) {
                    // No manual row yet — one baseline row sourced entirely from
                    // Postgres (including Role, when the active tags resolve to
                    // exactly one unambiguous role).
                    rolesList.push({
                        agent:          toSentenceCase(pg.name),
                        agent_type:     pg.agent_type || "",
                        ic_no:          pg.ic_no || "",
                        nick_name:      pg.nick_name || "",
                        full_name:      "",
                        branch:         pg.branch || "",
                        effective_from: CURRENT_YM,
                        hierarchy:      pg.hierarchy || "",
                        role_conflict:  !!pg.role_conflict,
                        reports_to:     "",
                        pg_bubble_id:   pg.bubble_id || "",
                    });
                } else if (!visibleRows.length) {
                    // Every saved row for this person is a hidden tombstone —
                    // they were explicitly deleted. Postgres still lists them
                    // (that's exactly why a plain delete kept reappearing), so
                    // skip creating any row at all instead of falling back to
                    // a fresh Postgres baseline.
                } else {
                    // One row per saved effective-dated entry — this is what
                    // lets the same agent carry an old Role and a new Role as
                    // two separate rows instead of one overwriting the other.
                    visibleRows.forEach((r) => {
                        rolesList.push({
                            agent:          toSentenceCase(r.agent),
                            agent_type:     r.agent_type   || pg.agent_type || "",
                            ic_no:          r.ic_no         || pg.ic_no || "",
                            nick_name:      r.nick_name     || pg.nick_name || "",
                            full_name:      r.full_name     || "",
                            branch:         r.branch        || pg.branch || "",
                            effective_from: r.effective_from || CURRENT_YM,
                            hierarchy:      r.hierarchy      || pg.hierarchy || "",
                            role_conflict:  !r.hierarchy && !!pg.role_conflict,
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
                if (hasIdentity && !alreadyShown && !r.hidden) {
                    rolesList.push({
                        agent:          toSentenceCase(r.agent),
                        agent_type:     r.agent_type     || "",
                        ic_no:          r.ic_no          || "",
                        nick_name:      r.nick_name      || "",
                        full_name:      r.full_name      || "",
                        branch:         r.branch         || "",
                        effective_from: r.effective_from || CURRENT_YM,
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
                ic_no:          r.ic_no          || "",
                nick_name:      r.nick_name      || "",
                full_name:      r.full_name      || "",
                branch:         r.branch         || "",
                effective_from: r.effective_from || CURRENT_YM,
                hierarchy:      r.hierarchy      || "",
                reports_to:     r.reports_to     || "",
                pg_bubble_id:   r.pg_bubble_id   || "",
            }));
        }
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

    /**
     * PRIMARY LOADER — calls /api/agent-roles/pg-list, populates pgAgentsCache,
     * updates metric cards and re-renders the table immediately.
     */
    async function loadPgAgents() {
        const statusEl = document.getElementById("pgAgentsStatus");
        if (statusEl) { statusEl.className = "save-status"; statusEl.textContent = "Loading from Postgres…"; }
        try {
            const res  = await api("/api/agent-roles/pg-list");
            const data = await res.json();
            if (!res.ok) throw new Error(data.error || "Failed to load");
            pgAgentsCache = data.agents || [];
            if (statusEl) {
                statusEl.className = "save-status ok";
                statusEl.textContent = `${pgAgentsCache.length} agent${pgAgentsCache.length === 1 ? "" : "s"} loaded from Postgres.`;
                setTimeout(() => { if (statusEl) statusEl.textContent = ""; }, 4000);
            }
            rebuildRolesList();
            renderRoles();
        } catch (e) {
            pgAgentsCache = [];
            if (statusEl) { statusEl.className = "save-status err"; statusEl.textContent = e.message; }
        }
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
        try {
            const res = await api("/api/agent-roles");
            loadedRoles = await res.json();
        } catch (e) {
            loadedRoles = [];
        }
        rebuildRolesList();
        renderRoles();
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

        let filtered = rolesList;
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

        const total = filtered.length;
        const totalPages = Math.max(1, Math.ceil(total / ROLES_PAGE_SIZE));
        rolesPage = Math.max(1, Math.min(rolesPage, totalPages));

        const start = (rolesPage - 1) * ROLES_PAGE_SIZE;
        const pageRows = filtered.slice(start, start + ROLES_PAGE_SIZE);

        pageRows.forEach((r) => {
            const tr = document.createElement("tr");
            tr.className = "role-row";
            
            const displayMonth = fmtMonth(r.effective_from || CURRENT_YM);
            const displayAgent = r.agent || "—";
            const displayFullName = r.full_name || "—";
            const displayNick = r.nick_name || "—";
            const displayIc = r.ic_no || "—";
            const displayAtype = r.agent_type || "—";
            const displayRole = r.hierarchy || (r.role_conflict ? "⚠ needs split" : "—");
            const displayReports = r.reports_to || "—";
            const displayBranch = r.branch || "—";

            tr.innerHTML = `
                <td class="sm-cell">${escapeHtml(displayMonth)}</td>
                <td class="sm-cell" style="font-weight:600;">${escapeHtml(displayAgent)}</td>
                <td class="sm-cell">${escapeHtml(displayFullName)}</td>
                <td class="sm-cell">${escapeHtml(displayNick)}</td>
                <td class="sm-cell">${escapeHtml(displayIc)}</td>
                <td class="sm-cell">${escapeHtml(displayAtype)}</td>
                <td class="sm-cell"><span class="role-badge" style="background:#e0e7ff; color:#3730a3; padding:2px 8px; border-radius:12px; font-size:11px; font-weight:600;">${escapeHtml(displayRole)}</span></td>
                <td class="sm-cell">${escapeHtml(displayReports)}</td>
                <td class="sm-cell">${escapeHtml(displayBranch)}</td>
                <td style="white-space:nowrap;">
                    ${isAdmin ? `<button class="btn btn-secondary row-edit-btn" title="Edit agent details" style="padding:4px 8px; font-size:12px;">✏️ Edit</button>` : ""}
                </td>
            `;

            tr.querySelector(".row-edit-btn")?.addEventListener("click", () => {
                openAgentRoleModal(r);
            });

            tbody.appendChild(tr);
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
            tbody.innerHTML = `<tr><td colspan="10" style="color:var(--text-muted); text-align:center;">${msg}</td></tr>`;
        }

        // Render pagination controls
        const paginationEl = document.getElementById("rolesPagination");
        if (paginationEl) {
            if (!filtered.length) {
                paginationEl.innerHTML = "";
            } else {
                const info = `<span style="font-size:13px; color:var(--text-muted); margin: 0 10px;">Page ${rolesPage} of ${totalPages} (${filtered.length} total rows)</span>`;
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

    function openAgentRoleModal(r) {
        editingAgentObj = r;
        const modal = document.getElementById("agentRoleModal");
        if (!modal) return;

        const roleSel = document.getElementById("roleModalRole");
        if (roleSel) {
            roleSel.innerHTML = `<option value="">(none)</option>` + 
                allRoleNames().map((x) => `<option value="${escapeHtml(x)}">${escapeHtml(x)}</option>`).join("");
        }

        if (r) {
            document.getElementById("agentRoleModalTitle").textContent = "Edit Agent Details";
            document.getElementById("roleModalMonth").value = r.effective_from || CURRENT_YM;
            document.getElementById("roleModalAgent").value = r.agent || "";
            document.getElementById("roleModalFullName").value = r.full_name || "";
            document.getElementById("roleModalNickName").value = r.nick_name || "";
            document.getElementById("roleModalIcNo").value = r.ic_no || "";
            document.getElementById("roleModalAgentType").value = r.agent_type || "Internal";
            if (roleSel) roleSel.value = r.hierarchy || "";
            document.getElementById("roleModalReportsTo").value = r.reports_to || "";
            document.getElementById("roleModalBranch").value = r.branch || "";
            document.getElementById("roleModalDeleteBtn").style.display = "";
        } else {
            document.getElementById("agentRoleModalTitle").textContent = "Add Agent Details";
            document.getElementById("roleModalMonth").value = CURRENT_YM;
            document.getElementById("roleModalAgent").value = "";
            document.getElementById("roleModalFullName").value = "";
            document.getElementById("roleModalNickName").value = "";
            document.getElementById("roleModalIcNo").value = "";
            document.getElementById("roleModalAgentType").value = "Internal";
            if (roleSel) roleSel.value = "";
            document.getElementById("roleModalReportsTo").value = "";
            document.getElementById("roleModalBranch").value = "";
            document.getElementById("roleModalDeleteBtn").style.display = "none";
        }

        modal.classList.remove("hidden");
    }

    // Modal Event Bindings
    document.getElementById("roleModalSaveBtn")?.addEventListener("click", async () => {
        const agentName = document.getElementById("roleModalAgent").value.trim();
        const fullName = document.getElementById("roleModalFullName").value.trim();
        const nickNameCheck = document.getElementById("roleModalNickName").value.trim();
        if (!agentName && !fullName && !nickNameCheck) {
            alert("Enter at least one of Agent Name (from eeAdmin), Full Name, or Nick Name.");
            return;
        }

        const effMonth = document.getElementById("roleModalMonth").value.trim() || CURRENT_YM;
        const nickName = nickNameCheck;
        const icNo = document.getElementById("roleModalIcNo").value.trim();
        const agentType = document.getElementById("roleModalAgentType").value;
        const role = document.getElementById("roleModalRole").value;
        const reportsTo = document.getElementById("roleModalReportsTo").value.trim();
        const branch = document.getElementById("roleModalBranch").value.trim();

        if (editingAgentObj) {
            editingAgentObj.effective_from = effMonth;
            editingAgentObj.agent = agentName;
            editingAgentObj.full_name = fullName;
            editingAgentObj.nick_name = nickName;
            editingAgentObj.ic_no = icNo;
            editingAgentObj.agent_type = agentType;
            editingAgentObj.hierarchy = role;
            editingAgentObj.reports_to = reportsTo;
            editingAgentObj.branch = branch;
        } else {
            const newAgent = {
                effective_from: effMonth,
                agent: agentName,
                full_name: fullName,
                nick_name: nickName,
                ic_no: icNo,
                agent_type: agentType,
                hierarchy: role,
                reports_to: reportsTo,
                branch: branch
            };
            rolesList.push(newAgent);
        }

        document.getElementById("agentRoleModal").classList.add("hidden");
        await saveRoles();
    });

    document.getElementById("roleModalDeleteBtn")?.addEventListener("click", async () => {
        if (!editingAgentObj) return;
        if (!confirm(`Are you sure you want to delete ${editingAgentObj.agent}?`)) return;

        // A Postgres-linked agent reappears on the very next reload — Postgres
        // itself was never asked to remove them — unless a hidden tombstone
        // row is kept for them. Only needed when this was their last visible
        // row; if another role-history row still exists, plain removal is
        // enough (Postgres won't recreate a baseline while a saved row exists).
        const bubbleId = editingAgentObj.pg_bubble_id;
        const hasOtherVisibleRow = bubbleId && rolesList.some(
            (r) => r !== editingAgentObj && r.pg_bubble_id === bubbleId && !r.hidden
        );
        rolesList = rolesList.filter(r => r !== editingAgentObj);
        if (bubbleId && !hasOtherVisibleRow) {
            rolesList.push({
                agent: editingAgentObj.agent,
                pg_bubble_id: bubbleId,
                hidden: true,
                effective_from: CURRENT_YM,
                agent_type: "", hierarchy: "", reports_to: "", branch: "",
                ic_no: "", nick_name: "", full_name: "",
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

    function collectRoles() {
        return rolesList.map(r => ({
            effective_from: r.effective_from || CURRENT_YM,
            agent: (r.agent || "").trim(),
            full_name: (r.full_name || "").trim(),
            nick_name: (r.nick_name || "").trim(),
            agent_type: r.agent_type,
            hierarchy: r.hierarchy,
            reports_to: r.reports_to,
            branch: r.branch,
            ic_no: r.ic_no,
            pg_bubble_id: r.pg_bubble_id || "",
            hidden: r.hidden ? 1 : 0,
            remarks: r.hidden ? "deleted via roles grid (kept as a tombstone so Postgres stops re-adding this agent)" : "edited via roles grid"
        })).filter(r => r.agent || r.full_name || r.nick_name);
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
                body: JSON.stringify({ month: "2026-01" })
            });
            const d = await res.json();
            if (!res.ok) throw new Error(d.error || "Seed failed");
            status.className = "save-status ok";
            status.textContent = `Added ${d.added} agent(s); ${d.total} listed. ` +
                `Now fill in Role and Reports To.`;
            await loadRoles();
        } catch (e) {
            status.className = "save-status err";
            status.textContent = e.message;
        }
    }

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
            // Default to the requested month, else the month selected above.
            const monthEl = document.getElementById("previewMonth");
            const pick = String(wantMonth || (monthEl ? monthEl.value : "")).padStart(2, "0");
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
                 .replace(/\bfeb\b/gi, "FEB");
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

        setPaymentRulesInList(parsePaymentRulesFromEntry(r));

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

    function getOverrideRolesForAgentType(agentType) {
        const atype = agentType || document.getElementById("modalAgentType")?.value || "Internal";
        const roles = RATE_ROLES[atype] || [];
        return [""].concat(roles);
    }

    function refreshOverrideFromDropdowns() {
        const rows = document.querySelectorAll("#overrideRulesList .orule-row");
        const availableRoles = getOverrideRolesForAgentType();
        rows.forEach((r) => {
            const sel = r.querySelector(".orule-from");
            if (!sel) return;
            const currentVal = sel.value;
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

        const availableRoles = getOverrideRolesForAgentType();

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

    function isSameRawEntry(e, target) {
        if (target.id && e.id && String(e.id) === String(target.id)) return true;
        const norm = (v) => String(v || "").trim().toLowerCase();
        const rType = norm(e.rate_type || "Basic Commission") === norm(target.rate_type || "Basic Commission");
        const aType = norm(e.agent_type) === norm(target.agent_type);
        const hier = norm(e.hierarchy) === norm(target.hierarchy);
        const ag = norm(e.agent) === norm(target.agent);
        const eff = norm(e.effective_from) === norm(target.effective_from);
        const prop = normProp(e.property_type) === normProp(target.property_type);
        return rType && aType && hier && ag && eff && prop;
    }
    function fullEntryKey(e) {
        const norm = (v) => String(v || "").trim().toLowerCase();
        return [
            norm(e.rate_type || "Basic Commission"),
            norm(e.agent_type),
            norm(e.hierarchy),
            norm(e.agent),
            normProp(e.property_type),
            norm(e.effective_from)
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
            profit_sharing_rate_pct: r.profit_sharing_rate_pct || "",
            profit_sharing_mode: r.profit_sharing_mode || "",
            property_type: r.property_type || "", trigger_pct: r.trigger_pct || "",
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
            profit_sharing_rate_pct: r.profit_sharing_rate_pct || "",
            profit_sharing_mode: r.profit_sharing_mode || "",
            property_type: r.property_type || "", trigger_pct: r.trigger_pct || "",
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
            effective_from: editingRawEntry.effective_from || CURRENT_YM,
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
            const primaryOverrideRate = oRules.map((r) => r.override_rate_pct).filter(Boolean).join(", ");
            const primaryOverrideFrom = oRules.map((r) => r.override_from).filter(Boolean).join(", ");

            const pRules = getPaymentRulesFromList();
            const formattedCond = formatPaymentRulesCondition(pRules);
            const primaryTrigger = pRules.map((r) => r.trigger_pct).filter(Boolean).join(", ");
            const primaryAdvance = (pRules.find((r) => r.rule_type === "Advance") || {}).amount_rm || "";
            const primaryRuleType = pRules.length > 1 ? "Multi-stage" : ((pRules[0] || {}).rule_type || "");

            // Rate type: prefer saved entry's type, then modal hidden field, then default
            const rateType = (editingRawEntry && editingRawEntry.rate_type)
                || getVal("modalType")
                || "Basic Commission";

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
        ["previewYear", "previewMonth", "previewPropertyType", "previewAgentName"].forEach((id) => {
            const el = document.getElementById(id);
            if (el) el.addEventListener("change", loadPreview);
        });
        // Agent Type drives Role, and both drive Agent Name, so each rebuilds
        // the dropdowns below it before re-filtering.
        const atypeEl = document.getElementById("previewAgentType");
        if (atypeEl) atypeEl.addEventListener("change", () => {
            populatePreviewRoleFilter();
            populatePreviewAgentNameFilter();
            loadPreview();
        });
        const roleEl = document.getElementById("previewRole");
        if (roleEl) roleEl.addEventListener("change", () => {
            populatePreviewAgentNameFilter();
            loadPreview();
        });
        populatePreviewRoleFilter();
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
            keyInput: "anpPeriod", keyKind: "month",
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
            keyInput: "egaYear", keyKind: "year",
            badge: "egaSavedBadge", status: "egaStatus", saveBtn: "egaSaveBtn",
            extraKey: "agent_type", extraInput: "egaAgentType",
            prefix: "ega_", fields: ["ega_threshold", "esa_threshold", "factory_from",
                                     "factory_first_block", "factory_balance_rate", "factory_min_panels"],
            childKey: "months", childBody: "egaMonthsBody", addBtn: "egaAddMonthBtn",
            childFields: [
                { name: "month", type: "number" },
                { name: "ep_threshold", type: "number" },
                { name: "label", type: "text" }
            ]
        },
        pb: {
            card: "pbCard", endpoint: "/api/production-bonus-rules", keyName: "effective_from",
            keyInput: "pbPeriod", keyKind: "month",
            badge: "pbSavedBadge", status: "pbStatus", saveBtn: "pbSaveBtn",
            prefix: "pb_", fields: ["min_paid", "property_types",
                                    "oum_team_target", "oum_personal_target", "oum_rate_pct",
                                    "ogm_team_target", "ogm_osa_rate_pct", "ogm_oum_rate_pct",
                                    "stage1_pct", "stage2_pct", "stage3_pct"]
        }
    };

    function ruleKeyValue(cfg) {
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
            setTimeout(() => { status.textContent = ""; }, 2500);
        } catch (err) {
            status.className = "save-status err";
            status.textContent = err.message;
        }
    }

    function initRuleSections() {
        Object.keys(RULE_SECTIONS).forEach(key => {
            const cfg = RULE_SECTIONS[key];
            document.getElementById(cfg.keyInput)?.addEventListener("change", () => loadRuleSection(key));
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
        const nfpListCard = document.getElementById("nfpListCard");
        const viewCardTitle = document.querySelector("#viewCard .card-header h3");

        const titles = {
            roles: "Agent Roles & Hierarchy",
            basic_nfp: "Basic & Net Floor Price Commission",
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
        // Sections with their own rules form, keyed by section id.
        const RULE_CARD_BY_SECTION = { anp: "anp", ega_esa: "ega", production_bonus: "pb" };
        const ruleKey = RULE_CARD_BY_SECTION[activeDataSection] || null;
        Object.keys(RULE_SECTIONS).forEach(k => {
            const el = document.getElementById(RULE_SECTIONS[k].card);
            if (el) el.style.display = (ruleKey === k) ? "block" : "none";
        });

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

        if (viewCard) {
            viewCard.style.display = (isRoles || isContest || ruleKey) ? "none" : "block";
        }

        // The price list is opened on demand, so switching section closes it.
        if (nfpListCard) {
            nfpListCard.style.display = "none";
        }

        // "View Net Floor Price List" only makes sense on Basic & NFP.
        const nfpListBtn = document.getElementById("nfpListBtn");
        if (nfpListBtn) {
            nfpListBtn.style.display = activeDataSection === "basic_nfp" ? "" : "none";
        }

        if (viewCardTitle) {
            if (activeDataSection === "basic_nfp") viewCardTitle.textContent = "Basic Commission";
            else viewCardTitle.textContent = titles[activeDataSection] || "Commission Rates";
        }

        if (isContest) {
            loadContestList();
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
    const wantSection = urlParams.get("section");

    initPreviewFilters();
    initDataSectionList();
    initContestRules();
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
        // updateDataSectionView() already ran, before isAdmin was known, so a
        // deep link straight to the contest section would have rendered its
        // rows without the admin controls. Re-render now that the role is in.
        if (activeDataSection === "monthly_contest") loadContestList();
        const reloadKey = { anp: "anp", ega_esa: "ega", production_bonus: "pb" }[activeDataSection];
        if (reloadKey) loadRuleSection(reloadKey);
        if (urlParams.get("showNfpList")) {
            showNfpList(urlParams.get("month") || "");
        }
    }).catch((err) => {
        console.error("loadMe error:", err);
    });
})();
