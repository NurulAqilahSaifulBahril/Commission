document.addEventListener("DOMContentLoaded", () => {
    // -------------------------------------------------------------
    // User Roles Configuration & Permissions System
    // -------------------------------------------------------------
    const USER_ROLES = {
        "finance_shuyee": {
            name: "Shu Yee",
            role: "Finance Manager",
            avatarColor: "#3b82f6",
            allowedScopes: ["internal", "outsource"],
            filterAgentName: null,
            readOnly: false
        },
        "hr_elise": {
            name: "Elise",
            role: "HR Executive",
            avatarColor: "#f59e0b",
            allowedScopes: ["internal"],
            filterAgentName: null,
            readOnly: true
        },
        "admin 1_gan zhi hong": {
            name: "Gan Zhi Hong",
            role: "Admin 1",
            avatarColor: "#ef4444",
            allowedScopes: ["internal", "outsource"],
            filterAgentName: null,
            readOnly: false
        },
        "admin 2_aqilah": {
            name: "Aqilah",
            role: "Admin 2",
            avatarColor: "#ec4899",
            allowedScopes: ["internal", "outsource"],
            filterAgentName: null,
            readOnly: false
        },
        "gan lai soon": {
            name: "Gan Lai Soon",
            role: "Outsource Agent",
            avatarColor: "#10b981",
            allowedScopes: ["outsource"],
            filterAgentName: "Gan Lai Soon",
            readOnly: true
        },
        "yin chou": {
            name: "Yin Chou",
            role: "Internal Agent",
            avatarColor: "#8b5cf6",
            allowedScopes: ["internal"],
            filterAgentName: "Teoh Yin Chiou",
            readOnly: true
        }
    };

    // -------------------------------------------------------------
    // State Management
    // -------------------------------------------------------------
    // The overview page's sub-nav links here as /report?section=<id>, so the
    // requested section is what opens. Anything unrecognised falls back to
    // Basic & NFP rather than leaving the page on a section that renders
    // nothing.
    const VALID_SECTIONS = new Set([
        "basic_nfp", "anp", "ega_esa", "production_bonus", "monthly_contest",
    ]);
    const requestedSection = new URLSearchParams(window.location.search).get("section");

    let state = {
        currentUser: "finance_shuyee",
        isAdmin: false,   // set by initAccountBar(); gates editing the slip's IC
        activeYear: "2026",
        activeAgentType: "internal",
        activeMonth: "5", // May
        activeSection: VALID_SECTIONS.has(requestedSection) ? requestedSection : "basic_nfp",
        rawData: null,
        filters: {
            search: "",
            // "all" | "advance" (RM300 tranche payable this month) | "balance"
            // (75%-milestone balance payable this month). July 2026+ only.
            payoutStage: "all",
            // "all" | "ega" | "esa". EGA/ESA Award section only. Each agent
            // carries ONE eligibility label and ESA outranks EGA, so the two
            // lists never overlap: an ESA winner appears under ESA only.
            egaAward: "all"
        },
        specialCaseRowRefs: new Set(), // Set of row-array references that were added via modal
        specialCasePairs: [],         // Array of [basicRow, nfpRow] pairs for bulk deletion
        factoryRates: [],
        editingFactoryRate: null,
        specialCaseMode: "standard",
        // Which commission row the open modal was raised from: "basic", "nfp",
        // or "all". Decides which rows the case creates.
        specialCaseRowKind: "all",
        customerSearchTimer: null,
        agentSearchTimer: null,
        selectedSpecialCaseAgent: "",
        selectedSpecialCaseCustomer: "",
        // Set when the modal is opened from the Gan Lai Soon column, so the
        // preview knows to focus that rate field. Cleared once it has.
        focusGanOverride: false,
        // Opened from that column the modal edits one thing: his override rate.
        // The case-type selector and the inputs it drives are hidden, since
        // none of them are what the click was about. Lives until the modal
        // closes, unlike focusGanOverride which is consumed on first use.
        specialCaseGanMode: false
    };
    // Months the contest workbook has a sheet for; populated by initContestMonths().
    // Starts null so the tab is not hidden before the list arrives.
    let contestMonths = null;

    function hasContestData(month) {
        if (contestMonths === null) return true;
        return contestMonths.includes(parseInt(month));
    }

    // Canonical agent full-name lookup (nickname -> full name), sourced from
    // the Agent Roles & Hierarchy page (agent_roles table) via
    // /api/agent-name-map. Used to display full agent names in Title Case
    // across every table.
    let agentNameMap = {};

    function normalizeAgentKey(name) {
        return String(name == null ? "" : name).replace(/\s+/g, "").toLowerCase();
    }

    function titleCaseName(name) {
        return String(name == null ? "" : name).replace(/[A-Za-z]+/g, w =>
            w.charAt(0).toUpperCase() + w.slice(1).toLowerCase()
        );
    }

    // Resolve any stored agent name to its canonical Title Case full name.
    // Blank values are returned unchanged; unknown names are Title Cased.
    function resolveAgentName(name) {
        if (name === null || name === undefined) return name;
        const text = String(name).trim();
        if (!text) return name;
        const full = agentNameMap[normalizeAgentKey(text)];
        return full ? full : titleCaseName(text);
    }

    function isAgentHeader(header) {
        return typeof header === "string" && header.toLowerCase().includes("agent");
    }

    async function initAgentNameMap() {
        try {
            const res = await fetch("/api/agent-name-map");
            const data = await res.json();
            agentNameMap = data && data.map ? data.map : {};
        } catch (err) {
            agentNameMap = {};
        }
    }

    async function initContestMonths() {
        try {
            const res = await fetch("/api/contest-months");
            const data = await res.json();
            contestMonths = Array.isArray(data.months) ? data.months : [];
        } catch (err) {
            contestMonths = [];
        }
        normalizeActiveSection();
        renderSectionTabs();
    }

    // The empty-state element is shared, so its default copy is captured once and
    // restored before each render (an error message may have overwritten it).
    const NO_DATA_DEFAULTS = (() => {
        const view = document.getElementById("noDataView");
        return {
            heading: view?.querySelector("h4")?.textContent ?? "",
            detail: view?.querySelector("p")?.textContent ?? ""
        };
    })();

    function resetNoDataView() {
        const view = document.getElementById("noDataView");
        if (!view) return;
        const heading = view.querySelector("h4");
        const detail = view.querySelector("p");
        if (heading) heading.textContent = NO_DATA_DEFAULTS.heading;
        if (detail) detail.textContent = NO_DATA_DEFAULTS.detail;
    }

    let commissionFetchToken = 0;
    let commissionRefreshInFlight = false;

    function getViewStateCacheKey() {
        return `commission_view_state_v1:${state.currentUser || "guest"}`;
    }

    function loadSavedViewState() {
        try {
            const raw = localStorage.getItem(getViewStateCacheKey());
            if (!raw) return;
            const saved = JSON.parse(raw);
            if (!saved || typeof saved !== "object") return;
            if (saved.activeYear) state.activeYear = String(saved.activeYear);
            if (saved.activeMonth) state.activeMonth = String(saved.activeMonth);
            if (saved.activeAgentType) state.activeAgentType = String(saved.activeAgentType);
            if (saved.activeSection) state.activeSection = String(saved.activeSection);
        } catch (_) {}
    }

    function saveViewState() {
        try {
            localStorage.setItem(getViewStateCacheKey(), JSON.stringify({
                activeYear: state.activeYear,
                activeMonth: state.activeMonth,
                activeAgentType: state.activeAgentType,
                activeSection: state.activeSection
            }));
        } catch (err) {
            console.warn("Failed to persist commission view state", err);
        }
    }

    function syncViewControls() {
        if (yearSelect) yearSelect.value = state.activeYear;
        if (agentTypeSelect) agentTypeSelect.value = state.activeAgentType;
        if (monthSelect) monthSelect.value = state.activeMonth;
    }

    function normalizeActiveSection() {
        let validConfigs = [...sectionConfigs[state.activeAgentType]];
        if (!hasContestData(state.activeMonth)) {
            validConfigs = validConfigs.filter(cfg => cfg.id !== "monthly_contest");
        }
        const validSections = validConfigs.map(s => s.id);
        if (!validSections.includes(state.activeSection)) {
            state.activeSection = "basic_nfp";
        }
    }

    // ANP Commission is internal agents only. Viewing the ANP tab always
    // forces Agent Type back to Internal (the dropdown is also disabled
    // while the tab is active — see renderActiveSection). Returns true when
    // the agent type actually changed, so the caller knows to re-fetch.
    function enforceAnpAgentTypeLock() {
        if (state.activeSection !== "anp" || state.activeAgentType === "internal") {
            return false;
        }
        state.activeAgentType = "internal";
        const agentTypeSelectEl = document.getElementById("agentTypeSelect");
        if (agentTypeSelectEl) agentTypeSelectEl.value = "internal";
        normalizeActiveSection();
        saveViewState();
        renderSectionTabs();
        return true;
    }

    // -------------------------------------------------------------
    // User Session Initialization
    // -------------------------------------------------------------
    function initUserSession() {
        const loginOverlay = document.getElementById("loginOverlay");
        const headerProfile = document.getElementById("headerProfile");
        const profileName = document.getElementById("profileName");
        const profileRole = document.getElementById("profileRole");
        const headerAvatar = document.getElementById("headerAvatar");
        const appContainer = document.querySelector(".app-container");
        
        if (!state.currentUser || !USER_ROLES[state.currentUser]) {
            if (loginOverlay) loginOverlay.classList.remove("hidden");
            if (headerProfile) headerProfile.classList.add("hidden");
            if (appContainer) {
                appContainer.style.filter = "blur(12px)";
                appContainer.classList.add("role-disabled");
            }
            return false;
        } else {
            if (loginOverlay) loginOverlay.classList.add("hidden");
            if (headerProfile) headerProfile.classList.remove("hidden");
            if (appContainer) {
                appContainer.style.filter = "none";
                appContainer.classList.remove("role-disabled");
            }
            
            const user = USER_ROLES[state.currentUser];
            if (profileName) profileName.textContent = user.name;
            if (profileRole) profileRole.textContent = user.role;
            if (headerAvatar) {
                headerAvatar.textContent = user.name.split(" ").map(n => n[0]).join("").toUpperCase().slice(0, 2);
                headerAvatar.style.backgroundColor = user.avatarColor;
            }
            
            // Adjust Scope Options based on allowedScopes
            const agentTypeSelect = document.getElementById("agentTypeSelect");
            if (agentTypeSelect) {
                const scopeAllowed = (scope) => scope === "all"
                    ? (user.allowedScopes.includes("internal") && user.allowedScopes.includes("outsource"))
                    : user.allowedScopes.includes(scope);

                for (let i = 0; i < agentTypeSelect.options.length; i++) {
                    const option = agentTypeSelect.options[i];
                    const isAllowed = scopeAllowed(option.value);
                    if (!isAllowed) {
                        option.disabled = true;
                        option.style.display = "none";
                    } else {
                        option.disabled = false;
                        option.style.display = "block";
                    }
                }

                // If currently selected scope is not allowed, switch automatically
                if (!scopeAllowed(state.activeAgentType)) {
                    state.activeAgentType = user.allowedScopes[0];
                    agentTypeSelect.value = state.activeAgentType;
                    normalizeActiveSection();
                    saveViewState();
                    // Render appropriate tabs
                    renderSectionTabs();
                }
            }

            // Sync, PDF and Excel button visibility
            const syncBtn = document.getElementById("syncDataBtn");
            const pdfBtn = document.getElementById("downloadPdfBtn");
            const excelBtn = document.getElementById("downloadExcelBtn");
            
            if (syncBtn) {
                syncBtn.classList.toggle("role-hidden", user.readOnly);
            }
            if (pdfBtn) {
                pdfBtn.classList.toggle("role-hidden", user.filterAgentName !== null);
            }
            if (excelBtn) {
                excelBtn.classList.toggle("role-hidden", user.filterAgentName !== null);
            }
            
            return true;
        }
    }

    // -------------------------------------------------------------
    // DOM Elements
    // -------------------------------------------------------------
    const yearSelect = document.getElementById("yearSelect");
    const agentTypeSelect = document.getElementById("agentTypeSelect");
    const monthSelect = document.getElementById("monthSelect");
    const sectionList = document.getElementById("sectionList");
    
    const monthHeader = document.getElementById("monthHeader");
    const subHeader = document.getElementById("subHeader");
    const tableTitle = document.getElementById("tableTitle");
    const rowCount = document.getElementById("rowCount");
    
    const totalAgents = document.getElementById("totalAgents");
    const totalCustomers = document.getElementById("totalCustomers");
    
    const searchFilter = document.getElementById("searchFilter");
    const clearFiltersBtn = document.getElementById("clearFiltersBtn");
    const rm300FilterGroup = document.getElementById("rm300FilterGroup");
    const payoutStageFilter = document.getElementById("payoutStageFilter");
    const egaAwardFilterGroup = document.getElementById("egaAwardFilterGroup");
    const egaAwardFilter = document.getElementById("egaAwardFilter");
    const rateCardsContainer = document.getElementById("rateCardsContainer");
    
    const syncDataBtn = document.getElementById("syncDataBtn");
    const downloadPdfBtn = document.getElementById("downloadPdfBtn");
    const downloadExcelBtn = document.getElementById("downloadExcelBtn");
    
    const dataTable = document.getElementById("dataTable");
    const tableHeaders = document.getElementById("tableHeaders");
    // Grouping row for the Basic & NFP detail table only ("Other Commission"
    // spanning OVERRIDE / Safwan / Gan Lai Soon). Created once and inserted
    // ahead of #tableHeaders so every other report table -- which never
    // populates it -- renders exactly as before with a single header row.
    const tableGroupHeaders = document.createElement("tr");
    tableGroupHeaders.id = "tableGroupHeaders";
    tableGroupHeaders.style.display = "none";
    tableHeaders.parentElement.insertBefore(tableGroupHeaders, tableHeaders);
    const tableBody = document.getElementById("tableBody");
    const loader = document.getElementById("loader");
    const noDataView = document.getElementById("noDataView");
    const tableContainer = document.querySelector(".table-container");


    const MONTH_KEY_MAP = {
        1: "jan", 2: "feb", 3: "mac", 4: "mac", 5: "mac",
        6: "jun", 7: "jul", 8: "aug", 9: "sep", 10: "oct", 11: "nov", 12: "dec"
    };

    // Residential / Shop Lot / Commercial rates
    const TIER_RATE_TABLE = {
        internal: {
            Senior:    { jan: 3.25, feb: 3.25, mac: 3.25, jun: 4.25, jul: 4.25, aug: 4.25, sep: 4.25, oct: 4.25, nov: 4.25, dec: 4.25 },
            Executive: { jan: 3,    feb: 3,    mac: 3,    jun: 4,    jul: 4,    aug: 4,    sep: 4,    oct: 4,    nov: 4,    dec: 4    }
        },
        outsource: {
            OGM: { jan: 5,   feb: 5,   mac: 5,   jun: 6,   jul: 6,   aug: 6,   sep: 6,   oct: 6,   nov: 6,   dec: 6   },
            OUM: { jan: 4.5, feb: 4.5, mac: 4.5, jun: 5.5, jul: 5.5, aug: 5.5, sep: 5.5, oct: 5.5, nov: 5.5, dec: 5.5 },
            OSA: { jan: 4.5, feb: 4.5, mac: 4.5, jun: 5.5, jul: 5.5, aug: 5.5, sep: 5.5, oct: 5.5, nov: 5.5, dec: 5.5 }
        }
    };
    // Factory rates (base rate; agent earns base + extra per contract)
    const FACTORY_RATE_TABLE = {
        internal: {
            Senior:    { res: 2.0 },   // 2% + x
            Executive: { res: 2.0 }
        },
        outsource: {
            OGM: { res: 2.5 },         // 2.5% + x
            OUM: { res: 2.5 },
            OSA: { res: 2.5 }
        }
    };

    const INTERNAL_TIER_ORDER = ["Senior", "Executive"];
    const OUTSOURCE_TIER_ORDER = ["OGM", "OUM", "OSA"];
    const sectionConfigs = {
        internal: [
            { id: "basic_nfp", label: "Basic & Net Floor Price Commission" },
            { id: "anp", label: "ANP Commission" },
            { id: "ega_esa", label: "EGA/ESA Award" },
            { id: "production_bonus", label: "Production Bonus" },
            { id: "monthly_contest", label: "Monthly Contest" }
        ],
        outsource: [
            { id: "basic_nfp", label: "Basic & Net Floor Price Commission" },
            { id: "anp", label: "ANP Commission" },
            { id: "ega_esa", label: "EGA/ESA Award" },
            { id: "production_bonus", label: "Production Bonus" },
            { id: "monthly_contest", label: "Monthly Contest" }
        ],
        all: [
            { id: "basic_nfp", label: "Basic & Net Floor Price Commission" },
            { id: "anp", label: "ANP Commission" },
            { id: "ega_esa", label: "EGA/ESA Award" },
            { id: "production_bonus", label: "Production Bonus" },
            { id: "monthly_contest", label: "Monthly Contest" }
        ]
    };

    // -------------------------------------------------------------
    // Event Listeners Setup
    // -------------------------------------------------------------
    function initEventListeners() {
        // Dropdown changes
        yearSelect.addEventListener("change", (e) => {
            state.activeYear = e.target.value;
            saveViewState();
            fetchData();
        });
        
        agentTypeSelect.addEventListener("change", (e) => {
            state.activeAgentType = e.target.value;
            normalizeActiveSection();
            saveViewState();
            renderSectionTabs();
            renderSectionTotalCards();
            fetchData();
        });

        // Month dropdown change
        monthSelect.addEventListener("change", (e) => {
            state.activeMonth = e.target.value;
            normalizeActiveSection();
            saveViewState();
            renderSectionTabs();
            fetchData();
        });

        // Section tab clicks
        sectionList.addEventListener("click", (e) => {
            const item = e.target.closest("li");
            if (!item) return;
            
            sectionList.querySelectorAll("li").forEach(li => li.classList.remove("active"));
            item.classList.add("active");
            
            state.activeSection = item.getAttribute("data-section");
            saveViewState();
            if (enforceAnpAgentTypeLock()) {
                fetchData();
            } else {
                renderActiveSection();
            }
        });

        // Data page link carries the current section's commission type so the
        // Data page opens pre-filtered to it (sections without a wired data
        // type fall back to the generic Data page).
        const dataPageLink = document.getElementById("dataPageLink");
        if (dataPageLink) {
            dataPageLink.addEventListener("click", () => {
                const sectionTypeMap = {
                    basic_nfp: "Basic Commission",
                    anp: "ANP Commission",
                    ega_esa: "EGA/ESA Award",
                    production_bonus: "Production Bonus Rate",
                    monthly_contest: "Monthly Contest"
                };
                const type = sectionTypeMap[state.activeSection];
                const params = new URLSearchParams();
                if (type) {
                    params.set("type", type);
                    params.set("agent_type", state.activeAgentType === "outsource" ? "Outsource" : "Internal");
                }
                dataPageLink.href = "/data" + (params.toString() ? "?" + params.toString() : "");
            });
        }

        // Search input (instant filtering across agent and customer)
        if (searchFilter) {
            searchFilter.addEventListener("input", (e) => {
                state.filters.search = e.target.value.toLowerCase().trim();
                renderActiveSection();
            });
        }

        // Payout Stage filter (Basic & Net Floor Price Commission section,
        // July 2026+ reporting months only). "Advance RM 300" keeps rows whose
        // Basic Commission (RM300) tranche has a value this month; "Balance
        // Payout" keeps Basic Commission rows whose 75%-milestone balance is
        // payable this month.
        if (payoutStageFilter) {
            payoutStageFilter.addEventListener("change", (e) => {
                state.filters.payoutStage = e.target.value;
                renderActiveSection();
            });
        }

        // EGA/ESA Award section only: keep agents whose eligibility is an EGA
        // award or an ESA award. Early-bird labels count under their own award.
        if (egaAwardFilter) {
            egaAwardFilter.addEventListener("change", (e) => {
                state.filters.egaAward = e.target.value;
                renderActiveSection();
            });
        }

        // Clear filters button
        clearFiltersBtn.addEventListener("click", () => {
            if (searchFilter) searchFilter.value = "";
            state.filters.search = "";
            if (payoutStageFilter) payoutStageFilter.value = "all";
            state.filters.payoutStage = "all";
            if (egaAwardFilter) egaAwardFilter.value = "all";
            state.filters.egaAward = "all";
            renderActiveSection();
        });

        // Download actions
        downloadPdfBtn.addEventListener("click", () => showPdfModal());
        downloadExcelBtn.addEventListener("click", () => triggerDownload("excel"));

        // PDF Modal handlers
        document.getElementById("pdfModalClose").addEventListener("click", closePdfModal);
        document.getElementById("pdfCloseBtn").addEventListener("click", closePdfModal);
        document.getElementById("pdfDownloadBtn").addEventListener("click", () => triggerDownload("pdf"));

        // Close modal when clicking outside
        document.getElementById("pdfModal").addEventListener("click", (e) => {
            if (e.target.id === "pdfModal") closePdfModal();
        });

        // Sync cache action
        if (syncDataBtn) {
            syncDataBtn.addEventListener("click", async () => {
                syncDataBtn.disabled = true;
                const originalText = syncDataBtn.innerHTML;
                syncDataBtn.innerHTML = `<span class="icon">⌛</span> Syncing...`;
                showLoader(true);
                
                try {
                    const res = await fetch("/api/cache/clear", { method: "POST" });
                    if (!res.ok) throw new Error("Failed to clear cache");
                    
                    // Trigger a data reload. Since clear/prefetch starts in background,
                    // we reload to let the user see the loader while the backend fetches data.
                    await fetchData();
                } catch (err) {
                    alert(`Sync error: ${err.message}`);
                } finally {
                    syncDataBtn.disabled = false;
                    syncDataBtn.innerHTML = originalText;
                }
            });
        }
    }

    // -------------------------------------------------------------
    // Page Tab/Section Builders
    // -------------------------------------------------------------
    function renderSectionTabs() {
        sectionList.innerHTML = "";
        let configs = [...sectionConfigs[state.activeAgentType]];
        
        if (!hasContestData(state.activeMonth)) {
            configs = configs.filter(cfg => cfg.id !== "monthly_contest");
        }
        
        configs.forEach(cfg => {
            const li = document.createElement("li");
            li.setAttribute("data-section", cfg.id);
            if (cfg.id === state.activeSection) {
                li.classList.add("active");
            }
            
            li.innerHTML = `
                <span class="bullet"></span>
                <span class="label-text">${cfg.label}</span>
            `;
            sectionList.appendChild(li);
        });
    }

    function updateHeaders() {
        const monthOption = monthSelect.querySelector(`option[value="${state.activeMonth}"]`);
        const monthLabel = monthOption ? monthOption.textContent : "";
        const agentTypeLabel = state.activeAgentType === "all" ? "All Agents" : (state.activeAgentType === "internal" ? "Internal Agents" : "Outsource Agents");
        
        monthHeader.textContent = monthLabel;
        subHeader.textContent = `${agentTypeLabel} Commission Summary`;
        
        const currentConfig = sectionConfigs[state.activeAgentType].find(s => s.id === state.activeSection);
        if (state.activeSection === "basic_nfp") {
            tableTitle.textContent = "Basic & Net Floor Price Commission";
        } else if (state.activeSection === "monthly_contest") {
            tableTitle.textContent = `${monthLabel} Monthly Contest`;
        } else {
            tableTitle.textContent = currentConfig ? currentConfig.label : "";
        }
    }

    function getCommissionCacheKey() {
        // v5: v4 payloads predate "agent_role_history", so a returning browser
        // would fall back to the report-month role on every agent hover for up
        // to six hours -- silently wrong for invoices that predate a role
        // change. (v4 did the same for "agent_roles", v3 for "system_details".)
        return `commission_api_cache_v5:${state.activeYear}:${state.activeMonth}:${state.activeAgentType}`;
    }

    function loadCachedCommissionPayload() {
        try {
            const raw = localStorage.getItem(getCommissionCacheKey());
            if (!raw) return null;
            const parsed = JSON.parse(raw);
            if (!parsed || typeof parsed !== "object" || !parsed.payload) return null;
            const age = Date.now() - Number(parsed.updatedAt || 0);
            if (Number.isFinite(age) && age > 6 * 60 * 60 * 1000) return null;
            return parsed.payload;
        } catch (_) {
            return null;
        }
    }

    function saveCachedCommissionPayload(payload) {
        try {
            localStorage.setItem(getCommissionCacheKey(), JSON.stringify({
                updatedAt: Date.now(),
                payload
            }));
        } catch (err) {
            console.warn("Failed to persist commission payload cache", err);
        }
    }

    // The server regenerates a random boot id every time it starts (e.g. after
    // a code fix is deployed and the process is restarted). If that id doesn't
    // match what we last saw, our localStorage commission cache was computed
    // by the old process and is stale — drop it so we hit the network instead
    // of showing outdated numbers.
    async function invalidateStaleCommissionCacheOnServerRestart() {
        try {
            const res = await fetch("/api/boot-id");
            if (!res.ok) return;
            const data = await res.json();
            const bootId = data && data.boot_id;
            if (!bootId) return;
            const storedBootId = localStorage.getItem("dashboard_server_boot_id");
            if (storedBootId !== bootId) {
                // Version-agnostic on purpose: pinning the prefix to one cache
                // version silently stops the eviction the day the version is
                // bumped, and leaves the superseded entries behind forever.
                Object.keys(localStorage)
                    .filter(k => k.startsWith("commission_api_cache_"))
                    .forEach(k => localStorage.removeItem(k));
                localStorage.setItem("dashboard_server_boot_id", bootId);
            }
        } catch (_) {
            // A network hiccup on this check shouldn't block the app from loading.
        }
    }

    function hydrateSpecialCaseState(data) {
        state.specialCaseRowRefs.clear();
        state.specialCasePairs = [];

        const nfpSection = data.sections ? data.sections.basic_nfp : null;
        if (!nfpSection || !nfpSection.rows || !nfpSection.headers) return;

        const headers = nfpSection.headers;
        const rows = nfpSection.rows;
        const scRows = [];
        rows.forEach(r => {
            if (r.length > headers.length) {
                const rawDataStr = r[r.length - 1];
                try {
                    const scData = JSON.parse(rawDataStr);
                    if (scData && scData.agent && scData.customer) {
                        r.specialCaseData = scData;
                        state.specialCaseRowRefs.add(r);
                        scRows.push(r);
                    }
                } catch (_) {}
            }
        });

        const paired = new Set();
        for (let i = 0; i < scRows.length; i++) {
            const row1 = scRows[i];
            if (paired.has(row1)) continue;
            for (let j = i + 1; j < scRows.length; j++) {
                const row2 = scRows[j];
                if (paired.has(row2)) continue;
                if (JSON.stringify(row1.specialCaseData) === JSON.stringify(row2.specialCaseData)) {
                    state.specialCasePairs.push([row1, row2]);
                    paired.add(row1);
                    paired.add(row2);
                    break;
                }
            }
        }
    }

    async function applyCommissionPayload(data, { persistCache = false } = {}) {
        state.rawData = data;
        hydrateSpecialCaseState(data);

        // Don't block first paint on the rate lookup.
        void loadFactoryRates();

        totalAgents.textContent = data.summary?.total_agents ?? "0";
        totalCustomers.textContent = data.summary?.total_customers ?? "0";

        if (persistCache) {
            saveCachedCommissionPayload(data);
        }

        renderActiveSection();
    }

    /** Re-read the role maps for the selected month and re-render if they
     *  changed. Cheap enough to run on every cache-rendered load, and it is
     *  what makes a Data page role edit show up in the report immediately
     *  instead of whenever the payload cache happens to expire. */
    async function refreshAgentRoleMaps() {
        if (!state.rawData) return;
        try {
            const res = await fetch(`/api/agent-role-maps?year=${encodeURIComponent(state.activeYear)}`
                                  + `&month=${encodeURIComponent(state.activeMonth)}`);
            if (!res.ok) return;
            const maps = await res.json();
            if (!maps || maps.error || !state.rawData) return;
            const changed =
                JSON.stringify(state.rawData.agent_roles || null) !== JSON.stringify(maps.agent_roles || null) ||
                JSON.stringify(state.rawData.agent_role_history || null) !== JSON.stringify(maps.agent_role_history || null);
            if (!changed) return;
            state.rawData.agent_roles = maps.agent_roles;
            state.rawData.agent_role_history = maps.agent_role_history;
            renderActiveSection();
        } catch (_) {
            // Stale roles are a display nuisance, not a reason to fail the page.
        }
    }

    function normalizeApiErrorMessage(message) {
        const text = String(message || "").trim();
        const lower = text.toLowerCase();
        if (
            lower.includes("winerror 10061") ||
            lower.includes("connection refused") ||
            lower.includes("urlopen error")
        ) {
            return "Unable to reach PG Proxy. Check the proxy/server connection and try again.";
        }
        if (lower.includes("traceback (most recent call last):")) {
            return "Dashboard request failed. Check dashboard.log for details.";
        }
        if (lower.includes("timed out") || lower.includes("timeout")) {
            return "The dashboard request timed out. Please try again in a moment.";
        }
        return text || "Dashboard request failed.";
    }

    // Fetches and parses one agent-type's commission payload. Throws a
    // normalized Error on any HTTP/JSON failure.
    async function fetchCommissionJson(agentType) {
        const res = await fetch(`/api/commission?year=${state.activeYear}&month=${state.activeMonth}&agent_type=${agentType}`);
        const text = await res.text();
        if (!res.ok) {
            let errMsg = `Server error (HTTP ${res.status})`;
            try {
                const errData = JSON.parse(text);
                errMsg = errData.error || errMsg;
            } catch (_) {
                const stripped = text.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim().slice(0, 300);
                errMsg = stripped || errMsg;
            }
            throw new Error(normalizeApiErrorMessage(errMsg));
        }
        try {
            return JSON.parse(text);
        } catch (jsonErr) {
            console.error("JSON parsing failed. Raw response text was:", text);
            console.error("Parse error was:", jsonErr);
            throw new Error("Server returned invalid JSON. Check the server console for errors.");
        }
    }

    // Merges two same-shaped {headers, rows} sections into one by unioning
    // their headers (order preserved, Internal's ordering first) and padding
    // each side's rows with "-" for columns only the other side has. Any
    // trailing elements past headers.length (special-case JSON blobs) are
    // preserved so hydrateSpecialCaseState keeps working on the merged rows.
    // `headerAlias`, if given, maps a raw header name to a display name before
    // union'ing, so e.g. Internal's "Count of Customer" and Outsource's
    // "Total Invoice" can be treated as the same column instead of two.
    function unionMergeSections(sectionA, sectionB, headerAlias) {
        const alias = headerAlias || (h => h);
        const parts = [sectionA, sectionB].filter(s => s && s.headers && s.rows);
        if (parts.length === 0) return { headers: [], rows: [] };
        // A column only one side has goes where that side keeps it -- directly
        // after the column it follows there -- not on the end of the table.
        // Outsource's "Gan Lai Soon" sits between "Safwan (RM)" and "Referral
        // Name"; appending it instead stranded it past Remarks, which broke the
        // "Other Commission" group header (see renderTable): that scan collects
        // a contiguous run, so it produced one group over OVERRIDE + Safwan and
        // a second, orphaned one over Gan Lai Soon at the far right.
        const unionHeaders = [];
        parts.forEach(s => {
            let anchor = -1;   // where this part's previous column landed
            s.headers.forEach(h => {
                const ah = alias(h);
                const at = unionHeaders.indexOf(ah);
                if (at !== -1) {
                    anchor = at;
                    return;
                }
                anchor += 1;
                unionHeaders.splice(anchor, 0, ah);
            });
        });
        const rows = [];
        parts.forEach(s => {
            const idxMap = s.headers.map(h => unionHeaders.indexOf(alias(h)));
            s.rows.forEach(r => {
                const newRow = new Array(unionHeaders.length).fill("-");
                idxMap.forEach((ui, oi) => { newRow[ui] = r[oi]; });
                for (let extra = s.headers.length; extra < r.length; extra++) newRow.push(r[extra]);
                rows.push(newRow);
            });
        });
        return { headers: unionHeaders, rows };
    }

    // Internal's "Count of Customer" and Outsource's "Total Invoice" mean the
    // same thing (invoice/customer count per agent) — merge them into one
    // "Total Invoices" column instead of showing both side by side.
    function agentSummaryHeaderAlias(h) {
        const n = String(h || "").toLowerCase().trim();
        if (n === "count of customer" || n === "total invoice") return "Total Invoices";
        return h;
    }

    // Sums the "Referral Fee" column of basic_nfp per agent (excluding
    // Total/Summary/Grand rows), so it can be attached to agent_summary,
    // which doesn't carry Referral Fee from the backend.
    function computeReferralFeeByAgent(basicNfpSection) {
        const map = new Map();
        if (!basicNfpSection || !basicNfpSection.headers || !basicNfpSection.rows) return map;
        const headers = basicNfpSection.headers;
        const agentIdx = headers.findIndex(h => h.toLowerCase().trim() === "agent");
        const referralFeeIdx = headers.findIndex(h => h.toLowerCase().trim() === "referral fee");
        if (agentIdx === -1 || referralFeeIdx === -1) return map;
        let cur = "";
        basicNfpSection.rows.forEach(r => {
            const v = r[agentIdx] ? String(r[agentIdx]).trim() : "";
            if (v) cur = v;
            const nameLower = cur.toLowerCase();
            if (!cur || nameLower.includes("total") || nameLower.includes("summary") || nameLower.includes("grand")) return;
            const fee = parseMoneyValue(r[referralFeeIdx]);
            if (fee) map.set(nameLower, (map.get(nameLower) || 0) + fee);
        });
        return map;
    }

    // Appends a "Referral Fee" column to agent_summary, placing each agent's
    // total on the first row of that agent's block (blank elsewhere) — the
    // same convention the backend already uses for the "Agent" column itself,
    // so renderAgentSummaryInline's existing group-rowspan logic applies to it
    // unchanged, and summing the column once per agent (not per row) is safe.
    function addReferralFeeColumn(agentSummarySection, referralFeeByAgent) {
        const headers = [...agentSummarySection.headers, "Referral Fee"];
        const agentIdx = agentSummarySection.headers.findIndex(h => h.toLowerCase().trim() === "agent");
        const rows = agentSummarySection.rows.map(r => {
            const rawAgentCell = agentIdx !== -1 && r[agentIdx] ? String(r[agentIdx]).trim() : "";
            let feeCell = "-";
            if (rawAgentCell) {
                const nameLower = rawAgentCell.toLowerCase();
                if (!nameLower.includes("total") && !nameLower.includes("summary") && !nameLower.includes("grand")) {
                    const total = referralFeeByAgent.get(nameLower) || 0;
                    feeCell = total ? total.toFixed(2) : "-";
                }
            }
            return [...r, feeCell];
        });
        return { headers, rows };
    }

    // Builds a lowercased-agent-name -> "internal"|"outsource" lookup from the
    // two source payloads (before merging), so rows in the combined "All"
    // table can be colored by their true origin instead of guessing a tier
    // from the agent's name (which risks misclassifying a mixed set).
    function buildAgentTypeMap(internalData, outsourceData) {
        const map = {};
        function addNames(payload, type) {
            const sections = payload?.sections;
            if (!sections) return;
            ["basic_nfp", "anp", "agent_summary"].forEach(key => {
                const sec = sections[key];
                if (!sec || !sec.headers || !sec.rows) return;
                const agentIdx = sec.headers.findIndex(h => h.toLowerCase().trim() === "agent");
                if (agentIdx === -1) return;
                let cur = "";
                sec.rows.forEach(r => {
                    const v = r[agentIdx] ? String(r[agentIdx]).trim() : "";
                    if (v) cur = v;
                    const nameLower = cur.toLowerCase();
                    if (!cur || nameLower.includes("total") || nameLower.includes("summary") || nameLower.includes("grand")) return;
                    map[nameLower] = type;
                });
            });
        }
        addNames(internalData, "internal");
        addNames(outsourceData, "outsource");
        return map;
    }

    function mergeEgaEsaSections(internalEga, outsourceEga) {
        if (!internalEga && !outsourceEga) return null;
        return {
            headers_t1: internalEga?.headers_t1 || outsourceEga?.headers_t1 || ["Agent Name", "Customer Count", "Accumulated Sales Price", "Accumulated EP Point", "Eligibility"],
            rows_t1: [...(internalEga?.rows_t1 || []), ...(outsourceEga?.rows_t1 || [])],
            headers_t2: internalEga?.headers_t2 || outsourceEga?.headers_t2 || ["Agent Name", "Customer Name", "Invoice Date", "1st Payment Date", "Sales Price", "Accumulated EP Point", "Eligibility"],
            rows_t2: [...(internalEga?.rows_t2 || []), ...(outsourceEga?.rows_t2 || [])]
        };
    }

    function mergeProductionBonusSections(internalPb, outsourcePb) {
        if (!internalPb && !outsourcePb) return outsourcePb || internalPb || null;
        return {
            headers_oum: outsourcePb?.headers_oum || internalPb?.headers_oum || ["Agent", "Total Sales", "Status", "Bonus Amount"],
            rows_oum: [...(internalPb?.rows_oum || []), ...(outsourcePb?.rows_oum || [])],
            headers_ogm: outsourcePb?.headers_ogm || internalPb?.headers_ogm || ["Agent", "Total Sales", "Status", "Bonus Amount"],
            rows_ogm: [...(internalPb?.rows_ogm || []), ...(outsourcePb?.rows_ogm || [])],
            headers_detail: outsourcePb?.headers_detail || internalPb?.headers_detail || ["Agent", "Customer", "Sales Price"],
            rows_detail: [...(internalPb?.rows_detail || []), ...(outsourcePb?.rows_detail || [])]
        };
    }

    // Combines an Internal and an Outsource commission payload into one
    // payload for the "All Agents" view.
    // Customer -> invoice system details, combined across both agent types.
    // Each side is keyed by lowercased customer name; an invoice present in
    // both payloads is kept once so a customer never lists it twice.
    function mergeSystemDetails(a, b) {
        const merged = {};
        [a, b].forEach(map => {
            Object.keys(map || {}).forEach(key => {
                const list = merged[key] || (merged[key] = []);
                (map[key] || []).forEach(entry => {
                    const dup = entry && entry.invoice
                        && list.some(e => e.invoice === entry.invoice);
                    if (!dup) list.push(entry);
                });
            });
        });
        return merged;
    }

    function mergeCommissionPayloads(internalData, outsourceData) {
        const basicNfp = unionMergeSections(internalData?.sections?.basic_nfp, outsourceData?.sections?.basic_nfp);
        // unionMergeSections rebuilds the section as bare {headers, rows}, so
        // anything else it carried is lost unless re-attached. Without this the
        // Customer hover has no data under "All Agents", and the Balance Payout
        // filter silently falls back to the column that holds the 100% date.
        basicNfp.system_details = mergeSystemDetails(
            internalData?.sections?.basic_nfp?.system_details,
            outsourceData?.sections?.basic_nfp?.system_details
        );
        const agentSummaryMerged = unionMergeSections(internalData?.sections?.agent_summary, outsourceData?.sections?.agent_summary, agentSummaryHeaderAlias);
        const referralFeeByAgent = computeReferralFeeByAgent(basicNfp);
        return {
            summary: {
                total_agents: (parseInt(internalData?.summary?.total_agents, 10) || 0) + (parseInt(outsourceData?.summary?.total_agents, 10) || 0),
                total_customers: (parseInt(internalData?.summary?.total_customers, 10) || 0) + (parseInt(outsourceData?.summary?.total_customers, 10) || 0)
            },
            sections: {
                basic_nfp: basicNfp,
                anp: unionMergeSections(internalData?.sections?.anp, outsourceData?.sections?.anp),
                agent_summary: addReferralFeeColumn(agentSummaryMerged, referralFeeByAgent),
                ega_esa: mergeEgaEsaSections(internalData?.sections?.ega_esa, outsourceData?.sections?.ega_esa),
                production_bonus: mergeProductionBonusSections(internalData?.sections?.production_bonus, outsourceData?.sections?.production_bonus),
                monthly_contest: outsourceData?.sections?.monthly_contest || internalData?.sections?.monthly_contest
            },
            agentTypeMap: buildAgentTypeMap(internalData, outsourceData),
            // Both maps are identical on either response -- they are keyed by
            // agent and cover every type -- but have to be re-attached
            // explicitly, since this object is rebuilt from scratch and would
            // otherwise drop them. Losing agent_role_history here is invisible
            // except that every agent-name hover in the combined view reports
            // "no role", which is exactly what happened.
            agent_roles: internalData?.agent_roles || outsourceData?.agent_roles || null,
            agent_role_history: internalData?.agent_role_history || outsourceData?.agent_role_history || null
        };
    }

    async function fetchCommissionDataForSelection() {
        if (state.activeAgentType === "all") {
            const [internalData, outsourceData] = await Promise.all([
                fetchCommissionJson("internal"),
                fetchCommissionJson("outsource")
            ]);
            return mergeCommissionPayloads(internalData, outsourceData);
        }
        return fetchCommissionJson(state.activeAgentType);
    }

    async function fetchCommissionPayload({ showErrors = true, persistCache = true } = {}) {
        const requestToken = ++commissionFetchToken;
        try {
            const data = await fetchCommissionDataForSelection();
            if (requestToken !== commissionFetchToken) return;
            await applyCommissionPayload(data, { persistCache });
        } catch (err) {
            if (requestToken !== commissionFetchToken) return;
            if (showErrors) {
                console.error(err);
                alert(`Error: ${err.message}`);
            } else {
                console.warn("Background commission refresh failed:", err);
            }
        } finally {
            if (requestToken === commissionFetchToken && showErrors) {
                showLoader(false);
            }
        }
    }

    // -------------------------------------------------------------
    // Data Loading & API Calls
    // -------------------------------------------------------------
    async function fetchData() {
        commissionFetchToken += 1;
        const cachedPayload = loadCachedCommissionPayload();
        if (cachedPayload) {
            showLoader(false);
            noDataView.classList.add("hidden");
            await applyCommissionPayload(cachedPayload, { persistCache: false });
            // A cached payload carries the roles as they stood when it was
            // saved -- up to six hours ago, and from before any Data page edit
            // made since. Roles are cheap and are edited without rebuilding
            // commissions, so re-read them rather than showing a stale answer;
            // the full refresh below is silent, so it cannot be relied on to
            // correct this.
            void refreshAgentRoleMaps();
            if (!commissionRefreshInFlight) {
                commissionRefreshInFlight = true;
                void fetchCommissionPayload({ showErrors: false, persistCache: true }).finally(() => {
                    commissionRefreshInFlight = false;
                });
            }
            return;
        }

        showLoader(true);
        noDataView.classList.add("hidden");
        
        try {
            const data = await fetchCommissionDataForSelection();
            state.rawData = data;
            await loadFactoryRates();
            
            // Reconstruct Special Cases from backend-injected rows
            state.specialCaseRowRefs.clear();
            state.specialCasePairs = [];

            const nfpSection = data.sections ? data.sections.basic_nfp : null;
            if (nfpSection && nfpSection.rows && nfpSection.headers) {
                const headers = nfpSection.headers;
                const rows = nfpSection.rows;
                const scRows = [];
                rows.forEach(r => {
                    if (r.length > headers.length) {
                        const rawDataStr = r[r.length - 1];
                        try {
                            const scData = JSON.parse(rawDataStr);
                            if (scData && scData.agent && scData.customer) {
                                r.specialCaseData = scData;
                                state.specialCaseRowRefs.add(r);
                                scRows.push(r);
                            }
                        } catch (_) {}
                    }
                });
                
                const paired = new Set();
                for (let i = 0; i < scRows.length; i++) {
                    const row1 = scRows[i];
                    if (paired.has(row1)) continue;
                    for (let j = i + 1; j < scRows.length; j++) {
                        const row2 = scRows[j];
                        if (paired.has(row2)) continue;
                        
                        if (JSON.stringify(row1.specialCaseData) === JSON.stringify(row2.specialCaseData)) {
                            state.specialCasePairs.push([row1, row2]);
                            paired.add(row1);
                            paired.add(row2);
                            break;
                        }
                    }
                    // A case that replaced only one commission row has no
                    // partner. It still needs a group of its own, or it would be
                    // un-editable and un-deletable after a reload.
                    if (!paired.has(row1)) {
                        state.specialCasePairs.push([row1]);
                        paired.add(row1);
                    }
                }
            }

            // Populate KPI totals
            totalAgents.textContent = data.summary.total_agents;
            totalCustomers.textContent = data.summary.total_customers;
            saveCachedCommissionPayload(data);
            
            // Render active section
            renderActiveSection();
            
        } catch (err) {
            console.error(err);
            alert(`Error: ${err.message}`);
            showLoader(false);
        }
    }

    async function loadFactoryRates() {
        if (state.activeAgentType === "all") {
            state.factoryRates = [];
            return;
        }
        try {
            const res = await fetch(`/api/factory-rates?year=${state.activeYear}&month=${state.activeMonth}&agent_type=${state.activeAgentType}`);
            state.factoryRates = res.ok ? await res.json() : [];
            if (!Array.isArray(state.factoryRates)) state.factoryRates = [];
        } catch (err) {
            console.error("Error loading profit sharing rates:", err);
            state.factoryRates = [];
        }
    }

    function setSpecialCaseModalMode(mode) {
        state.specialCaseMode = mode;
        const isCustomerMode = mode === "customer";
        const modalAgentCustomerDisplayRow = document.getElementById("modalAgentCustomerDisplayRow");
        if (modalSelectionGroupRow) modalSelectionGroupRow.classList.toggle("hidden", !isCustomerMode);
        if (modalAgentCustomerDisplayRow) modalAgentCustomerDisplayRow.classList.toggle("hidden", isCustomerMode);
        if (modalProfitSharingRow) modalProfitSharingRow.classList.remove("hidden");
        if (previewProfitSharingRow) previewProfitSharingRow.classList.remove("hidden");
        if (!isCustomerMode) {
            state.selectedSpecialCaseAgent = "";
            state.selectedSpecialCaseCustomer = "";
        }
        if (modalAgentName) modalAgentName.disabled = true;
        if (modalCustomerName) modalCustomerName.disabled = true;
        if (modalSalesPrice) modalSalesPrice.disabled = true;
        if (modalPackageType) modalPackageType.disabled = true;
        if (modalSystemPrice) modalSystemPrice.disabled = true;
        if (modalBaselineRate) modalBaselineRate.disabled = true;
        if (modalOrigBasicComm) modalOrigBasicComm.disabled = true;
        if (modalOrigNfpComm) modalOrigNfpComm.disabled = true;
    }

    function getDefaultBasicRateForPackage(pkg, agentName = "") {
        const packageName = String(pkg || "").toLowerCase();
        if (packageName.includes("factory")) {
            return state.activeAgentType === "internal" ? 2.0 : 2.5;
        }
        const tier = getAgentTier(agentName);
        if (tier) {
            const tierRate = getBasicRateForTier(tier);
            if (Number.isFinite(tierRate)) {
                return tierRate;
            }
        }
        return state.activeAgentType === "internal" ? 3.0 : 4.5;
    }

    function findCurrentBasicNfpRowByCustomer(customerName, agentName = "") {
        const section = state.rawData?.sections?.basic_nfp;
        if (!section || !Array.isArray(section.rows) || !Array.isArray(section.headers)) return null;
        const headers = section.headers;
        const rows = section.rows;
        const agentIdx = headers.findIndex(h => h.toLowerCase().trim() === "agent");
        const customerIdx = headers.findIndex(h => h.toLowerCase().trim() === "customer");
        const commissionIdx = headers.findIndex(h => h.toLowerCase().trim() === "commission");

        let currentAgent = "";
        for (const row of rows) {
            if (state.specialCaseRowRefs.has(row)) continue;
            if (agentIdx !== -1 && row[agentIdx] && String(row[agentIdx]).trim() !== "") {
                currentAgent = String(row[agentIdx]).trim();
            }
            if (agentName && currentAgent.toLowerCase() !== agentName.toLowerCase()) continue;
            const custVal = customerIdx !== -1 && row[customerIdx] ? String(row[customerIdx]).trim() : "";
            if (custVal.toLowerCase() === customerName.toLowerCase()) {
                const commType = commissionIdx !== -1 ? String(row[commissionIdx] || "").toLowerCase() : "";
                if (commType.includes("basic") || commType === "") {
                    return { row, agent: currentAgent, customer: custVal };
                }
            }
        }
        currentAgent = "";
        for (const row of rows) {
            if (state.specialCaseRowRefs.has(row)) continue;
            if (agentIdx !== -1 && row[agentIdx] && String(row[agentIdx]).trim() !== "") {
                currentAgent = String(row[agentIdx]).trim();
            }
            if (agentName && currentAgent.toLowerCase() !== agentName.toLowerCase()) continue;
            const custVal = customerIdx !== -1 && row[customerIdx] ? String(row[customerIdx]).trim() : "";
            if (custVal.toLowerCase() === customerName.toLowerCase()) {
                return { row, agent: currentAgent, customer: custVal };
            }
        }
        return null;
    }

    function getCustomerSpecialCaseDefaults(customerName) {
        const found = findCurrentBasicNfpRowByCustomer(customerName, state.selectedSpecialCaseAgent);
        if (!found) return null;

        const headers = state.rawData.sections.basic_nfp.headers;
        const packageIdx = headers.findIndex(h => h.toLowerCase().trim() === "package type" || h.toLowerCase().trim() === "package");
        const systemIdx = headers.findIndex(h => h.toLowerCase().trim() === "system price");
        const nfpIdx = headers.findIndex(h => h.toLowerCase().trim() === "net floor price" || h.toLowerCase().trim() === "netfloor price");
        const salesIdx = headers.findIndex(h => h.toLowerCase().trim() === "sales price" || h.toLowerCase().trim() === "total amount");
        const pkg = packageIdx !== -1 ? String(found.row[packageIdx] || "").trim() : "-";

        return {
            agent: found.agent || "",
            customer: found.customer || customerName,
            pkg,
            system: systemIdx !== -1 ? parseMoneyValue(found.row[systemIdx]) : 0,
            nfp: nfpIdx !== -1 ? parseMoneyValue(found.row[nfpIdx]) : 0,
            sales: salesIdx !== -1 ? parseMoneyValue(found.row[salesIdx]) : 0,
            rate: getDefaultBasicRateForPackage(pkg, found.agent)
        };
    }

    async function searchCustomers(query) {
        const res = await fetch(`/api/customers/search?q=${encodeURIComponent(query || "")}&limit=20`);
        if (!res.ok) {
            throw new Error("Failed to load customers");
        }
        const data = await res.json();
        return Array.isArray(data) ? data : [];
    }

    async function searchAgents(query) {
        const res = await fetch(`/api/agents/search?q=${encodeURIComponent(query || "")}&agent_type=${encodeURIComponent(state.activeAgentType)}&year=${encodeURIComponent(state.activeYear)}&month=${encodeURIComponent(state.activeMonth)}&limit=20`);
        if (!res.ok) {
            throw new Error("Failed to load agents");
        }
        const data = await res.json();
        return Array.isArray(data) ? data : [];
    }

    function renderAgentSearchResults(agents) {
        if (!agentSearchResults) return;
        agentSearchResults.innerHTML = "";
        if (!agents.length) {
            agentSearchResults.classList.add("hidden");
            return;
        }
        agents.forEach(agent => {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "customer-search-result";
            btn.textContent = agent.name;
            btn.addEventListener("click", () => {
                selectAgentForSpecialCase(agent.name);
            });
            agentSearchResults.appendChild(btn);
        });
        agentSearchResults.classList.remove("hidden");
    }

    function renderCustomerSearchResults(customers) {
        if (!customerSearchResults) return;
        customerSearchResults.innerHTML = "";
        if (!customers.length) {
            customerSearchResults.classList.add("hidden");
            return;
        }

        customers.forEach(cust => {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "customer-search-result";
            btn.textContent = cust.name;
            btn.addEventListener("click", () => {
                selectCustomerForSpecialCase(cust.name);
            });
            customerSearchResults.appendChild(btn);
        });
        customerSearchResults.classList.remove("hidden");
    }

    function onSpecialCaseTypeChange() {
        const type = modalCaseType ? modalCaseType.value : "adjusted_nfp";
        if (rowAdjustedNfp) rowAdjustedNfp.classList.toggle("hidden", type !== "adjusted_nfp");
        if (rowFeeWaiver) rowFeeWaiver.classList.toggle("hidden", type !== "fee_waiver");
        if (rowAdjustedRate) rowAdjustedRate.classList.toggle("hidden", type !== "adjusted_rate");
        if (rowAdjustedSalesPrice) rowAdjustedSalesPrice.classList.toggle("hidden", type !== "adjusted_sales_price");
        if (rowRevisedSalesPrice) rowRevisedSalesPrice.classList.toggle("hidden", type !== "adjusted_sales_price");
        
        const pkgName = String(modalPackageType?.value || "").toLowerCase();
        const isSpecialPkg = pkgName.includes("factory") || pkgName.includes("ngo") || pkgName.includes("goverment") || pkgName.includes("government");
        const showProfitSharing = type === "profit_sharing" || isSpecialPkg;
        
        if (rowProfitSharing) rowProfitSharing.classList.toggle("hidden", !showProfitSharing);
        if (previewProfitSharingRow) previewProfitSharingRow.classList.toggle("hidden", !showProfitSharing);
        if (rowWaiverSummary) rowWaiverSummary.classList.toggle("hidden", type !== "fee_waiver");

        updateSpecialCasePreview();
    }

    // Gan Lai Soon's OGM override, mirroring _inject_special_case_rows() in
    // app.py so a saved case can never render differently from its preview.
    // He takes a cut of the sales price on outsource invoices only, and never
    // on his own. The rate is negotiated per agent, so a case may carry its
    // own; blank falls back to the standard 0.75%.
    const DEFAULT_GAN_OVERRIDE_PCT = 0.75;

    // Where the modal's agent actually lives, matching how confirmModalBtn
    // resolves it at save time. In standard mode it is only ever in the
    // modalAgentName field: state.selectedSpecialCaseAgent is cleared when the
    // modal opens and set again only in customer mode or when editing, so
    // reading state alone left this blank and hid the override field.
    function currentSpecialCaseAgent() {
        return state.specialCaseMode === "customer"
            ? (state.selectedSpecialCaseAgent || (modalAgentName ? modalAgentName.value : ""))
            : ((modalAgentName ? modalAgentName.value : "") || state.selectedSpecialCaseAgent);
    }

    // The agent-type bucket a case belongs to. "all" is a view, not a bucket:
    // the report is rebuilt one agent type at a time (_inject_special_case_rows
    // is only ever called with "internal" or "outsource"), so anything filed
    // under "all" is written once and then never read back. Resolve it to the
    // agent's own type instead. Returns "" when the agent is unknown to the
    // merged payload, which callers must treat as "cannot file this".
    function agentTypeBucketFor(agentName) {
        if (state.activeAgentType !== "all") return state.activeAgentType;
        const agent = String(agentName || "").trim().toLowerCase();
        return String(state.rawData?.agentTypeMap?.[agent] || "");
    }

    function ganOverrideApplies(agentName) {
        const agent = String(agentName || "").trim();
        if (!agent) return false;
        return agentTypeBucketFor(agent) === "outsource" && getOutsourceAgentTier(agent) !== "OGM";
    }

    function ganOverridePctFor(rawValue) {
        const pct = parseFloat(rawValue);
        // 0 is a deliberate "no override"; only blank/garbage means "use default".
        return isNaN(pct) ? DEFAULT_GAN_OVERRIDE_PCT : pct;
    }

    // Reduce the modal to the one thing the Gan Lai Soon column is about.
    //
    // Runs at the end of updateSpecialCasePreview rather than in
    // onSpecialCaseTypeChange, because that function decides row visibility
    // from the selected case type and would put these back on the next
    // keystroke. Last writer wins, so this has to be last.
    function applyGanModeVisibility() {
        const on = !!state.specialCaseGanMode;
        // Left as-is when off: the type selector drives them normally.
        if (!on) return;
        [rowCaseType, rowAdjustedNfp, rowFeeWaiver, rowAdjustedRate,
         rowProfitSharing, rowAdjustedSalesPrice, rowRevisedSalesPrice,
         rowWaiverSummary, previewProfitSharingRow].forEach(el => {
            if (el) el.classList.add("hidden");
        });
    }

    function updateSpecialCasePreview() {
        const sales = parseFloat(modalSalesPrice?.value) || 0;
        const baselineRate = parseFloat(modalBaselineRate?.value) || parseFloat(modalRatePct?.value) || 3.0;
        const type = modalCaseType ? modalCaseType.value : "adjusted_nfp";

        // Management's manually adjusted sales price replaces the auto-calculated
        // one for every downstream calc (basic commission AND the NFP bonus/
        // clawback, since that also compares sales price against the floor).
        let salesForCalc = sales;
        if (type === "adjusted_sales_price") {
            const adj = parseFloat(modalAdjustedSalesPrice?.value);
            if (!isNaN(adj)) salesForCalc = adj;
        }

        let rate = baselineRate;
        if (type === "adjusted_rate") {
            rate = parseFloat(modalRatePct?.value) || baselineRate;
        }

        let profitSharingPct = 0;
        const pkgName = String(modalPackageType?.value || "").toLowerCase();
        const isSpecialPkg = pkgName.includes("factory") || pkgName.includes("ngo") || pkgName.includes("goverment") || pkgName.includes("government");
        if (type === "profit_sharing" || isSpecialPkg) {
            profitSharingPct = parseFloat(modalProfitSharingPct?.value) || 0;
        }

        const basicComm = salesForCalc * ((rate + profitSharingPct) / 100);
        const profitSharingComm = salesForCalc * (profitSharingPct / 100);

        let nfp = parseFloat(modalNetFloorPrice?.value) || 0;
        let feeWaiver = 0;
        if (type === "fee_waiver") {
            feeWaiver = parseFloat(modalFeeWaiver?.value) || 0;
        }

        let nfpA = 0, nfpC = 0;
        if (salesForCalc > nfp) nfpA = (salesForCalc - nfp) * 0.25;
        if (salesForCalc < nfp) nfpC = (nfp - salesForCalc) * 0.20;
        let nfpComm = nfpA - nfpC + feeWaiver;

        if (previewRevisedSalesPrice) previewRevisedSalesPrice.textContent = formatRM(salesForCalc);
        if (previewBasicComm) previewBasicComm.textContent = formatRM(basicComm);
        if (previewNfpComm) previewNfpComm.textContent = nfp === 0 ? "TBC with Finance" : formatRM(nfpComm);
        if (previewProfitSharingComm) previewProfitSharingComm.textContent = formatRM(profitSharingComm);
        if (previewWaiverSummary) previewWaiverSummary.textContent = `+${formatRM(feeWaiver)}`;

        // Shown for information only: this is Gan Lai Soon's money, so it stays
        // out of the agent's Total Net Commission below.
        const ganApplies = ganOverrideApplies(currentSpecialCaseAgent());
        if (rowGanOverride) rowGanOverride.classList.toggle("hidden", !ganApplies);
        if (previewGanOverrideRow) previewGanOverrideRow.classList.toggle("hidden", !ganApplies);
        // Opened by clicking his column: put the caret on the rate that cell
        // shows, so the field is not just visible but ready to type into. The
        // flag is consumed once, or every later preview would steal focus.
        if (ganApplies && state.focusGanOverride && modalGanOverridePct) {
            state.focusGanOverride = false;
            modalGanOverridePct.focus();
            modalGanOverridePct.select();
        }
        if (ganApplies) {
            const ganPct = ganOverridePctFor(modalGanOverridePct?.value);
            if (previewGanOverrideLabel) {
                previewGanOverrideLabel.textContent = `Gan Lai Soon Override (${ganPct}%):`;
            }
            if (previewGanOverrideComm) {
                previewGanOverrideComm.textContent = formatRM(salesForCalc * (ganPct / 100));
            }
        }

        const totalNetComm = basicComm + (nfp === 0 ? 0 : nfpComm);
        if (previewTotalNetComm) previewTotalNetComm.textContent = formatRM(totalNetComm);

        applyGanModeVisibility();
    }

    function selectCustomerForSpecialCase(customerName) {
        state.selectedSpecialCaseCustomer = customerName;
        const defaults = getCustomerSpecialCaseDefaults(customerName);
        if (modalCustomerSearch) modalCustomerSearch.value = customerName;
        if (customerSearchResults) {
            customerSearchResults.innerHTML = "";
            customerSearchResults.classList.add("hidden");
        }

        modalCustomerName.value = customerName;
        if (defaults) {
            modalAgentName.value = defaults.agent;
            state.selectedSpecialCaseAgent = defaults.agent;
            if (modalAgentSearch) modalAgentSearch.value = defaults.agent;
            modalCustomerName.value = defaults.customer;
            modalPackageType.value = defaults.pkg || "-";
            modalSystemPrice.value = defaults.system;
            modalNetFloorPrice.value = defaults.nfp;
            modalSalesPrice.value = defaults.sales;
            modalRatePct.value = defaults.rate;
        } else {
            modalAgentName.value = state.selectedSpecialCaseAgent || "";
            modalPackageType.value = "-";
            modalSystemPrice.value = 0;
            modalNetFloorPrice.value = 0;
            modalSalesPrice.value = 0;
            modalRatePct.value = getDefaultBasicRateForPackage("", state.selectedSpecialCaseAgent);
        }

        if (modalProfitSharingPct && !modalProfitSharingPct.value) {
            modalProfitSharingPct.value = "0";
        }
        updateSpecialCasePreview();
    }

    function selectAgentForSpecialCase(agentName) {
        state.selectedSpecialCaseAgent = agentName;
        if (modalAgentSearch) modalAgentSearch.value = agentName;
        if (modalAgentName) modalAgentName.value = agentName;
        // Whether Gan Lai Soon's override applies depends on who the agent is,
        // so the preview has to re-run when that changes.
        updateSpecialCasePreview();
        if (agentSearchResults) {
            agentSearchResults.innerHTML = "";
            agentSearchResults.classList.add("hidden");
        }

        if (state.selectedSpecialCaseCustomer) {
            const defaults = getCustomerSpecialCaseDefaults(state.selectedSpecialCaseCustomer);
            if (defaults) {
                modalCustomerName.value = defaults.customer;
modalPackageType.value = defaults.pkg || "-";
                modalSystemPrice.value = defaults.system;
                modalNetFloorPrice.value = defaults.nfp;
                modalSalesPrice.value = defaults.sales;
                modalRatePct.value = defaults.rate;
            } else {
                modalPackageType.value = "-";
                modalSystemPrice.value = 0;
                modalNetFloorPrice.value = 0;
                modalSalesPrice.value = 0;
            }
            updateSpecialCasePreview();
        }
    }

    async function saveFactoryRates() {
        try {
            const res = await fetch("/api/factory-rates", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    year: state.activeYear,
                    month: state.activeMonth,
                    agent_type: state.activeAgentType,
                    factory_rates: state.factoryRates
                })
            });
            if (!res.ok) console.error("Failed to save profit sharing rates to server");
        } catch (err) {
            console.error("Error saving profit sharing rates:", err);
        }
    }

    async function showPdfModal() {
        const modal = document.getElementById("pdfModal");
        const viewer = document.getElementById("pdfViewer");
        const overlay = document.getElementById("pdfLoadingOverlay");

        // Open the popup and show the spinner immediately, instead of making
        // the user stare at the dashboard while the PDF builds server-side.
        viewer.style.visibility = "hidden";
        overlay.classList.remove("hidden");
        modal.style.display = "flex";

        try {
            const pdfUrl = `/api/download/pdf?year=${state.activeYear}&month=${state.activeMonth}`;
            const response = await fetch(pdfUrl);

            if (!response.ok) {
                alert("Failed to load PDF");
                closePdfModal();
                return;
            }

            const blob = await response.blob();
            const blobUrl = URL.createObjectURL(blob);
            viewer.src = blobUrl;
            viewer.style.visibility = "visible";
            overlay.classList.add("hidden");

            // Store the blob URL for cleanup later
            modal.dataset.blobUrl = blobUrl;
        } catch (error) {
            console.error("Error loading PDF:", error);
            alert("Error loading PDF: " + error.message);
            closePdfModal();
        }
    }

    function closePdfModal() {
        const modal = document.getElementById("pdfModal");
        const viewer = document.getElementById("pdfViewer");
        const overlay = document.getElementById("pdfLoadingOverlay");

        // Clean up blob URL
        if (modal.dataset.blobUrl) {
            URL.revokeObjectURL(modal.dataset.blobUrl);
            modal.dataset.blobUrl = "";
        }

        modal.style.display = "none";
        viewer.src = "";
        overlay.classList.add("hidden");
    }

    function triggerDownload(format) {
        window.location.href = `/api/download/${format}?year=${state.activeYear}&month=${state.activeMonth}`;
    }

    // ------------------------------------------------------------------
    // Filtering helpers
    // ------------------------------------------------------------------
    function matchesSearch(agentName, customerName) {
        const query = state.filters.search;
        if (!query) return true;
        return String(agentName || "").toLowerCase().includes(query) ||
               String(customerName || "").toLowerCase().includes(query);
    }

    function getMonthRateKey() {
        return MONTH_KEY_MAP[parseInt(state.activeMonth, 10)] || "mac";
    }

    function getBasicRateForTier(tier) {
        const agentType = state.activeAgentType;
        const monthKey = getMonthRateKey();
        const monthOrder = ["jan", "feb", "mac", "jun", "jul"];
        const tierRates = (TIER_RATE_TABLE[agentType] || {})[tier] || {};
        if (tierRates[monthKey] != null) return tierRates[monthKey];
        if (agentType === "internal" && tier === "Senior") {
            const execRate = getBasicRateForTier("Executive");
            return execRate != null ? execRate + 0.25 : 3.25;
        }
        const targetIdx = monthOrder.indexOf(monthKey);
        for (let idx = targetIdx; idx >= 0; idx--) {
            const rate = tierRates[monthOrder[idx]];
            if (rate != null) return rate;
        }
        if (agentType === "internal") return tier === "Senior" ? 3.25 : 3;
        return tier === "OGM" ? 5 : 4.5;
    }

    // ------------------------------------------------------------------
    // Rate Cards
    // ------------------------------------------------------------------
    function renderRateCards(visibleAgents) {
        if (!rateCardsContainer) return;
        if (state.activeAgentType === "all") {
            rateCardsContainer.innerHTML = "";
            rateCardsContainer.classList.add("hidden");
            return;
        }

        const isOutsource = state.activeAgentType === "outsource";
        const allTiers = isOutsource ? OUTSOURCE_TIER_ORDER : INTERNAL_TIER_ORDER;
        const searchActive = state.filters.search !== "";
        const tiersPresent = new Set();
        visibleAgents.forEach(ag => { const t = getAgentTier(ag); if (t) tiersPresent.add(t); });
        const tiersToShow = searchActive ? allTiers.filter(t => tiersPresent.has(t)) : allTiers;
        rateCardsContainer.innerHTML = "";
        rateCardsContainer.classList.toggle("hidden", tiersToShow.length === 0);

        // Helper: make a standard rate sub-box (Residential / Factory)
        function makeSubBox(label, value, isSpecial) {
            const color = isSpecial ? "var(--accent-green,#16a34a)" : "var(--accent-blue,#2563eb)";
            return `<div style="display:flex;align-items:baseline;gap:8px;padding:2px 0">
                <div style="font-size:18px;font-weight:700;color:${color};width:95px;flex-shrink:0;white-space:nowrap">${value}</div>
                <div style="font-size:14px;color:var(--text-secondary);font-weight:500">${label}</div>
            </div>`;
        }

        // Helper: make a small info/override card
        function makeInfoCard(title, lines) {
            const lineHtml = lines.map(l =>
                `<div style="font-size:13px;color:var(--text-secondary);margin-top:2px">${l}</div>`
            ).join("");
            return `<div class="card metric-card rate-card" style="flex-direction:column;align-items:flex-start;gap:6px;min-width:140px;max-width:220px;background:var(--bg-secondary,#f8fafc);border:1.5px solid var(--border-color,#e2e8f0)">
                <div style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:var(--text-muted,#94a3b8)">${title}</div>
                ${lineHtml}
            </div>`;
        }

        tiersToShow.forEach(tier => {
            const resRate = getBasicRateForTier(tier);
            const facRates = isOutsource ? FACTORY_RATE_TABLE.outsource[tier] : FACTORY_RATE_TABLE.internal[tier];
            const facRate = facRates ? facRates.res : null;

            // --- Main tier card ---
            const card = document.createElement("div");
            card.className = "card metric-card rate-card";
            card.style.cssText = "flex-direction:column;align-items:flex-start;gap:8px";
            
            let innerHtml = `<div class="metric-details" style="width:100%"><span class="metric-label">${tier} Rate %</span></div>
            <div style="display:flex;flex-direction:column;gap:4px;width:100%">
                ${makeSubBox("", resRate.toFixed(2) + "%", false)}
                ${facRate !== null ? makeSubBox("Factory", facRate.toFixed(2) + "% + x", false) : ""}`;
            if (!isOutsource && tier === "Senior") {
                innerHtml += `\n                ${makeSubBox("Override Rate%", "+0.25%", false)}`;
            }
            if (isOutsource && tier === "OUM") {
                innerHtml += `\n                ${makeSubBox("Override Rate %", "+0.5%", false)}`;
            }
            
            innerHtml += `\n            </div>`;
            card.innerHTML = innerHtml;
            rateCardsContainer.appendChild(card);
        });

        if (isOutsource) {
            const ganCard = document.createElement("div");
            ganCard.className = "card metric-card rate-card";
            ganCard.style.cssText = "flex-direction:column;align-items:flex-start;gap:8px";
            ganCard.innerHTML = `<div class="metric-details" style="width:100%"><span class="metric-label">Gan Lai Soon Rate %</span></div>
            <div style="display:flex;flex-direction:column;gap:4px;width:100%">
                ${makeSubBox("", "0.75%", false)}
            </div>`;
            rateCardsContainer.appendChild(ganCard);
        }

        // Referral Rate card (rendered once at the end)
        if (tiersToShow.length > 0) {
            const refCard = document.createElement("div");
            refCard.className = "card metric-card rate-card";
            refCard.style.cssText = "flex-direction:column;align-items:flex-start;gap:8px";
            refCard.innerHTML = `<div class="metric-details" style="width:100%"><span class="metric-label">Referral Rate %</span></div>
            <div style="display:flex;flex-direction:column;gap:4px;width:100%">
                ${makeSubBox("", "2.00%", false)}
            </div>`;
            rateCardsContainer.appendChild(refCard);
        }
    }

    // ------------------------------------------------------------------
    // Basic & NFP Commission page: Total Commission / Other Commission / Referral Fee cards
    // ------------------------------------------------------------------
    // Sums one money column of agent_summary, counting each agent's block once
    // (skips Total/Summary/Grand rows the same way basic_nfp rows are skipped).
    function sumAgentSummaryColumn(agSummarySection, columnLabel) {
        if (!agSummarySection || !agSummarySection.headers || !agSummarySection.rows) return 0;
        const headers = agSummarySection.headers;
        const agentIdx = headers.findIndex(h => h.toLowerCase().trim() === "agent");
        const colIdx = headers.findIndex(h => h.toLowerCase().trim() === columnLabel);
        if (colIdx === -1) return 0;
        let cur = "", total = 0;
        agSummarySection.rows.forEach(r => {
            const v = agentIdx !== -1 && r[agentIdx] ? String(r[agentIdx]).trim() : "";
            if (v) cur = v;
            const nameLower = cur.toLowerCase();
            if (!cur || nameLower.includes("total") || nameLower.includes("summary") || nameLower.includes("grand")) return;
            total += parseMoneyValue(r[colIdx]);
        });
        return total;
    }

    // A special case does not edit the invoice's original rows — it appends a
    // "New Basic Commission" / "New Net Floor Price Commission" pair alongside
    // them. Summing the column blind therefore counts that invoice twice, at the
    // old rate and the new one. The new figures supersede the old, so the
    // originals for that agent+customer are dropped from every total.
    function isSupersededOriginalRow(p, commIdx) {
        if (p.isSpecialCase || commIdx === -1) return false;
        const commType = String(p.rawRow[commIdx] || "").toLowerCase();
        if (commType.includes("new ")) return false;   // already a replacement
        // Only the two commission rows are replaced; anything else the invoice
        // carries (referral, other commission) still stands on its own.
        return commType.includes("basic") || commType.includes("net floor") || commType.includes("netfloor");
    }

    // Payout Stage classification, shared by the detail table and the total
    // cards so the two can never disagree about which rows are in a stage.
    function payoutStageIndexes(headers) {
        return {
            rm300: headers.findIndex(h => h.toLowerCase().includes("rm300") || h.toLowerCase().includes("basic commission (rm")),
            pct75: headers.findIndex(h => h.toLowerCase().includes("75%") || h.toLowerCase().includes("75 %")),
            comm: headers.findIndex(h => {
                const n = String(h).toLowerCase().trim();
                return n === "commission" || n === "commission type";
            }),
            price: headers.findIndex(h => h.toLowerCase().trim() === "commission price"),
            customer: headers.findIndex(h => h.toLowerCase().trim() === "customer")
        };
    }

    // The real 75% milestone for a customer's invoice. The "75% Payment Date"
    // column cannot supply it: that column is filled from the invoice's
    // full_payment_date (the 100% date), so an invoice that reached 75% but not
    // 100% reads "pending" there and would drop out of the balance run
    // entirely. Served by app.py's system_details, matched on customer name.
    function realPct75Date(customerName) {
        const details = state.rawData?.sections?.basic_nfp?.system_details;
        const key = String(customerName || "").trim().toLowerCase();
        if (!details || !key) return "";
        const entries = details[key];
        return entries && entries.length ? String(entries[0].pct75_date || "") : "";
    }

    // The 75% milestone must land in the month being reported. An invoice that
    // reached 5% this month but whose balance is due in a later month is still
    // only paying its advance today, so it is not a balance payout yet.
    function milestoneLandsInActiveMonth(dateText) {
        const m = /^(\d{4})-(\d{2})-\d{2}$/.exec(String(dateText || "").trim());
        if (!m) return false;
        return parseInt(m[1], 10) === parseInt(state.activeYear, 10)
            && parseInt(m[2], 10) === parseInt(state.activeMonth, 10);
    }

    function rowPassesPayoutStage(rawRow, idx, stage) {
        if (idx.rm300 === -1) return true;
        const rm300v = String(rawRow[idx.rm300] || "").trim().toLowerCase();
        if (stage === "advance") {
            // Pre-July invoices predate the two-stage policy and never receive
            // an advance -- they pay in full at their milestone instead.
            return rm300v !== "" && rm300v !== "-" && rm300v !== "pending" && rm300v !== "invoice before july";
        }
        // Both commission kinds settle at the 75% milestone, so both belong in
        // the balance run: Basic pays whatever the advance did not cover, and
        // NFP (never advanced at all) pays in full -- possibly as a deduction.
        const kind = idx.comm !== -1 ? String(rawRow[idx.comm] || "").trim().toLowerCase() : "";
        const isCommissionRow = !kind || kind.includes("basic")
            || kind.includes("net floor") || kind.includes("netfloor");
        if (!isCommissionRow) return false;
        // "pending" means the 5% milestone has not triggered, so nothing is
        // payable yet. Pre-July invoices DO belong here: they never received an
        // advance, so their full commission is settled at the 75% milestone.
        if (rm300v === "pending") return false;
        if (idx.pct75 === -1) return false;
        // A Basic Commission row of a July 2026+ invoice settles at 75%, so it
        // is tested against the real milestone rather than the column (which
        // holds the 100% date and would hide an invoice sitting between the
        // two). Everything else is correctly served by the column: a pre-July
        // invoice settles at 100%, and so does NFP, which is never advanced.
        let milestoneDate = rawRow[idx.pct75];
        if (kind.includes("basic") && rm300v !== "invoice before july") {
            const real = idx.customer === -1 ? "" : realPct75Date(rawRow[idx.customer]);
            if (real) milestoneDate = real;
        }
        if (!milestoneLandsInActiveMonth(milestoneDate)) return false;
        const price = idx.price !== -1 ? String(rawRow[idx.price] || "").trim().toLowerCase() : "";
        return price !== "" && price !== "-" && !price.includes("pending");
    }

    // Under "Advance RM 300" a Basic Commission row owes only its advance, not
    // the invoice's full commission -- the two differ when an invoice clears
    // both milestones in one month. The numeric twin of advanceOnlyPrice (which
    // works on cell text so the table keeps the payload's formatting).
    function advanceCappedValue(rawRow, idx, fullValue) {
        if (idx.comm !== -1 && !String(rawRow[idx.comm] || "").toLowerCase().includes("basic")) return fullValue;
        const advance = idx.rm300 === -1 ? 0 : parseMoneyValue(rawRow[idx.rm300]);
        if (!advance || !fullValue || advance >= fullValue) return fullValue;
        return advance;
    }

    // The mirror of advanceCappedValue: what the balance run still owes once the
    // advance is taken off. It only bites when an invoice clears BOTH milestones
    // in the same month -- that is the only case where the row carries a live
    // RM300 cell and also qualifies for the balance, and paying the full
    // commission there would hand over the RM 300 advance a second time. When
    // the advance went out in an earlier month the cell reads "-", and a
    // pre-July invoice reads "invoice before july"; both parse to 0, leaving
    // the full amount alone.
    function balanceRemainderValue(rawRow, idx, fullValue) {
        if (idx.comm !== -1 && !String(rawRow[idx.comm] || "").toLowerCase().includes("basic")) return fullValue;
        const advance = idx.rm300 === -1 ? 0 : parseMoneyValue(rawRow[idx.rm300]);
        if (!advance || !fullValue || advance >= fullValue) return fullValue;
        return fullValue - advance;
    }

    // A special case appends "New ..." rows next to the invoice's originals, so
    // summing a column blind counts that invoice at both the old rate and the
    // new one. Returns a predicate spotting the originals that were superseded.
    function makeIsReplacedPredicate(processed, commIdx) {
        const supersededKeys = new Set();
        (processed || []).forEach(p => {
            if (p.isSpecialCase && !p.isTotalRow) {
                supersededKeys.add(`${String(p.fullAgentName).toLowerCase()}||${String(p.customerName).toLowerCase()}`);
            }
        });
        if (supersededKeys.size === 0) return () => false;
        return (p) => supersededKeys.has(`${String(p.fullAgentName).toLowerCase()}||${String(p.customerName).toLowerCase()}`)
            && isSupersededOriginalRow(p, commIdx);
    }

    // Override money always sits on the DOWNLINE agent's detail row while
    // belonging to their upline, and the detail table spells the recipient two
    // different ways. This one is the cell text: "RM 85.00 (Sunny Tan)" is
    // Sunny Tan's, which the rollup states as "RM 85.00 override from
    // Zulkarnain" on Sunny Tan's own row. Credits each amount to the agent
    // named in trailing parentheses, falling back to the row's agent.
    function creditOtherCommissionCell(cellText, rowAgentKey, credit) {
        const text = String(cellText || "").trim();
        if (!text || text === "-") return;
        text.split(/<br\s*\/?>/i).forEach(part => {
            const amount = parseMoneyValue(part);
            if (!amount) return;
            const named = /\(([^()]+)\)\s*$/.exec(part.trim());
            credit(named ? named[1].trim().toLowerCase() : rowAgentKey, amount);
        });
    }

    // The other spelling: an OGM's override is a whole column named after them
    // ("Gan Lai Soon") on every other agent's row, and the money is the OGM's
    // own Other Commission -- again how the rollup already reports it. Matching
    // against agents that actually have a summary row keeps a name deliberately
    // kept out of these reports (Safwan) from being resurrected by its column.
    function overrideColumnsByAgent(headers) {
        const summaryAgents = new Set();
        ((state.rawData?.sections?.agent_summary?.rows) || []).forEach(r => {
            const v = String((r || [])[0] || "").trim().toLowerCase();
            if (v) summaryAgents.add(v);
        });
        const found = [];
        (headers || []).forEach((h, i) => {
            const name = String(h || "").toLowerCase().replace(/\(\s*rm\s*\)/g, "").trim();
            if (name && summaryAgents.has(name)) found.push({ idx: i, agent: name });
        });
        return found;
    }

    // Per-agent figures for the detail rows a filter left visible. The Summary
    // Agent Commission rollup is a whole-month figure built server side, so
    // under the search box or a Payout Stage it has to be recomputed from the
    // surviving rows or it contradicts the table beneath it. Keyed by
    // lowercased agent name.
    function buildFilteredAgentStats(visibleProcessed, creditProcessed, headers) {
        const idx = payoutStageIndexes(headers);
        const otherIdx = headers.findIndex(h => h.toLowerCase().trim() === "override");
        const overrideCols = overrideColumnsByAgent(headers);
        const isReplaced = makeIsReplacedPredicate(visibleProcessed, idx.comm);
        const stats = new Map();
        const entryFor = (key) => {
            if (!stats.has(key)) {
                stats.set(key, { commission: 0, basic: 0, nfp: 0, other: 0, otherBy: new Map(),
                                 customers: new Set(), ownRows: 0 });
            }
            return stats.get(key);
        };

        // Commission and customers come from what the filters left on screen.
        (visibleProcessed || []).forEach(p => {
            if (p.isTotalRow) return;
            const entry = entryFor(String(p.fullAgentName).toLowerCase().trim());
            entry.ownRows++;

            // Counted per distinct customer, not per row: an invoice carries a
            // Basic and an NFP row, and both stages can surface both.
            const cust = String(p.customerName || "").trim();
            const custLower = cust.toLowerCase();
            if (cust && cust !== "-" && !custLower.includes("total") && !custLower.includes("summary")) {
                entry.customers.add(custLower);
            }

            if (isReplaced(p) || idx.price === -1) return;
            let val = parseMoneyValue(p.rawRow[idx.price]);
            if (state.filters.payoutStage === "advance") val = advanceCappedValue(p.rawRow, idx, val);
            else if (state.filters.payoutStage === "balance") val = balanceRemainderValue(p.rawRow, idx, val);
            entry.commission += val;
            // Split the same way the hover breakdown reports it.
            const kind = idx.comm === -1 ? "" : String(p.rawRow[idx.comm] || "").toLowerCase();
            if (kind.includes("basic")) entry.basic += val;
            else if (kind.includes("net floor") || kind.includes("netfloor")) entry.nfp += val;
        });

        // Override credits are read from a wider set of rows -- the recipient is
        // normally not the agent whose row carries the money, so the search must
        // not hide it -- and both spellings credit the RECIPIENT, never the row.
        (creditProcessed || []).forEach(p => {
            if (p.isTotalRow) return;
            // Both spellings credit the recipient, and in both the money was
            // earned on THIS row -- so the row's own agent is the source the
            // rollup names in "RM x override from <source>".
            const sourceName = resolveAgentName(p.fullAgentName);
            const credit = (agentKey, amount) => {
                const entry = entryFor(agentKey);
                entry.other += amount;
                // Keep who it came from, not just the running total: the cell
                // itemises each source when unfiltered, and collapsing it to
                // one figure under a filter drops the only place that
                // attribution is shown.
                if (sourceName) entry.otherBy.set(sourceName, (entry.otherBy.get(sourceName) || 0) + amount);
            };
            if (otherIdx !== -1) {
                creditOtherCommissionCell(p.rawRow[otherIdx], String(p.fullAgentName).toLowerCase().trim(), credit);
            }
            overrideCols.forEach(col => {
                const amount = parseMoneyValue(p.rawRow[col.idx]);
                if (amount) credit(col.agent, amount);
            });
        });
        return stats;
    }

    // Mirrors format_senior_override_cell() in build_commission_pack.py, so a
    // filtered Other Commission cell reads exactly like the server rollup it
    // stands in for -- one "RM x override from <agent>" line per source,
    // ordered by name.
    function formatOtherCommissionBreakdown(agentStats) {
        if (!agentStats || !agentStats.other) return "-";
        const parts = [...(agentStats.otherBy || new Map()).entries()]
            .filter(([, amount]) => amount > 0)
            .sort((a, b) => String(a[0]).localeCompare(String(b[0])))
            .map(([name, amount]) => `${formatRM(amount)} override from ${escapeHtml(name)}`);
        // No named source survived (negative-only, or a spelling this build
        // doesn't attribute) -- the total is still true, so show that.
        return parts.length ? parts.join("<br/>") : formatRM(agentStats.other);
    }

    // The three commission totals are a whole-month figure, not a property of
    // whichever table is on screen, so every section shows the same cards. They
    // are rebuilt from the basic_nfp payload rather than the active section's
    // rows, which carry no commission columns of their own. Headers are used
    // exactly as the payload gives them, so column indexes stay aligned.
    function buildBasicNfpProcessed() {
        const sec = state.rawData?.sections?.basic_nfp;
        if (!sec || !sec.headers || !sec.rows) return null;
        const headers = sec.headers;
        const agentIdx = headers.findIndex(h => h.toLowerCase().trim() === "agent");
        const customerIdx = headers.findIndex(h => h.toLowerCase().trim() === "customer");
        let curAgent = "", curCustomer = "";
        const processed = sec.rows.map(row => {
            if (agentIdx !== -1 && row[agentIdx] && String(row[agentIdx]).trim()) curAgent = String(row[agentIdx]).trim();
            if (customerIdx !== -1 && row[customerIdx] && String(row[customerIdx]).trim()) curCustomer = String(row[customerIdx]).trim();
            const n = curAgent.toLowerCase();
            return {
                rawRow: [...row], originalRow: row,
                fullAgentName: curAgent, customerName: curCustomer,
                isTotalRow: n.includes("total") || n.includes("summary") || n.includes("grand"),
                isSpecialCase: state.specialCaseRowRefs.has(row)
            };
        });
        return { processed, headers };
    }

    function renderSectionTotalCards() {
        const built = buildBasicNfpProcessed();
        // No payload yet (first paint, or a failed fetch): render empty cards
        // rather than nothing, so the row does not jump when data arrives.
        if (!built) { renderBasicNfpTotalCards([], []); return; }
        renderBasicNfpTotalCards(built.processed, built.headers);
    }

    function renderBasicNfpTotalCards(processed, headers, agentStats, agentsInView) {
        if (!rateCardsContainer) return;
        rateCardsContainer.innerHTML = "";
        rateCardsContainer.classList.remove("hidden");

        let totalCommission = 0, totalOtherCommission = 0, totalReferralFee = 0, totalSales = 0;

        const commPriceIdx = headers.findIndex(h => h.toLowerCase().trim() === "commission price");
        const otherCommIdx = headers.findIndex(h => h.toLowerCase().trim() === "override");
        const referralFeeIdx = headers.findIndex(h => h.toLowerCase().trim() === "referral fee");
        const salesIdx = headers.findIndex(h => {
            const n = String(h).toLowerCase().trim();
            return n === "sales price" || n === "total amount";
        });
        const commIdx = headers.findIndex(h => {
            const n = String(h).toLowerCase().trim();
            return n === "commission" || n === "commission type";
        });

        // Every invoice contributes TWO rows -- a "Basic Commission" row and a
        // paired "Net Floor Price Commission" row -- and both carry the same
        // Sales Price. Summing the column blind would double-count every
        // invoice, so Total Sales counts the Basic row only. A customer with
        // NFP-only source rows still gets a Basic row (its shared columns are
        // backfilled server-side), so nothing is missed. Matches "New Basic
        // Commission" too, which is how a special case restates the pair.
        const isBasicRow = (p) => commIdx !== -1
            && String(p.rawRow[commIdx] || "").toLowerCase().includes("basic");
        const stageIdx = payoutStageIndexes(headers);
        const rm300ColIdx = stageIdx.rm300;

        const isReplaced = makeIsReplacedPredicate(processed, commIdx);

        // On the Basic & NFP tab, the search box and Payout Stage filter should
        // narrow these cards to match the detail table below them (so picking
        // "Advance RM 300" shows the advance total, not the whole month's). Every
        // other tab keeps showing the whole month's figure, same as before.
        const payoutStageOn = state.activeSection === "basic_nfp"
            && state.filters.payoutStage !== "all"
            && parseInt(state.activeMonth) >= 7
            && rm300ColIdx !== -1;
        const searchOn = state.activeSection === "basic_nfp" && !!state.filters.search;
        const filtersActive = payoutStageOn || searchOn;

        const passesPayoutStage = (rawRow) =>
            rowPassesPayoutStage(rawRow, stageIdx, state.filters.payoutStage);

        const isVisible = (p) => {
            if (!filtersActive) return true;
            if (searchOn && !matchesSearch(p.fullAgentName, p.customerName)) return false;
            if (payoutStageOn && !passesPayoutStage(p.rawRow)) return false;
            return true;
        };

        if (state.activeAgentType === "all" && !filtersActive) {
            // "All" sources the cards from the Summary Agent Commission rollup
            // (agent_summary) rather than the raw basic_nfp detail rows. That
            // rollup is built server-side from the ORIGINAL invoices, so it is
            // corrected by the same delta: add the new figures, remove the ones
            // they replace.
            const agSummary = state.rawData?.sections?.agent_summary;
            totalCommission = sumAgentSummaryColumn(agSummary, "commission price");
            totalOtherCommission = sumAgentSummaryColumn(agSummary, "other commission");
            totalReferralFee = sumAgentSummaryColumn(agSummary, "referral fee");
            // One row per agent in the rollup, so this can't double-count.
            totalSales = sumAgentSummaryColumn(agSummary, "sales price");

            (processed || []).forEach(p => {
                if (p.isTotalRow) return;
                if (commPriceIdx !== -1) {
                    if (p.isSpecialCase) totalCommission += parseMoneyValue(p.rawRow[commPriceIdx]);
                    else if (isReplaced(p)) totalCommission -= parseMoneyValue(p.rawRow[commPriceIdx]);
                }
                // A special case can restate the sales price (Adjusted Sales
                // Price), so apply the same add-new / remove-replaced delta.
                if (salesIdx !== -1 && isBasicRow(p)) {
                    if (p.isSpecialCase) totalSales += parseMoneyValue(p.rawRow[salesIdx]);
                    else if (isReplaced(p)) totalSales -= parseMoneyValue(p.rawRow[salesIdx]);
                }
            });
        } else {
            // With a filter active (or a single agent type), the rollup above
            // can't be sliced by row, so sum the visible detail rows directly.
            (processed || []).forEach(p => {
                if (p.isTotalRow) return;
                if (isReplaced(p)) return;
                if (!isVisible(p)) return;
                if (commPriceIdx !== -1) {
                    let val = parseMoneyValue(p.rawRow[commPriceIdx]);
                    if (payoutStageOn && state.filters.payoutStage === "advance") val = advanceCappedValue(p.rawRow, stageIdx, val);
                    else if (payoutStageOn && state.filters.payoutStage === "balance") val = balanceRemainderValue(p.rawRow, stageIdx, val);
                    totalCommission += val;
                }
                if (otherCommIdx !== -1 && !agentStats) totalOtherCommission += parseMoneyValue(p.rawRow[otherCommIdx]);
                if (referralFeeIdx !== -1) totalReferralFee += parseMoneyValue(p.rawRow[referralFeeIdx]);
                // Not stage-capped: an invoice's sales value is the same
                // figure whether the advance or the balance is being paid.
                if (salesIdx !== -1 && isBasicRow(p)) totalSales += parseMoneyValue(p.rawRow[salesIdx]);
            });

            // Other Commission follows the RECIPIENT, not the row it sits on:
            // the RM 85.00 written on Zulkarnain's row is Sunny Tan's money, so
            // filtering to Zulkarnain owes RM 0 and filtering to Sunny Tan owes
            // RM 85.00. Summing the visible rows' cells would report the exact
            // opposite, so the per-agent credits are used instead.
            if (agentStats) {
                totalOtherCommission = 0;
                agentStats.forEach((s, name) => {
                    if (!agentsInView || agentsInView.has(name)) totalOtherCommission += s.other;
                });
            }
        }

        function makeTotalCard(label, value) {
            const card = document.createElement("div");
            card.className = "card metric-card rate-card";
            card.innerHTML = `<div class="metric-details">
                <span class="metric-label">${label}</span>
                <span class="metric-value">${formatRM(value)}</span>
            </div>`;
            return card;
        }

        rateCardsContainer.appendChild(makeTotalCard("Total Sales (RM)", totalSales));
        rateCardsContainer.appendChild(makeTotalCard("Total Commission (RM)", totalCommission));
        rateCardsContainer.appendChild(makeTotalCard("Total Other Commission (RM)", totalOtherCommission));
        rateCardsContainer.appendChild(makeTotalCard("Total Referral Fee (RM)", totalReferralFee));
    }

    function getAgentTier(agentName) {
        if (!agentName) return "";
        if (state.activeAgentType === "outsource") return getOutsourceAgentTier(agentName);
        return getInternalAgentTier(agentName);
    }

    // Internal roles grouped into the bands the rate cards price on -- seven
    // roles share three basic rates, so the band is what a rate lookup needs.
    // "Senior" / "Executive" are the retired labels rows saved before July 2026
    // still hold -- same rename _HIERARCHY_ALIASES absorbs on the rate side, so
    // they have to land in the same band as their new names.
    const INTERNAL_ROLE_BANDS = {
        seniorbranchdirector: "Management",
        regionalsalesdirector: "Management",
        branchsalesmanager: "Management",
        salesdevelopmentmanager: "Management",
        salesteammanager: "Management",
        seniorsalesconsultant: "Senior",
        salessenior: "Senior",
        senior: "Senior",
        salesconsultant: "Consultant",
        salesexecutive: "Consultant",
        executive: "Consultant",
    };

    function normRoleKey(role) {
        return String(role || "").toLowerCase().replace(/[^a-z0-9]/g, "");
    }

    /** The rate band an internal agent is priced on, from the role on the
     *  Agent Roles & Hierarchy page as at the selected month.
     *
     *  Falls back to the old hardcoded first-name split only when no role data
     *  reached the browser at all (an older cached payload, or the lookup
     *  failed server side) -- otherwise the rate cards would lose their figures
     *  entirely on a stale response. */
    function getInternalAgentTier(agentName) {
        if (!agentName) return "";
        const n = agentName.toLowerCase().trim();
        if (n.includes("total") || n.includes("summary") || n.includes("grand")) return "";

        const roles = state.rawData?.agent_roles;
        if (roles) {
            const entry = roles[normalizeAgentKey(agentName)];
            const role = entry ? String(entry.role || "").trim() : "";
            // No row for this agent, a blank role, or a role this build does
            // not recognise all mean the same thing for pricing: nothing on the
            // Data page governs them, so they read as unset rather than being
            // quietly shaded as though they were priced.
            return role ? (INTERNAL_ROLE_BANDS[normRoleKey(role)] || "Unset") : "Unset";
        }

        const seniors = ["sunny", "martin", "kent", "zhe hang", "ching zhe hang", "teng kah kent", "sunny tan", "martin hing"];
        if (seniors.some(s => n.includes(s))) return "Senior";
        return "Consultant";
    }

    function getOutsourceAgentTier(agentName) {
        if (!agentName) return "";
        const n = agentName.toLowerCase().trim();
        if (n.includes("total") || n.includes("summary") || n.includes("grand")) return "";
        if (n === "gan lai soon") return "OGM";
        const oums = ["carol siow","oliver koh","dean wai","chan wing on","chan jia wei","caryn dong","ling liang kang","loo chew yin","gan lai hock","phil moo","kok shao hong","wilson tan"];
        if (oums.some(oum => n.includes(oum))) return "OUM";
        return "OSA";
    }

    /** Shading for one table row. Total / summary rows keep theirs; every other
     *  row is flat.
     *
     *  Rows used to be tinted by the agent's role band, decoded by an "Agent
     *  Tiers Legend" bar above each table. Both are gone: a colour can only
     *  carry the band, and it carried the band as at the *report* month, which
     *  is the wrong role for any invoice raised before a promotion. The exact
     *  role now lives on the agent-name hover, resolved per row against that
     *  row's own invoice date -- see agentRoleTooltip(). */
    function getRowClass(agentName) {
        if (!agentName) return "";
        const nameLower = agentName.toLowerCase();
        if (nameLower.includes("total") || nameLower.includes("summary") || nameLower.includes("grand")) return "total-bg";
        return "";
    }

    // -------------------------------------------------------------
    // Agent role hover (Agent Roles & Hierarchy, by invoice date)
    // -------------------------------------------------------------
    const MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

    /** "2026-08" -> "Aug 2026". Anything else is passed through untouched, so a
     *  malformed effective_from shows as stored rather than as "NaN". */
    function fmtRoleMonth(ym) {
        const m = /^(\d{4})-(\d{2})$/.exec(String(ym || "").trim());
        if (!m) return String(ym || "");
        const idx = parseInt(m[2], 10) - 1;
        return idx >= 0 && idx < 12 ? `${MONTH_ABBR[idx]} ${m[1]}` : String(ym);
    }

    /** How long a role ran, for the tooltip. "9999-12" is the open-ended end
     *  split_effective_range() uses, and reads as "current" rather than as a
     *  date no one will ever see. */
    function fmtRolePeriod(start, end) {
        const from = fmtRoleMonth(start);
        if (!end || String(end).startsWith("9999")) return `${from} — current`;
        return start === end ? from : `${from} — ${fmtRoleMonth(end)}`;
    }

    /** The Agent Roles & Hierarchy row(s) governing this agent in "YYYY-MM".
     *
     *  Same rule the server prices on -- of the rows whose effective range
     *  covers the month, the latest start wins -- but applied per agent type,
     *  because an agent can hold an Internal and an Outsource role at the same
     *  time (Chan Jia Wei is a Branch Sales Manager and an OUM concurrently).
     *  The report is read one agent type at a time, so the role belonging to
     *  the active view is the one that answers the question; the combined
     *  "All Agents" view keeps both. */
    function roleEntriesAt(agentName, ym) {
        const history = state.rawData?.agent_role_history;
        if (!history || !ym) return [];
        // The server keys this map on each agent's canonical full name, but a
        // source row carries whatever the invoice system holds -- often a
        // nickname ("Carol Siow" for Siow Sio Chui, "ZUL" for Ahmad
        // Zulkarnain). Resolve before keying, then fall back to the raw name
        // for anything the name map does not cover. Keying on the raw name
        // alone missed 13 of the 47 agents actually present in the 2026 data.
        const entries = history[normalizeAgentKey(resolveAgentName(agentName) || agentName)]
                     || history[normalizeAgentKey(agentName)];
        if (!entries || !entries.length) return [];

        const covering = entries.filter(e => e.start <= ym && ym <= (e.end || "9999-12"));
        if (!covering.length) return [];

        const active = String(state.activeAgentType || "").toLowerCase();
        const scoped = (active === "internal" || active === "outsource")
            ? covering.filter(e => String(e.agent_type || "").toLowerCase() === active)
            : covering;
        // Falling back to every covering row rather than showing nothing: a
        // role recorded under the other type still beats a blank tooltip.
        const pool = scoped.length ? scoped : covering;

        const winners = new Map();
        pool.forEach(e => {
            const t = String(e.agent_type || "").toLowerCase();
            const cur = winners.get(t);
            if (!cur || e.start > cur.start) winners.set(t, e);
        });
        return [...winners.values()];
    }

    /** Every "YYYY-MM" in a rendered date cell. One cell can stack several
     *  invoice dates joined by <br/> (get_dates_for_invoices does exactly that
     *  for a customer with more than one invoice), and they can straddle a role
     *  change, so each has to be resolved on its own. */
    function invoiceMonthsIn(cellText) {
        const found = String(cellText || "").match(/\d{4}-\d{2}(?=-\d{2}|\b)/g);
        return found ? [...new Set(found)] : [];
    }

    /** This row's Invoice Date cell, or "" when the table has no such column.
     *  Passing "" is what makes agentRoleTooltip() fall back to the report
     *  month, so the caller never has to test for the column itself. */
    function invoiceDateCellOf(row, headers) {
        if (!row || !headers) return "";
        const idx = headers.findIndex(h => String(h || "").toLowerCase().includes("invoice date"));
        return idx === -1 ? "" : String(row[idx] || "");
    }

    /** Tooltip payload for an agent-name cell, or null when there is nothing
     *  useful to say.
     *
     *  `invoiceCellText` is the row's own Invoice Date cell where the table has
     *  one — the role is then whatever the agent held when the invoice was
     *  raised, not when it happened to pay out. Tables that carry no invoice
     *  date (Summary Agent Commission, Production Bonus, EGA/ESA, Monthly
     *  Contest) aggregate a whole month, so they fall back to the report month
     *  and list every role that month's invoices span. */
    function agentRoleTooltip(agentName, invoiceCellText) {
        // Monthly Contest marks captains with a trailing "*" that is stripped
        // before display -- it is not part of the name the roles table keys on.
        const name = String(agentName || "").replace(/\*/g, "").trim();
        if (!name) return null;
        const low = name.toLowerCase();
        if (low.includes("total") || low.includes("summary") || low.includes("grand")) return null;

        const months = invoiceMonthsIn(invoiceCellText);
        const reportMonth = `${state.activeYear}-${String(state.activeMonth).padStart(2, "0")}`;
        const basis = months.length ? months : [reportMonth];

        // Dedupe on the role row itself: two invoice dates inside one effective
        // range are one role, and repeating it would imply a change that never
        // happened.
        const seen = new Map();
        basis.forEach(ym => {
            roleEntriesAt(name, ym).forEach(entry => {
                seen.set(`${entry.agent_type}|${entry.start}|${entry.end}|${entry.role}`, entry);
            });
        });

        // Same key rule as roleEntriesAt(): resolved name first, raw as fallback.
        const roleMap = state.rawData?.agent_roles;
        const resolved = roleMap?.[normalizeAgentKey(resolveAgentName(name) || name)]
                      || roleMap?.[normalizeAgentKey(name)];
        const displayName = resolved?.agent || resolveAgentName(name) || name;

        if (!seen.size) {
            // No row covers it. Say so plainly — the agent is priced by a
            // built-in fallback, and a silent tooltip would hide that.
            return {
                title: displayName,
                formula: "",
                subtext: `<div style="color:#fcd34d;">No role on the Data page for `
                       + `${escapeHtml(fmtRoleMonth(basis[0]))}.</div>`
                       + `<div style="margin-top:6px;color:#cbd5e1;">Set one under `
                       + `Data → Agent Roles &amp; Hierarchy.</div>`,
            };
        }

        const entries = [...seen.values()].sort((a, b) => (a.start < b.start ? -1 : a.start > b.start ? 1 : 0));
        const basisNote = months.length
            ? "Role as at the invoice date"
            : `Role as at ${fmtRoleMonth(reportMonth)}`;

        // Two roles can appear for two different reasons, and they mean
        // opposite things: sequentially (the agent was promoted part-way
        // through the invoices this row covers) or concurrently (they hold an
        // Internal and an Outsource role at once). Only the first is a change.
        const types = new Set(entries.map(e => String(e.agent_type || "").toLowerCase()));
        const showType = types.size > 1;

        const lines = entries.map(e => `
            <div style="display:flex; justify-content:space-between; gap:20px; margin-bottom:4px;">
                <strong>${escapeHtml(e.role || "(no role)")}${showType && e.agent_type
                    ? ` <span style="font-weight:400;color:#94a3b8;">(${escapeHtml(e.agent_type)})</span>`
                    : ""}</strong>
                <span style="color:#cbd5e1; white-space:nowrap;">${escapeHtml(fmtRolePeriod(e.start, e.end))}</span>
            </div>`).join("");

        let multiNote = "";
        if (entries.length > 1) {
            const starts = new Set(entries.map(e => e.start));
            multiNote = showType && starts.size === 1
                ? `<div style="margin-top:6px; color:#fcd34d;">Holds both roles concurrently.</div>`
                : `<div style="margin-top:6px; color:#fcd34d;">This row spans a role change.</div>`;
        }

        return {
            title: displayName,
            formula: "",
            subtext: `<div style="margin-bottom:6px; color:#cbd5e1;">${escapeHtml(basisNote)}</div>`
                   + lines + multiNote,
        };
    }

    /** Wires the role hover onto an agent-name cell. No-op when the agent has
     *  nothing to show, so ordinary cells keep their plain cursor. */
    function attachAgentRoleHover(td, agentName, invoiceCellText) {
        const info = agentRoleTooltip(agentName, invoiceCellText);
        if (!info) return;
        td.classList.add("agent-role-hover");
        td.addEventListener("mouseenter", (e) => showCommCalcTooltip(e, info));
        td.addEventListener("mousemove", moveCommCalcTooltip);
        td.addEventListener("mouseleave", hideCommCalcTooltip);
    }

    function escapeHtml(str) {
        if (!str) return "";
        return String(str)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    let commCalcTooltipEl = null;
    function getOrCreateCommCalcTooltip() {
        if (!commCalcTooltipEl) {
            commCalcTooltipEl = document.getElementById("commCalcTooltip");
            if (!commCalcTooltipEl) {
                commCalcTooltipEl = document.createElement("div");
                commCalcTooltipEl.id = "commCalcTooltip";
                commCalcTooltipEl.className = "comm-calc-tooltip";
                document.body.appendChild(commCalcTooltipEl);
            }
        }
        return commCalcTooltipEl;
    }

    // Hover breakdown for the Gan Lai Soon column, in the same shape the Basic
    // and Net Floor Price cells use. When a case has changed his rate, both
    // lines are shown — the standard one it would have been, and the one the
    // case applies — so the two figures stacked in the cell are accounted for.
    function getGanOverrideBreakdown(row, headers, custName) {
        if (!row || !headers) return null;
        const salesIdx = headers.findIndex(h => {
            const n = h.toLowerCase().trim();
            return n === "sales price" || n === "total amount";
        });
        if (salesIdx === -1) return null;

        const parseNum = (str) => {
            if (!str || str === "-") return 0;
            const val = parseFloat(String(str).replace(/RM/gi, "").replace(/,/g, "").trim());
            return isNaN(val) ? 0 : val;
        };
        const sales = parseNum(row[salesIdx]);
        if (!sales) return null;

        const fmt = (v) => `RM ${v.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
        const casePct = row.specialCaseData ? row.specialCaseData.ganOverridePct : "";
        const hasOverride = String(casePct ?? "").trim() !== ""
            && Number(casePct) !== DEFAULT_GAN_OVERRIDE_PCT;
        const pct = hasOverride ? Number(casePct) : DEFAULT_GAN_OVERRIDE_PCT;

        const standardLine =
            `Standard: ${fmt(sales)} × ${DEFAULT_GAN_OVERRIDE_PCT}% = ${fmt(sales * DEFAULT_GAN_OVERRIDE_PCT / 100)}`;
        const subtext = hasOverride
            ? `${standardLine}<br><span style="color:#dc2626;font-weight:600">`
              + `Special case: ${fmt(sales)} × ${pct}% = ${fmt(sales * pct / 100)}</span>`
              + `<br><br>Click to edit Gan Lai Soon's override rate`
            : `${standardLine}<br><br>Click to set Gan Lai Soon's override rate`;

        return {
            title: `Gan Lai Soon Override — ${custName || "Agent"}`,
            formula: `OGM Override = Sales Price × Override Rate %`,
            subtext,
        };
    }

    // Hover breakdown for the Override ("Other Commission") column. The cell
    // packs every credit into one <br/>-joined string; this lays them out one
    // per line, names each party, and totals them when there is more than one.
    //
    // The percentage is DERIVED from the figures rather than assumed. The
    // override behind this column is 0.5% of sales for an outsource OUM,
    // 0.25% for an internal senior, and 20% of the whole commission on a
    // Factory profit-sharing split -- quoting any one of those as "the" rate
    // would be wrong on the other two.
    function getOverrideBreakdownTooltip(row, headers, custName, cellValue) {
        const raw = String(cellValue == null ? "" : cellValue).trim();
        if (!raw || raw === "-") return null;

        const salesIdx = headers.findIndex(h => {
            const n = String(h).toLowerCase().trim();
            return n === "sales price" || n === "total amount";
        });
        const sales = salesIdx !== -1 ? parseMoneyValue(row[salesIdx]) : 0;

        const entries = raw.split(/<br\s*\/?>/i)
            .map(s => s.replace(/<[^>]*>/g, "").trim())
            .filter(s => s && s !== "-");
        if (!entries.length) return null;

        const num = (s) => {
            const v = parseFloat(String(s).replace(/,/g, ""));
            return isNaN(v) ? 0 : v;
        };

        let total = 0, parsedCount = 0;
        const lines = entries.map(text => {
            // "RM 85.00 (Recipient)" on a detail row -- money leaving this
            // row. "RM 85.00 override from Source" on a summary row -- money
            // arriving from someone else's row.
            const toRecipient = text.match(/^RM\s*([-\d,.]+)\s*\((.+)\)\s*$/i);
            const fromSource = text.match(/^RM\s*([-\d,.]+)\s*override from\s+(.+?)\s*$/i);
            const m = toRecipient || fromSource;
            if (!m) return `<div style="margin-bottom:4px;">${escapeHtml(text)}</div>`;

            const amount = num(m[1]);
            const who = m[2].trim();
            total += amount;
            parsedCount += 1;

            // Only the "(recipient)" form is generated from THIS row's sales,
            // so only that form can honestly be shown as a share of them.
            const share = (toRecipient && sales)
                ? `<div style="opacity:0.7; font-size:11px;">${(amount / sales * 100).toFixed(2)}% of ${formatRM(sales)} sales price</div>`
                : "";
            const direction = toRecipient ? "credited to" : "from";

            return `<div style="margin-bottom:6px;">
                <div style="display:flex; justify-content:space-between; gap:20px;">
                    <span><span style="opacity:0.7;">${direction}</span> <strong>${escapeHtml(who)}</strong></span>
                    <strong>${formatRM(amount)}</strong>
                </div>
                ${share}
            </div>`;
        });

        if (parsedCount > 1) {
            lines.push(`<div style="display:flex; justify-content:space-between; gap:20px;
                border-top:1px dashed rgba(255,255,255,0.25); margin-top:6px; padding-top:6px;">
                <span>Total</span><strong>${formatRM(total)}</strong>
            </div>`);
        }

        return {
            title: `Other Commission${custName && custName !== "-" ? ` — ${custName}` : ""}`,
            subtext: lines.join(""),
        };
    }

    function showCommCalcTooltip(e, info) {
        const el = getOrCreateCommCalcTooltip();
        if (!el || !info) return;
        const safeTitle = escapeHtml(info.title);
        const safeFormula = escapeHtml(info.formula);
        // `preformula` is raw HTML placed above the formula line -- the payment
        // milestone dates sit there so they read before the calculation they
        // gate, rather than being buried under it.
        el.innerHTML = `
            <div class="tooltip-header">${safeTitle}</div>
            ${info.preformula ? `<div class="tooltip-subtext">${info.preformula}</div>` : ""}
            ${info.formula ? `<div class="tooltip-formula">${safeFormula}</div>` : ""}
            ${info.subtext ? `<div class="tooltip-subtext">${info.subtext}</div>` : ""}
        `;
        el.classList.add("visible");
        moveCommCalcTooltip(e);
    }

    function moveCommCalcTooltip(e) {
        const el = getOrCreateCommCalcTooltip();
        if (!el) return;
        const mouseX = e.clientX || e.pageX || 0;
        const mouseY = e.clientY || e.pageY || 0;
        const x = Math.min(mouseX + 15, window.innerWidth - 450);
        const y = Math.min(mouseY + 15, window.innerHeight - 200);
        el.style.left = `${Math.max(10, x)}px`;
        el.style.top = `${Math.max(10, y)}px`;
    }

    function hideCommCalcTooltip() {
        const el = getOrCreateCommCalcTooltip();
        if (el) el.classList.remove("visible");
    }

    // Hover breakdown for a Commission Price cell the filters have restated.
    // Same shape as the unfiltered one in getCommissionCalcBreakdown (Basic +
    // Net Floor Price, then the total) -- only the figures differ, because the
    // rollup's whole-month numbers would contradict the restated cell.
    function getStageCommissionBreakdown(agentName, stats) {
        if (!stats || stats.ownRows === 0) return null;
        return {
            title: `Commission Price Breakdown — ${agentName || "Agent"}`,
            formula: `Total Commission Price = Basic Commission + Net Floor Price Commission`,
            subtext: `
                <div style="display:flex; justify-content:space-between; gap:20px; margin-bottom:4px;">
                    <span>Basic Commission:</span> <strong>${formatRM(stats.basic)}</strong>
                </div>
                <div style="display:flex; justify-content:space-between; gap:20px; margin-bottom:6px;">
                    <span>Net Floor Price Commission:</span> <strong>${formatRM(stats.nfp)}</strong>
                </div>
                <div style="display:flex; justify-content:space-between; gap:20px; padding-top:6px; border-top:1px solid rgba(255,255,255,0.2); font-weight:700; color:#4ade80;">
                    <span>Total Commission Price:</span> <span>${formatRM(stats.commission)}</span>
                </div>
            `
        };
    }

    // Customer-column hover on the Basic & NFP table: the panels and the supply
    // phase for that customer's invoice(s), served by app.py's system_details.
    // A customer with more than one invoice gets a line each rather than a
    // merged figure, since the panel counts belong to separate systems.
    function getCustomerSystemTooltip(customerName) {
        const map = state.rawData?.sections?.basic_nfp?.system_details;
        const name = String(customerName || "").trim();
        if (!map || !name) return null;
        const entries = map[name.toLowerCase()];
        if (!entries || !entries.length) return null;
        const lines = entries.map(e => {
            const panel = e.panel_qty && e.panel_rating
                ? `${e.panel_qty}x ${e.panel_rating}W${e.brand ? ` ${e.brand}` : ""}`
                : "-";
            return `<div style="margin-bottom:8px;">
                    <div><strong>Panels</strong> ${escapeHtml(panel)}</div>
                    <div><strong>Phase</strong> ${escapeHtml(e.phase || "-")}</div>
                </div>`;
        }).join("");
        return {
            title: `System Details — ${name}`,
            subtext: lines
        };
    }

    // Payment milestones for a Basic Commission row, rendered above the formula.
    //
    // The advance triggers on the 1st Payment Date. The balance triggers at 75%
    // for invoices under the multi-stage policy (July 2026+); a pre-July invoice
    // was never advanced at all and settles in full at 100% instead, so it gets
    // the single 100% line.
    //
    // The 75% date is read from system_details, NOT from the row: the column
    // headed "75% Payment Date" is filled from the invoice's full_payment_date,
    // so an invoice that reached 75% but not 100% shows "pending" there. That
    // same column IS the right source for the pre-July 100% line.
    function getBasicMilestoneLines(row, headers, custName) {
        const findIdx = (pred) => headers.findIndex(h => pred(String(h).toLowerCase().trim()));
        const firstPayIdx = findIdx(n => n === "1st payment date");
        const rm300Idx = findIdx(n => n.includes("rm300") || n.includes("basic commission (rm"));
        const payDateIdx = findIdx(n => n === "75% payment date" || n === "full payment date");
        if (firstPayIdx === -1 && payDateIdx === -1) return "";

        const cell = (i) => (i === -1 ? "" : String(row[i] || "").trim());
        const isPreJuly = cell(rm300Idx).toLowerCase() === "invoice before july";

        let payoutDate;
        if (isPreJuly) {
            payoutDate = cell(payDateIdx);
        } else {
            const details = state.rawData?.sections?.basic_nfp?.system_details;
            const entries = details ? details[String(custName || "").toLowerCase().trim()] : null;
            payoutDate = entries && entries.length ? entries[0].pct75_date : "";
        }

        const shown = (v) => {
            const s = String(v || "").trim();
            return !s || s === "-" ? "pending" : s;
        };
        const line = (label, value) => `
            <div style="display:flex; justify-content:space-between; gap:20px; margin-bottom:4px;">
                <span>${label}:</span> <strong>${escapeHtml(shown(value))}</strong>
            </div>`;

        const out = [];
        if (!isPreJuly) out.push(line("Advance RM 300 (1st Payment)", cell(firstPayIdx)));
        out.push(line(isPreJuly ? "Payout (100% Payment)" : "Balance Payout (75% Payment)", payoutDate));
        return out.join("");
    }

    function getCommissionCalcBreakdown(row, headers, agentName, custName, ri, rowsList) {
        if (!row || !headers) return null;

        const commIdx = headers.findIndex(h => h.toLowerCase().trim() === "commission" || h.toLowerCase().trim() === "commission type");
        const priceIdx = headers.findIndex(h => h.toLowerCase().trim() === "commission price" || h.toLowerCase().trim() === "commission (rm)");
        const salesIdx = headers.findIndex(h => h.toLowerCase().trim() === "sales price" || h.toLowerCase().trim() === "total amount");
        const nfpIdx = headers.findIndex(h => h.toLowerCase().trim() === "net floor price" || h.toLowerCase().trim() === "netfloor price");
        const dateIdx = headers.findIndex(h => h.toLowerCase().includes("invoice date"));
        const pkgIdx = headers.findIndex(h => h.toLowerCase().includes("package"));

        const parseNum = (str) => {
            if (!str || str === "-") return 0;
            const cleaned = String(str).replace(/RM/gi, "").replace(/,/g, "").trim();
            const val = parseFloat(cleaned);
            return isNaN(val) ? 0 : val;
        };

        // If current row is missing Sales Price or NFP, look up customer block partner row
        let basicPartnerRow = row;
        let nfpPartnerRow = row;
        if (rowsList && Array.isArray(rowsList) && ri !== undefined && ri !== null) {
            if (ri > 0 && rowsList[ri - 1]) {
                const prevComm = commIdx !== -1 ? String(rowsList[ri - 1][commIdx] || "").toLowerCase() : "";
                if (prevComm.includes("basic commission")) basicPartnerRow = rowsList[ri - 1];
            }
            if (ri < rowsList.length - 1 && rowsList[ri + 1]) {
                const nextComm = commIdx !== -1 ? String(rowsList[ri + 1][commIdx] || "").toLowerCase() : "";
                if (nextComm.includes("net floor price")) nfpPartnerRow = rowsList[ri + 1];
            }
        }

        const commType = commIdx !== -1 ? String(row[commIdx] || "").trim() : "";
        const commValStr = priceIdx !== -1 ? String(row[priceIdx] || "").trim() : "";
        
        const rawSales = salesIdx !== -1 ? (row[salesIdx] && row[salesIdx] !== "-" ? row[salesIdx] : basicPartnerRow[salesIdx]) : "";
        const rawNfp = nfpIdx !== -1 ? (row[nfpIdx] && row[nfpIdx] !== "-" ? row[nfpIdx] : (basicPartnerRow[nfpIdx] && basicPartnerRow[nfpIdx] !== "-" ? basicPartnerRow[nfpIdx] : nfpPartnerRow[nfpIdx])) : "";
        const rawDate = dateIdx !== -1 ? (row[dateIdx] && row[dateIdx] !== "-" ? row[dateIdx] : basicPartnerRow[dateIdx]) : "";
        const rawPkg = pkgIdx !== -1 ? (row[pkgIdx] && row[pkgIdx] !== "-" ? row[pkgIdx] : basicPartnerRow[pkgIdx]) : "";

        const salesStr = String(rawSales || "").trim();
        const nfpStr = String(rawNfp || "").trim();
        const invDate = String(rawDate || "").trim();
        const pkgStr = String(rawPkg || "").trim();

        const sales = parseNum(salesStr);
        const nfp = parseNum(nfpStr);
        const commPrice = parseNum(commValStr);
        const commTypeLower = commType.toLowerCase();

        const clickTip = "<div class='tooltip-click-note'>💡 Click cell to open Special Case editor</div>";
        const fmtNum = (n) => n.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});

        // 0. Summary Agent Commission table (no "Commission Type" column, or contains "Count of Customer"/"Total Invoice")
        const isAgentSummaryRow = commIdx === -1 || headers.some(h => h.toLowerCase().includes("count of customer") || h.toLowerCase().includes("total invoice"));
        if (isAgentSummaryRow) {
            let basicSum = 0;
            let nfpSum = 0;
            if (state.rawData && state.rawData.sections && state.rawData.sections.basic_nfp) {
                const custRows = state.rawData.sections.basic_nfp.rows || [];
                const custHeaders = state.rawData.sections.basic_nfp.headers || [];
                const cAgentIdx = custHeaders.findIndex(h => h.toLowerCase().trim() === "agent");
                const cCommTypeIdx = custHeaders.findIndex(h => h.toLowerCase().trim() === "commission");
                const cPriceIdx = custHeaders.findIndex(h => h.toLowerCase().trim() === "commission price");

                let curAgent = "";
                custRows.forEach(cr => {
                    const aVal = cAgentIdx !== -1 && cr[cAgentIdx] ? String(cr[cAgentIdx]).trim() : "";
                    if (aVal) curAgent = aVal;
                    const normCur = resolveAgentName(curAgent).toLowerCase().trim();
                    const normTarget = resolveAgentName(agentName).toLowerCase().trim();
                    if (normCur === normTarget || curAgent.toLowerCase().trim() === (agentName || "").toLowerCase().trim()) {
                        const cType = cCommTypeIdx !== -1 ? String(cr[cCommTypeIdx] || "").toLowerCase() : "";
                        const cVal = cPriceIdx !== -1 ? parseNum(cr[cPriceIdx]) : 0;
                        if (cType.includes("basic commission")) basicSum += cVal;
                        else if (cType.includes("net floor price")) nfpSum += cVal;
                    }
                });
            }

            const totalSum = basicSum + nfpSum;
            const formatRm = (val) => `RM ${fmtNum(val)}`;

            return {
                title: `Commission Price Breakdown — ${agentName || "Agent"}`,
                formula: `Total Commission Price = Basic Commission + Net Floor Price Commission`,
                subtext: `
                    <div style="display:flex; justify-content:space-between; gap:20px; margin-bottom:4px;">
                        <span>Basic Commission:</span> <strong>${formatRm(basicSum)}</strong>
                    </div>
                    <div style="display:flex; justify-content:space-between; gap:20px; margin-bottom:6px;">
                        <span>Net Floor Price Commission:</span> <strong>${formatRm(nfpSum)}</strong>
                    </div>
                    <div style="display:flex; justify-content:space-between; gap:20px; padding-top:6px; border-top:1px solid rgba(255,255,255,0.2); font-weight:700; color:#4ade80;">
                        <span>Total Commission Price:</span> <span>${commValStr || formatRm(totalSum)}</span>
                    </div>
                `
            };
        }

        // 1. Basic Commission
        if (commTypeLower.includes("basic commission")) {
            const milestones = getBasicMilestoneLines(row, headers, custName);
            if (row.specialCaseData) {
                return {
                    title: `Basic Commission — ${custName || "Special Case"}`,
                    preformula: milestones,
                    formula: `Sales Price = Total Amount - EPP Effective`,
                    subtext: `Basic Commission = Sales Price × Rate % = ${fmtNum(sales)} × Rate = ${commValStr}` + clickTip
                };
            }
            if (sales > 0 && commPrice > 0) {
                const ratePct = ((commPrice / sales) * 100).toFixed(2);
                return {
                    title: `Basic Commission — ${custName}`,
                    preformula: milestones,
                    formula: `Sales Price = Total Amount - EPP Effective`,
                    subtext: `Basic Commission = Sales Price × Rate % = ${fmtNum(sales)} × ${ratePct}% = ${commValStr}` + clickTip
                };
            }
            return {
                title: `Basic Commission — ${custName}`,
                preformula: milestones,
                formula: `Sales Price = Total Amount - EPP Effective`,
                subtext: `Basic Commission = Sales Price × Rate % = ${commValStr || "Standard Rate"}` + clickTip
            };
        }

        // 2. Net Floor Price Commission
        if (commTypeLower.includes("net floor price")) {
            if (row.specialCaseData) {
                return {
                    title: `Net Floor Price Commission — ${custName || "Special Case"}`,
                    formula: `Net Floor Price Commission = (Sales Price - Net Floor Price) × Rate%`,
                    subtext: `Special Case Override = ${commValStr}` + clickTip
                };
            }

            if (nfpStr.toLowerCase().includes("invoice before oct") || commValStr.toLowerCase().includes("invoice before oct")) {
                return {
                    title: `Net Floor Price Commission — ${custName}`,
                    formula: `Net Floor Price Commission = (Sales Price - Net Floor Price) × Rate%`,
                    subtext: `Invoice Date (${invDate || "pre-Oct 2025"}) < October 2025 → invoice before Oct 25` + clickTip
                };
            }

            if (nfpStr.toLowerCase().includes("jinkosolar package not included") || commValStr.toLowerCase().includes("jinkosolar package not included")) {
                return {
                    title: `Net Floor Price Commission — ${custName}`,
                    formula: `Net Floor Price Commission = (Sales Price - Net Floor Price) × Rate%`,
                    subtext: `Package (${pkgStr || "Standard"}) without 590W / 620W / 650W → JinkoSolar package not included` + clickTip
                };
            }

            if (sales > 0 && nfp > 0) {
                if (sales >= nfp) {
                    const diff = sales - nfp;
                    const commCalc = diff * 0.25;
                    return {
                        title: `Net Floor Price Commission — ${custName}`,
                        formula: `Net Floor Price Commission = (Sales Price - Net Floor Price) × Rate%`,
                        subtext: `= (${fmtNum(sales)} - ${fmtNum(nfp)}) × 25% = RM ${fmtNum(commCalc)}` + clickTip
                    };
                } else {
                    const diff = sales - nfp;
                    const commCalc = diff * 0.20;
                    return {
                        title: `Net Floor Price Commission — ${custName}`,
                        formula: `Net Floor Price Commission = (Sales Price - Net Floor Price) × Rate%`,
                        subtext: `= (${fmtNum(sales)} - ${fmtNum(nfp)}) × 20% = -RM ${fmtNum(Math.abs(commCalc))} (Deduction)` + clickTip
                    };
                }
            }

            return {
                title: `Net Floor Price Commission — ${custName}`,
                formula: `Net Floor Price Commission = (Sales Price - Net Floor Price) × Rate%`,
                subtext: `= (${fmtNum(sales)} - ${fmtNum(nfp)}) × Rate% = ${commValStr || "-"}` + clickTip
            };
        }

        return null;
    }

    // ------------------------------------------------------------------
    // Agent Summary Inline Renderer
    // ------------------------------------------------------------------
    function renderAgentSummaryInline(agSummarySection, isOutsource, visibleAgents, filteredAgentStats, agentRemarks) {
        const headers = agSummarySection.headers;
        const agentColIdx = headers.findIndex(h => h.toLowerCase().trim() === "agent");
        const totalInvColIdx = getCountColumnIdx(headers);
        const overrideColIdx = headers.findIndex(h => h.toLowerCase().includes("override"));
        const referralFeeColIdx = headers.findIndex(h => h.toLowerCase().trim() === "referral fee");
        // Dropped from this table only -- the columns stay in the payload (and
        // in the detail table below), they are just not rendered here. Skipping
        // them at render time keeps every index-based merge/rowspan pass below
        // working against the payload's own column positions.
        const HIDDEN_SUMMARY_HEADERS = ["package type", "system price", "net floor price"];
        const hiddenSummaryCols = new Set(
            headers.reduce((acc, h, i) => {
                if (HIDDEN_SUMMARY_HEADERS.includes(String(h).toLowerCase().trim())) acc.push(i);
                return acc;
            }, [])
        );

        const userObj = USER_ROLES[state.currentUser];
        const originalRows = agSummarySection.rows;
        // The summary rolls up the detail table, so whenever a filter narrows
        // those rows only agents that still have one stay. `visibleAgents` is
        // built from the detail rows AFTER every filter, so membership is
        // exactly that test -- and unlike an agent-name match it keeps the
        // right agent when the search term is a CUSTOMER name. An OGM earns
        // only via the override column and owns no detail rows at all, so the
        // stats map (which credits them) is what keeps their row alive.
        const visibleAgentsLower = filteredAgentStats
            ? new Set([
                ...[...visibleAgents].map(a => String(a).toLowerCase().trim()),
                ...[...filteredAgentStats.entries()]
                    .filter(([name, s]) => s.ownRows === 0 && s.other !== 0 && matchesSearch(name, ""))
                    .map(([name]) => name)
            ])
            : null;
        const rows = [];
        const fullAgentNames = [];
        let tempCur = "";
        originalRows.forEach(r => {
            const v = agentColIdx !== -1 && r[agentColIdx] ? String(r[agentColIdx]).trim() : "";
            if (v) tempCur = v;
            // Filter by role-based agent restriction
            if (userObj && userObj.filterAgentName !== null) {
                if (tempCur.toLowerCase().trim() !== userObj.filterAgentName.toLowerCase().trim()) return;
            }
            if (visibleAgentsLower) {
                if (!visibleAgentsLower.has(tempCur.toLowerCase().trim())) return;
            } else if (!matchesSearch(tempCur, "")) {
                // Callers without per-agent stats (other sections) keep the
                // original agent-name-only search behaviour.
                return;
            }
            rows.push(r);
            fullAgentNames.push(tempCur);
        });

        const wrapper = document.createElement("div");
        wrapper.className = "agent-summary-inline-wrapper";
        const headerDiv = document.createElement("div");
        headerDiv.className = "agent-summary-inline-header";
        const h4 = document.createElement("h4");
        h4.textContent = "Summary Agent Commission";
        headerDiv.appendChild(h4);
        const badge = document.createElement("span");
        badge.className = "agent-summary-inline-badge";
        badge.textContent = rows.length + " rows";
        headerDiv.appendChild(badge);
        wrapper.appendChild(headerDiv);

        const table = document.createElement("table");
        table.className = "agent-summary-inline-table";
        const thead = document.createElement("thead");
        const trHead = document.createElement("tr");
        const tableHeaders = [...headers, "Action"];
        headers.forEach((h, ci) => {
            if (hiddenSummaryCols.has(ci)) return;
            const th = document.createElement("th");
            th.textContent = h;
            if (isNumericHeader(h)) th.classList.add("numeric");
            else if (isDateHeader(h)) th.classList.add("date");
            trHead.appendChild(th);
        });
        const thRemark = document.createElement("th");
        thRemark.textContent = "Remark";
        trHead.appendChild(thRemark);
        const thAction = document.createElement("th");
        thAction.textContent = "Action";
        thAction.style.textAlign = "center";
        thAction.style.width = "75px";
        trHead.appendChild(thAction);

        thead.appendChild(trHead);
        table.appendChild(thead);

        const tbody = document.createElement("tbody");
        const N = rows.length;
        const agentSpans = new Array(N).fill(1);
        const agentVisible = new Array(N).fill(true);
        if (agentColIdx !== -1) {
            let i = 0;
            while (i < N) {
                let j = i + 1;
                while (j < N && fullAgentNames[j] === fullAgentNames[i]) j++;
                agentSpans[i] = j - i;
                for (let k = i + 1; k < j; k++) agentVisible[k] = false;
                i = j;
            }
        }
        const totalInvSpans = new Array(N).fill(1);
        const totalInvVisible = new Array(N).fill(true);
        if (totalInvColIdx !== -1) {
            let si = 0;
            while (si < N) {
                let sj = si + 1;
                while (sj < N && fullAgentNames[sj] === fullAgentNames[si]) sj++;
                totalInvSpans[si] = sj - si;
                for (let k = si + 1; k < sj; k++) totalInvVisible[k] = false;
                si = sj;
            }
        }
        const referralFeeSpans = new Array(N).fill(1);
        const referralFeeVisible = new Array(N).fill(true);
        if (referralFeeColIdx !== -1) {
            let fi = 0;
            while (fi < N) {
                let fj = fi + 1;
                while (fj < N && fullAgentNames[fj] === fullAgentNames[fi]) fj++;
                referralFeeSpans[fi] = fj - fi;
                for (let k = fi + 1; k < fj; k++) referralFeeVisible[k] = false;
                fi = fj;
            }
        }
        // While a filter is narrowing the detail rows, the rollup's whole-month
        // Commission Price, Count of Customer and Other Commission are replaced
        // by the figures for what survived -- before the merge pass runs, so
        // rowspans and the hover breakdown both read the value on screen. The
        // restated Other Commission is a bare total: the rollup's per-source
        // wording ("override from Ng Zhan Yi") describes the whole month and
        // would no longer match the amount beside it.
        const commPriceColIdx = headers.findIndex(h => h.toLowerCase().trim() === "commission price");
        const otherCommColIdx = headers.findIndex(h => h.toLowerCase().trim() === "other commission");
        const cellGrid = [];
        for (let ri = 0; ri < N; ri++) {
            const agentStats = filteredAgentStats
                ? filteredAgentStats.get(String(fullAgentNames[ri]).toLowerCase().trim())
                : null;
            cellGrid.push(rows[ri].map((val, ci) => {
                if (agentStats && ci === commPriceColIdx) {
                    // An override-only agent (an OGM) has no invoices of their
                    // own; the rollup writes "-" rather than RM 0.00 there.
                    const commission = agentStats.ownRows === 0 ? "-" : formatRM(agentStats.commission);
                    return { value: commission, rowspan: 1, visible: true };
                }
                if (agentStats && ci === totalInvColIdx) {
                    return { value: String(agentStats.customers.size), rowspan: 1, visible: true };
                }
                if (agentStats && ci === otherCommColIdx) {
                    return { value: formatOtherCommissionBreakdown(agentStats), rowspan: 1, visible: true };
                }
                return {
                    value: val === null || val === undefined || String(val).trim() === "" ? "-" : String(val),
                    rowspan: 1, visible: true
                };
            }));
        }
        const _pkgCol = headers.findIndex(h => h.toLowerCase().includes("package"));
        const colsToMerge = [];
        for (let ci = 0; ci < headers.length; ci++) {
            if (ci !== agentColIdx && ci !== totalInvColIdx && ci !== referralFeeColIdx) colsToMerge.push(ci);
        }

        let i = 0;
        while (i < N) {
            let j = i + 1;
            while (j < N && fullAgentNames[j] === fullAgentNames[i]) j++;
            colsToMerge.forEach(col => {
                if (col < headers.length) {
                    let r = i;
                    while (r < j) {
                        let next_r = r + 1;
                        if (col === _pkgCol) {
                            while (next_r < j) {
                                const cv = cellGrid[r][col].value, nv = cellGrid[next_r][col].value;
                                const isCvEmpty = (cv === "-" || !cv || cv.trim() === "");
                                const isNvEmpty = (nv === "-" || !nv || nv.trim() === "");
                                const cvNorm = String(cv || "").trim().toLowerCase();
                                const nvNorm = String(nv || "").trim().toLowerCase();
                                if ((!isCvEmpty && cvNorm === nvNorm) || (isNvEmpty && !isCvEmpty)) next_r++;
                                else break;
                            }
                        } else {
                            while (next_r < j && cellGrid[next_r][col].value === cellGrid[r][col].value && cellGrid[r][col].value !== "-") next_r++;
                        }
                        if (next_r - r > 1) {
                            cellGrid[r][col].rowspan = next_r - r;
                            for (let k = r + 1; k < next_r; k++) cellGrid[k][col].visible = false;
                        }
                        r = next_r;
                    }
                }
            });
            i = j;
        }

        for (let ri = 0; ri < N; ri++) {
            const row = rows[ri];
            const tr = document.createElement("tr");
            const rc = getRowClass(fullAgentNames[ri], isOutsource);
            if (rc) tr.className = rc;
            for (let ci = 0; ci < headers.length; ci++) {
                const cellVal = row[ci];
                if (hiddenSummaryCols.has(ci)) continue;
                if (ci === agentColIdx && !agentVisible[ri]) continue;
                if (ci === totalInvColIdx && !totalInvVisible[ri]) continue;
                if (ci === referralFeeColIdx && !referralFeeVisible[ri]) continue;
                if (colsToMerge.includes(ci) && !cellGrid[ri][ci].visible) continue;
                const td = document.createElement("td");
                td.innerHTML = cellGrid[ri][ci].value;
                if (ci === agentColIdx && cellGrid[ri][ci].value !== "-") {
                    td.textContent = resolveAgentName(cellGrid[ri][ci].value);
                    // No Invoice Date column on this rollup, so the hover falls
                    // back to the report month and names every role the month's
                    // invoices span.
                    attachAgentRoleHover(td, fullAgentNames[ri], "");
                }
                if (ci === agentColIdx && agentSpans[ri] > 1) td.rowSpan = agentSpans[ri];
                if (ci === totalInvColIdx && totalInvVisible[ri] && totalInvSpans[ri] > 1) td.rowSpan = totalInvSpans[ri];
                if (ci === referralFeeColIdx && referralFeeVisible[ri] && referralFeeSpans[ri] > 1) td.rowSpan = referralFeeSpans[ri];
                if (colsToMerge.includes(ci) && cellGrid[ri][ci].rowspan > 1) td.rowSpan = cellGrid[ri][ci].rowspan;
                const colHeader = headers[ci];
                if (isNumericHeader(colHeader)) td.classList.add("numeric");
                else if (isDateHeader(colHeader)) td.classList.add("date");
                if (ci === overrideColIdx && cellGrid[ri][ci].value !== "-") { td.style.fontWeight = "600"; td.style.color = "#1d4ed8"; }

                // A restated cell itemises the rows it was summed from; an
                // untouched one keeps the stock whole-month breakdown. Using
                // the rollup's explanation on a restated figure would describe
                // a number that is not the one on screen.
                if (colHeader.toLowerCase().trim() === "commission price") {
                    const rowStats = filteredAgentStats
                        ? filteredAgentStats.get(String(fullAgentNames[ri]).toLowerCase().trim())
                        : null;
                    const calcInfo = filteredAgentStats
                        ? getStageCommissionBreakdown(fullAgentNames[ri], rowStats)
                        : getCommissionCalcBreakdown(row, headers, fullAgentNames[ri], "", ri, rows);
                    if (calcInfo) {
                        td.addEventListener("mouseenter", (e) => showCommCalcTooltip(e, calcInfo));
                        td.addEventListener("mousemove", (e) => moveCommCalcTooltip(e));
                        td.addEventListener("mouseleave", hideCommCalcTooltip);
                    }
                }

                // Same breakdown on the summary's Override cell, which holds
                // the "override from <source agent>" spelling.
                if (ci === overrideColIdx && cellGrid[ri][ci].value !== "-") {
                    const ovrInfo = getOverrideBreakdownTooltip(
                        row, headers, resolveAgentName(fullAgentNames[ri]), cellGrid[ri][ci].value);
                    if (ovrInfo) {
                        td.addEventListener("mouseenter", (e) => showCommCalcTooltip(e, ovrInfo));
                        td.addEventListener("mousemove", (e) => moveCommCalcTooltip(e));
                        td.addEventListener("mouseleave", hideCommCalcTooltip);
                    }
                }

                tr.appendChild(td);
            }

            // Remark: the distinct Remarks this agent's detail rows carry, so
            // it narrows with the same filters as the rest of the table. Merged
            // per agent block like the Agent cell, since it is an agent-level
            // roll-up rather than a per-row value.
            if (agentVisible[ri]) {
                const tdRemark = document.createElement("td");
                if (agentSpans[ri] > 1) tdRemark.rowSpan = agentSpans[ri];
                const remarks = agentRemarks
                    ? agentRemarks.get(String(fullAgentNames[ri]).toLowerCase().trim())
                    : null;
                tdRemark.textContent = remarks && remarks.size ? [...remarks].join("; ") : "-";
                tr.appendChild(tdRemark);
            }

            // Action column cell for Monthly Commission Slip
            if (agentVisible[ri]) {
                const tdAction = document.createElement("td");
                tdAction.style.textAlign = "center";
                tdAction.style.verticalAlign = "middle";
                if (agentSpans[ri] > 1) tdAction.rowSpan = agentSpans[ri];

                const agName = fullAgentNames[ri];
                const isTotalRow = !agName || agName.toLowerCase().includes("total") || agName.toLowerCase().includes("summary") || agName.toLowerCase().includes("grand");

                if (!isTotalRow) {
                    const btnSlip = document.createElement("button");
                    btnSlip.className = "btn-commission-slip";
                    btnSlip.setAttribute("data-agent", agName);
                    btnSlip.innerHTML = "📄 Slip";
                    btnSlip.addEventListener("click", (e) => {
                        e.preventDefault();
                        e.stopPropagation();
                        openCommissionSlipModal(agName);
                    });
                    tdAction.appendChild(btnSlip);
                } else {
                    tdAction.textContent = "-";
                }
                tr.appendChild(tdAction);
            }

            tbody.appendChild(tr);
        }
        table.appendChild(tbody);
        wrapper.appendChild(table);
        const hr = document.createElement("hr");
        hr.className = "agent-summary-inline-divider";
        wrapper.appendChild(hr);
        tableContainer.insertBefore(wrapper, dataTable);
    }

    // ------------------------------------------------------------------
    // Main Section Renderer
    // ------------------------------------------------------------------
    function renderActiveSection() {
        showLoader(false);
        updateHeaders();
        updateNoteBox();


        if (rm300FilterGroup) {
            // The advance/balance split only exists from July 2026 (the
            // multi-stage payout months) — earlier months pay in full at 100%,
            // so the dropdown would have nothing to distinguish.
            const showPayoutStage = state.activeSection === "basic_nfp"
                && parseInt(state.activeMonth) >= 7;
            rm300FilterGroup.style.display = showPayoutStage ? "" : "none";
            if (!showPayoutStage && state.filters.payoutStage !== "all") {
                state.filters.payoutStage = "all";
                if (payoutStageFilter) payoutStageFilter.value = "all";
            }
        }

        if (egaAwardFilterGroup) {
            // Only the award tables carry an Eligibility column to filter on.
            const showAwardFilter = state.activeSection === "ega_esa";
            egaAwardFilterGroup.style.display = showAwardFilter ? "" : "none";
            // Leaving the section drops the filter, so returning to it later
            // never shows a partial report with the control out of sight.
            if (!showAwardFilter && state.filters.egaAward !== "all") {
                state.filters.egaAward = "all";
                if (egaAwardFilter) egaAwardFilter.value = "all";
            }
        }

        // ANP Commission is internal agents only — lock the Agent Type
        // dropdown to Internal while this tab is active so it can't be
        // switched away from underneath the (already-forced) data fetch.
        if (agentTypeSelect) {
            const onAnp = state.activeSection === "anp";
            agentTypeSelect.disabled = onAnp;
            agentTypeSelect.title = onAnp ? "ANP Commission is available to internal agents only." : "";
        }

        tableContainer.querySelectorAll(".dynamic-bonus-table-wrapper,.agent-summary-inline-wrapper,.customer-detail-title,.agent-summary-inline-actions").forEach(el => el.remove());

        resetNoDataView();

        if (!state.rawData || !state.rawData.sections) {
            dataTable.classList.add("hidden");
            noDataView.classList.remove("hidden");
            rowCount.textContent = "0 rows";
            totalAgents.textContent = "0";
            totalCustomers.textContent = "0";
            renderSectionTotalCards();
            return;
        }

        const sectionData = state.rawData.sections[state.activeSection];

        if (sectionData && sectionData.error) {
            dataTable.classList.add("hidden");
            noDataView.classList.remove("hidden");
            const heading = noDataView.querySelector("h4");
            const detail = noDataView.querySelector("p");
            if (heading) heading.textContent = "Could Not Load This Section";
            if (detail) detail.textContent = sectionData.error;
            rowCount.textContent = "0 rows";
            totalAgents.textContent = "0";
            totalCustomers.textContent = "0";
            renderSectionTotalCards();
            return;
        }

        if (state.activeSection === "production_bonus") {
            dataTable.classList.add("hidden");
            renderProductionBonus(sectionData);
            return;
        }

        if (state.activeSection === "ega_esa") {
            dataTable.classList.add("hidden");
            renderEgaEsa(sectionData);
            return;
        }

        if (state.activeSection === "monthly_contest") {
            dataTable.classList.add("hidden");
            renderMonthlyContest(sectionData);
            return;
        }

        dataTable.classList.remove("hidden");

        if (!sectionData || !sectionData.headers || !sectionData.rows || sectionData.rows.length === 0) {
            tableHeaders.innerHTML = ""; tableBody.innerHTML = "";
            tableGroupHeaders.innerHTML = ""; tableGroupHeaders.style.display = "none";
            noDataView.classList.remove("hidden");
            rowCount.textContent = "0 rows"; totalAgents.textContent = "0"; totalCustomers.textContent = "0";
            renderSectionTotalCards();
            return;
        }

        let headers = [...sectionData.headers];
        if (state.activeSection === "basic_nfp" && state.specialCaseRowRefs.size === 0) {
            headers = headers.filter(h => h !== "Remarks");
        }

        // Hidden from the Basic & NFP table, but deliberately NOT dropped from
        // `headers`: the Payout Stage filter reads the RM300 and 75% columns to
        // decide which rows a stage pays, the Basic Commission hover reads the
        // 1st Payment Date, and every merge/rowspan pass below is index-based.
        // Removing them from the array would shift all of that; skipping them
        // when the cells are emitted changes only what is drawn.
        const HIDDEN_DETAIL_HEADERS = ["1st payment date", "basic commission (rm300)", "75% payment date"];
        const hiddenDetailCols = new Set(
            state.activeSection !== "basic_nfp" ? [] : headers.reduce((acc, h, i) => {
                if (HIDDEN_DETAIL_HEADERS.includes(String(h).toLowerCase().trim())) acc.push(i);
                return acc;
            }, [])
        );
        const rows = sectionData.rows;
        const isOutsource = state.activeAgentType === "outsource";
        const agentIdx = headers.findIndex(h => h.toLowerCase().trim() === "agent");
        const customerIdx = headers.findIndex(h => h.toLowerCase().trim() === "customer");
        const commissionIdx = headers.findIndex(h => h.toLowerCase().trim() === "commission");
        const commissionPriceIdx = headers.findIndex(h => h.toLowerCase().trim() === "commission price");
        const ganColIdx = headers.findIndex(h => {
            const n = h.toLowerCase().trim();
            return n === "gan lai soon" || n === "gan lai soon (rm)";
        });
        const safwanColIdx = headers.findIndex(h => {
            const n = h.toLowerCase().trim();
            return n === "safwan" || n === "safwan (rm)";
        });
        const overrideColIdx = headers.findIndex(h => h.toLowerCase().includes("override"));
        const totalInvColIdx = getCountColumnIdx(headers);
        const stageIdx = payoutStageIndexes(headers);
        const rm300ColIdx = stageIdx.rm300;
        const payoutStageActive = state.activeSection === "basic_nfp"
            && state.filters.payoutStage !== "all"
            && parseInt(state.activeMonth) >= 7
            && rm300ColIdx !== -1;
        const passesPayoutStage = (rawRow) =>
            rowPassesPayoutStage(rawRow, stageIdx, state.filters.payoutStage);

        // Under "Advance RM 300" the table is the advance payout run, so a Basic
        // Commission row states the advance payable rather than the invoice's
        // full commission. The two only differ when an invoice clears both the
        // 5% and the 75% milestone inside the same month: the full figure is
        // what the month owes, but it is not what the advance run pays. The
        // amount is read off the row's own RM300 cell instead of recomputed --
        // that cell already carries the Data page's advance, which is not always
        // RM 300 (a commission smaller than the advance is capped to itself).
        const advanceOnlyPrice = (rawRow, fullPriceText) => {
            if (commissionIdx !== -1
                && !String(rawRow[commissionIdx] || "").toLowerCase().includes("basic")) return fullPriceText;
            const advanceText = String(rawRow[rm300ColIdx] || "").trim();
            const advance = parseMoneyValue(advanceText);
            const fullPrice = parseMoneyValue(fullPriceText);
            if (!advance || !fullPrice || advance >= fullPrice) return fullPriceText;
            return advanceText;
        };

        let currentAgentName = "", currentCustomerName = "";
        const processedRows = rows.map(row => {
            const rowCopy = [...row];
            if (agentIdx !== -1 && rowCopy[agentIdx] && String(rowCopy[agentIdx]).trim()) currentAgentName = String(rowCopy[agentIdx]).trim();
            if (customerIdx !== -1 && rowCopy[customerIdx] && String(rowCopy[customerIdx]).trim()) currentCustomerName = String(rowCopy[customerIdx]).trim();
            return { rawRow: rowCopy, originalRow: row, fullAgentName: currentAgentName, customerName: currentCustomerName,
                isTotalRow: currentAgentName.toLowerCase().includes("total") || currentAgentName.toLowerCase().includes("summary") || currentAgentName.toLowerCase().includes("grand"),
                isSpecialCase: state.specialCaseRowRefs.has(row) };
        });

        let activeAgents = null;
        if (state.activeSection === "basic_nfp" && state.rawData.sections.agent_summary) {
            activeAgents = new Set();
            const agRows = state.rawData.sections.agent_summary.rows || [];
            let cag = "";
            agRows.forEach(r => { const ag = String(r[0] || "").trim(); if (ag) cag = ag; if (cag) activeAgents.add(cag.toLowerCase()); });
        }

        const userObj = USER_ROLES[state.currentUser];

        // `applySearch` is what separates the two views of the data. The search
        // box narrows the table and the Total Agents / Total Customers counts,
        // but NOT the money cards — those stay on the month's full figures, so
        // searching for one customer can never look like the month's commission
        // total dropped. Permission scoping (filterAgentName) applies to both.
        const passesFilters = (p, applySearch) => {
            if (p.isTotalRow) {
                return userObj && userObj.filterAgentName === null;
            }
            if (applySearch && !matchesSearch(p.fullAgentName, p.customerName)) return false;
            if (applySearch && payoutStageActive && !passesPayoutStage(p.rawRow)) return false;

            // Single agent filtering
            if (userObj && userObj.filterAgentName !== null) {
                if (p.fullAgentName.toLowerCase().trim() !== userObj.filterAgentName.toLowerCase().trim()) {
                    return false;
                }
            }

            if (p.isSpecialCase) return true;
            if (activeAgents !== null && !activeAgents.has(p.fullAgentName.toLowerCase())) return false;
            return true;
        };

        const filteredProcessed = processedRows.filter(p => passesFilters(p, true));
        const unsearchedProcessed = processedRows.filter(p => passesFilters(p, false));

        const visibleAgents = new Set(filteredProcessed.filter(p => !p.isTotalRow && p.fullAgentName).map(p => p.fullAgentName));
        totalAgents.textContent = visibleAgents.size;

        // While a Payout Stage or the search box is narrowing the detail rows,
        // the summary's Commission Price and Count of Customer are restated
        // from what survived; unfiltered, the server rollup stands as-is.
        const summaryFiltersActive = state.activeSection === "basic_nfp"
            && (payoutStageActive || !!state.filters.search);
        // Override money is credited to its RECIPIENT, who is normally not the
        // agent whose row carries it -- searching "Sunny" must still find the
        // RM 85.00 sitting on Zulkarnain's row. So credits are collected from
        // the stage-filtered rows BEFORE the search narrows them, then shown
        // only for the agents actually in view.
        const stageOnlyProcessed = processedRows.filter(p =>
            passesFilters(p, false) && (!payoutStageActive || passesPayoutStage(p.rawRow)));
        const filteredAgentStats = summaryFiltersActive
            ? buildFilteredAgentStats(filteredProcessed, stageOnlyProcessed, headers)
            : null;
        // An agent is in view if they still have detail rows, or if they earn
        // an override and their own name matches the search (an OGM owns no
        // detail rows at all, so only the credit keeps their row alive).
        // Per-agent Remark roll-up for the summary table. Read off the payload's
        // own headers rather than the local copy, which drops "Remarks" when no
        // special case is present -- the row arrays keep the column either way.
        const agentRemarks = new Map();
        const payloadHeaders = state.rawData?.sections?.basic_nfp?.headers || headers;
        const remarksIdx = payloadHeaders.findIndex(h => String(h).toLowerCase().trim() === "remarks");
        if (remarksIdx !== -1) {
            filteredProcessed.forEach(p => {
                if (p.isTotalRow) return;
                const text = String(p.rawRow[remarksIdx] || "").trim();
                if (!text || text === "-") return;
                const key = String(p.fullAgentName).toLowerCase().trim();
                if (!agentRemarks.has(key)) agentRemarks.set(key, new Set());
                agentRemarks.get(key).add(text);
            });
        }

        const agentsInView = new Set([...visibleAgents].map(a => String(a).toLowerCase().trim()));
        if (filteredAgentStats) {
            filteredAgentStats.forEach((s, name) => {
                if (s.ownRows === 0 && s.other !== 0 && matchesSearch(name, "")) agentsInView.add(name);
            });
        }

        const visibleCustomers = new Set();
        if (customerIdx !== -1) {
            filteredProcessed.forEach(p => {
                if (p.isTotalRow) return;
                const custName = p.customerName;
                if (custName && custName !== "-" && !custName.toLowerCase().includes("total") && !custName.toLowerCase().includes("summary")) {
                    visibleCustomers.add(`${p.fullAgentName.toLowerCase()}||${custName.toLowerCase()}`);
                }
            });
        }
        totalCustomers.textContent = visibleCustomers.size;

        if (state.activeSection === "basic_nfp") {
            // Unsearched on purpose — see passesFilters above.
            renderBasicNfpTotalCards(unsearchedProcessed, headers, filteredAgentStats, agentsInView);
        } else {
            renderSectionTotalCards();
        }

        tableHeaders.innerHTML = "";
        tableGroupHeaders.innerHTML = "";

        // "Other Commission" groups OVERRIDE / Safwan / Gan Lai Soon under one
        // merged header, Basic & NFP detail table only. They are adjacent by
        // construction (see basic_nfp_headers in app.py), including after a
        // no-factory-deal month pops Safwan out from between them, so a single
        // left-to-right scan always finds the whole run, whatever survives.
        const OTHER_COMMISSION_GROUP_HEADERS = new Set(["override", "safwan (rm)", "gan lai soon"]);
        const isOtherCommissionHeader = (h) => OTHER_COMMISSION_GROUP_HEADERS.has(String(h).toLowerCase().trim());
        const useGroupedHeader = state.activeSection === "basic_nfp"
            && headers.some((h, ci) => !hiddenDetailCols.has(ci) && isOtherCommissionHeader(h));

        if (useGroupedHeader) {
            tableGroupHeaders.style.display = "";
            let ci = 0;
            while (ci < headers.length) {
                if (hiddenDetailCols.has(ci)) { ci++; continue; }
                const h = headers[ci];
                if (isOtherCommissionHeader(h)) {
                    const runCols = [];
                    while (ci < headers.length) {
                        if (hiddenDetailCols.has(ci)) { ci++; continue; }
                        if (!isOtherCommissionHeader(headers[ci])) break;
                        runCols.push(ci);
                        ci++;
                    }
                    const groupTh = document.createElement("th");
                    groupTh.textContent = "Other Commission";
                    groupTh.colSpan = runCols.length;
                    groupTh.classList.add("group-header");
                    tableGroupHeaders.appendChild(groupTh);
                    runCols.forEach(rc => {
                        const subH = headers[rc];
                        const th = document.createElement("th");
                        th.textContent = subH;
                        if (isNumericHeader(subH)) th.classList.add("numeric");
                        else if (isDateHeader(subH)) th.classList.add("date");
                        tableHeaders.appendChild(th);
                    });
                } else {
                    const th = document.createElement("th");
                    th.textContent = h;
                    th.rowSpan = 2;
                    if (isNumericHeader(h)) th.classList.add("numeric");
                    else if (isDateHeader(h)) th.classList.add("date");
                    tableGroupHeaders.appendChild(th);
                    ci++;
                }
            }
        } else {
            tableGroupHeaders.style.display = "none";
            headers.forEach((h, ci) => {
                if (hiddenDetailCols.has(ci)) return;
                const th = document.createElement("th");
                th.textContent = h;
                if (isNumericHeader(h)) th.classList.add("numeric");
                else if (isDateHeader(h)) th.classList.add("date");
                tableHeaders.appendChild(th);
            });
        }
        tableBody.innerHTML = "";
        noDataView.classList.add("hidden");

        if (state.activeSection === "basic_nfp") {
            const agSummarySection = state.rawData.sections.agent_summary;
            const hasAgentSummary = agSummarySection && agSummarySection.headers && agSummarySection.rows && agSummarySection.rows.length > 0;

            const actionsGroup = document.createElement("div");
            actionsGroup.className = "legend-actions-group";

            const nfpListBtn = document.createElement("button");
            nfpListBtn.className = "btn btn-secondary btn-sm-inline";
            nfpListBtn.innerHTML = '<span class="icon">📋</span> View Net Floor Price List';
            nfpListBtn.addEventListener("click", goToNfpListPage);
            actionsGroup.appendChild(nfpListBtn);

            if (userObj && !userObj.readOnly) {
                const addCustBtn = document.createElement("button");
                addCustBtn.className = "btn btn-primary btn-sm-inline";
                addCustBtn.innerHTML = '<span class="icon">➕</span> Add Special Case Customer';
                // "Adds" already implies the customer is missing from the table,
                // so the reason they are missing is the only thing worth saying.
                addCustBtn.title = "Adds a customer below the payout threshold — creates both commission rows.";
                addCustBtn.addEventListener("click", openAddCustomerSpecialCaseModal);
                actionsGroup.appendChild(addCustBtn);
            }

            if (state.activeAgentType === "all") {
                // Give these their own toolbar directly above the Summary Agent
                // Commission table, instead of tucking them into the legend bar.
                const actionsTarget = document.createElement("div");
                actionsTarget.className = "agent-summary-inline-actions";
                tableContainer.insertBefore(actionsTarget, dataTable);
                actionsTarget.appendChild(actionsGroup);

                if (hasAgentSummary) renderAgentSummaryInline(agSummarySection, isOutsource, visibleAgents, filteredAgentStats, agentRemarks);
            } else {
                if (hasAgentSummary) renderAgentSummaryInline(agSummarySection, isOutsource, visibleAgents, filteredAgentStats, agentRemarks);

                // These used to be tucked into the legend bar when it was on
                // screen; with the legend gone they always get their own
                // toolbar row, the same as the "All Agents" branch above.
                const actionsTarget = document.createElement("div");
                actionsTarget.className = "agent-summary-inline-actions";
                tableContainer.insertBefore(actionsTarget, dataTable);
                actionsTarget.appendChild(actionsGroup);
            }

            // Create and append the title for Table 2 (the details table)
            const detailTitle = document.createElement("div");
            detailTitle.className = "customer-detail-title";
            detailTitle.style.cssText = "margin-top:20px;margin-bottom:12px";
            const detailH4 = document.createElement("h4");
            detailH4.style.cssText = "font-family:'Outfit',sans-serif;font-size:20px;font-weight:700;color:var(--text-main);margin-bottom:4px";
            detailH4.textContent = "Summary Agent by Customer";
            detailTitle.appendChild(detailH4);
            tableContainer.insertBefore(detailTitle, dataTable);

            // One source for the rendered rows, whether or not any special case
            // exists. These used to be two different code paths — a second,
            // looser filter kicked in the moment the first special case was
            // added — so the same customer search produced a different table
            // (and a different insert position for the new rows) before and
            // after. Always the originalRow, never the copy, so .specialCaseData
            // survives and the special-case rendering below still sees it.
            const rowsToRender = filteredProcessed.map(p => p.originalRow);

            const N = rowsToRender.length;
            const agentSpans = new Array(N).fill(1), agentVisible = new Array(N).fill(true);
            const fullAgentNames = [];
            let cur = "";
            rowsToRender.forEach(r => { const v = agentIdx !== -1 && r[agentIdx] ? String(r[agentIdx]).trim() : ""; if (v) cur = v; fullAgentNames.push(cur); });

            if (customerIdx !== -1) {
                const visibleCustomerGroups = new Set();
                rowsToRender.forEach((r, ri) => {
                    const agentName = fullAgentNames[ri] ? String(fullAgentNames[ri]).trim() : "";
                    const rawCustomer = String(r[customerIdx] || "").trim();
                    const specialCustomer = r.specialCaseData && r.specialCaseData.customer ? String(r.specialCaseData.customer).trim() : "";
                    const customerName = rawCustomer && rawCustomer !== "-" ? rawCustomer : specialCustomer;
                    if (!agentName || !customerName) return;
                    if (agentName.toLowerCase().includes("total") || agentName.toLowerCase().includes("summary")) return;
                    if (customerName.toLowerCase().includes("total") || customerName.toLowerCase().includes("summary")) return;
                    visibleCustomerGroups.add(`${agentName.toLowerCase()}||${customerName.toLowerCase()}`);
                });
                totalCustomers.textContent = String(visibleCustomerGroups.size);
            }

            if (agentIdx !== -1) {
                let i = 0;
                while (i < N) { let j = i+1; while (j < N && fullAgentNames[j] === fullAgentNames[i]) j++; agentSpans[i] = j-i; for (let k=i+1;k<j;k++) agentVisible[k]=false; i=j; }
            }
            const totalInvSpans = new Array(N).fill(1), totalInvVisible = new Array(N).fill(true);
            if (totalInvColIdx !== -1) {
                let si = 0;
                while (si < N) { let sj=si+1; while (sj<N && fullAgentNames[sj]===fullAgentNames[si]) sj++; totalInvSpans[si]=sj-si; for (let k=si+1;k<sj;k++) totalInvVisible[k]=false; si=sj; }
            }
            const cellGrid = [];
            for (let ri = 0; ri < N; ri++) {
                cellGrid.push(rowsToRender[ri].map(val => ({
                    value: val === null || val === undefined || String(val).trim() === "" ? "-" : String(val),
                    rowspan: 1, visible: true
                })));
            }

            const effectiveCustomerNames = [];
            let currentCustomerName = "";
            rowsToRender.forEach((r) => {
                const rawCustomer = customerIdx !== -1 ? String(r[customerIdx] || "").trim() : "";
                const specialCustomer = r.specialCaseData && r.specialCaseData.customer ? String(r.specialCaseData.customer).trim() : "";
                const effectiveCustomer = rawCustomer && rawCustomer !== "-" ? rawCustomer : (specialCustomer || currentCustomerName);
                if (effectiveCustomer) currentCustomerName = effectiveCustomer;
                effectiveCustomerNames.push(currentCustomerName);
            });

            const _pkgCol = headers.findIndex(h => h.toLowerCase().includes("package"));
            const _nfpCol = headers.findIndex(h => h.toLowerCase().trim() === "net floor price" || h.toLowerCase().trim() === "netfloor price");
            const colsToMerge = [...new Set([
                headers.findIndex(h => h.toLowerCase().includes("rm300") || h.toLowerCase().includes("basic commission (rm")),
                headers.findIndex(h => h.toLowerCase().includes("75%") || h.toLowerCase().includes("75 %")),
                headers.findIndex(h => h.toLowerCase().includes("full payment")),
                _pkgCol,
                _nfpCol
            ].filter(c => c !== -1))];

            // Columns that show "-" on the 2nd (NFP) row of each Basic+NFP pair
            const nfpDashCols = [...new Set([
                customerIdx,
                headers.findIndex(h => h.toLowerCase().includes("invoice date")),
                headers.findIndex(h => h.toLowerCase().includes("1st payment")),
                headers.findIndex(h => h.toLowerCase().includes("full payment")),
                headers.findIndex(h => h.toLowerCase().trim() === "system price"),
                headers.findIndex(h => h.toLowerCase().trim() === "sales price" || h.toLowerCase().trim() === "total amount")
            ].filter(c => c !== -1))];

            let i = 0;
            while (i < N) {
                let j = i + 1;
                while (j < N && fullAgentNames[j] === fullAgentNames[i] && effectiveCustomerNames[j] === effectiveCustomerNames[i]) j++;
                colsToMerge.forEach(col => {
                    if (col < headers.length) {
                        let r = i;
                        while (r < j) {
                            let next_r = r + 1;
                            if (col === _pkgCol) {
                                while (next_r < j) {
                                    const cv = cellGrid[r][col].value, nv = cellGrid[next_r][col].value;
                                    const isCvEmpty = (cv === "-" || !cv || cv.trim() === "");
                                    const isNvEmpty = (nv === "-" || !nv || nv.trim() === "");
                                    const cvNorm = String(cv || "").trim().toLowerCase();
                                    const nvNorm = String(nv || "").trim().toLowerCase();
                                    if ((!isCvEmpty && cvNorm === nvNorm) || (isNvEmpty && !isCvEmpty)) next_r++;
                                    else break;
                                }
                            } else {
                                while (next_r < j && cellGrid[next_r][col].value === cellGrid[r][col].value && cellGrid[r][col].value !== "-") next_r++;
                            }
                            if (next_r - r > 1) { cellGrid[r][col].rowspan = next_r-r; for (let k=r+1;k<next_r;k++) cellGrid[k][col].visible=false; }
                            r = next_r;
                        }
                    }
                });
                i = j;
            }

            // Pass: blank out repeated fields on NFP rows with "-"
            if (commissionIdx !== -1) {
                for (let ri = 0; ri < N; ri++) {
                    const commVal = String(rowsToRender[ri][commissionIdx] || "").toLowerCase().trim();
                    const isNfpRow = commVal.includes("net floor price") && !commVal.includes("new");
                    if (isNfpRow) {
                        nfpDashCols.forEach(col => {
                            if (col < (cellGrid[ri] || []).length) {
                                cellGrid[ri][col].value = "-";
                                cellGrid[ri][col].rowspan = 1;
                                if (cellGrid[ri][col].visible !== false) {
                                    cellGrid[ri][col].visible = true;
                                }
                            }
                        });
                    }
                }
            }

            // Build a lookup: for each original NFP row, find its special case NFP price
            const nfpSpecialPriceMap = new Map();
            state.specialCasePairs.forEach(pair => {
                // A case raised from the Basic row only has no NFP row, so there
                // is no revised NFP price to stack under the original.
                const nfpRow = pair.find(r => r && r.specialCaseData
                    && commissionIdx !== -1
                    && String(r[commissionIdx] || "").toLowerCase().includes("net floor"));
                if (!nfpRow) return;
                const scData = nfpRow.specialCaseData;
                if (!scData) return;
                const scNfpPrice = nfpRow[commissionPriceIdx];
                // Find the original NFP row for this agent+customer
                const agent = scData.agent;
                const customer = scData.customer;
                for (let ri = 0; ri < N; ri++) {
                    const r = rowsToRender[ri];
                    const an = fullAgentNames[ri];
                    const cust = customerIdx !== -1 ? String(r[customerIdx] || "").trim() : "";
                    const commVal = commissionIdx !== -1 ? String(r[commissionIdx] || "").toLowerCase().trim() : "";
                    if (an.toLowerCase() === agent.toLowerCase() && cust.toLowerCase() === customer.toLowerCase() && commVal.includes("net floor price") && !commVal.includes("new")) {
                        nfpSpecialPriceMap.set(r, scNfpPrice);
                    }
                }
            });

            const remarksIdx = headers.findIndex(h => h.toLowerCase().trim() === "remarks");
            const invoiceDateIdx = headers.findIndex(h => h.toLowerCase().includes("invoice date"));
            const firstPaymentIdx = headers.findIndex(h => h.toLowerCase().includes("1st payment"));
            const fullPaymentIdx = headers.findIndex(h => h.toLowerCase().includes("full payment"));
            const systemPriceIdx = headers.findIndex(h => h.toLowerCase().trim() === "system price");
            const nfpIdx = headers.findIndex(h => h.toLowerCase().trim() === "net floor price" || h.toLowerCase().trim() === "netfloor price");
            const salesPriceIdx = headers.findIndex(h => h.toLowerCase().trim() === "sales price" || h.toLowerCase().trim() === "total amount");
            const referralFeeIdx = headers.findIndex(h => h.toLowerCase().trim() === "referral fee");
            const specialCaseMergeCols = [...new Set([
                customerIdx,
                invoiceDateIdx,
                firstPaymentIdx,
                fullPaymentIdx,
                headers.findIndex(h => h.toLowerCase().includes("rm300") || h.toLowerCase().includes("basic commission (rm")),
                headers.findIndex(h => h.toLowerCase().includes("75%") || h.toLowerCase().includes("75 %")),
                headers.findIndex(h => h.toLowerCase().includes("package")),
                systemPriceIdx,
                nfpIdx,
                salesPriceIdx
            ].filter(c => c !== -1))];

            // effectiveCustomerNames defined above

            const specialCaseGroupAnchorByRowIndex = new Array(N).fill(-1);
            const specialCaseGroupSpanByAnchor = new Map();
            const specialCaseGroupNfpByAnchor = new Map();
            let groupStart = 0;
            while (groupStart < N) {
                const groupCustomer = effectiveCustomerNames[groupStart];
                const groupAgent = fullAgentNames[groupStart];
                if (!groupCustomer) {
                    specialCaseGroupAnchorByRowIndex[groupStart] = groupStart;
                    specialCaseGroupSpanByAnchor.set(groupStart, 1);
                    groupStart++;
                    continue;
                }

                let groupEnd = groupStart + 1;
                while (
                    groupEnd < N &&
                    effectiveCustomerNames[groupEnd] === groupCustomer &&
                    fullAgentNames[groupEnd] === groupAgent
                ) {
                    groupEnd++;
                }

                for (let idx = groupStart; idx < groupEnd; idx++) {
                    specialCaseGroupAnchorByRowIndex[idx] = groupStart;
                }
                specialCaseGroupSpanByAnchor.set(groupStart, groupEnd - groupStart);

                for (let idx = groupStart; idx < groupEnd; idx++) {
                    const row = rowsToRender[idx];
                    if (!row || !row.specialCaseData || nfpIdx === -1) continue;
                    const rawNfp = row.specialCaseData.nfp;
                    if (rawNfp === undefined || rawNfp === null || String(rawNfp).trim() === "") continue;
                    const parsedNfp = Number(rawNfp);
                    if (parsedNfp === 0) continue;
                    const newNfpVal = Number.isFinite(parsedNfp)
                        ? `RM ${parsedNfp.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`
                        : String(rawNfp);
                    specialCaseGroupNfpByAnchor.set(groupStart, newNfpVal);
                    break;
                }

                groupStart = groupEnd;
            }

            for (let ri = 0; ri < N; ri++) {
                const row = rowsToRender[ri];
                const agentName = fullAgentNames[ri];
                const custName = customerIdx !== -1 ? String(row[customerIdx] || "").trim() : "";
                const rowCommType = commissionIdx !== -1 ? String(row[commissionIdx] || "").trim() : "";
                const isTotalRow = agentName.toLowerCase().includes("total") || agentName.toLowerCase().includes("grand");
                const rowClass = getRowClass(agentName, isOutsource);
                // A Gan-only case restates neither commission, so its row is not
                // dressed as a special case: the red "New Basic Commission"
                // treatment would claim a change that was never made. It still
                // counts as a special case for editing, which is what carries the
                // saved rate back into the modal.
                const isGanOnlyCase = !!row.specialCaseData
                    && String(row.specialCaseData.rowKind || "").toLowerCase() === "gan";
                const isSpecialRow = state.specialCaseRowRefs.has(row) && !isGanOnlyCase;
                const tr = document.createElement("tr");
                if (rowClass) tr.className = rowClass;
                if (isSpecialRow) tr.classList.add("special-case-row");
                const groupAnchorIndex = specialCaseGroupAnchorByRowIndex[ri];
                const isGroupAnchor = groupAnchorIndex === ri;
                const isGroupFollower = groupAnchorIndex !== ri;

                for (let ci = 0; ci < headers.length; ci++) {
                    const cellVal = row[ci];
                    if (hiddenDetailCols.has(ci)) continue;
                    if (ci === agentIdx && !agentVisible[ri]) continue;
                    if (ci === totalInvColIdx && !totalInvVisible[ri]) continue;
                    if (colsToMerge.includes(ci) && !cellGrid[ri][ci].visible) continue;
                    if (isGroupFollower && specialCaseMergeCols.includes(ci)) continue;
                    const td = document.createElement("td");
                    let displayVal = cellGrid[ri][ci] ? cellGrid[ri][ci].value : (cellVal === null || cellVal === undefined ? "-" : String(cellVal));
                    if (payoutStageActive && state.filters.payoutStage === "advance" && ci === commissionPriceIdx) {
                        displayVal = advanceOnlyPrice(row, displayVal);
                    } else if (payoutStageActive && state.filters.payoutStage === "balance" && ci === commissionPriceIdx) {
                        // Same reasoning as advanceOnlyPrice, from the other end:
                        // when an invoice clears both milestones in one month the
                        // balance run owes the commission LESS the advance it
                        // already paid, not the whole figure.
                        const remainder = balanceRemainderValue(row, stageIdx, parseMoneyValue(displayVal));
                        if (remainder !== parseMoneyValue(displayVal)) displayVal = formatRM(remainder);
                    }
                    td.innerHTML = displayVal;
                    if (ci === agentIdx && displayVal !== "-") {
                        td.textContent = resolveAgentName(displayVal);
                        // The agent cell is merged down every row it owns, so
                        // the hover reads the invoice dates of all of them --
                        // one row's date would describe a single customer while
                        // the cell itself covers several, and they can straddle
                        // a role change.
                        let spannedDates = "";
                        if (invoiceDateIdx !== -1) {
                            const span = agentSpans[ri] > 1 ? agentSpans[ri] : 1;
                            for (let k = ri; k < Math.min(ri + span, N); k++) {
                                spannedDates += " " + String(rowsToRender[k][invoiceDateIdx] || "");
                            }
                        }
                        attachAgentRoleHover(td, agentName, spannedDates);
                    }
                    if (ci === agentIdx && agentSpans[ri] > 1) td.rowSpan = agentSpans[ri];
                    if (ci === totalInvColIdx && totalInvVisible[ri] && totalInvSpans[ri] > 1) td.rowSpan = totalInvSpans[ri];
                    if (colsToMerge.includes(ci) && cellGrid[ri][ci] && cellGrid[ri][ci].rowspan > 1) td.rowSpan = cellGrid[ri][ci].rowspan;
                    if (isGroupAnchor && specialCaseMergeCols.includes(ci)) td.rowSpan = specialCaseGroupSpanByAnchor.get(ri) || 1;
                    const colHeader = headers[ci];
                    if (isNumericHeader(colHeader)) td.classList.add("numeric");
                    else if (isDateHeader(colHeader)) td.classList.add("date");
                    if (ci === overrideColIdx && displayVal !== "-") { td.style.fontWeight = "600"; td.style.color = "#1d4ed8"; }

                    if (ci === customerIdx && !isTotalRow) {
                        const sysInfo = getCustomerSystemTooltip(custName);
                        if (sysInfo) {
                            td.addEventListener("mouseenter", (e) => showCommCalcTooltip(e, sysInfo));
                            td.addEventListener("mousemove", (e) => moveCommCalcTooltip(e));
                            td.addEventListener("mouseleave", hideCommCalcTooltip);
                        }
                    }

                    if (ci === overrideColIdx && !isTotalRow && displayVal !== "-") {
                        const ovrInfo = getOverrideBreakdownTooltip(row, headers, custName, displayVal);
                        if (ovrInfo) {
                            td.addEventListener("mouseenter", (e) => showCommCalcTooltip(e, ovrInfo));
                            td.addEventListener("mousemove", (e) => moveCommCalcTooltip(e));
                            td.addEventListener("mouseleave", hideCommCalcTooltip);
                        }
                    }

                    if (isGroupAnchor && ci === nfpIdx) {
                        const secondaryValue = specialCaseGroupNfpByAnchor.get(ri);
                        if (secondaryValue && secondaryValue !== "-") {
                            td.innerHTML = `
                                <div class="special-case-merge-stack">
                                    <span class="special-case-primary-value">${displayVal}</span>
                                    <span class="special-case-secondary-value">${secondaryValue}</span>
                                </div>
                            `;
                            td.classList.add("special-case-merge-cell");
                        } else {
                            td.innerHTML = displayVal;
                        }
                    }



                    // Item 4: Red remark TEXT for special case rows. The cell's
                    // background is left to the row — it used to be forced to
                    // the row's tier colour in JS, but with the tier tint gone
                    // there is nothing left to match, and inheriting keeps it
                    // level with its neighbours whether or not there's a remark.
                    if (isSpecialRow && remarksIdx !== -1 && ci === remarksIdx) {
                        td.textContent = (row.specialCaseData && row.specialCaseData.remarks) ? String(row.specialCaseData.remarks) : displayVal;
                        td.classList.add("new-commission-value");
                        td.classList.add("special-case-remarks");
                        td.classList.add("special-case-remarks-cell");
                        td.style.whiteSpace = "normal";
                    }

                    if (isSpecialRow && referralFeeIdx !== -1 && ci === referralFeeIdx) {
                        td.classList.add("special-case-referral-fee");
                    }

                    // Special row: red commission price value, edit on click
                    if (isSpecialRow && ci === commissionPriceIdx) {
                        td.classList.add("new-commission-value");
                        if (userObj && !userObj.readOnly && state.activeSection === "basic_nfp" && isEditSpecialCaseEligible(rowCommType)) {
                            td.style.cursor = "pointer";
                            td.title = "Click to edit Special Case";
                            td.classList.add("clickable-special-case");
                            td.addEventListener("click", () => openEditSpecialCaseModal(row));
                        }
                    }

                    // Special row: red commission type label, edit on click
                    if (isSpecialRow && ci === commissionIdx && isEditSpecialCaseEligible(rowCommType)) {
                        td.classList.add("new-commission-value");
                        if (userObj && !userObj.readOnly) {
                            td.style.cursor = "pointer";
                            td.title = "Click to edit Special Case";
                            td.classList.add("clickable-special-case");
                            td.addEventListener("click", () => openEditSpecialCaseModal(row));
                        }
                    }

                    // Factory: brick-orange Insert Profit Sharing / clickable commission price
                    if (!isTotalRow && !isSpecialRow && ci === commissionPriceIdx && isFactoryBasicCommission(row, headers, agentName, custName)) {
                        const factoryDisplay = getFactoryCellDisplay(row, headers, agentName, custName, "agent");
                        if (factoryDisplay) td.innerHTML = factoryDisplay;
                        if (userObj && !userObj.readOnly) {
                            td.style.cursor = "pointer"; td.title = "Click to set Factory profit sharing rate";
                            td.addEventListener("click", () => openFactoryRateModal(row, headers, agentName, custName, "agent"));
                        }
                    }

                    // Factory: Safwan's own separate profit-sharing rate --
                    // independent of the agent's own rate above.
                    if (!isTotalRow && !isSpecialRow && safwanColIdx !== -1 && ci === safwanColIdx && isFactoryBasicCommission(row, headers, agentName, custName)) {
                        const safwanDisplay = getFactoryCellDisplay(row, headers, agentName, custName, "safwan");
                        if (safwanDisplay) td.innerHTML = safwanDisplay;
                        if (userObj && !userObj.readOnly) {
                            td.style.cursor = "pointer"; td.title = "Click to set Safwan's Factory profit sharing rate";
                            td.addEventListener("click", () => openFactoryRateModal(row, headers, agentName, custName, "safwan"));
                        }
                    }

                    // Gan Lai Soon's override cell opens the same special case the
                    // Commission Price cell does, landing on the rate that drives it.
                    // Only on the Basic row: the paired Net Floor Price row never
                    // carries the override and always reads "-". Rows where he earns
                    // nothing (his own invoices, internal agents) show "-" too and are
                    // left alone rather than offering an edit that changes nothing.
                    if (state.activeSection === "basic_nfp" && !isTotalRow
                        && ganColIdx !== -1 && ci === ganColIdx
                        && String(row[ci] || "").trim() !== "-"
                        && String(rowCommType).toLowerCase().includes("basic")
                        && userObj && !userObj.readOnly) {
                        // Edit whenever a case is already stored against this row,
                        // including a Gan-only one. isSpecialRow is deliberately
                        // false for those (they carry no red restatement), so it
                        // cannot be the test here or the saved rate would come
                        // back as an empty box.
                        const hasStoredCase = !!row.specialCaseData;
                        td.style.cursor = "pointer";
                        td.classList.add("clickable-special-case");

                        // Same hover breakdown the Basic and NFP cells give. No
                        // title attribute alongside it: the browser's own tooltip
                        // would surface on top of this one.
                        const ganInfo = getGanOverrideBreakdown(row, headers, custName);
                        if (ganInfo) {
                            td.addEventListener("mouseenter", (e) => showCommCalcTooltip(e, ganInfo));
                            td.addEventListener("mousemove", (e) => moveCommCalcTooltip(e));
                            td.addEventListener("mouseleave", hideCommCalcTooltip);
                        } else {
                            td.title = hasStoredCase
                                ? "Click to edit Gan Lai Soon's override rate"
                                : "Click to set Gan Lai Soon's override rate";
                        }
                        td.addEventListener("click", () => {
                            hideCommCalcTooltip();
                            state.focusGanOverride = true;
                            state.specialCaseGanMode = true;
                            if (hasStoredCase) {
                                openEditSpecialCaseModal(row);
                            } else {
                                openAddSpecialCaseModal(agentName, custName, row);
                            }
                        });
                    }

                    // Add hover breakdown & click handlers for Basic/NFP commission cells
                    if (state.activeSection === "basic_nfp" && !isTotalRow) {
                        if (ci === commissionIdx || ci === commissionPriceIdx) {
                            const isFactoryCell = isFactoryBasicCommission(row, headers, agentName, custName);
                            const calcInfo = getCommissionCalcBreakdown(row, headers, agentName, custName, ri, rowsToRender);
                            if (calcInfo) {
                                td.addEventListener("mouseenter", (e) => showCommCalcTooltip(e, calcInfo));
                                td.addEventListener("mousemove", (e) => moveCommCalcTooltip(e));
                                td.addEventListener("mouseleave", hideCommCalcTooltip);
                            }

                            if (!isFactoryCell && userObj && !userObj.readOnly) {
                                td.style.cursor = "pointer";
                                td.classList.add("clickable-special-case");
                                td.addEventListener("click", () => {
                                    hideCommCalcTooltip();
                                    if (isSpecialRow) {
                                        openEditSpecialCaseModal(row);
                                    } else {
                                        openAddSpecialCaseModal(agentName, custName, row);
                                    }
                                });
                            }
                        }
                    }

                    tr.appendChild(td);
                }
                tableBody.appendChild(tr);
            }
            rowCount.textContent = N + " rows";

        } else {
            let displayedRows = 0;
            let currentAgentName = "";
            rows.forEach(row => {
                const rawAgentName = agentIdx !== -1 && row[agentIdx] ? String(row[agentIdx]).trim() : "";
                if (rawAgentName) currentAgentName = rawAgentName;
                const agentName = currentAgentName;
                const custName  = customerIdx !== -1 && row[customerIdx] ? String(row[customerIdx]).trim() : "";
                if (!matchesSearch(agentName, custName)) return;
                
                // Single agent filtering
                if (userObj && userObj.filterAgentName !== null) {
                    if (agentName.toLowerCase().trim() !== userObj.filterAgentName.toLowerCase().trim()) return;
                }
                
                const tr = document.createElement("tr");
                const rowClass = getRowClass(agentName, isOutsource);
                if (rowClass) tr.className = rowClass;
                const isTotalRow = agentName.toLowerCase().includes("total") || agentName.toLowerCase().includes("grand");

                for (let ci = 0; ci < headers.length; ci++) {
                    const cellVal = row[ci];
                    const td = document.createElement("td");
                    td.innerHTML = cellVal === null || cellVal === undefined ? "-" : String(cellVal);
                    const colHeader = headers[ci];
                    if (isNumericHeader(colHeader)) td.classList.add("numeric");
                    else if (isDateHeader(colHeader)) td.classList.add("date");
                    if (isAgentHeader(colHeader) && cellVal != null && String(cellVal).trim()) {
                        td.textContent = resolveAgentName(cellVal);
                        attachAgentRoleHover(td, agentName, invoiceDateCellOf(row, headers));
                    }

                    // Add hover breakdown & click handlers for Basic/NFP commission cells
                    if (state.activeSection === "basic_nfp" && !isTotalRow) {
                        if (ci === commissionIdx || ci === commissionPriceIdx) {
                            const isFactoryCell = isFactoryBasicCommission(row, headers, agentName, custName);
                            const calcInfo = getCommissionCalcBreakdown(row, headers, agentName, custName, displayedRows, rows);
                            if (calcInfo) {
                                td.addEventListener("mouseenter", (e) => showCommCalcTooltip(e, calcInfo));
                                td.addEventListener("mousemove", (e) => moveCommCalcTooltip(e));
                                td.addEventListener("mouseleave", hideCommCalcTooltip);
                            }

                            if (!isFactoryCell && userObj && !userObj.readOnly) {
                                td.style.cursor = "pointer";
                                td.classList.add("clickable-special-case");
                                td.addEventListener("click", () => {
                                    hideCommCalcTooltip();
                                    openAddSpecialCaseModal(agentName, custName, row);
                                });
                            }
                        }
                    }

                    tr.appendChild(td);
                }
                tableBody.appendChild(tr);
                displayedRows++;
            });
            rowCount.textContent = displayedRows + " rows";
        }
    }

    // -------------------------------------------------------------
    // Production Bonus refunds
    // -------------------------------------------------------------
    // An invoice that collected more than it was worth owes the customer the
    // difference. Small overpayments are rounding, not refunds, so only amounts
    // above this get a remark -- without it the column fills with 20-sen
    // "refunds" nobody will ever make, and the real ones stop standing out.
    // Set so the live data splits where it was agreed to: the RM 5.00 case is a
    // refund, the RM 2.00 / RM 0.20 ones are noise.
    // The bank account and the done tick are per invoice, not per month: the
    // same overpayment shown in two reports is still one refund.
    const PB_REFUND_MIN = 2;
    let pbRefunds = null;          // invoice number -> saved refund; null = not fetched
    let pbRefundsLoading = false;

    function loadPbRefunds(onLoaded) {
        if (pbRefundsLoading) return;
        pbRefundsLoading = true;
        fetch("/api/pb-refunds")
            .then(r => r.json())
            .then(d => { pbRefunds = (d && d.refunds) || {}; })
            .catch(() => { pbRefunds = {}; })
            .finally(() => { pbRefundsLoading = false; if (onLoaded) onLoaded(); });
    }

    function pbOverpayment(headers, row) {
        const ci = headers.findIndex(h => String(h).toLowerCase().trim() === "collected payment");
        const si = headers.findIndex(h => String(h).toLowerCase().trim() === "sales price");
        if (ci === -1 || si === -1) return 0;
        return parseMoneyValue(row[ci]) - parseMoneyValue(row[si]);
    }

    function pbRefundCellHtml(headers, row) {
        const over = pbOverpayment(headers, row);
        if (!(over > PB_REFUND_MIN)) return "-";
        const invIdx = headers.findIndex(h => String(h).toLowerCase().includes("invoice number"));
        const inv = invIdx === -1 ? "" : String(row[invIdx] || "").trim();
        const saved = (pbRefunds && pbRefunds[inv]) || {};
        const bank = String(saved.bank_account || "");
        const done = !!saved.refund_done;
        const stamp = done && saved.done_at
            ? `— ${String(saved.done_at).slice(0, 10)}, ${saved.done_by || ""}`
            : "";
        // The amount is written by the report, never typed: it has to keep
        // agreeing with the two columns it is the difference of.
        return `<div class="pb-refund" data-invoice="${escapeHtml(inv)}" style="display:flex; flex-direction:column; gap:4px; white-space:normal;">
            <div>To refund <b>${formatRM(over)}</b><span class="pb-refund-bank-hint" style="color:#64748b;"></span> to <span class="pb-refund-bank" contenteditable="true"
                  title="Click to enter the account this is refunded to"
                  style="border-bottom:1px dashed #94a3b8; padding:0 2px; cursor:text; ${bank ? "" : "color:#94a3b8; font-style:italic;"}"
                  >${escapeHtml(bank || "[insert Bank Account Number]")}</span></div>
            <label style="display:inline-flex; align-items:center; gap:6px; font-size:12px; color:#475569; cursor:pointer;">
              <input type="checkbox" class="pb-refund-done"${done ? " checked" : ""}> Refund done
              <span class="pb-refund-stamp" style="color:#64748b;">${escapeHtml(stamp)}</span>
            </label>
        </div>`;
    }

    // The Collected Payment cell is a sum. Clicking it opens what it is a sum
    // of, slips included -- which is also the only place a refund's bank and
    // account holder are recorded, since no column in the database holds them.
    function pbPaymentsLinkHtml(headers, row) {
        const ci = headers.findIndex(h => String(h).toLowerCase().trim() === "collected payment");
        const invIdx = headers.findIndex(h => String(h).toLowerCase().includes("invoice number"));
        const custIdx = headers.findIndex(h => String(h).toLowerCase().includes("customer"));
        const val = ci === -1 ? "-" : String(row[ci] ?? "-");
        const inv = invIdx === -1 ? "" : String(row[invIdx] || "").trim();
        if (!inv) return escapeHtml(val);
        const cust = custIdx === -1 ? "" : String(row[custIdx] || "").trim();
        // Reads as an ordinary amount -- no link colour, no underline. The
        // pointer cursor and the tooltip are the whole affordance.
        return `<span class="pb-pay-link" data-invoice="${escapeHtml(inv)}" data-customer="${escapeHtml(cust)}"
            title="Click to see the payments and slips behind this figure"
            style="cursor:pointer; color:inherit;">${escapeHtml(val)}</span>`;
    }

    function paymentSlipsHtml(d) {
        const rows = (d && d.payments) || [];
        if (!rows.length) return `<p style="color:#64748b;">No payments recorded against this invoice.</p>`;
        const items = rows.map(p => {
            const ref = String(p.remark || "").replace(/^\[Ref:\s*/i, "").replace(/\]$/, "").trim();
            const slips = (p.slips || []).map(u =>
                `<a href="${escapeHtml(u)}" target="_blank" rel="noopener noreferrer" title="Open the full slip">
                   <img src="${escapeHtml(u)}" loading="lazy" alt="Payment slip"
                        style="max-width:200px; max-height:160px; border:1px solid var(--border-color); border-radius:6px; display:block;">
                 </a>`).join("")
                || `<span style="color:#94a3b8; font-size:12px;">no slip attached</span>`;
            return `<div style="display:flex; gap:16px; padding:12px 0; border-bottom:1px solid var(--border-color); align-items:flex-start;">
                <div style="flex:1; min-width:0;">
                  <div style="font-weight:700; font-size:15px;">${formatRM(p.amount)}</div>
                  <div style="color:#475569; font-size:13px;">${escapeHtml(p.payment_date || "-")}</div>
                  <div style="color:#475569; font-size:13px;">${escapeHtml(p.payment_method || "-")}${p.issuer_bank ? " · " + escapeHtml(p.issuer_bank) : ""}</div>
                  ${ref && ref.toUpperCase() !== "N/A" ? `<div style="color:#64748b; font-size:12px;">Ref ${escapeHtml(ref)}</div>` : ""}
                </div>
                <div>${slips}</div>
            </div>`;
        }).join("");
        return `<div style="margin-bottom:10px; color:#475569;">
                  <b>${rows.length}</b> payment${rows.length === 1 ? "" : "s"} · total <b>${formatRM(d.total || 0)}</b>
                  <div style="font-size:12px; color:#64748b; margin-top:4px;">
                    A cash bank-in slip shows the account paid <em>into</em>; a cheque or transfer slip
                    shows who paid. Confirm any account with the payer before refunding.
                  </div>
                </div>${items}`;
    }

    function openPaymentSlipsModal(invoiceNumber, customerName) {
        const modal = document.getElementById("paymentSlipsModal");
        const body = document.getElementById("paymentSlipsBody");
        const titleEl = document.getElementById("paymentSlipsTitle");
        if (!modal || !body) return;
        if (titleEl) {
            titleEl.textContent = customerName
                ? `Payments — ${customerName} (Invoice ${invoiceNumber})`
                : `Payments — Invoice ${invoiceNumber}`;
        }
        body.innerHTML = `<p style="color:#64748b;">Loading payments…</p>`;
        modal.classList.remove("hidden");
        fetch(`/api/invoice-payments?invoice_number=${encodeURIComponent(invoiceNumber)}`)
            .then(r => r.json())
            .then(d => { body.innerHTML = paymentSlipsHtml(d); })
            .catch(() => {
                body.innerHTML = `<p style="color:#b91c1c;">Could not load the payments for this invoice.</p>`;
            });
    }

    function wirePbPaymentLinks(root) {
        root.querySelectorAll(".pb-pay-link").forEach(el => {
            el.addEventListener("click", (e) => {
                e.stopPropagation();
                openPaymentSlipsModal(el.getAttribute("data-invoice"),
                                      el.getAttribute("data-customer"));
            });
        });
    }

    // Which bank the money came from, for the refund remark. Free text typed by
    // whoever keyed the payment ("MAYBANK", "MAYBAK", "Ocbc "), so it is offered
    // as a hint and never as the account itself -- and an invoice paid from two
    // banks says so rather than picking one.
    function fillPbRefundBankHints(root) {
        root.querySelectorAll(".pb-refund").forEach(box => {
            const inv = box.getAttribute("data-invoice");
            const hintEl = box.querySelector(".pb-refund-bank-hint");
            if (!inv || !hintEl) return;
            fetch(`/api/invoice-payments?invoice_number=${encodeURIComponent(inv)}`)
                .then(r => r.json())
                .then(d => {
                    const banks = [...new Set((d.payments || [])
                        .map(p => String(p.issuer_bank || "").trim())
                        .filter(Boolean)
                        .map(b => b.toUpperCase()))];
                    hintEl.textContent = banks.length ? ` (paid from ${banks.join(" / ")})` : "";
                })
                .catch(() => {});
        });
    }

    function wirePbRefundCells(root) {
        root.querySelectorAll(".pb-refund").forEach(box => {
            const inv = box.getAttribute("data-invoice");
            const bankEl = box.querySelector(".pb-refund-bank");
            const doneEl = box.querySelector(".pb-refund-done");
            const stampEl = box.querySelector(".pb-refund-stamp");
            const placeholder = "[insert Bank Account Number]";
            const bankValue = () => {
                const t = bankEl ? bankEl.textContent.trim() : "";
                return t === placeholder ? "" : t;
            };
            const save = () => {
                const body = {
                    invoice_number: inv,
                    bank_account: bankValue(),
                    refund_done: doneEl ? doneEl.checked : false
                };
                fetch("/api/pb-refunds", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(body)
                })
                    .then(r => r.json())
                    .then(d => {
                        if (!d || !d.refund) return;
                        pbRefunds = pbRefunds || {};
                        pbRefunds[inv] = d.refund;
                        // Updated in place rather than by redrawing the table:
                        // a redraw here would throw away the row the user is
                        // still working in.
                        if (stampEl) {
                            stampEl.textContent = d.refund.refund_done && d.refund.done_at
                                ? `— ${String(d.refund.done_at).slice(0, 10)}, ${d.refund.done_by || ""}`
                                : "";
                        }
                    })
                    .catch(() => {});
            };
            if (bankEl) {
                bankEl.addEventListener("focus", () => {
                    if (bankEl.textContent.trim() === placeholder) {
                        bankEl.textContent = "";
                        bankEl.style.color = "";
                        bankEl.style.fontStyle = "";
                    }
                });
                bankEl.addEventListener("keydown", (e) => {
                    if (e.key === "Enter") { e.preventDefault(); bankEl.blur(); }
                });
                bankEl.addEventListener("blur", () => {
                    if (!bankEl.textContent.trim()) {
                        bankEl.textContent = placeholder;
                        bankEl.style.color = "#94a3b8";
                        bankEl.style.fontStyle = "italic";
                    }
                    save();
                });
            }
            if (doneEl) doneEl.addEventListener("change", save);
        });
    }

    // -------------------------------------------------------------
    // Render Custom Outsource Production Bonus Layout
    // -------------------------------------------------------------
    function renderProductionBonus(bonusData) {
        if (!bonusData) {
            noDataView.classList.remove("hidden");
            rowCount.textContent = "0 rows";
            totalAgents.textContent = "0";
            totalCustomers.textContent = "0";
            renderSectionTotalCards();
            return;
        }

        // Apply filters to row_details first, then extract matching agents
        const detailHeaders = bonusData.headers_detail || [];
        const detailRows = bonusData.rows_detail || [];
        const agentIdx = detailHeaders.findIndex(h => h.toLowerCase().includes("agent"));
        const customerIdx = detailHeaders.findIndex(h => h.toLowerCase().includes("customer"));

        const userObj = USER_ROLES[state.currentUser];

        const matchingDetailRows = detailRows.filter(row => {
            const agentVal = agentIdx !== -1 && row[agentIdx] ? String(row[agentIdx]) : "";
            const customerVal = customerIdx !== -1 && row[customerIdx] ? String(row[customerIdx]) : "";
            if (userObj && userObj.filterAgentName !== null) {
                if (agentVal.toLowerCase().trim() !== userObj.filterAgentName.toLowerCase().trim()) {
                    return false;
                }
            }
            return matchesSearch(agentVal, customerVal);
        });

        const matchingAgents = new Set(matchingDetailRows.map(row => agentIdx !== -1 && row[agentIdx] ? String(row[agentIdx]).trim().toLowerCase() : ""));

        const searchQuery = state.filters.search;
        const matchesAgentSearch = (agentVal) => {
            if (!searchQuery) return true;
            return agentVal.includes(searchQuery) || matchingAgents.has(agentVal);
        };

        // Filter summary tables by matching agents
        const oumRows = (bonusData.rows_oum || []).filter(row => {
            const agentVal = row[0] ? String(row[0]).trim().toLowerCase() : "";
            if (userObj && userObj.filterAgentName !== null) {
                if (agentVal !== userObj.filterAgentName.toLowerCase().trim()) return false;
            }
            return matchesAgentSearch(agentVal);
        });

        const ogmRows = (bonusData.rows_ogm || []).filter(row => {
            const agentVal = row[0] ? String(row[0]).trim().toLowerCase() : "";
            if (userObj && userObj.filterAgentName !== null) {
                if (agentVal !== userObj.filterAgentName.toLowerCase().trim()) return false;
            }
            return matchesAgentSearch(agentVal);
        });

        // Compute dynamic KPI metrics for production bonus
        const visibleAgents = new Set();
        const visibleCustomers = new Set();

        oumRows.forEach(row => {
            const agName = row[0] ? String(row[0]).trim() : "";
            if (agName && !agName.toLowerCase().includes("total") && !agName.toLowerCase().includes("summary")) {
                visibleAgents.add(agName);
            }
        });

        ogmRows.forEach(row => {
            const agName = row[0] ? String(row[0]).trim() : "";
            if (agName && !agName.toLowerCase().includes("total") && !agName.toLowerCase().includes("summary")) {
                visibleAgents.add(agName);
            }
        });

        matchingDetailRows.forEach(row => {
            const agName = agentIdx !== -1 && row[agentIdx] ? String(row[agentIdx]).trim() : "";
            const custName = customerIdx !== -1 && row[customerIdx] ? String(row[customerIdx]).trim() : "";
            if (agName && !agName.toLowerCase().includes("total") && !agName.toLowerCase().includes("summary")) {
                visibleAgents.add(agName);
            }
            if (custName && !custName.toLowerCase().includes("total") && !custName.toLowerCase().includes("summary")) {
                visibleCustomers.add(custName);
            }
        });

        totalAgents.textContent = visibleAgents.size;
        totalCustomers.textContent = visibleCustomers.size;
        renderSectionTotalCards();

        // ── Inline Agent Summary sub-table (Basic & NFP Details only) ──────────
        if (state.activeSection === "basic_nfp") {
            const agentSummarySection = state.rawData.sections.agent_summary;
            if (agentSummarySection && agentSummarySection.rows && agentSummarySection.rows.length > 0) {
                renderAgentSummaryInline(agentSummarySection, true, visibleAgents);
            }
        }

        let totalDisplayedRows = oumRows.length + ogmRows.length + matchingDetailRows.length;
        rowCount.textContent = `${totalDisplayedRows} total rows`;

        if (totalDisplayedRows === 0) {
            noDataView.classList.remove("hidden");
            return;
        } else {
            noDataView.classList.add("hidden");
        }

        // Build wrapper
        const wrapper = document.createElement("div");
        wrapper.className = "dynamic-bonus-table-wrapper";
        wrapper.style.display = "flex";
        wrapper.style.flexDirection = "column";
        wrapper.style.gap = "28px";
        wrapper.style.padding = "24px";

        // Table 1: OUM Production Bonus Summary
        if (oumRows.length > 0) {
            wrapper.appendChild(createBonusSubTable("Production Bonus Summary", bonusData.headers_oum, oumRows));
        }

        // Table 2: OGM Production Bonus Summary
        if (ogmRows.length > 0) {
            wrapper.appendChild(createBonusSubTable("OGM Production Bonus Summary", bonusData.headers_ogm, ogmRows));
        }

        // Table 3: Team Sales Details, with a Remarks column for the invoices
        // that collected more than they were worth.
        if (matchingDetailRows.length > 0) {
            if (pbRefunds === null) loadPbRefunds(() => renderActiveSection());
            const detailHeadersWithRemarks = detailHeaders.concat(["Remarks"]);
            const collectedIdx = detailHeaders.findIndex(
                h => String(h).toLowerCase().trim() === "collected payment");
            const detailRowsWithRemarks = matchingDetailRows.map(r => {
                const out = r.slice();
                if (collectedIdx !== -1) out[collectedIdx] = pbPaymentsLinkHtml(detailHeaders, r);
                return out.concat([pbRefundCellHtml(detailHeaders, r)]);
            });
            const detailTable = createBonusSubTable(
                "Production Bonus Summary by customer", detailHeadersWithRemarks, detailRowsWithRemarks);
            wirePbRefundCells(detailTable);
            wirePbPaymentLinks(detailTable);
            fillPbRefundBankHints(detailTable);
            wrapper.appendChild(detailTable);
        }

        tableContainer.appendChild(wrapper);
    }

    // Which team's agent breakdown is open on the Monthly Contest tab, or null
    // when none is. Table 3 is hidden until a team name in table 1 is clicked.
    let contestSelectedTeam = null;

    // Table 1 names a team "Brazil Team" while table 3 says just "Brazil", so
    // the two are only comparable once the suffix is dropped.
    function contestTeamKey(val) {
        return String(val || "").trim().toLowerCase().replace(/\s+team$/, "");
    }

    function renderMonthlyContest(contestData) {
        if (!contestData) {
            noDataView.classList.remove("hidden");
            rowCount.textContent = "0 rows";
            totalAgents.textContent = "0";
            totalCustomers.textContent = "0";
            renderSectionTotalCards();
            return;
        }

        const t3Headers = contestData.headers_t3 || [];
        const t3Rows = contestData.rows_t3 || [];
        const agentIdx = t3Headers.findIndex(h => h.toLowerCase().includes("agent"));
        const customerIdx = t3Headers.findIndex(h => h.toLowerCase().includes("customer"));

        const userObj = USER_ROLES[state.currentUser];

        // Table 1 is per team now and carries no agent column, so there are no
        // captain asterisks to harvest. Kept as an empty set because the shared
        // sub-table renderer still takes it.
        const captains = new Set();

        const matchingT3Rows = t3Rows.filter(row => {
            const agentVal = agentIdx !== -1 && row[agentIdx] ? String(row[agentIdx]).replace("*", "").trim() : "";
            const customerVal = customerIdx !== -1 && row[customerIdx] ? String(row[customerIdx]) : "";
            if (userObj && userObj.filterAgentName !== null) {
                if (agentVal.toLowerCase().trim() !== userObj.filterAgentName.toLowerCase().trim()) {
                    return false;
                }
            }
            return matchesSearch(agentVal, customerVal);
        });

        const matchingAgents = new Set(matchingT3Rows.map(row => agentIdx !== -1 && row[agentIdx] ? String(row[agentIdx]).replace("*", "").trim().toLowerCase() : ""));

        const searchQuery = state.filters.search;
        const matchesAgentSearch = (agentVal) => {
            if (!searchQuery) return true;
            const cleanAgent = agentVal.replace(" *", "").trim().toLowerCase();
            return cleanAgent.includes(searchQuery.toLowerCase().trim()) || matchingAgents.has(cleanAgent);
        };

        // Table 1 is one row per team, so it is filtered by the teams the
        // visible agents belong to rather than by agent name. Team Name is the
        // first column; table 3 supplies each agent's team.
        const teamIdx = t3Headers.findIndex(h => h.toLowerCase().includes("team"));
        const visibleTeams = new Set(
            teamIdx === -1 ? [] : matchingT3Rows.map(row => contestTeamKey(row[teamIdx]))
        );
        const t1Restricted = !!(userObj && userObj.filterAgentName !== null) || !!state.filters.search;
        const t1Rows = (contestData.rows_t1 || []).filter(row => {
            if (!t1Restricted) return true;
            return visibleTeams.has(contestTeamKey(row[0]));
        });

        const visibleAgents = new Set();
        const visibleCustomers = new Set();

        matchingT3Rows.forEach(row => {
            const agName = agentIdx !== -1 && row[agentIdx] ? String(row[agentIdx]).replace("*", "").trim() : "";
            const custName = customerIdx !== -1 && row[customerIdx] ? String(row[customerIdx]).trim() : "";
            if (agName && !agName.toLowerCase().includes("total")) visibleAgents.add(agName);
            if (custName && !custName.toLowerCase().includes("total")) visibleCustomers.add(custName);
        });

        totalAgents.textContent = visibleAgents.size;
        totalCustomers.textContent = visibleCustomers.size;
        renderSectionTotalCards();

        let totalDisplayedRows = t1Rows.length + matchingT3Rows.length;
        rowCount.textContent = `${totalDisplayedRows} total rows`;

        if (totalDisplayedRows === 0) {
            noDataView.classList.remove("hidden");
            return;
        } else {
            noDataView.classList.add("hidden");
        }

        const wrapper = document.createElement("div");
        wrapper.className = "dynamic-bonus-table-wrapper";
        wrapper.style.display = "flex";
        wrapper.style.flexDirection = "column";
        wrapper.style.gap = "28px";
        wrapper.style.padding = "24px";

        // The agent breakdown is a drill-down of table 1: it stays hidden until
        // a team name is clicked, then shows only that team's rows. Without a
        // team column to filter on there is nothing to drill into, so table 3
        // is shown whole as before.
        const t1Headers = contestData.headers_t1 || [];
        const drillDown = teamIdx !== -1 && t1Rows.length > 0;
        const t1TeamIdx = Math.max(0, t1Headers.findIndex(h => /team\s*name/i.test(h)));

        // A team picked before a month/filter change may no longer be listed.
        if (contestSelectedTeam && !t1Rows.some(r => contestTeamKey(r[t1TeamIdx]) === contestTeamKey(contestSelectedTeam))) {
            contestSelectedTeam = null;
        }

        const summaryHost = document.createElement("div");
        const teamCells = [];

        const renderContestSummary = () => {
            summaryHost.innerHTML = "";
            teamCells.forEach(({ cell, name }) => {
                const on = contestSelectedTeam !== null && contestTeamKey(name) === contestTeamKey(contestSelectedTeam);
                cell.style.textDecoration = on ? "underline" : "";
                cell.style.color = on ? "#2563eb" : "";
                cell.style.fontWeight = on ? "700" : "";
            });

            const rows = !drillDown
                ? matchingT3Rows
                : (contestSelectedTeam === null
                    ? []
                    : matchingT3Rows.filter(r => contestTeamKey(r[teamIdx]) === contestTeamKey(contestSelectedTeam)));

            rowCount.textContent = `${t1Rows.length + rows.length} total rows`;

            if (drillDown && contestSelectedTeam === null) return;

            if (rows.length === 0) {
                const empty = document.createElement("div");
                empty.style.fontSize = "16px";
                empty.style.color = "#64748b";
                empty.textContent = `No cases recorded for ${contestSelectedTeam} this month.`;
                summaryHost.appendChild(empty);
                return;
            }

            const title = drillDown
                ? `Summary of Cases and Awards by Agent — ${contestSelectedTeam}`
                : "Summary of Cases and Awards by Agent";
            const t3Table = createBonusSubTable(title, t3Headers, rows, captains);

            if (drillDown) {
                const clear = document.createElement("button");
                clear.type = "button";
                clear.className = "btn-secondary";
                clear.textContent = "✕ Hide breakdown";
                clear.style.alignSelf = "flex-start";
                clear.addEventListener("click", () => {
                    contestSelectedTeam = null;
                    renderContestSummary();
                });
                t3Table.appendChild(clear);
            }
            summaryHost.appendChild(t3Table);
        };

        if (t1Rows.length > 0) {
            const t1Table = createBonusSubTable("Team Championship and Team Achievement Bonus", t1Headers, t1Rows, captains);
            if (drillDown) {
                t1Table.querySelectorAll("tbody tr").forEach((tr, idx) => {
                    const name = String((t1Rows[idx] || [])[t1TeamIdx] || "").trim();
                    const cell = tr.children[t1TeamIdx];
                    if (!cell || !name || /total|grand|summary/i.test(name)) return;
                    cell.style.cursor = "pointer";
                    cell.title = `Show ${name}'s agent breakdown`;
                    cell.addEventListener("click", () => {
                        // Clicking the open team again closes the breakdown.
                        contestSelectedTeam = contestTeamKey(contestSelectedTeam) === contestTeamKey(name) ? null : name;
                        renderContestSummary();
                    });
                    teamCells.push({ cell, name });
                });
            }
            wrapper.appendChild(t1Table);
        }

        wrapper.appendChild(summaryHost);
        renderContestSummary();

        tableContainer.appendChild(wrapper);
    }

    function createBonusSubTable(title, headers, rows, captains = new Set()) {
        const subWrapper = document.createElement("div");
        subWrapper.className = "bonus-sub-section";
        subWrapper.style.display = "flex";
        subWrapper.style.flexDirection = "column";
        subWrapper.style.gap = "12px";

        const h4 = document.createElement("h4");
        h4.textContent = title;
        h4.style.fontFamily = "'Outfit', sans-serif";
        h4.style.fontSize = "19px";
        h4.style.fontWeight = "700";
        h4.style.color = "#1e293b";
        subWrapper.appendChild(h4);

        // Team Championship is per team now, so only the rank badges need
        // explaining there; the captain highlight belongs with the table that
        // actually names agents.
        if (title.includes("Team Championship")) {
            const legend = document.createElement("div");
            legend.style.fontSize = "16px";
            legend.style.color = "#64748b";
            legend.style.marginBottom = "8px";
            legend.innerHTML = `<em>Gold, Silver, Bronze badges indicate Rank 1, 2, and 3 respectively. Click a team name to see that team's agent breakdown.</em>`;
            subWrapper.appendChild(legend);
        }

        if (title.includes("Summary of Cases and Awards")) {
            const legend = document.createElement("div");
            legend.style.fontSize = "16px";
            legend.style.color = "#64748b";
            legend.style.marginBottom = "8px";
            legend.style.display = "flex";
            legend.style.alignItems = "center";
            legend.style.gap = "8px";
            legend.innerHTML = `
                <span class="legend-dot" style="background-color: #FEF3C7; border: 1px solid #f59e0b; width: 14px; height: 14px; border-radius: 4px; display: inline-block;"></span>
                <span style="font-weight: 500; color: var(--text-main);">Team Captain (highlighted in gold)</span>
            `;
            subWrapper.appendChild(legend);
        }

        const table = document.createElement("table");
        table.className = "dashboard-table";

        // Head
        const thead = document.createElement("thead");
        const trHead = document.createElement("tr");
        headers.forEach(h => {
            const th = document.createElement("th");
            th.textContent = h;
            if (isNumericHeader(h)) th.classList.add("numeric");
            else if (isDateHeader(h)) th.classList.add("date");
            trHead.appendChild(th);
        });
        thead.appendChild(trHead);
        table.appendChild(thead);

        // Body
        const tbody = document.createElement("tbody");
        const agentIdx = headers.findIndex(h => h.toLowerCase().includes("agent"));
        const isOutsource = state.activeAgentType === "outsource";

        // Fast Start is won once per agent, not per case, so repeating it on
        // every one of an agent's case rows reads as several separate awards.
        // Merge it vertically instead. Only runs of ADJACENT rows are merged, so
        // an agent split across the table is never joined across other agents.
        let mergeColIdx = -1;
        let mergeSpans = null;
        if (title.includes("Summary of Cases and Awards")) {
            const fsIdx = headers.findIndex(h => h.toLowerCase().includes("fast start"));
            if (fsIdx !== -1 && agentIdx !== -1) {
                mergeColIdx = fsIdx;
                mergeSpans = new Array(rows.length).fill(0);
                const nameAt = (i) => String(rows[i][agentIdx] ?? "").replace("*", "").trim().toLowerCase();
                let i = 0;
                while (i < rows.length) {
                    let j = i + 1;
                    while (j < rows.length && nameAt(j) === nameAt(i)) j++;
                    mergeSpans[i] = j - i;   // 0 on the rows the first cell covers
                    i = j;
                }
            }
        }

        rows.forEach((row, rowIdx) => {
            const trRow = document.createElement("tr");
            const agentName = agentIdx !== -1 && row[agentIdx] ? String(row[agentIdx]).trim() : (row[0] ? String(row[0]).trim() : "");
            
            let rowClass = "";
            const cleanAgentName = agentName.replace("*", "").trim().toLowerCase();
            if (captains.has(cleanAgentName) || agentName.includes("*")) {
                rowClass = "captain-bg";
            } else if (cleanAgentName.includes("total") || cleanAgentName.includes("summary") || cleanAgentName.toLowerCase().includes("grand")) {
                rowClass = "total-bg";
            } else if (title.includes("OGM")) {
                rowClass = "ogm-bg";
            } else if (title.includes("OUM")) {
                rowClass = "oum-bg";
            } else {
                rowClass = getRowClass(agentName, isOutsource);
            }
            if (rowClass) {
                trRow.className = rowClass;
            }
            
            row.forEach((cellVal, cellIdx) => {
                const td = document.createElement("td");
                const colHeader = headers[cellIdx];
                
                let displayVal = cellVal === null || cellVal === undefined ? "-" : String(cellVal);
                
                // Remove "*" at Captain name
                if (colHeader.toLowerCase().includes("agent") && displayVal.includes("*")) {
                    displayVal = displayVal.replace("*", "").trim();
                }
                
                // Format Rank column in Table 1 with medals
                if (title.includes("Team Championship") && colHeader.toLowerCase() === "rank") {
                    const cleanVal = displayVal.replace("🥇", "").replace("🥈", "").replace("🥉", "").trim();
                    if (cleanVal === "1") {
                        displayVal = `<span class="medal-badge gold">🥇 1st</span>`;
                    } else if (cleanVal === "2") {
                        displayVal = `<span class="medal-badge silver">🥈 2nd</span>`;
                    } else if (cleanVal === "3") {
                        displayVal = `<span class="medal-badge bronze">🥉 3rd</span>`;
                    }
                }
                
                // Golden Boot placings get the same medal badge as the Rank
                // column, with the award amount left alongside it.
                if (colHeader.toLowerCase().includes("golden boot")) {
                    const medal = { "🥇": "gold", "🥈": "silver", "🥉": "bronze" };
                    const m = displayVal.trim().match(/^(🥇|🥈|🥉)\s*(1st|2nd|3rd)\s*(.*)$/);
                    if (m) {
                        const amount = m[3].trim();
                        displayVal = `<span class="medal-badge ${medal[m[1]]}">${m[1]} ${m[2]}</span>`
                                   + (amount ? ` ${amount}` : "");
                    }
                }

                // Add flag next to team name in Table 1
                if (title.includes("Team Championship") && colHeader.toLowerCase().includes("team")) {
                    const teamLower = displayVal.toLowerCase();
                    let code = "";
                    if (teamLower.includes("brazil")) code = "br";
                    else if (teamLower.includes("germany")) code = "de";
                    else if (teamLower.includes("england")) code = "gb-eng";
                    else if (teamLower.includes("france")) code = "fr";
                    else if (teamLower.includes("spain")) code = "es";
                    else if (teamLower.includes("portugal")) code = "pt";
                    
                    if (code) {
                        displayVal = `<img src="https://flagcdn.com/w40/${code}.png" class="team-flag-img" style="height:14px;width:auto;margin-right:8px;border-radius:2px;vertical-align:middle;box-shadow:0 1px 3px rgba(0,0,0,0.15);display:inline-block;" alt="${displayVal}"/>${displayVal}`;
                    }
                }
                
                td.innerHTML = displayVal;
                if (isAgentHeader(colHeader) && displayVal !== "-") {
                    td.textContent = resolveAgentName(displayVal);
                    attachAgentRoleHover(td, agentName, invoiceDateCellOf(row, headers));
                }

                if (isNumericHeader(colHeader)) td.classList.add("numeric");
                else if (isDateHeader(colHeader)) td.classList.add("date");

                if (cellIdx === mergeColIdx && mergeSpans) {
                    const span = mergeSpans[rowIdx];
                    if (!span) return;               // covered by the cell above
                    if (span > 1) {
                        td.rowSpan = span;
                        td.style.verticalAlign = "middle";
                    }
                }
                trRow.appendChild(td);
            });
            tbody.appendChild(trRow);
        });
        table.appendChild(tbody);
        subWrapper.appendChild(table);

        return subWrapper;
    }

    function renderEgaEsa(egaData) {
        if (!egaData) {
            noDataView.classList.remove("hidden");
            rowCount.textContent = "0 rows";
            totalAgents.textContent = "0";
            totalCustomers.textContent = "0";
            renderSectionTotalCards();
            return;
        }

        const t2Headers = egaData.headers_t2 || [];
        const t2Rows = egaData.rows_t2 || [];
        const agentIdx = t2Headers.findIndex(h => h.toLowerCase().includes("agent"));
        const customerIdx = t2Headers.findIndex(h => h.toLowerCase().includes("customer"));

        // Award filter. determine_eligibility() hands back ONE label per agent
        // and ESA outranks EGA, so "EGA" and "ESA" are disjoint lists — an ESA
        // winner is not listed under EGA even though they cleared that bar.
        // Early-bird labels ("EGA (Feb)", "ESA (Nov)") count under their award.
        const t1EligIdx = (egaData.headers_t1 || [])
            .findIndex(h => String(h).toLowerCase().includes("eligib"));
        const t2EligIdx = t2Headers.findIndex(h => String(h).toLowerCase().includes("eligib"));
        const awardOf = (value) => {
            const v = String(value ?? "").trim().toUpperCase();
            if (v.startsWith("ESA")) return "esa";
            if (v.startsWith("EGA")) return "ega";
            return "";           // "-" — qualified for nothing yet
        };
        const wantedAward = state.filters.egaAward || "all";
        const matchesAward = (row, idx) =>
            wantedAward === "all" || (idx !== -1 && awardOf(row[idx]) === wantedAward);

        const userObj = USER_ROLES[state.currentUser];

        const matchingT2Rows = t2Rows.filter(row => {
            const agentVal = agentIdx !== -1 && row[agentIdx] ? String(row[agentIdx]) : "";
            const customerVal = customerIdx !== -1 && row[customerIdx] ? String(row[customerIdx]) : "";
            if (userObj && userObj.filterAgentName !== null) {
                if (agentVal.toLowerCase().trim() !== userObj.filterAgentName.toLowerCase().trim()) {
                    return false;
                }
            }
            if (!matchesAward(row, t2EligIdx)) return false;
            return matchesSearch(agentVal, customerVal);
        });

        const matchingAgents = new Set(matchingT2Rows.map(row => agentIdx !== -1 && row[agentIdx] ? String(row[agentIdx]).trim().toLowerCase() : ""));

        const searchQuery = state.filters.search;
        const matchesAgentSearch = (agentVal) => {
            if (!searchQuery) return true;
            const cleanAgent = agentVal.trim().toLowerCase();
            return cleanAgent.includes(searchQuery.toLowerCase().trim()) || matchingAgents.has(cleanAgent);
        };

        // Filter Table 1 by matching agents
        const t1Rows = (egaData.rows_t1 || []).filter(row => {
            const agentVal = row[0] ? String(row[0]).trim().toLowerCase() : "";
            if (userObj && userObj.filterAgentName !== null) {
                const cleanAgent = agentVal.trim().toLowerCase();
                if (cleanAgent !== userObj.filterAgentName.toLowerCase().trim()) return false;
            }
            if (!matchesAward(row, t1EligIdx)) return false;
            return matchesAgentSearch(agentVal);
        });

        const visibleAgents = new Set();
        const visibleCustomers = new Set();

        t1Rows.forEach(row => {
            const agName = row[0] ? String(row[0]).trim() : "";
            if (agName && !agName.toLowerCase().includes("total")) visibleAgents.add(agName);
        });
        matchingT2Rows.forEach(row => {
            const agName = agentIdx !== -1 && row[agentIdx] ? String(row[agentIdx]).trim() : "";
            const custName = customerIdx !== -1 && row[customerIdx] ? String(row[customerIdx]).trim() : "";
            if (agName && !agName.toLowerCase().includes("total")) visibleAgents.add(agName);
            if (custName && !custName.toLowerCase().includes("total")) visibleCustomers.add(custName);
        });

        totalAgents.textContent = visibleAgents.size;
        totalCustomers.textContent = visibleCustomers.size;
        renderSectionTotalCards();

        let totalDisplayedRows = t1Rows.length + matchingT2Rows.length;
        rowCount.textContent = `${totalDisplayedRows} total rows`;

        if (totalDisplayedRows === 0) {
            noDataView.classList.remove("hidden");
            return;
        } else {
            noDataView.classList.add("hidden");
        }

        const wrapper = document.createElement("div");
        wrapper.className = "dynamic-bonus-table-wrapper";
        wrapper.style.display = "flex";
        wrapper.style.flexDirection = "column";
        wrapper.style.gap = "28px";
        wrapper.style.padding = "24px";

        if (t1Rows.length > 0) {
            wrapper.appendChild(createEgaEsaSubTable("Summary Agent Award", egaData.headers_t1, t1Rows));
        }
        if (matchingT2Rows.length > 0) {
            wrapper.appendChild(createEgaEsaSubTable("Summary Agent Award by Customer", egaData.headers_t2, matchingT2Rows));
        }

        tableContainer.appendChild(wrapper);
    }

    function createEgaEsaSubTable(title, headers, rows) {
        const subWrapper = document.createElement("div");
        subWrapper.className = "bonus-sub-section";
        subWrapper.style.display = "flex";
        subWrapper.style.flexDirection = "column";
        subWrapper.style.gap = "12px";

        const h4 = document.createElement("h4");
        h4.textContent = title;
        h4.style.fontFamily = "'Outfit', sans-serif";
        h4.style.fontSize = "19px";
        h4.style.fontWeight = "700";
        h4.style.color = "#1e293b";
        subWrapper.appendChild(h4);

        const table = document.createElement("table");
        table.className = "dashboard-table";

        // Head
        const thead = document.createElement("thead");
        const trHead = document.createElement("tr");
        headers.forEach(h => {
            const th = document.createElement("th");
            th.textContent = h;
            if (isNumericHeader(h)) th.classList.add("numeric");
            else if (isDateHeader(h)) th.classList.add("date");
            trHead.appendChild(th);
        });
        thead.appendChild(trHead);
        table.appendChild(thead);

        // Body
        const tbody = document.createElement("tbody");
        const isOutsource = state.activeAgentType === "outsource";
        rows.forEach(row => {
            const trRow = document.createElement("tr");
            const agentName = row[0] ? String(row[0]).trim() : "";
            
            let rowClass = "";
            const cleanAgentName = agentName.toLowerCase();
            if (cleanAgentName.includes("total") || cleanAgentName.includes("summary") || cleanAgentName.includes("grand")) {
                rowClass = "total-bg";
            } else {
                rowClass = getRowClass(agentName, isOutsource);
            }
            if (rowClass) {
                trRow.className = rowClass;
            }
            
            row.forEach((cellVal, cellIdx) => {
                const td = document.createElement("td");
                const colHeader = headers[cellIdx];
                
                let displayVal = cellVal === null || cellVal === undefined ? "-" : String(cellVal);
                td.innerHTML = displayVal;
                if (isAgentHeader(colHeader) && displayVal !== "-") {
                    td.textContent = resolveAgentName(displayVal);
                    attachAgentRoleHover(td, agentName, invoiceDateCellOf(row, headers));
                }

                if (isNumericHeader(colHeader)) td.classList.add("numeric");
                else if (isDateHeader(colHeader)) td.classList.add("date");
                trRow.appendChild(td);
            });
            tbody.appendChild(trRow);
        });
        table.appendChild(tbody);
        subWrapper.appendChild(table);
        return subWrapper;
    }

    // Helper functions for alignments
    function isNumericHeader(header) {
        if (!header) return false;
        const h = header.toLowerCase();
        // Two money columns are named after a person rather than after what they
        // hold, so they match none of the words below and sat left-aligned
        // against every other amount. Matching the name covers the
        // "Gan Lai Soon (RM)" spelling as well as the bare one.
        if (h.includes("gan lai soon") || h.includes("safwan")) return true;
        return h.includes("price") || h.includes("fee") || h.includes("commission") || h.includes("amount") || h.includes("sales") || h.includes("points") || h.includes("clawback") || h.includes("total");
    }

    function isDateHeader(header) {
        if (!header) return false;
        const h = header.toLowerCase();
        return h.includes("date");
    }

    function getCountColumnIdx(headers) {
        return headers.findIndex(h => {
            const n = String(h || "").toLowerCase().replace(/\s+/g, " ").trim();
            return n === "count of customer"
                || n === "total invoice"
                || n === "invoice count"
                || n === "invoice total"
                || n.includes("count of customer")
                || n.includes("total invoice")
                || n.includes("invoice count")
                || n.includes("invoice total");
        });
    }

    function parseMoneyValue(val) {
        if (!val || val === "-") return 0;
        const text = String(val).trim();
        // A real amount leads with its figure ("RM 300.00", "-RM 361.62",
        // "RM 49.50 (Teng Kah Kent)"). Text sentinels like "invoice before
        // Oct 25" lead with words, and their digits are not money -- stripping
        // non-numerics blind would read that one as RM 25.
        if (!/^-?\s*(rm)?\s*-?\s*[\d.]/i.test(text)) return 0;
        // One cell can carry several amounts -- an agent's Other Commission
        // lists an override per downline agent, joined by <br/>. The cell's
        // value is their sum; parsing only the leading figure dropped the rest.
        // Restricted to RM-prefixed amounts so digits inside a name are ignored.
        const amounts = text.match(/-?\s*rm\s*-?\s*[\d,]+(?:\.\d+)?/gi);
        if (amounts && amounts.length > 1) {
            return amounts.reduce((sum, a) => sum + (parseFloat(a.replace(/[^0-9.-]/g, "")) || 0), 0);
        }
        return parseFloat(text.replace(/[^0-9.-]/g, "")) || 0;
    }

    function formatRM(value) {
        return `RM ${value.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
    }

    function getFactoryBaseRate(target) {
        // Matches build_commission_pack.py / outsource_basic_commission.py /
        // full_internal_basic_commission.py: the Factory base rate is a flat
        // 2.0% for both agent types, and 0.5% for Safwan's own separate cut.
        if (target === "safwan") return 0.5;
        return 2.0;
    }

    function getFactoryRateKey(agent, customer) {
        return `${String(agent || "").trim().toLowerCase()}|${String(customer || "").trim().toLowerCase()}`;
    }

    function findFactoryRate(agent, customer) {
        const key = getFactoryRateKey(agent, customer);
        return state.factoryRates.find(r => getFactoryRateKey(r.agent, r.customer) === key) || null;
    }

    function getFactoryProfitSharingValue(rateData, target) {
        if (!rateData) return null;
        const field = target === "safwan" ? "safwan_rate" : "agent_rate";
        const val = parseFloat(rateData[field]);
        return Number.isFinite(val) ? val : null;
    }

    function getFactoryCommissionValue(sales, profitSharing, target) {
        return sales * ((getFactoryBaseRate(target) + profitSharing) / 100);
    }

    function isFactoryBasicCommission(row, headers, fullAgentName, customerName) {
        if (state.activeSection !== "basic_nfp") return false;
        const packageIdx = headers.findIndex(h => h.toLowerCase().trim() === "package type" || h.toLowerCase().trim() === "package");
        const commissionIdx = headers.findIndex(h => h.toLowerCase().trim() === "commission");
        if (packageIdx === -1 || commissionIdx === -1) return false;
        const pkg = String(row[packageIdx] || "").toLowerCase();
        const comm = String(row[commissionIdx] || "").toLowerCase().trim();
        return pkg.includes("factory")
            && comm === "basic commission"
            && fullAgentName
            && customerName
            && customerName !== "-";
    }

    // Only ever returns the "Insert Profit Sharing" placeholder, or null.
    // Once a rate is saved, the cell already holds the server-computed
    // figure (build_commission_pack.py applies the OSA/OUM/OGM split there),
    // so this must NOT overwrite it with a client-side recomputation --
    // that would only ever show the agent's un-split 100% share again.
    function getFactoryCellDisplay(row, headers, fullAgentName, customerName, target) {
        const rateData = findFactoryRate(fullAgentName, customerName);
        const profitSharing = getFactoryProfitSharingValue(rateData, target);
        if (profitSharing === null) return '<span class="profit-sharing-value">Insert Profit Sharing</span>';
        return null;
    }

    function isSpecialCaseCommissionType(commissionType) {
        const t = String(commissionType || "").toLowerCase().trim();
        return t === "basic commission" || t.includes("net floor price");
    }

    function shouldHighlightSpecialCaseCell(cell, cellIdx, rowCommissionType, commissionPriceIdx, remarksIdx) {
        if (!cell.isSpecialCase) return false;
        if (remarksIdx !== -1 && cellIdx === remarksIdx) return true;
        if (commissionPriceIdx !== -1 && cellIdx === commissionPriceIdx && isSpecialCaseCommissionType(rowCommissionType)) {
            return true;
        }
        return false;
    }

    function isBasicCommissionType(commissionType) {
        return String(commissionType || "").toLowerCase().trim() === "basic commission";
    }

    function isNfpCommissionType(commissionType) {
        const t = String(commissionType || "").toLowerCase().trim();
        return t.includes("net floor price") && t.includes("commission");
    }

    function isAddSpecialCaseEligible(commissionType) {
        return isBasicCommissionType(commissionType) || isNfpCommissionType(commissionType);
    }

    function isEditSpecialCaseEligible(commissionType) {
        const t = String(commissionType || "").toLowerCase().trim();
        return t === "new basic commission" || (t.includes("new") && t.includes("net floor price") && t.includes("commission"));
    }

    function findOriginalCommissionRowIndices(originalRows, headers, agent, customer) {
        let basicOrigIdx = -1;
        let nfpOrigIdx = -1;
        
        const agentIdx = headers.findIndex(h => h.toLowerCase().trim() === "agent");
        const customerIdx = headers.findIndex(h => h.toLowerCase().trim() === "customer");
        const commissionIdx = headers.findIndex(h => h.toLowerCase().trim() === "commission");

        let currentAgent = "";
        let currentCust = "";
        for (let idx = 0; idx < originalRows.length; idx++) {
            const row = originalRows[idx];
            if (state.specialCaseRowRefs.has(row)) continue;

            if (agentIdx !== -1 && row[agentIdx] && String(row[agentIdx]).trim() !== "") {
                currentAgent = String(row[agentIdx]).trim();
            }
            if (customerIdx !== -1 && row[customerIdx] && String(row[customerIdx]).trim() !== "") {
                currentCust = String(row[customerIdx]).trim();
            }
            
            if (currentAgent.toLowerCase() !== agent.toLowerCase() || currentCust.toLowerCase() !== customer.toLowerCase()) {
                continue;
            }

            const commType = commissionIdx !== -1 ? String(row[commissionIdx] || "") : "";
            if (basicOrigIdx === -1 && isBasicCommissionType(commType)) {
                basicOrigIdx = idx;
            } else if (nfpOrigIdx === -1 && isNfpCommissionType(commType)) {
                nfpOrigIdx = idx;
            }
        }

        return { basicOrigIdx, nfpOrigIdx };
    }

    function insertSpecialCaseRows(originalRows, basicRow, nfpRow, headers, agent, customer) {
        const { basicOrigIdx, nfpOrigIdx } = findOriginalCommissionRowIndices(
            originalRows, headers, agent, customer
        );

        // Insert from the bottom up so earlier row indices stay valid. Either
        // row may be null — a case raised from one commission row only replaces
        // that one.
        if (nfpRow) {
            if (nfpOrigIdx !== -1) originalRows.splice(nfpOrigIdx + 1, 0, nfpRow);
            else originalRows.push(nfpRow);
        }

        if (basicRow) {
            if (basicOrigIdx !== -1) originalRows.splice(basicOrigIdx + 1, 0, basicRow);
            else originalRows.push(basicRow);
        }
    }

    function customerHasSpecialCase(agentName, customerName) {
        return state.specialCasePairs.some(pair => {
            const data = (pair[0] || pair[1]).specialCaseData;
            return data
                && data.agent.toLowerCase() === agentName.toLowerCase()
                && data.customer.toLowerCase() === customerName.toLowerCase();
        });
    }

    function showLoader(show) {
        if (show) {
            loader.classList.remove("hidden");
        } else {
            loader.classList.add("hidden");
        }
    }

    // -------------------------------------------------------------
    // Notes Box
    // -------------------------------------------------------------
    // The Agent Tiers Legend used to sit here, decoding the row tint. Both are
    // retired: the tint could only ever say which *band* an agent sat in, and
    // it said it for the report month, so an invoice raised before a promotion
    // was coloured by a role its agent did not hold at the time. Hovering an
    // agent name now names the exact role, resolved against that row's own
    // invoice date -- see agentRoleTooltip().

    function updateNoteBox() {
        const noteBox = document.getElementById("noteBox");
        if (!noteBox) return;

        const noteBoxGroup = document.getElementById("noteBoxGroup");
        const existingSecondary = document.getElementById("secondaryNoteBox");
        if (existingSecondary) existingSecondary.remove();

        let content = "";
        let secondaryContent = "";
        if (state.activeSection === "basic_nfp") {
            const isJulyOrLater = parseInt(state.activeMonth) >= 7;
            const ratesText = state.activeAgentType === "internal" 
                ? `Executive - 3%, Senior - 3.25%, Senior Override - 0.25%`
                : `OUM - 2.25%, OSA - 2.0%`;
            
            content = `
                <b>Basic Commission Rules:</b>
                <ul>
                    <li>Total Amount - EPP Price (if applicable) = Sales Price</li>
                    <li>Sales Price x Rate % = Basic Commission</li>
                    <li><b>Payout Condition:</b>
                        <ul>
                            <li><b>Jan–June:</b> Full Basic Commission payout once paid = 100%</li>
                            <li><b>July onwards:</b> RM 300 payout at least paid => 5% and Full Basic Commission payout at least paid => 75%</li>
                        </ul>
                    </li>
                </ul>
            `;
            
            secondaryContent = `
                <b>Net Floor Price (NFP) Rules:</b>
                <ul>
                    <li>NFP Commission payout once paid = 100%</li>
                    <li>NFP Commission effective for invoice starting OCT 1, 2025</li>
                    <li><b>Sales above Net Floor Price:</b> (Sales Price - Net Floor Price) x 25% = NFP Commission</li>
                    <li><b>Sales above System Price:</b> (System Price - Net Floor Price) x 100% = NFP Commission</li>
                    <li><b>Sales below Net Floor Price:</b> (Sales Price - Net Floor Price) x bears 20% = NFP Commission</li>
                </ul>
            `;
        } else if (state.activeSection === "anp") {
            content = `
                <b>ANP Commission Rules:</b>
                <ul>
                    <li>Internal agents only.</li>
                    <li>ANP Commission payout once at least RM0.01 is paid.</li>
                    <li>ANP Commission is rewarded the month following case issuance.</li>
                    <li><b>Commission Tiers:</b>
                        <ul>
                            <li>RM 0 - 59k: RM 0</li>
                            <li>RM 60k - 179k: RM 500</li>
                            <li>RM 180k - 359k: RM 1000</li>
                            <li>Above RM 360k: RM 1500</li>
                            <li>Above RM 720k: RM 2000</li>
                        </ul>
                    </li>
                    <li><b>EP Point Recognition Structure:</b>
                        <ul>
                            <li><b>Residence & Shop Lot:</b> 100% recognition rate.</li>
                            <li><b>Factory:</b> Prior to May 2026: 100%. Effective May 2026: 100% for the first RM40,000, and 40% for the balance (unless factory has &lt; 36pcs).</li>
                        </ul>
                    </li>
                </ul>
            `;
        } else if (state.activeSection === "ega_esa") {
            content = `
                <b>EGA/ESA Awards Rules:</b>
                <ul>
                    <li>EGA is a semiannual award</li>
                    <li>ESA is a yearly award</li>
                    <li><b>EP Point Recognition Structure:</b>
                        <ul>
                            <li><b>Residence & Shop Lot:</b> 100% recognition rate.</li>
                            <li><b>Factory:</b> Prior to May 2026: 100%. Effective May 2026: 100% for the first RM40,000, and 40% for the balance (unless factory has &lt; 36pcs).</li>
                        </ul>
                    </li>
                </ul>
            `;
        } else if (state.activeSection === "production_bonus") {
            content = `
                <b>Outsource Production Bonus:</b>
                <ul>
                    <li><b>OUM Production Bonus Summary:</b> Paid based on collective team monthly volume milestones.</li>
                    <li><b>OGM Production Bonus Summary:</b> Paid based on group-level cumulative monthly sales performance.</li>
                </ul>
            `;
        } else if (state.activeSection === "monthly_contest") {
            content = `
                <b>Monthly Contest Rules:</b>
                <ul>
                    <li><b>Team Ranking Rewards:</b>
                        <ul>
                            <li>Champion = RM 5,000</li>
                            <li>Runner-up = RM 3,000</li>
                            <li>Third Place = RM 2,000</li>
                        </ul>
                    </li>
                    <li><b>Golden Boot Award</b> (Top 3 Individuals):
                        <ul>
                            <li>Champion = RM 3,000</li>
                            <li>Runner-up = RM 2,000</li>
                            <li>Third Place = RM 1,000</li>
                        </ul>
                    </li>
                    <li><b>Eligibility:</b>
                        <ul>
                            <li>At least 3 cases completed</li>
                            <li>Monthly sales => RM 150,000</li>
                            <li>Meets company performance recognition standards</li>
                        </ul>
                    </li>
                    <li><b>Team Achievement Bonus:</b>
                        <ul>
                            <li>Team target achieved</li>
                            <li>Every member completes at least 1 deal</li>
                            <li>Reward for all qualified members: RM 2,000</li>
                        </ul>
                    </li>
                    <li><b>Fast Start Award:</b>
                        <ul>
                            <li>Complete 2 deals</li>
                            <li>Reward: Xiaomi Smart Watch (RM 250)</li>
                        </ul>
                    </li>
                </ul>
            `;
        } else {
            content = `Select a section to view specific notes.`;
        }
        
        noteBox.innerHTML = content;

        const hasRealNote = state.activeSection !== null && content.indexOf("Select a section") === -1;

        if (secondaryContent) {
            const secondaryBox = document.createElement("div");
            secondaryBox.className = "note-box";
            secondaryBox.id = "secondaryNoteBox";
            secondaryBox.style.marginTop = "12px";
            secondaryBox.style.borderLeftColor = "#16a34a"; // Green highlight for NFP Rules
            secondaryBox.innerHTML = secondaryContent;
            noteBoxGroup.appendChild(secondaryBox);
        }

        // Make the note box(es) clickable to open a readable pop-up modal
        noteBoxGroup.querySelectorAll(".note-box").forEach(box => {
            if (hasRealNote) {
                box.classList.add("note-box-clickable");
                box.title = "Click to read notes in a larger view";
                box.onclick = openNoteModal;
            } else {
                box.classList.remove("note-box-clickable");
                box.title = "";
                box.onclick = null;
            }
        });
    }

    // -------------------------------------------------------------
    // Section Note Pop-up Modal
    // -------------------------------------------------------------
    function openNoteModal() {
        const noteModal = document.getElementById("noteModal");
        const noteModalBody = document.getElementById("noteModalBody");
        const noteModalTitle = document.getElementById("noteModalTitle");
        const noteBoxGroup = document.getElementById("noteBoxGroup");
        if (!noteModal || !noteModalBody || !noteBoxGroup) return;

        const label = noteBoxGroup.querySelector(".group-label");
        if (noteModalTitle) noteModalTitle.textContent = label ? label.textContent : "Section Notes";

        noteModalBody.innerHTML = "";
        noteBoxGroup.querySelectorAll(".note-box").forEach(box => {
            const clone = document.createElement("div");
            clone.className = "note-modal-section";
            clone.style.borderLeftColor = box.style.borderLeftColor || "";
            clone.innerHTML = box.innerHTML;
            noteModalBody.appendChild(clone);
        });

        noteModal.classList.remove("hidden");
    }

    function closeNoteModal() {
        const noteModal = document.getElementById("noteModal");
        if (noteModal) noteModal.classList.add("hidden");
    }

    // -------------------------------------------------------------
    // Special Case Modal Handler
    // -------------------------------------------------------------
    const specialCaseModal = document.getElementById("specialCaseModal");
    const modalSelectionGroupRow = document.getElementById("modalSelectionGroupRow");
    const modalAgentSelect = document.getElementById("modalAgentSelect");
    const modalCustomerSelect = document.getElementById("modalCustomerSelect");
    const modalAgentName = document.getElementById("modalAgentName");
    const modalCustomerName = document.getElementById("modalCustomerName");
    const modalPackageType = document.getElementById("modalPackageType");
    const modalSystemPrice = document.getElementById("modalSystemPrice");
    const modalNetFloorPrice = document.getElementById("modalNetFloorPrice");
    const modalSalesPrice = document.getElementById("modalSalesPrice");
    const modalRatePct = document.getElementById("modalRatePct");
    const modalRemarks = document.getElementById("modalRemarks");
    const modalProfitSharingRow = document.getElementById("modalProfitSharingRow");
    const modalProfitSharingPct = document.getElementById("modalProfitSharingPct");

    const modalCaseType = document.getElementById("modalCaseType");
    const modalBaselineRate = document.getElementById("modalBaselineRate");
    const modalOrigBasicComm = document.getElementById("modalOrigBasicComm");
    const modalOrigNfpComm = document.getElementById("modalOrigNfpComm");
    const modalFeeWaiver = document.getElementById("modalFeeWaiver");
    const modalAdjustedSalesPrice = document.getElementById("modalAdjustedSalesPrice");
    const rowAdjustedSalesPrice = document.getElementById("rowAdjustedSalesPrice");
    const rowRevisedSalesPrice = document.getElementById("rowRevisedSalesPrice");
    const previewRevisedSalesPrice = document.getElementById("previewRevisedSalesPrice");
    const rowAdjustedNfp = document.getElementById("rowAdjustedNfp");
    const rowFeeWaiver = document.getElementById("rowFeeWaiver");
    const rowAdjustedRate = document.getElementById("rowAdjustedRate");
    const rowProfitSharing = document.getElementById("rowProfitSharing");
    const rowWaiverSummary = document.getElementById("rowWaiverSummary");
    const previewWaiverSummary = document.getElementById("previewWaiverSummary");
    const previewTotalNetComm = document.getElementById("previewTotalNetComm");

    const previewBasicComm = document.getElementById("previewBasicComm");
    const previewNfpComm = document.getElementById("previewNfpComm");
    const previewProfitSharingRow = document.getElementById("previewProfitSharingRow");
    const previewProfitSharingComm = document.getElementById("previewProfitSharingComm");
    const rowCaseType = document.getElementById("rowCaseType");
    const rowGanOverride = document.getElementById("rowGanOverride");
    const modalGanOverridePct = document.getElementById("modalGanOverridePct");
    const previewGanOverrideRow = document.getElementById("previewGanOverrideRow");
    const previewGanOverrideLabel = document.getElementById("previewGanOverrideLabel");
    const previewGanOverrideComm = document.getElementById("previewGanOverrideComm");
    const confirmModalBtn = document.getElementById("confirmModalBtn");
    const deleteModalBtn = document.getElementById("deleteModalBtn");
    const closeModalBtn = document.getElementById("closeModalBtn");

    function getAvailableAgentsForSpecialCase() {
        const agents = new Set();
        const section = state.rawData?.sections?.basic_nfp;
        if (section && section.rows && section.headers) {
            const agentIdx = section.headers.findIndex(h => h.toLowerCase().trim() === "agent");
            if (agentIdx !== -1) {
                let cur = "";
                section.rows.forEach(r => {
                    const a = r[agentIdx] ? String(r[agentIdx]).trim() : "";
                    if (a) cur = a;
                    const lower = cur.toLowerCase();
                    if (cur && !lower.includes("total") && !lower.includes("summary") && !lower.includes("grand")) {
                        agents.add(cur);
                    }
                });
            }
        }
        return Array.from(agents).sort((a, b) => a.localeCompare(b));
    }

    function getAvailableCustomersForAgent(agentName) {
        const customers = new Set();
        const section = state.rawData?.sections?.basic_nfp;
        if (section && section.rows && section.headers) {
            const agentIdx = section.headers.findIndex(h => h.toLowerCase().trim() === "agent");
            const custIdx = section.headers.findIndex(h => h.toLowerCase().trim() === "customer");
            if (custIdx !== -1) {
                let curAgent = "";
                section.rows.forEach(r => {
                    if (agentIdx !== -1 && r[agentIdx] && String(r[agentIdx]).trim()) {
                        curAgent = String(r[agentIdx]).trim();
                    }
                    const cust = r[custIdx] ? String(r[custIdx]).trim() : "";
                    const custLower = cust.toLowerCase();
                    if (cust && cust !== "-" && !custLower.includes("total") && !custLower.includes("summary")) {
                        if (!agentName || curAgent.toLowerCase() === agentName.toLowerCase()) {
                            customers.add(cust);
                        }
                    }
                });
            }
        }
        return Array.from(customers).sort((a, b) => a.localeCompare(b));
    }

    function populateAgentDropdown(selectedAgent = "") {
        if (!modalAgentSelect) return;
        modalAgentSelect.innerHTML = '<option value="">-- Choose Agent --</option>';
        const agents = getAvailableAgentsForSpecialCase();
        agents.forEach(agent => {
            const opt = document.createElement("option");
            opt.value = agent;
            opt.textContent = agent;
            if (agent.toLowerCase() === String(selectedAgent || "").toLowerCase()) {
                opt.selected = true;
            }
            modalAgentSelect.appendChild(opt);
        });
    }

    function populateCustomerDropdown(agentName = "", selectedCustomer = "") {
        if (!modalCustomerSelect) return;
        modalCustomerSelect.innerHTML = '<option value="">-- Choose Customer --</option>';
        const customers = getAvailableCustomersForAgent(agentName);
        customers.forEach(cust => {
            const opt = document.createElement("option");
            opt.value = cust;
            opt.textContent = cust;
            if (cust.toLowerCase() === String(selectedCustomer || "").toLowerCase()) {
                opt.selected = true;
            }
            modalCustomerSelect.appendChild(opt);
        });
    }

    // Cases the user deleted since the last successful save. The server merges
    // rather than replaces, so a removal has to be stated outright — leaving a
    // case out of the posted list no longer deletes it. That asymmetry is
    // deliberate: omission used to mean deletion, which quietly destroyed saved
    // cases whenever this page held fewer than the server did.
    let pendingSpecialCaseDeletes = [];

    async function saveSpecialCases() {
        const specialCases = state.specialCasePairs.map(pair => {
            const r = pair.find(Boolean);
            return r && r.specialCaseData;
        }).filter(Boolean);

        const deleted = pendingSpecialCaseDeletes.slice();

        // One POST per agent-type bucket. The All Agents view holds internal and
        // outsource rows side by side, so a single save can touch both, and
        // posting the view's own name ("all") would file them where the report
        // never looks. Every other view yields exactly one bucket, as before.
        const buckets = new Map();
        const unfiled = [];
        const bucketFor = (item) => {
            const type = agentTypeBucketFor(item && item.agent);
            if (type !== "internal" && type !== "outsource") {
                unfiled.push(item);
                return null;
            }
            if (!buckets.has(type)) buckets.set(type, { special_cases: [], deleted: [] });
            return buckets.get(type);
        };

        const filedDeletes = [];
        specialCases.forEach(c => { const b = bucketFor(c); if (b) b.special_cases.push(c); });
        deleted.forEach(d => { const b = bucketFor(d); if (b) { b.deleted.push(d); filedDeletes.push(d); } });

        if (unfiled.length) {
            console.error("Special cases skipped — agent type could not be resolved:",
                unfiled.map(c => c && c.agent));
        }
        if (buckets.size === 0) return;

        try {
            const results = await Promise.all([...buckets.entries()].map(([agentType, payload]) =>
                fetch("/api/special-cases", {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json"
                    },
                    body: JSON.stringify({
                        year: state.activeYear,
                        month: state.activeMonth,
                        agent_type: agentType,
                        special_cases: payload.special_cases,
                        deleted: payload.deleted
                    })
                })
            ));
            if (results.some(res => !res.ok)) {
                console.error("Failed to save special cases to server");
                return;
            }
            // Only clear the queue once the server has accepted the removals,
            // so a failed request retries them on the next save.
            pendingSpecialCaseDeletes = pendingSpecialCaseDeletes.filter(
                d => !filedDeletes.includes(d));
        } catch (err) {
            console.error("Error saving special cases:", err);
        }
    }

    // Which special-case types make sense depends on the row that was clicked:
    // a Basic Commission row can only be adjusted through the basic-side levers,
    // an NFP row through the NFP-side ones. Adjusted Sales Price feeds both
    // calculations, so it belongs to each list.
    const SPECIAL_CASE_TYPE_LABELS = {
        adjusted_nfp: "Adjusted Net Floor Price",
        fee_waiver: "Net Floor Price Commission Fee Waiver",
        adjusted_rate: "Adjusted Basic Commission Rate %",
        profit_sharing: "Profit Sharing",
        adjusted_sales_price: "Adjusted Sales Price"
    };

    const SPECIAL_CASE_TYPES_BY_ROW = {
        basic: ["adjusted_rate", "profit_sharing", "adjusted_sales_price"],
        nfp: ["adjusted_nfp", "fee_waiver", "adjusted_sales_price"],
        all: ["adjusted_nfp", "fee_waiver", "adjusted_rate", "profit_sharing", "adjusted_sales_price"]
    };

    function getSpecialCaseRowKind(rowRef) {
        const headers = state.rawData?.sections?.basic_nfp?.headers || [];
        const commIdx = headers.findIndex(h => {
            const n = String(h).toLowerCase().trim();
            return n === "commission" || n === "commission type";
        });
        const val = (commIdx !== -1 && rowRef) ? String(rowRef[commIdx] || "").toLowerCase() : "";
        if (val.includes("net floor") || val.includes("netfloor")) return "nfp";
        if (val.includes("basic")) return "basic";
        return "all";
    }

    /** Rebuild the Type of Special Case list for a row kind. `preferred` (an
     *  already-saved type) is kept selectable even when it falls outside the
     *  list, so editing an older case can never silently change its type. */
    function setSpecialCaseTypeOptions(kind, preferred) {
        if (!modalCaseType) return;
        const keys = SPECIAL_CASE_TYPES_BY_ROW[kind] || SPECIAL_CASE_TYPES_BY_ROW.all;
        const list = (preferred && !keys.includes(preferred)) ? keys.concat(preferred) : keys;
        modalCaseType.innerHTML = list.map(k =>
            `<option value="${k}">${SPECIAL_CASE_TYPE_LABELS[k] || k}</option>`).join("");
        modalCaseType.value = (preferred && list.includes(preferred)) ? preferred : list[0];
    }

    function openAddSpecialCaseModal(agentName, customerName, rowRef) {
        state.editingPair = null;
        state.pendingAddRowRef = rowRef;
        setSpecialCaseModalMode("standard");
        document.querySelector("#specialCaseModal h3").textContent = "Special Case";
        if (deleteModalBtn) {
            deleteModalBtn.classList.add("hidden");
            deleteModalBtn.disabled = true;
        }
        
        modalAgentName.value = agentName;
        modalCustomerName.value = customerName;
        
        const headers = state.rawData.sections.basic_nfp.headers;
        const packageIdx = headers.findIndex(h => h.toLowerCase().trim() === "package type" || h.toLowerCase().trim() === "package");
        const systemIdx = headers.findIndex(h => h.toLowerCase().trim() === "system price");
        const nfpIdx = headers.findIndex(h => h.toLowerCase().trim() === "net floor price" || h.toLowerCase().trim() === "netfloor price");
        const salesIdx = headers.findIndex(h => h.toLowerCase().trim() === "sales price" || h.toLowerCase().trim() === "total amount");
        const priceIdx = headers.findIndex(h => h.toLowerCase().includes("commission price") || h.toLowerCase().includes("commission (rm)") || h.toLowerCase().includes("commission amount"));

        const parseMoney = (val) => {
            if (!val || val === "-") return 0;
            return parseFloat(String(val).replace(/[^0-9.-]/g, "")) || 0;
        };

        const pkg = packageIdx !== -1 ? String(rowRef[packageIdx] || "").trim() : "";
        const sales = salesIdx !== -1 ? parseMoney(rowRef[salesIdx]) : 0;
        const system = systemIdx !== -1 ? parseMoney(rowRef[systemIdx]) : 0;
        const origNfp = nfpIdx !== -1 ? parseMoney(rowRef[nfpIdx]) : 0;
        // Read the rate the report ALREADY computed for this exact invoice
        // (basic commission ÷ sales price) instead of recalculating it from
        // scratch — getDefaultBasicRateForPackage() is a simplified client-side
        // guess that knows nothing about invoice date or effective-dated Data
        // page rates, so it can (and did) disagree with the real server value.
        const actualCommVal = priceIdx !== -1 ? parseMoney(rowRef[priceIdx]) : 0;
        let baseRate = sales > 0 && actualCommVal > 0 ? (actualCommVal / sales) * 100 : NaN;
        if (!Number.isFinite(baseRate)) baseRate = getDefaultBasicRateForPackage(pkg, agentName);

        modalPackageType.value = pkg || "Residential";
        modalSystemPrice.value = system;
        modalSalesPrice.value = sales;
        modalNetFloorPrice.value = origNfp;
        if (modalBaselineRate) modalBaselineRate.value = baseRate;
        if (modalRatePct) modalRatePct.value = String(baseRate);

        const origBasicCommVal = sales * (baseRate / 100);
        let origNfpCommVal = 0;
        if (sales > origNfp) origNfpCommVal = (sales - origNfp) * 0.25;
        if (sales < origNfp) origNfpCommVal = (sales - origNfp) * 0.20;

        if (modalOrigBasicComm) modalOrigBasicComm.value = formatRM(origBasicCommVal);
        if (modalOrigNfpComm) modalOrigNfpComm.value = origNfp === 0 ? "TBC with Finance" : formatRM(origNfpCommVal);

        state.specialCaseRowKind = getSpecialCaseRowKind(rowRef);
        setSpecialCaseTypeOptions(state.specialCaseRowKind);
        if (modalFeeWaiver) modalFeeWaiver.value = "0";
        if (modalProfitSharingPct) modalProfitSharingPct.value = "0";
        if (modalAdjustedSalesPrice) modalAdjustedSalesPrice.value = "";
        // Blank, not "0.75": a new case tracks the standard rate until someone
        // deliberately overrides it.
        if (modalGanOverridePct) modalGanOverridePct.value = "";
        modalRemarks.value = "";

        onSpecialCaseTypeChange();
        confirmModalBtn.textContent = "Confirm & Add";
        specialCaseModal.classList.remove("hidden");
    }

    function openAddCustomerSpecialCaseModal() {
        state.editingPair = null;
        state.pendingAddRowRef = null;
        setSpecialCaseModalMode("customer");
        document.querySelector("#specialCaseModal h3").textContent = "Special Case Customer";
        if (deleteModalBtn) {
            deleteModalBtn.classList.add("hidden");
            deleteModalBtn.disabled = true;
        }

        modalAgentName.value = "";
        modalCustomerName.value = "";
        state.selectedSpecialCaseAgent = "";
        state.selectedSpecialCaseCustomer = "";

        populateAgentDropdown("");
        populateCustomerDropdown("", "");

        modalPackageType.value = "";
        modalSystemPrice.value = 0;
        modalNetFloorPrice.value = 0;
        modalSalesPrice.value = 0;
        const baseRate = getDefaultBasicRateForPackage("", "");
        if (modalBaselineRate) modalBaselineRate.value = baseRate;
        if (modalRatePct) modalRatePct.value = baseRate;
        if (modalOrigBasicComm) modalOrigBasicComm.value = "-";
        if (modalOrigNfpComm) modalOrigNfpComm.value = "-";
        // A brand-new customer has no row yet, so every type stays on offer and
        // the case creates both commission rows.
        state.specialCaseRowKind = "all";
        setSpecialCaseTypeOptions("all");
        if (modalFeeWaiver) modalFeeWaiver.value = "0";
        if (modalProfitSharingPct) modalProfitSharingPct.value = "0";
        if (modalAdjustedSalesPrice) modalAdjustedSalesPrice.value = "";
        // Blank, not "0.75": a new case tracks the standard rate until someone
        // deliberately overrides it.
        if (modalGanOverridePct) modalGanOverridePct.value = "";
        modalRemarks.value = "";

        confirmModalBtn.textContent = "Confirm & Add";
        onSpecialCaseTypeChange();
        specialCaseModal.classList.remove("hidden");
    }

    function openEditSpecialCaseModal(rowRef) {
        const data = rowRef.specialCaseData;
        if (!data) return;
        
        const pairIdx = state.specialCasePairs.findIndex(pair => pair.includes(rowRef));
        if (pairIdx === -1) return;
        state.editingPair = state.specialCasePairs[pairIdx];
        const isCustomerMode = data.caseType === "customer";
        setSpecialCaseModalMode(isCustomerMode ? "customer" : "standard");
        
        document.querySelector("#specialCaseModal h3").textContent = isCustomerMode ? "Special Case Customer" : "Special Case";
        if (deleteModalBtn) {
            deleteModalBtn.classList.remove("hidden");
            deleteModalBtn.disabled = false;
        }
        
        modalAgentName.value = data.agent;
        modalCustomerName.value = data.customer;
        state.selectedSpecialCaseAgent = data.agent || "";
        state.selectedSpecialCaseCustomer = data.customer || "";
        
        if (isCustomerMode) {
            populateAgentDropdown(data.agent);
            populateCustomerDropdown(data.agent, data.customer);
        }

        modalPackageType.value = data.pkg;
        modalSystemPrice.value = data.system;
        modalSalesPrice.value = data.sales;
        modalNetFloorPrice.value = data.nfp;
        if (modalBaselineRate) modalBaselineRate.value = data.rate;
        if (modalRatePct) modalRatePct.value = data.rate;
        // Re-opening an existing case shows the list its own row allows, with
        // the saved type preselected (and kept available even if it predates
        // this filtering). Cases saved before rowKind existed fall back to the
        // clicked row so they keep behaving as they always did.
        state.specialCaseRowKind = isCustomerMode
            ? "all"
            : (data.rowKind || getSpecialCaseRowKind(rowRef));
        setSpecialCaseTypeOptions(
            state.specialCaseRowKind,
            data.specialCaseType || "adjusted_nfp"
        );
        if (modalFeeWaiver) modalFeeWaiver.value = data.feeWaiver ?? 0;
        if (modalProfitSharingPct) modalProfitSharingPct.value = data.profitSharingPct ?? 0;
        if (modalAdjustedSalesPrice) modalAdjustedSalesPrice.value = data.adjustedSalesPrice ?? "";
        // Blank for cases saved before this field existed, which is exactly the
        // "use the standard rate" state they have always had.
        if (modalGanOverridePct) modalGanOverridePct.value = data.ganOverridePct ?? "";
        modalRemarks.value = data.remarks || "";
        
        onSpecialCaseTypeChange();
        confirmModalBtn.textContent = "Save Changes";
        specialCaseModal.classList.remove("hidden");
    }

    function closeModal() {
        state.pendingAddRowRef = null;
        // Dropped here too, not only when consumed: closing before the field
        // ever showed would otherwise leave it armed to steal focus from the
        // next special case opened from somewhere else entirely.
        state.focusGanOverride = false;
        // Same reasoning: the next modal opened from a Commission Price cell
        // must get the full form back, so the cut-down view ends with this one.
        state.specialCaseGanMode = false;
        specialCaseModal.classList.add("hidden");
    }

    // Modal Input & Select Listeners
    [modalSalesPrice, modalRatePct, modalSystemPrice, modalNetFloorPrice, modalFeeWaiver,
     modalAdjustedSalesPrice, modalGanOverridePct].forEach(input => {
        if (input) {
            input.addEventListener("input", updateSpecialCasePreview);
        }
    });
    if (modalProfitSharingPct) {
        modalProfitSharingPct.addEventListener("input", updateSpecialCasePreview);
    }
    if (modalCaseType) {
        modalCaseType.addEventListener("change", onSpecialCaseTypeChange);
    }
    if (modalAgentSelect) {
        modalAgentSelect.addEventListener("change", (e) => {
            const selectedAgent = e.target.value;
            state.selectedSpecialCaseAgent = selectedAgent;
            if (modalAgentName) modalAgentName.value = selectedAgent;
            populateCustomerDropdown(selectedAgent, "");
            if (modalCustomerSelect && modalCustomerSelect.options.length > 1) {
                modalCustomerSelect.value = modalCustomerSelect.options[1].value;
                const selectedCust = modalCustomerSelect.value;
                state.selectedSpecialCaseCustomer = selectedCust;
                if (modalCustomerName) modalCustomerName.value = selectedCust;
                selectCustomerForSpecialCase(selectedCust);
            } else {
                modalCustomerName.value = "";
            }
        });
    }
    if (modalCustomerSelect) {
        modalCustomerSelect.addEventListener("change", (e) => {
            const selectedCust = e.target.value;
            state.selectedSpecialCaseCustomer = selectedCust;
            if (modalCustomerName) modalCustomerName.value = selectedCust;
            if (selectedCust) {
                selectCustomerForSpecialCase(selectedCust);
            }
        });
    }

    if (closeModalBtn) closeModalBtn.addEventListener("click", closeModal);

    // Payment slips modal close handlers
    const paymentSlipsModalEl = document.getElementById("paymentSlipsModal");
    const closePaymentSlipsBtn = document.getElementById("closePaymentSlipsBtn");
    const closePaymentSlips = () => {
        if (paymentSlipsModalEl) paymentSlipsModalEl.classList.add("hidden");
    };
    if (closePaymentSlipsBtn) closePaymentSlipsBtn.addEventListener("click", closePaymentSlips);
    if (paymentSlipsModalEl) {
        paymentSlipsModalEl.addEventListener("click", (e) => {
            if (e.target === paymentSlipsModalEl) closePaymentSlips();
        });
    }
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") closePaymentSlips();
    });

    // Section Note modal close handlers
    const closeNoteModalBtn = document.getElementById("closeNoteModalBtn");
    const noteModalEl = document.getElementById("noteModal");
    if (closeNoteModalBtn) closeNoteModalBtn.addEventListener("click", closeNoteModal);
    if (noteModalEl) {
        noteModalEl.addEventListener("click", (e) => {
            if (e.target === noteModalEl) closeNoteModal();
        });
    }
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && noteModalEl && !noteModalEl.classList.contains("hidden")) {
            closeNoteModal();
        }
    });
    if (deleteModalBtn) {
        deleteModalBtn.addEventListener("click", () => {
            if (state.editingPair) {
                const capturedRow = state.editingPair[0] || state.editingPair[1];
                deleteSpecialCasePair(capturedRow);
                closeModal();
            }
        });
    }
    
    if (confirmModalBtn) {
        confirmModalBtn.addEventListener("click", () => {
            const agent = state.specialCaseMode === "customer"
                ? (state.selectedSpecialCaseAgent || modalAgentName.value)
                : modalAgentName.value;
            const customer = state.specialCaseMode === "customer"
                ? (state.selectedSpecialCaseCustomer || modalCustomerName.value.trim())
                : (modalCustomerName.value.trim() || "(Unknown)");
            const pkg = modalPackageType.value.trim() || "-";
            const system = parseFloat(modalSystemPrice.value) || 0;
            const nfp = parseFloat(modalNetFloorPrice.value) || 0;
            const sales = parseFloat(modalSalesPrice.value) || 0;
            const baselineRate = parseFloat(modalBaselineRate?.value) || parseFloat(modalRatePct.value) || 3.0;
            const specialCaseType = modalCaseType ? modalCaseType.value : "adjusted_nfp";
            const rate = specialCaseType === "adjusted_rate" ? (parseFloat(modalRatePct.value) || baselineRate) : baselineRate;
            const feeWaiver = specialCaseType === "fee_waiver" ? (parseFloat(modalFeeWaiver.value) || 0) : 0;
            const remarks = modalRemarks.value.trim();
            const profitSharingPct = parseFloat(modalProfitSharingPct?.value) || 0;
            const adjustedSalesPrice = specialCaseType === "adjusted_sales_price"
                ? (parseFloat(modalAdjustedSalesPrice?.value) || 0) : null;
            // Management's manually adjusted sales price replaces the auto-
            // calculated one for every downstream calc — same as the live preview.
            const salesForCalc = (specialCaseType === "adjusted_sales_price" && adjustedSalesPrice)
                ? adjustedSalesPrice : sales;

            if (state.specialCaseMode === "customer" && (!agent || !customer || customer === "(Unknown)")) {
                alert("Please select both an agent and a customer from the database list first.");
                return;
            }

            const basicComm = salesForCalc * ((rate + profitSharingPct) / 100);
            let nfpComm = 0;
            if (specialCaseType === "fee_waiver") {
                let origNfpComm = 0;
                if (salesForCalc > nfp) origNfpComm = (salesForCalc - nfp) * 0.25;
                if (salesForCalc < nfp) origNfpComm = (salesForCalc - nfp) * 0.20;
                nfpComm = origNfpComm + feeWaiver;
            } else {
                if (salesForCalc > nfp) {
                    nfpComm = (salesForCalc - nfp) * 0.25;
                } else if (salesForCalc < nfp) {
                    nfpComm = (salesForCalc - nfp) * 0.20;
                }
            }

            // OGM override: Gan Lai Soon takes a cut of the sales price on every
            // OUM/OSA invoice (outsource_basic_commission.py), so a special case
            // booked under one of his agents has to fill his column too. The rate
            // defaults to 0.75% but is negotiated per agent, so the case can carry
            // its own. He earns nothing on his own invoices.
            const ganOverridePct = ganOverridePctFor(modalGanOverridePct?.value);
            const ganLaiSoonComm = ganOverrideApplies(agent)
                ? salesForCalc * (ganOverridePct / 100) : 0;
            const ganLaiSoonStr = ganLaiSoonComm
                ? `RM ${ganLaiSoonComm.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`
                : "-";

            const sysStr = system !== 0 ? `RM ${system.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}` : "-";
            const nfpStr = nfp !== 0 ? `RM ${nfp.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}` : "-";
            const salesStr = salesForCalc !== 0 ? `RM ${salesForCalc.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}` : "-";
            const basicCommStr = `RM ${basicComm.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
            
            let nfpCommStr = "-";
            if (nfp === 0) {
                nfpCommStr = "TBC with Finance";
            } else if (nfpComm !== 0) {
                nfpCommStr = `RM ${nfpComm.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
            }

            const headers = state.rawData.sections.basic_nfp.headers;
            
            const setRowValues = (rowArr, commType, commPriceVal) => {
                const setVal = (headerName, val) => {
                    let searchName = headerName.toLowerCase().trim();
                    let idx = headers.findIndex(h => h.toLowerCase().trim() === searchName);
                    // Fallbacks for header name variations
                    if (idx === -1 && searchName === "net floor price") {
                        idx = headers.findIndex(h => h.toLowerCase().trim() === "netfloor price");
                    }
                    if (idx === -1 && searchName === "commission") {
                        idx = headers.findIndex(h => h.toLowerCase().trim() === "commission type");
                    }
                    if (idx === -1 && searchName === "gan lai soon") {
                        idx = headers.findIndex(h => h.toLowerCase().trim() === "gan lai soon (rm)");
                    }
                    if (idx !== -1) {
                        rowArr[idx] = val;
                    }
                };
                setVal("Agent", agent);
                setVal("Customer", customer);
                setVal("Package Type", pkg);
                setVal("Package", pkg);
                setVal("System Price", sysStr);
                setVal("Net Floor Price", nfpStr);
                setVal("Sales Price", commType.toLowerCase().includes("net floor price") ? "-" : salesStr);
                setVal("Commission", commType);
                setVal("Commission Price", commPriceVal);
                // The override rides on the basic commission only, never on the
                // paired Net Floor Price row.
                setVal("Gan Lai Soon", commType.toLowerCase().includes("basic") ? ganLaiSoonStr : "-");
                setVal("Remarks", remarks || "-");
            };

            // A case raised from the Net Floor Price row must not restate Basic
            // Commission, and vice versa — only the clicked row is replaced.
            // "all" (Add Special Case Customer) still creates both.
            // Raised from Gan Lai Soon's column: his override is the only thing
            // being changed, so neither commission is restated. Kept ahead of the
            // Adjusted Sales Price rule below, which would otherwise drag both
            // rows back in.
            const ganOnly = !!state.specialCaseGanMode;
            // "gan" is only for a case BORN from the Gan column, where there is
            // nothing else to keep. Gan-editing an existing case must preserve
            // its stored rowKind: stamping "gan" over a restating case stops the
            // injector restating it, and a hand-added customer (caseType
            // "customer") has no organic row to fall back on — the whole
            // customer would vanish from the report on the next load.
            const editingStored = state.editingPair
                ? (state.editingPair.find(r => r && r.specialCaseData) || {}).specialCaseData
                : null;
            const rowKind = ganOnly
                ? (editingStored ? editingStored.rowKind : "gan")
                : (state.specialCaseMode === "customer"
                    ? "all" : (state.specialCaseRowKind || "all"));
            // Exception: Adjusted Sales Price changes the figure BOTH
            // commissions are derived from (basic = sales × rate, NFP =
            // (sales − net floor price) × rate), so it always restates both —
            // whichever row it was raised from. Every other type touches only
            // one side of the calculation.
            const affectsBothCommissions = !ganOnly && specialCaseType === "adjusted_sales_price";
            const wantsBasic = !ganOnly && (affectsBothCommissions || rowKind === "basic" || rowKind === "all");
            const wantsNfp = !ganOnly && (affectsBothCommissions || rowKind === "nfp" || rowKind === "all");

            // Stored as typed, so a blank stays blank and keeps tracking the
            // standard rate rather than freezing today's 0.75% into the case.
            const ganOverridePctRaw = ganOverrideApplies(agent)
                ? String(modalGanOverridePct?.value ?? "").trim() : "";

            const dataObject = {
                agent, customer, pkg, system, nfp, sales, rate, remarks,
                profitSharingPct, specialCaseType, feeWaiver, adjustedSalesPrice,
                caseType: state.specialCaseMode, rowKind,
                ganOverridePct: ganOverridePctRaw
            };

            // Gan-only: nothing is restated, so the row the user clicked keeps
            // its own commissions and just gains the revised override, drawn as
            // the old figure above the new one. Mirrors _annotate_gan_override()
            // in app.py, which does the same on a fresh page load.
            if (ganOnly) {
                const targetRow = state.pendingAddRowRef
                    || (state.editingPair && state.editingPair[0]);
                const ganIdx = headers.findIndex(h => {
                    const n = String(h).toLowerCase().trim();
                    return n === "gan lai soon" || n === "gan lai soon (rm)";
                });
                if (targetRow && ganIdx !== -1) {
                    const prev = String(targetRow[ganIdx] || "-");
                    // Keep the first render's original: re-editing must not
                    // promote a previously revised figure into the "was" slot.
                    const originalMatch = prev.match(/special-case-primary-value">([^<]*)</);
                    const original = originalMatch ? originalMatch[1] : prev;
                    const newVal = ganLaiSoonStr;
                    targetRow[ganIdx] = (original === newVal || newVal === "-")
                        ? original
                        : `<div class="special-case-merge-stack">`
                          + `<span class="special-case-primary-value">${original}</span>`
                          + `<span class="special-case-secondary-value">${newVal}</span>`
                          + `</div>`;
                    targetRow.specialCaseData = dataObject;
                }
                closeModal();
                saveSpecialCases();
                renderActiveSection();
                return;
            }

            if (state.editingPair) {
                // A pair may hold just one row when the case only replaced one
                // commission, so each side is handled independently.
                const agentIdx = headers.findIndex(h => h.toLowerCase().trim() === "agent");
                const commIdxEdit = headers.findIndex(h => {
                    const n = String(h).toLowerCase().trim();
                    return n === "commission" || n === "commission type";
                });
                state.editingPair.forEach(rowRef => {
                    if (!rowRef) return;
                    const existing = commIdxEdit !== -1
                        ? String(rowRef[commIdxEdit] || "").toLowerCase() : "";
                    const isNfpRow = existing.includes("net floor") || existing.includes("netfloor");
                    if (isNfpRow) {
                        setRowValues(rowRef, "New Net Floor Price Commission", nfpCommStr);
                        if (agentIdx !== -1) rowRef[agentIdx] = "";
                    } else {
                        setRowValues(rowRef, "New Basic Commission", basicCommStr);
                    }
                    rowRef.specialCaseData = dataObject;
                    // Update final JSON string at the end of the row array
                    if (rowRef.length > headers.length) {
                        rowRef[rowRef.length - 1] = JSON.stringify(dataObject);
                    }
                });
            } else {
                const originalRows = state.rawData.sections.basic_nfp.rows;
                const agentIdx = headers.findIndex(h => h.toLowerCase().trim() === "agent");
                const customerIdx = headers.findIndex(h => h.toLowerCase().trim() === "customer");

                let templateRow = state.pendingAddRowRef || null;
                if (!templateRow) {
                    templateRow = findCurrentBasicNfpRowByCustomer(customer, agent)?.row
                        || findCurrentBasicNfpRowByCustomer(customer)?.row
                        || null;
                    if (!templateRow) {
                        let currentAgent = "";
                        for (const r of originalRows) {
                            if (agentIdx !== -1 && r[agentIdx] && String(r[agentIdx]).trim() !== "") {
                                currentAgent = String(r[agentIdx]).trim();
                            }
                            if (currentAgent.toLowerCase() === agent.toLowerCase()) {
                                const cust = customerIdx !== -1 && r[customerIdx] ? String(r[customerIdx]).trim() : "";
                                if (cust.toLowerCase() === customer.toLowerCase()) {
                                    templateRow = r;
                                    break;
                                }
                            }
                        }
                    }
                }
                state.pendingAddRowRef = null;

                const makeRow = () => {
                    const r = templateRow ? [...templateRow] : new Array(headers.length).fill("-");
                    // Drop any specialCaseData JSON copied from a template row
                    while (r.length > headers.length) r.pop();
                    return r;
                };

                let basicRow = null, nfpRow = null;
                if (wantsBasic) {
                    basicRow = makeRow();
                    setRowValues(basicRow, "New Basic Commission", basicCommStr);
                    basicRow.specialCaseData = dataObject;
                    basicRow.push(JSON.stringify(dataObject));
                }
                if (wantsNfp) {
                    nfpRow = makeRow();
                    setRowValues(nfpRow, "New Net Floor Price Commission", nfpCommStr);
                    if (agentIdx !== -1) nfpRow[agentIdx] = "";
                    nfpRow.specialCaseData = dataObject;
                    nfpRow.push(JSON.stringify(dataObject));
                }

                insertSpecialCaseRows(originalRows, basicRow, nfpRow, headers, agent, customer);

                const created = [basicRow, nfpRow].filter(Boolean);
                created.forEach(r => state.specialCaseRowRefs.add(r));
                state.specialCasePairs.push(created);
            }


            closeModal();
            saveSpecialCases();
            renderActiveSection();
        });
    }

    // -------------------------------------------------------------
    // Special Case Deletion
    // -------------------------------------------------------------
    function deleteSpecialCasePair(rowRef) {
        const pairIdx = state.specialCasePairs.findIndex(pair => pair.includes(rowRef));
        if (pairIdx === -1) return;

        const pair = state.specialCasePairs[pairIdx];
        const rows = state.rawData.sections.basic_nfp.rows;

        // Tell the server outright — omitting it from the next save no longer
        // removes it.
        const scData = (pair.find(Boolean) || {}).specialCaseData;
        if (scData && scData.agent && scData.customer) {
            pendingSpecialCaseDeletes.push({ agent: scData.agent, customer: scData.customer });
        }

        pair.forEach(r => {
            const i = rows.indexOf(r);
            if (i !== -1) rows.splice(i, 1);
            state.specialCaseRowRefs.delete(r);
        });

        state.specialCasePairs.splice(pairIdx, 1);

        saveSpecialCases();
        renderActiveSection();
    }

    // -------------------------------------------------------------
    // Profit Sharing Modal Handler
    // -------------------------------------------------------------
    const factoryRateModal = document.getElementById("factoryRateModal");
    const factoryAgentName = document.getElementById("factoryAgentName");
    const factoryCustomerName = document.getElementById("factoryCustomerName");
    const factoryPackageType = document.getElementById("factoryPackageType");
    const factorySystemPrice = document.getElementById("factorySystemPrice");
    const factoryNetFloorPrice = document.getElementById("factoryNetFloorPrice");
    const factorySalesPrice = document.getElementById("factorySalesPrice");
    const factoryBasicCommissionRate = document.getElementById("factoryBasicCommissionRate");
    const factoryProfitSharingRate = document.getElementById("factoryProfitSharingRate");
    const factoryTargetLabel = document.getElementById("factoryTargetLabel");
    const factoryFormula = document.getElementById("factoryFormula");
    const previewFactoryComm = document.getElementById("previewFactoryComm");
    const closeFactoryModalBtn = document.getElementById("closeFactoryModalBtn");
    const saveFactoryModalBtn = document.getElementById("saveFactoryModalBtn");

    // Cache of agent -> {tier, oum_name, ogm_name} from the Agent Roles &
    // Hierarchy Data page, via /api/factory-split-hierarchy. Kept client-side
    // only for the preview breakdown below; the actual split that gets paid
    // is always computed server-side in build_commission_pack.py.
    const factorySplitHierarchyCache = new Map();

    async function fetchFactorySplitHierarchy(agent) {
        const key = String(agent || "").trim().toLowerCase();
        if (!key) return { tier: "", oum_name: null, ogm_name: null };
        if (factorySplitHierarchyCache.has(key)) return factorySplitHierarchyCache.get(key);
        try {
            const res = await fetch(`/api/factory-split-hierarchy?agent=${encodeURIComponent(agent)}`);
            const data = await res.json().catch(() => ({}));
            const result = res.ok ? data : { tier: "", oum_name: null, ogm_name: null };
            factorySplitHierarchyCache.set(key, result);
            return result;
        } catch (err) {
            console.error("Error fetching factory split hierarchy:", err);
            return { tier: "", oum_name: null, ogm_name: null };
        }
    }

    function openFactoryRateModal(rowRef, headers, agentName, customerName, target) {
        if (!factoryRateModal) return;
        if (state.activeAgentType === "all") {
            alert("Switch to Internal or Outsource Agents to set a Profit Sharing rate.");
            return;
        }

        const packageIdx = headers.findIndex(h => h.toLowerCase().trim() === "package type" || h.toLowerCase().trim() === "package");
        const systemIdx = headers.findIndex(h => h.toLowerCase().trim() === "system price");
        const nfpIdx = headers.findIndex(h => h.toLowerCase().trim() === "net floor price" || h.toLowerCase().trim() === "netfloor price");
        const salesIdx = headers.findIndex(h => h.toLowerCase().trim() === "sales price" || h.toLowerCase().trim() === "total amount");

        const pkg = packageIdx !== -1 ? String(rowRef[packageIdx] || "").trim() : "Factory";
        const system = systemIdx !== -1 ? parseMoneyValue(rowRef[systemIdx]) : 0;
        const nfp = nfpIdx !== -1 ? parseMoneyValue(rowRef[nfpIdx]) : 0;
        const sales = salesIdx !== -1 ? parseMoneyValue(rowRef[salesIdx]) : 0;
        const baseRate = getFactoryBaseRate(target);
        const rateData = findFactoryRate(agentName, customerName);
        const profitSharing = getFactoryProfitSharingValue(rateData, target);

        state.editingFactoryRate = {
            agent: agentName,
            customer: customerName,
            pkg,
            system,
            nfp,
            sales,
            target,
            hierarchy: null
        };

        factoryAgentName.value = agentName;
        factoryCustomerName.value = customerName;
        factoryPackageType.value = pkg;
        factorySystemPrice.value = system;
        factoryNetFloorPrice.value = nfp;
        factorySalesPrice.value = sales;
        factoryBasicCommissionRate.value = baseRate;
        factoryProfitSharingRate.value = profitSharing === null ? "" : profitSharing;
        factoryTargetLabel.textContent = target === "safwan" ? "Safwan" : "Agent";

        updateFactoryModalCalculations();
        factoryRateModal.classList.remove("hidden");

        // The split only applies to the agent's own rate, never Safwan's.
        if (target === "agent") {
            fetchFactorySplitHierarchy(agentName).then((hierarchy) => {
                if (!state.editingFactoryRate || state.editingFactoryRate.agent !== agentName) return;
                state.editingFactoryRate.hierarchy = hierarchy;
                updateFactoryModalCalculations();
            });
        }
    }

    function closeFactoryModal() {
        state.editingFactoryRate = null;
        if (factoryRateModal) factoryRateModal.classList.add("hidden");
    }

    // Mirrors the confirmed split in build_commission_pack.py's
    // fetch_outsource_basic(): applied to the WHOLE Agent Commission (base +
    // sharing), only once profit sharing is actually set.
    //   OSA -> OUM found:    OSA 70% / OUM 20% / OGM 10%
    //   OSA -> OGM directly: OSA 70% / OGM 10%, 20% unpaid
    //   OSA -> no report:    OSA 70%, 30% unpaid
    //   agent is the OUM:    OUM 20%, 80% unpaid
    function renderFactorySplitBreakdown(fullCommission, sharing, hierarchy) {
        const box = document.getElementById("factorySplitBreakdown");
        if (!box) return;
        if (!hierarchy || sharing <= 0) { box.innerHTML = ""; return; }

        const tier = String(hierarchy.tier || "").toUpperCase();
        const rows = [];
        const row = (label, amount) => rows.push(`
            <div class="preview-row">
                <span>${label}:</span>
                <strong>${formatRM(amount)}</strong>
            </div>`);

        if (tier === "OSA" || tier === "OSA 1" || tier === "OSA1") {
            row("OSA Share (70%)", fullCommission * 0.70);
            if (hierarchy.oum_name) row(`OUM Share (20%) — ${hierarchy.oum_name}`, fullCommission * 0.20);
            if (hierarchy.ogm_name) row(`OGM Share (10%) — ${hierarchy.ogm_name}`, fullCommission * 0.10);
            if (!hierarchy.oum_name && !hierarchy.ogm_name) {
                row("Unallocated (30% — no Reports To on file)", fullCommission * 0.30);
            } else if (!hierarchy.oum_name || !hierarchy.ogm_name) {
                row("Unallocated (20%)", fullCommission * 0.20);
            }
        } else if (tier === "OUM") {
            row("OUM Share (20%)", fullCommission * 0.20);
            row("Unallocated (80%)", fullCommission * 0.80);
        } else {
            box.innerHTML = "";
            return;
        }

        box.innerHTML = rows.join("");
    }

    function updateFactoryModalCalculations() {
        if (!state.editingFactoryRate) return;
        const target = state.editingFactoryRate.target;
        const baseRate = getFactoryBaseRate(target);
        const profitSharing = parseFloat(factoryProfitSharingRate.value) || 0;
        const sales = parseFloat(factorySalesPrice.value) || 0;
        const totalRate = baseRate + profitSharing;
        const commission = sales * (totalRate / 100);

        factoryBasicCommissionRate.value = baseRate;
        factoryFormula.textContent = `${baseRate.toFixed(1)}% base + ${profitSharing}% sharing`;
        previewFactoryComm.textContent = formatRM(commission);

        if (target === "agent") {
            renderFactorySplitBreakdown(commission, profitSharing, state.editingFactoryRate.hierarchy);
        } else {
            const box = document.getElementById("factorySplitBreakdown");
            if (box) box.innerHTML = "";
        }
    }

    if (factoryProfitSharingRate) {
        factoryProfitSharingRate.addEventListener("input", updateFactoryModalCalculations);
    }
    if (closeFactoryModalBtn) closeFactoryModalBtn.addEventListener("click", closeFactoryModal);
    if (saveFactoryModalBtn) {
        saveFactoryModalBtn.addEventListener("click", () => {
            if (!state.editingFactoryRate) return;
            const edit = state.editingFactoryRate;
            const rate = parseFloat(factoryProfitSharingRate.value) || 0;
            let rateData = findFactoryRate(edit.agent, edit.customer);

            if (!rateData) {
                rateData = {
                    agent: edit.agent,
                    customer: edit.customer,
                    pkg: edit.pkg,
                    system: edit.system,
                    nfp: edit.nfp,
                    sales: edit.sales
                };
                state.factoryRates.push(rateData);
            }

            rateData.pkg = edit.pkg;
            rateData.system = edit.system;
            rateData.nfp = edit.nfp;
            rateData.sales = edit.sales;
            if (edit.target === "safwan") {
                rateData.safwan_rate = rate;
            } else {
                rateData.agent_rate = rate;
            }

            closeFactoryModal();
            renderActiveSection();
            saveFactoryRates();
        });
    }

    // -------------------------------------------------------------
    // Account bar (login/admin)
    // -------------------------------------------------------------
    async function initAccountBar() {
        try {
            const res = await fetch("/api/me");
            if (!res.ok) {
                window.location.href = "/login";
                return;
            }
            const me = await res.json();
            state.isAdmin = me.role === "admin";
            const whoami = document.getElementById("accountWhoami");
            if (whoami) whoami.textContent = `${me.username} (${me.role})`;
            const adminLink = document.getElementById("adminLink");
            if (adminLink && me.role === "admin") adminLink.style.display = "flex";
        } catch (err) {
            console.error("Failed to load account info:", err);
        }
    }

    const logoutBtn = document.getElementById("logoutBtn");
    if (logoutBtn) {
        logoutBtn.addEventListener("click", async () => {
            await fetch("/logout", { method: "POST" });
            window.location.href = "/login";
        });
    }

    function getMonthName(monthVal) {
        const MONTH_NAMES = [
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December"
        ];
        if (typeof monthVal === "number") {
            return MONTH_NAMES[monthVal - 1] || MONTH_NAMES[0];
        }
        if (!monthVal) return "January";
        const str = String(monthVal).trim();
        if (!isNaN(parseInt(str))) {
            const num = parseInt(str);
            if (num >= 1 && num <= 12) return MONTH_NAMES[num - 1];
        }
        return str.charAt(0).toUpperCase() + str.slice(1);
    }

    // -------------------------------------------------------------
    // Net Floor Price List navigation
    // -------------------------------------------------------------
    // Sends the user to the Data page, on the Net Floor Price section, with
    // the list already open on the same month the report page was showing.
    function goToNfpListPage() {
        const month = String(state.activeMonth || "").padStart(2, "0");
        window.location.href = `/data?section=nfp&showNfpList=1&month=${encodeURIComponent(month)}`;
    }

    // -------------------------------------------------------------
    // Monthly Commission Slip Handler
    // -------------------------------------------------------------
    // The IC on the slip is often the first place a wrong/missing number is
    // noticed, so it is editable in place instead of sending the user to the
    // Data page's roles table. Saves to the same agent_roles record either way.
    // Records the loaded value too, so blurring an untouched field is a no-op.
    function setSlipIcValue(value) {
        const icEl = document.getElementById("slipAgentNric");
        if (!icEl) return;
        icEl.textContent = value || "-";
        icEl.dataset.saved = value || "";
    }

    function setupSlipIcEditing(resolvedName) {
        const icEl = document.getElementById("slipAgentNric");
        const statusEl = document.getElementById("slipIcStatus");
        if (!icEl) return;
        if (statusEl) { statusEl.textContent = ""; statusEl.className = "slip-ic-status"; }

        icEl.dataset.agent = resolvedName || "";
        icEl.dataset.saved = "";
        if (!state.isAdmin || !resolvedName) {
            icEl.removeAttribute("contenteditable");
            icEl.title = "";
            return;
        }
        icEl.setAttribute("contenteditable", "true");
        icEl.title = "Click to edit — saves when you click away";

        if (icEl.dataset.bound) return;
        icEl.dataset.bound = "true";

        const setStatus = (msg, cls) => {
            if (!statusEl) return;
            statusEl.textContent = msg;
            statusEl.className = "slip-ic-status" + (cls ? " " + cls : "");
        };

        // Enter commits rather than inserting a newline into the slip.
        icEl.addEventListener("keydown", (e) => {
            if (e.key === "Enter") { e.preventDefault(); icEl.blur(); }
            if (e.key === "Escape") { icEl.textContent = icEl.dataset.saved || "-"; icEl.blur(); }
        });

        icEl.addEventListener("focus", () => {
            if (icEl.textContent.trim() === "-") icEl.textContent = "";
            setStatus("", "");
        });

        icEl.addEventListener("blur", async () => {
            const agent = icEl.dataset.agent || "";
            const value = icEl.textContent.replace(/\s+/g, " ").trim();
            icEl.textContent = value || "-";
            if (!agent) return;
            if (value === (icEl.dataset.saved || "")) return;

            setStatus("Saving…", "");
            try {
                const res = await fetch("/api/agent-ic", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ agent, ic_no: value })
                });
                const body = await res.json().catch(() => ({}));
                if (!res.ok) throw new Error(body.error || "Save failed");
                icEl.dataset.saved = value;
                setStatus("Saved", "ok");
                setTimeout(() => setStatus("", ""), 2500);
            } catch (err) {
                setStatus(err.message || "Save failed", "err");
            }
        });
    }

    function openCommissionSlipModal(agentName) {
        const slipModal = document.getElementById("commissionSlipModal");
        if (!slipModal) return;

        // Show modal immediately
        slipModal.classList.remove("hidden");

        const resolvedName = resolveAgentName(agentName) || agentName;
        document.getElementById("slipAgentName").textContent = resolvedName || "-";
        document.getElementById("slipAgentNric").textContent = "-";
        setupSlipIcEditing(resolvedName);
        fetch("/api/agent-roles")
            .then(res => res.json())
            .then(roles => {
                const matched = Array.isArray(roles) && roles.find(r => r.agent && r.agent.toLowerCase().trim() === resolvedName.toLowerCase().trim());
                if (matched && matched.ic_no) {
                    setSlipIcValue(matched.ic_no);
                } else {
                    fetch("/api/agent-roles/pg-list")
                        .then(res => res.json())
                        .then(data => {
                            const pgMatched = data.agents && data.agents.find(a => a.name && a.name.toLowerCase().trim() === resolvedName.toLowerCase().trim());
                            if (pgMatched && pgMatched.ic_no) {
                                setSlipIcValue(pgMatched.ic_no);
                            }
                        })
                        .catch(() => {});
                }
            })
            .catch(() => {});
        document.getElementById("slipMonthLabel").textContent = `${getMonthName(state.activeMonth)} ${state.activeYear}`;
        document.getElementById("slipCurrentDate").textContent = new Date().toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: '2-digit' });

        const tableBody = document.getElementById("slipItemsTableBody");
        tableBody.innerHTML = "";

        const section = state.rawData?.sections?.basic_nfp;
        if (!section || !section.rows || !section.headers) {
            tableBody.innerHTML = `<tr><td colspan="10" style="text-align:center; padding:20px; color:#64748b;">No data loaded for ${escapeHtml(resolvedName)}.</td></tr>`;
            return;
        }

        const headers = section.headers;
        const agentIdx = headers.findIndex(h => h.toLowerCase().includes("agent") || h.toLowerCase().includes("salesperson"));
        const custIdx = headers.findIndex(h => h.toLowerCase().includes("customer") || h.toLowerCase().includes("client"));
        const pkgIdx = headers.findIndex(h => h.toLowerCase().includes("package"));
        const sysIdx = headers.findIndex(h => h.toLowerCase().includes("system price"));
        const nfpIdx = headers.findIndex(h => h.toLowerCase().includes("net floor") || h.toLowerCase().includes("netfloor"));
        const salesIdx = headers.findIndex(h => h.toLowerCase().includes("sales price") || h.toLowerCase().includes("cash price") || h.toLowerCase().includes("total amount"));
        const commIdx = headers.findIndex(h => h.toLowerCase().includes("commission type") || h.toLowerCase().trim() === "commission");
        const priceIdx = headers.findIndex(h => h.toLowerCase().includes("commission price") || h.toLowerCase().includes("commission (rm)") || h.toLowerCase().includes("commission amount"));
        const remarksIdx = headers.findIndex(h => h.toLowerCase().includes("remark"));
        const refPersonIdx = headers.findIndex(h => h.toLowerCase().includes("referral person") || h.toLowerCase().includes("referral name"));
        const refRateIdx = headers.findIndex(h => h.toLowerCase().includes("referral rate"));
        const refFeeIdx = headers.findIndex(h => h.toLowerCase().includes("referral fee"));
        const otherCommIdx = headers.findIndex(h => h.toLowerCase().includes("other comm") || h.toLowerCase().trim() === "override");
        const advanceIdx = headers.findIndex(h => h.toLowerCase().includes("rm300") || h.toLowerCase().includes("basic commission (rm"));

        const parseVal = (v) => {
            if (!v || v === "-") return 0;
            return parseFloat(String(v).replace(/[^0-9.-]/g, "")) || 0;
        };

        const cleanAgent = (s) => String(s || "").replace(/[\*\(\)]/g, "").replace(/\b(senior|executive|ogm|oum|osa)\b/gi, "").toLowerCase().trim();
        const targetClean = cleanAgent(agentName);

        let currentAgent = "";
        const agentRows = [];
        section.rows.forEach(r => {
            const a = agentIdx !== -1 && r[agentIdx] ? String(r[agentIdx]).trim() : "";
            if (a && !a.toLowerCase().includes("total") && !a.toLowerCase().includes("summary")) {
                currentAgent = a;
            }
            const curClean = cleanAgent(currentAgent);
            if (curClean && targetClean && (curClean === targetClean || curClean.includes(targetClean) || targetClean.includes(curClean))) {
                agentRows.push(r);
            }
        });

        // Other Commission belongs to the agent NAMED in the cell -- the
        // "RM 85.00 (Sunny Tan)" sitting on Zulkarnain's row is Sunny Tan's
        // money -- and an OGM's override is a whole column named after them.
        // Same rule the Data page and the summary tables apply, so a slip can
        // never credit the agent whose row merely carries the amount. Scans
        // every row, not just this agent's: the override that belongs to them
        // is by definition written on somebody else's invoice.
        const slipOverrideCols = overrideColumnsByAgent(headers);
        const otherCommCredits = [];
        if (otherCommIdx !== -1 || slipOverrideCols.length) {
            let rowAgent = "";
            section.rows.forEach(r => {
                const a = agentIdx !== -1 && r[agentIdx] ? String(r[agentIdx]).trim() : "";
                if (a && !a.toLowerCase().includes("total") && !a.toLowerCase().includes("summary")) rowAgent = a;
                const cust = custIdx !== -1 && r[custIdx] ? String(r[custIdx]).trim() : "";
                const custLower = cust.toLowerCase();
                if (!cust || custLower.includes("total") || custLower.includes("summary")) return;
                const credit = (who, amount) => {
                    if (cleanAgent(who) === targetClean) {
                        otherCommCredits.push({ customer: cust, fromAgent: rowAgent, amount });
                    }
                };
                if (otherCommIdx !== -1) creditOtherCommissionCell(r[otherCommIdx], rowAgent, credit);
                slipOverrideCols.forEach(col => {
                    const amount = parseMoneyValue(r[col.idx]);
                    if (amount) credit(col.agent, amount);
                });
            });
        }

        let itemNo = 1;
        let totalCommSum = 0;
        const notesList = [];

        const customerGroups = new Map();
        agentRows.forEach(r => {
            const cust = custIdx !== -1 && r[custIdx] ? String(r[custIdx]).trim() : "(Unknown)";
            const custLower = cust.toLowerCase();
            if (custLower.includes("total") || custLower.includes("summary")) return;
            if (!customerGroups.has(cust)) {
                customerGroups.set(cust, []);
            }
            customerGroups.get(cust).push(r);
        });

        // An OGM sells nothing themselves -- their whole income is the override
        // column on other agents' invoices -- so "no customers" is only really
        // an empty slip when there are no credits to them either.
        if (customerGroups.size === 0 && otherCommCredits.length === 0) {
            tableBody.innerHTML = `<tr><td colspan="10" style="text-align:center; padding:20px; color:#64748b; font-style:italic;">No customer transactions found for ${escapeHtml(resolvedName)} in ${getMonthName(state.activeMonth)} ${state.activeYear}.</td></tr>`;
            document.getElementById("slipTotalComm").textContent = "RM 0.00";
            document.getElementById("slipAmountPayable").textContent = "RM 0.00";
            return;
        }

        customerGroups.forEach((rows, customerName) => {
            let basicRow = rows.find(r => commIdx !== -1 && String(r[commIdx]).toLowerCase().includes("basic")) || rows[0];
            let nfpRow = rows.find(r => commIdx !== -1 && String(r[commIdx]).toLowerCase().includes("net floor"));

            const sysPrice = sysIdx !== -1 ? parseVal(basicRow[sysIdx]) : 0;
            const salesPrice = salesIdx !== -1 ? parseVal(basicRow[salesIdx]) : 0;
            const basicCommVal = priceIdx !== -1 ? parseVal(basicRow[priceIdx]) : 0;
            const pkg = pkgIdx !== -1 ? String(basicRow[pkgIdx] || "").trim() : "";
            
            let basicRate = salesPrice > 0 ? ((basicCommVal / salesPrice) * 100) : getDefaultBasicRateForPackage(pkg, agentName);
            if (isNaN(basicRate)) basicRate = 3.0;

            // The RM 300 advance is a flat tranche, not a percentage of the
            // sale, so dividing it by the sales price invents a rate that
            // nobody agreed to (RM 300 on a RM 30k invoice reads as 1.00%,
            // not the 5% the agent is actually on). When the line is paying
            // the advance -- its commission amount matches the Basic
            // Commission (RM300) cell -- the rate column stays blank.
            const advanceVal = advanceIdx !== -1 ? parseVal(basicRow[advanceIdx]) : 0;
            const isAdvanceLine = advanceVal > 0 && basicCommVal > 0
                && Math.abs(advanceVal - basicCommVal) < 0.005;

            const remarks = remarksIdx !== -1 && basicRow[remarksIdx] ? String(basicRow[remarksIdx]).trim() : "";
            if (remarks && remarks !== "-") {
                notesList.push(`Item No. ${itemNo} - ${remarks}`);
            }

            const refPerson = refPersonIdx !== -1 && basicRow[refPersonIdx] ? String(basicRow[refPersonIdx]).trim() : "-";
            const refRate = refRateIdx !== -1 ? parseVal(basicRow[refRateIdx]) : 0;
            const refFee = refFeeIdx !== -1 ? parseVal(basicRow[refFeeIdx]) : 0;
            // Only what this customer's invoice credits to THIS agent. An
            // amount on their row naming someone else is that person's.
            const otherCommVal = otherCommCredits
                .filter(c => c.customer === customerName && cleanAgent(c.fromAgent) === targetClean)
                .reduce((sum, c) => sum + c.amount, 0);

            totalCommSum += basicCommVal + otherCommVal;

            const tr1 = document.createElement("tr");
            tr1.innerHTML = `
                <td style="text-align: center;">${itemNo}</td>
                <td><b>${escapeHtml(customerName)}</b></td>
                <td class="numeric">${sysPrice > 0 ? formatRM(sysPrice) : "-"}</td>
                <td class="numeric">${salesPrice > 0 ? formatRM(salesPrice) : "-"}</td>
                <td style="text-align: center;">${isAdvanceLine ? "-" : basicRate.toFixed(2) + "%"}</td>
                <td class="numeric" style="font-weight: 700;">${formatRM(basicCommVal)}</td>
                <td style="text-align: center;">${escapeHtml(refPerson)}</td>
                <td style="text-align: center;">${refRate > 0 ? refRate.toFixed(1) + "%" : "-"}</td>
                <td class="numeric">${refFee > 0 ? formatRM(refFee) : "-"}</td>
                <td class="numeric">${otherCommVal > 0 ? formatRM(otherCommVal) : "-"}</td>
            `;
            tableBody.appendChild(tr1);

            if (nfpRow) {
                const nfpVal = nfpIdx !== -1 ? parseVal(nfpRow[nfpIdx]) : 0;
                const nfpCommVal = priceIdx !== -1 ? parseVal(nfpRow[priceIdx]) : 0;
                totalCommSum += nfpCommVal;

                let nfpRateStr = "25.0%";
                if (salesPrice < nfpVal) nfpRateStr = "20.0%";

                const tr2 = document.createElement("tr");
                tr2.style.backgroundColor = "#f8fafc";
                tr2.innerHTML = `
                    <td style="text-align: center;"></td>
                    <td style="padding-left: 16px; color: #475569; font-style: italic;">Net Floor Price</td>
                    <td class="numeric">${nfpVal > 0 ? formatRM(nfpVal) : "-"}</td>
                    <td class="numeric">${nfpVal > 0 ? formatRM(nfpVal) : "-"}</td>
                    <td style="text-align: center;">${nfpRateStr}</td>
                    <td class="numeric" style="font-weight: 700; color: ${nfpCommVal < 0 ? '#dc2626' : '#0f172a'};">${formatRM(nfpCommVal)}</td>
                    <td style="text-align: center;">-</td>
                    <td style="text-align: center;">-</td>
                    <td style="text-align: center;">-</td>
                    <td style="text-align: center;">-</td>
                `;
                tableBody.appendChild(tr2);
            }

            itemNo++;
        });

        // Overrides earned on OTHER agents' invoices. Those customers are not
        // this agent's, so they get their own line items rather than being
        // folded into a customer group that does not belong to them -- and
        // without these the money would simply vanish from every slip.
        const externalCredits = otherCommCredits.filter(c => cleanAgent(c.fromAgent) !== targetClean);
        externalCredits.forEach(c => {
            totalCommSum += c.amount;
            const tr = document.createElement("tr");
            tr.innerHTML = `
                <td style="text-align: center;">${itemNo}</td>
                <td><b>${escapeHtml(c.customer)}</b><br/><span style="color:#475569; font-style:italic; font-size:11px;">Override from ${escapeHtml(resolveAgentName(c.fromAgent))}</span></td>
                <td class="numeric">-</td>
                <td class="numeric">-</td>
                <td style="text-align: center;">-</td>
                <td class="numeric">-</td>
                <td style="text-align: center;">-</td>
                <td style="text-align: center;">-</td>
                <td class="numeric">-</td>
                <td class="numeric" style="font-weight: 700;">${formatRM(c.amount)}</td>
            `;
            tableBody.appendChild(tr);
            itemNo++;
        });

        const notesContent = document.getElementById("slipNotesContent");
        if (notesContent) {
            notesContent.innerHTML = notesList.length > 0
                ? notesList.map(n => `<div>${escapeHtml(n)}</div>`).join("")
                : "No special notes for this period.";
        }

        document.getElementById("slipTotalComm").textContent = formatRM(totalCommSum);
        document.getElementById("slipAmountPayable").textContent = formatRM(totalCommSum);
    }

    const closeSlipModalBtn = document.getElementById("closeSlipModalBtn");
    const printSlipBtn = document.getElementById("printSlipBtn");
    const commissionSlipModal = document.getElementById("commissionSlipModal");

    if (closeSlipModalBtn && commissionSlipModal) {
        closeSlipModalBtn.addEventListener("click", () => {
            commissionSlipModal.classList.add("hidden");
        });
        commissionSlipModal.addEventListener("click", (e) => {
            if (e.target === commissionSlipModal) {
                commissionSlipModal.classList.add("hidden");
            }
        });
    }
    if (printSlipBtn) {
        printSlipBtn.addEventListener("click", () => {
            window.print();
        });
    }

    // -------------------------------------------------------------
    // Application Entry Point
    // -------------------------------------------------------------
    loadSavedViewState();
    syncViewControls();
    normalizeActiveSection();
    enforceAnpAgentTypeLock();
    renderSectionTabs();
    initEventListeners();
    renderSectionTotalCards();

    const sessionActive = initUserSession();
    initAccountBar();
    initContestMonths();
    // Load the agent full-name map before the first data render so every table
    // shows canonical full names from the outset.
    initAgentNameMap().finally(() =>
        invalidateStaleCommissionCacheOnServerRestart().finally(() => fetchData())
    );
});


