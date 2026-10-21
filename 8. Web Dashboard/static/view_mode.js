// Phone layout on the commission portal, switched on by itself on a
// phone-sized screen (narrower than 768px) and off on anything wider. There is
// no Desktop / Phone switch any more (removed at the user's request); the
// menu opens from a ☰ button shown only in the phone layout. Like
// sidebar_toggle.js, the styling travels with the script, so a cached
// styles.css cannot leave it unstyled.
(function () {
    const KEY = "viewMode";
    const host = document.getElementById("viewModeToggle");
    if (!host) return;

    const style = document.createElement("style");
    style.textContent = `
        .view-mode-toggle { display: none; }
        body.phone-mode .view-mode-toggle {
            display: inline-flex; border: 1px solid #cbd5e1; border-radius: 9999px;
            background: #ffffff; padding: 2px; gap: 2px;
        }
        .view-mode-toggle button {
            border: 0; background: transparent; color: #475569; cursor: pointer;
            font-size: 12px; font-weight: 600; padding: 4px 12px; border-radius: 9999px;
            display: inline-flex; align-items: center; gap: 5px;
        }
        .view-mode-toggle button:hover { background: #f1f5f9; }
        .view-mode-toggle button.active { background: #2563eb; color: #ffffff; }
        .header-right { display: flex; flex-direction: column; align-items: center; gap: 8px; }

        body.phone-mode { height: auto; }
        body.phone-mode .app-container {
            flex-direction: column; height: auto; width: 100%;
            max-width: 480px; margin: 0 auto; box-shadow: 0 0 0 1px #e2e8f0;
        }
        /* Phone: the menu is closed until ☰ Menu opens it over the page. */
        body.phone-mode aside.sidebar {
            display: none; position: fixed; top: 0; z-index: 950;
            left: max(0px, calc(50vw - 240px)); width: min(300px, 85vw);
            height: 100vh; overflow-y: auto; padding: 16px;
            box-shadow: 4px 0 24px rgba(15, 23, 42, 0.35);
        }
        body.phone-mode.phone-menu-open aside.sidebar { display: flex !important; }
        body.phone-mode .sidebar-toggle-btn { display: none !important; }
        .view-mode-toggle .view-mode-menu { display: none; }
        body.phone-mode .view-mode-toggle .view-mode-menu { display: inline-flex; }
        body.phone-mode.phone-menu-open .view-mode-toggle .view-mode-menu { background: #0f172a; color: #ffffff; }
        body.phone-mode .main-content { padding: 12px; height: auto; overflow: visible; gap: 14px; }
        /* Header: title and month on one small line (subtitle dropped), then
           the switch on the left and icon-only buttons on the right. */
        body.phone-mode .content-header { flex-direction: column; align-items: stretch; gap: 8px; }
        body.phone-mode .header-title { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0 6px; }
        body.phone-mode .header-title .section-title-header,
        body.phone-mode .header-title h1 { font-size: 16px; margin: 0; letter-spacing: 0; text-shadow: none; }
        body.phone-mode .header-title h1::before { content: "· "; color: #94a3b8; }
        body.phone-mode .header-title p { display: none; }
        body.phone-mode .header-right { flex-direction: row; align-items: center; justify-content: space-between; }
        body.phone-mode .view-mode-toggle .vm-txt { display: none; }
        body.phone-mode .view-mode-toggle button { padding: 5px 9px; font-size: 14px; }
        body.phone-mode .header-actions { gap: 6px; }
        body.phone-mode .header-actions .btn { gap: 0; padding: 7px 10px; }
        body.phone-mode .header-actions .btn .icon { font-size: 16px; }
        body.phone-mode .vm-btn-txt { display: none; }

        /* Cards: every card on one compact row; a row too long scrolls. */
        body.phone-mode .metrics-row {
            display: grid; grid-auto-flow: column; grid-auto-columns: minmax(0, 1fr);
            grid-template-columns: none; gap: 6px; overflow-x: auto; -webkit-overflow-scrolling: touch;
        }
        body.phone-mode .metrics-row > .metric-card,
        body.phone-mode .rate-cards-group .rate-card {
            min-height: 0; height: auto; padding: 6px 2px; border-radius: 8px; gap: 0;
            min-width: 0; max-width: none; cursor: pointer;
        }
        body.phone-mode .metrics-row .metric-card { transition: none; }
        body.phone-mode .metrics-row .metric-card:hover { transform: none; }
        body.phone-mode .metric-details { min-width: 0; width: 100%; text-align: center; }
        body.phone-mode .metric-details .metric-label {
            font-size: 9.5px; margin-bottom: 2px; line-height: 1.2;
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
        }
        body.phone-mode .metric-details .metric-value { font-size: 12px; line-height: 1.2; white-space: nowrap; letter-spacing: -0.2px; }

        /* Filters: one line, no labels; Clear becomes an x. */
        body.phone-mode .filter-panel { flex-direction: row; flex-wrap: wrap; align-items: center; gap: 6px; padding: 8px; }
        body.phone-mode .filter-group { gap: 0; min-width: 0; }
        body.phone-mode .filter-group label { display: none; }
        body.phone-mode .filter-group-narrow { flex: 0 0 43%; min-width: 0; max-width: none; }
        body.phone-mode .filter-group:not(.filter-group-narrow) { flex: 1 1 0; }
        body.phone-mode .filter-group input,
        body.phone-mode .filter-group select { padding: 7px 8px; font-size: 13px; width: 100%; min-width: 0; }
        body.phone-mode .btn-clear { padding: 6px 8px; }
        body.phone-mode .btn-clear::before { content: "✕"; font-size: 15px; }
        body.phone-mode .table-card .card-header { flex-wrap: wrap; gap: 8px; padding: 12px; }
        body.phone-mode .table-container,
        body.phone-mode .agent-summary-inline-wrapper,
        body.phone-mode .dynamic-bonus-table-wrapper { overflow-x: auto; -webkit-overflow-scrolling: touch; }

        /* Everything below the cards at the cards' scale: phones enlarge text
           in wide areas (text autosizing), which blew the tables up next to
           the cards, so that is switched off and each part sized down. */
        html:has(body.phone-mode), body.phone-mode { -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }
        body.phone-mode .dashboard-table,
        body.phone-mode .agent-summary-inline-table,
        body.phone-mode .dynamic-bonus-table-wrapper table {
            table-layout: auto; min-width: 620px; font-size: 11px !important;
        }
        body.phone-mode .dashboard-table th,
        body.phone-mode .agent-summary-inline-table th,
        body.phone-mode .dynamic-bonus-table-wrapper th {
            font-size: 9.5px !important; padding: 4px 5px !important; line-height: 1.25;
        }
        body.phone-mode .dashboard-table td,
        body.phone-mode .agent-summary-inline-table td,
        body.phone-mode .dynamic-bonus-table-wrapper td {
            font-size: 11px !important; padding: 4px 5px !important; line-height: 1.3;
        }
        /* The customer table has many columns: give it room to scroll
           rather than breaking words letter by letter. */
        body.phone-mode #dataTable { min-width: 1150px; }
        body.phone-mode .dashboard-table th,
        body.phone-mode .dashboard-table td,
        body.phone-mode .agent-summary-inline-table th,
        body.phone-mode .agent-summary-inline-table td { word-break: normal; overflow-wrap: normal; hyphens: none; }
        body.phone-mode .dashboard-table td.numeric,
        body.phone-mode .dashboard-table td.date,
        body.phone-mode .agent-summary-inline-table td.numeric { white-space: nowrap; }
        body.phone-mode .dashboard-table td *,
        body.phone-mode .agent-summary-inline-table td * { font-size: inherit; }
        body.phone-mode .agent-summary-inline-header { flex-wrap: wrap; gap: 6px; padding: 10px 0 6px; }
        body.phone-mode .agent-summary-inline-header h4,
        body.phone-mode .customer-detail-title h4,
        body.phone-mode .dynamic-bonus-table-wrapper h4 { font-size: 14px !important; margin: 0 !important; }
        body.phone-mode .customer-detail-title { margin: 12px 0 6px !important; }
        body.phone-mode .agent-summary-inline-badge,
        body.phone-mode .row-count-badge { font-size: 10px; padding: 2px 7px; }
        body.phone-mode #tableHeaderHint { font-size: 11px !important; }
        body.phone-mode .agent-summary-inline-actions,
        body.phone-mode .legend-actions-group { flex-wrap: wrap; gap: 6px; padding: 6px 10px; }
        body.phone-mode .btn-sm-inline { font-size: 11px !important; padding: 4px 8px !important; }
        body.phone-mode .btn-sm-inline .icon { font-size: 11px !important; }
        /* Agent and customer names get room for a couple of words a line. */
        body.phone-mode .agent-summary-inline-table td:first-child,
        body.phone-mode .agent-summary-inline-table th:first-child { min-width: 104px; }
        body.phone-mode #dataTable td:nth-child(-n+2),
        body.phone-mode #dataTable th:nth-child(-n+2) { min-width: 92px; }
        body.phone-mode .btn-commission-slip { font-size: 10px; padding: 2px 6px; }
        body.phone-mode .empty-state { padding: 24px 12px; }
        body.phone-mode .empty-state h4 { font-size: 14px; }
        body.phone-mode .empty-state p { font-size: 12px; }
    `;
    document.head.appendChild(style);

    host.className = "view-mode-toggle";
    host.setAttribute("role", "group");
    host.setAttribute("aria-label", "Layout");
    host.innerHTML = `
        <button type="button" class="view-mode-menu" title="Open or close the menu">☰<span class="vm-txt"> Menu</span></button>`;
    // Icon-only header buttons (and the filters' Clear) keep their name as a
    // tooltip; the visible words go in a span Phone mode hides.
    document.querySelectorAll(".header-actions .btn, #clearFiltersBtn").forEach((b) => {
        const name = b.textContent.replace(/\s+/g, " ").trim();
        if (name && !b.title) b.title = name;
        if (name) b.setAttribute("aria-label", name);
        [...b.childNodes].forEach((n) => {
            if (n.nodeType === 3 && n.textContent.trim()) {
                const span = document.createElement("span");
                span.className = "vm-btn-txt";
                span.textContent = n.textContent;
                b.replaceChild(span, n);
            }
        });
    });

    // The old switch remembered a choice here; drop it so a computer that was
    // set to Phone does not stay narrow.
    try { localStorage.removeItem(KEY); } catch (e) { /* storage blocked */ }
    function read() {
        return window.innerWidth < 768 ? "phone" : "desktop";
    }
    function apply(mode) {
        const was = document.body.classList.contains("phone-mode");
        document.body.classList.toggle("phone-mode", mode === "phone");
        if (was !== (mode === "phone")) document.body.classList.remove("phone-menu-open");
        syncCards();
        // The sidebar toggle button and wide tables size themselves on resize.
        window.dispatchEvent(new Event("resize"));
    }

    host.addEventListener("click", (e) => {
        if (e.target.closest(".view-mode-menu")) document.body.classList.toggle("phone-menu-open");
    });
    // ── Compact cards ────────────────────────────────────────────────────
    // In Phone mode a card's label drops "Total" and "(RM)" and a large amount
    // is shortened (RM 2,046,898.36 -> RM 2.05M); tapping a card shows its full
    // amount. The page keeps writing full values into the cards, so each change
    // is caught and shortened again; Desktop mode puts the full text back.
    function shortLabel(t) {
        const out = t.replace(/^\s*Total\s+/i, "").replace(/\s*\(RM\)\s*$/i, "").trim();
        return ({ "Other Commission": "Other", "Referral Fee": "Referral", "Commission": "Comm.", "Customers": "Cust." })[out] || out;
    }
    function shortValue(t) {
        const m = /^\s*(-?)\s*RM\s*(-?)([\d,]+(?:\.\d+)?)\s*$/.exec(t);
        if (!m) return t.trim();
        const sign = m[1] || m[2] ? "-" : "";
        const n = parseFloat(m[3].replace(/,/g, ""));
        if (n >= 1e6) return `${sign}RM${(n / 1e6).toFixed(2)}M`;
        if (n >= 1e4) return `${sign}RM${(n / 1e3).toFixed(1)}k`;
        return `${sign}RM${Math.round(n).toLocaleString("en-MY")}`;
    }
    let syncing = false;
    function syncCards() {
        if (syncing) return;
        syncing = true;
        const phone = document.body.classList.contains("phone-mode");
        document.querySelectorAll(".metrics-row .metric-label, .metrics-row .metric-value").forEach((el) => {
            const shown = el.textContent;
            // A value the page wrote since the last pass becomes the new full text.
            if (el.dataset.vmShort === undefined || shown !== el.dataset.vmShort) el.dataset.vmFull = shown;
            const full = el.dataset.vmFull;
            const card = el.closest(".metric-card");
            const wantFull = !phone || (card && card.classList.contains("vm-show-full") && el.classList.contains("metric-value"));
            const next = wantFull ? full : (el.classList.contains("metric-label") ? shortLabel(full) : shortValue(full));
            el.dataset.vmShort = next;
            if (shown !== next) el.textContent = next;
            if (phone) el.title = full; else el.removeAttribute("title");
        });
        syncing = false;
    }
    const metricsRow = document.querySelector(".metrics-row");
    if (metricsRow) {
        new MutationObserver(() => { if (!syncing) syncCards(); })
            .observe(metricsRow, { childList: true, subtree: true, characterData: true });
        metricsRow.addEventListener("click", (e) => {
            if (!document.body.classList.contains("phone-mode")) return;
            const card = e.target.closest(".metric-card");
            if (!card) return;
            card.classList.toggle("vm-show-full");
            syncCards();
        });
    }

    // Picking a section (or any link) in the open phone menu closes it, and
    // so does a tap outside it.
    const sidebar = document.querySelector("aside.sidebar");
    if (sidebar) {
        sidebar.addEventListener("click", (e) => {
            if (!document.body.classList.contains("phone-mode")) return;
            if (e.target.closest("li, a")) document.body.classList.remove("phone-menu-open");
        });
    }
    document.addEventListener("click", (e) => {
        if (!document.body.classList.contains("phone-menu-open")) return;
        if (e.target.closest("aside.sidebar") || e.target.closest(".view-mode-menu")) return;
        document.body.classList.remove("phone-menu-open");
    });
    apply(read());
    // Turning a phone sideways or resizing a window can cross the line.
    let lastMode = read();
    window.addEventListener("resize", () => {
        const mode = read();
        if (mode !== lastMode) { lastMode = mode; apply(mode); }
    });
})();
