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
    let state = {
        currentUser: "finance_shuyee",
        isAdmin: false,   // set by initAccountBar(); gates editing the slip's IC
        activeYear: "2026",
        activeAgentType: "internal",
        activeMonth: "5", // May
        activeSection: "basic_nfp",
        rawData: null,
        filters: {
            search: "",
            rm300Only: false
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
        selectedSpecialCaseCustomer: ""
    };
    // Months the contest workbook has a sheet for; populated by initContestMonths().
    // Starts null so the tab is not hidden before the list arrives.
    let contestMonths = null;

    function hasContestData(month) {
        if (contestMonths === null) return true;
        return contestMonths.includes(parseInt(month));
    }

    // Canonical agent full-name lookup (nickname -> full name), sourced from
    // "1. Agent Name List.xlsx" via /api/agent-name-map. Used to display full
    // agent names in Title Case across every table.
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
    const rm300OnlyFilter = document.getElementById("rm300OnlyFilter");
    const rateCardsContainer = document.getElementById("rateCardsContainer");
    
    const syncDataBtn = document.getElementById("syncDataBtn");
    const downloadPdfBtn = document.getElementById("downloadPdfBtn");
    const downloadExcelBtn = document.getElementById("downloadExcelBtn");
    
    const dataTable = document.getElementById("dataTable");
    const tableHeaders = document.getElementById("tableHeaders");
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

        // RM300-only filter (Basic & Net Floor Price Commission section only —
        // narrows the table to rows whose Basic Commission (RM300) tranche
        // actually has a value this month, i.e. skips "-", "pending", and
        // "invoice before july" rows).
        if (rm300OnlyFilter) {
            rm300OnlyFilter.addEventListener("change", (e) => {
                state.filters.rm300Only = e.target.checked;
                renderActiveSection();
            });
        }

        // Clear filters button
        clearFiltersBtn.addEventListener("click", () => {
            if (searchFilter) searchFilter.value = "";
            state.filters.search = "";
            if (rm300OnlyFilter) rm300OnlyFilter.checked = false;
            state.filters.rm300Only = false;
            renderActiveSection();
        });

        // Download actions
        downloadPdfBtn.addEventListener("click", () => triggerDownload("pdf"));
        downloadExcelBtn.addEventListener("click", () => triggerDownload("excel"));

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
        return `commission_api_cache_v2:${state.activeYear}:${state.activeMonth}:${state.activeAgentType}`;
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
                Object.keys(localStorage)
                    .filter(k => k.startsWith("commission_api_cache_v2:"))
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
        const unionHeaders = [];
        parts.forEach(s => s.headers.forEach(h => { const ah = alias(h); if (!unionHeaders.includes(ah)) unionHeaders.push(ah); }));
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
    function mergeCommissionPayloads(internalData, outsourceData) {
        const basicNfp = unionMergeSections(internalData?.sections?.basic_nfp, outsourceData?.sections?.basic_nfp);
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
            agentTypeMap: buildAgentTypeMap(internalData, outsourceData)
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
        
        const totalNetComm = basicComm + (nfp === 0 ? 0 : nfpComm);
        if (previewTotalNetComm) previewTotalNetComm.textContent = formatRM(totalNetComm);
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

    function renderBasicNfpTotalCards(processed, headers) {
        if (!rateCardsContainer) return;
        rateCardsContainer.innerHTML = "";
        rateCardsContainer.classList.remove("hidden");

        let totalCommission = 0, totalOtherCommission = 0, totalReferralFee = 0;

        const commPriceIdx = headers.findIndex(h => h.toLowerCase().trim() === "commission price");
        const otherCommIdx = headers.findIndex(h => h.toLowerCase().trim() === "other commission");
        const referralFeeIdx = headers.findIndex(h => h.toLowerCase().trim() === "referral fee");
        const commIdx = headers.findIndex(h => {
            const n = String(h).toLowerCase().trim();
            return n === "commission" || n === "commission type";
        });

        // agent||customer of every invoice a special case has replaced.
        const supersededKeys = new Set();
        (processed || []).forEach(p => {
            if (p.isSpecialCase && !p.isTotalRow) {
                supersededKeys.add(`${String(p.fullAgentName).toLowerCase()}||${String(p.customerName).toLowerCase()}`);
            }
        });

        const isReplaced = (p) => supersededKeys.size > 0
            && supersededKeys.has(`${String(p.fullAgentName).toLowerCase()}||${String(p.customerName).toLowerCase()}`)
            && isSupersededOriginalRow(p, commIdx);

        if (state.activeAgentType === "all") {
            // "All" sources the cards from the Summary Agent Commission rollup
            // (agent_summary) rather than the raw basic_nfp detail rows. That
            // rollup is built server-side from the ORIGINAL invoices, so it is
            // corrected by the same delta: add the new figures, remove the ones
            // they replace.
            const agSummary = state.rawData?.sections?.agent_summary;
            totalCommission = sumAgentSummaryColumn(agSummary, "commission price");
            totalOtherCommission = sumAgentSummaryColumn(agSummary, "other commission");
            totalReferralFee = sumAgentSummaryColumn(agSummary, "referral fee");

            (processed || []).forEach(p => {
                if (p.isTotalRow || commPriceIdx === -1) return;
                if (p.isSpecialCase) totalCommission += parseMoneyValue(p.rawRow[commPriceIdx]);
                else if (isReplaced(p)) totalCommission -= parseMoneyValue(p.rawRow[commPriceIdx]);
            });
        } else {
            (processed || []).forEach(p => {
                if (p.isTotalRow) return;
                if (isReplaced(p)) return;
                if (commPriceIdx !== -1) totalCommission += parseMoneyValue(p.rawRow[commPriceIdx]);
                if (otherCommIdx !== -1) totalOtherCommission += parseMoneyValue(p.rawRow[otherCommIdx]);
                if (referralFeeIdx !== -1) totalReferralFee += parseMoneyValue(p.rawRow[referralFeeIdx]);
            });
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

        rateCardsContainer.appendChild(makeTotalCard("Total Commission (RM)", totalCommission));
        rateCardsContainer.appendChild(makeTotalCard("Total Other Commission (RM)", totalOtherCommission));
        rateCardsContainer.appendChild(makeTotalCard("Total Referral Fee (RM)", totalReferralFee));
    }

    function getAgentTier(agentName) {
        if (!agentName) return "";
        if (state.activeAgentType === "outsource") return getOutsourceAgentTier(agentName);
        return getInternalAgentTier(agentName);
    }

    function getInternalAgentTier(agentName) {
        if (!agentName) return "";
        const n = agentName.toLowerCase().trim();
        if (n.includes("total") || n.includes("summary") || n.includes("grand")) return "";
        const seniors = ["sunny", "martin", "kent", "zhe hang", "ching zhe hang", "teng kah kent", "sunny tan", "martin hing"];
        if (seniors.some(s => n.includes(s))) return "Senior";
        return "Executive";
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

    // Same hex values as the Agent Tiers Legend swatches and the *-bg CSS
    // classes, so a special case's remark cell can be forced to match the
    // row's own tier color explicitly (in JS) instead of the CSS cascade,
    // which a more specific rule (.special-case-remarks-cell) was winning
    // over and turning pink regardless of the row's actual tier.
    const TIER_BG_COLORS = {
        "senior-bg": "#E0F2FE", "executive-bg": "#F0F9FF",
        "ogm-bg": "rgba(30, 58, 138, 0.28)",
        "oum-bg": "rgba(30, 58, 138, 0.16)",
        "osa-bg": "rgba(30, 58, 138, 0.07)",
        "total-bg": "#F8FAFB",
        "all-internal-bg": "#E0F2FE",
        "all-outsource-bg": "rgba(30, 58, 138, 0.16)"
    };

    function getRowClass(agentName, isOutsource) {
        if (!agentName) return "";
        const nameLower = agentName.toLowerCase();
        if (nameLower.includes("total") || nameLower.includes("summary") || nameLower.includes("grand")) return "total-bg";
        if (state.activeAgentType === "all") {
            const t = state.rawData?.agentTypeMap?.[nameLower];
            if (t === "internal") {
                const tier = getInternalAgentTier(agentName);
                if (tier === "Senior") return "senior-bg";
                if (tier === "Executive") return "executive-bg";
                return "all-internal-bg";
            }
            if (t === "outsource") {
                const tier = getOutsourceAgentTier(agentName);
                if (tier === "OGM") return "ogm-bg";
                if (tier === "OUM") return "oum-bg";
                if (tier === "OSA") return "osa-bg";
                return "all-outsource-bg";
            }
            return "";
        }
        if (isOutsource) {
            const tier = getOutsourceAgentTier(agentName);
            if (tier === "OGM") return "ogm-bg";
            if (tier === "OUM") return "oum-bg";
            if (tier === "OSA") return "osa-bg";
            return "all-outsource-bg";
        }
        const tier = getInternalAgentTier(agentName);
        if (tier === "Senior") return "senior-bg";
        if (tier === "Executive") return "executive-bg";
        return "all-internal-bg";
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

    function showCommCalcTooltip(e, info) {
        const el = getOrCreateCommCalcTooltip();
        if (!el || !info) return;
        const safeTitle = escapeHtml(info.title);
        const safeFormula = escapeHtml(info.formula);
        el.innerHTML = `
            <div class="tooltip-header">${safeTitle}</div>
            <div class="tooltip-formula">${safeFormula}</div>
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
            if (row.specialCaseData) {
                return {
                    title: `Basic Commission — ${custName || "Special Case"}`,
                    formula: `Sales Price = Total Amount - EPP Effective`,
                    subtext: `Basic Commission = Sales Price × Rate % = ${fmtNum(sales)} × Rate = ${commValStr}` + clickTip
                };
            }
            if (sales > 0 && commPrice > 0) {
                const ratePct = ((commPrice / sales) * 100).toFixed(2);
                return {
                    title: `Basic Commission — ${custName}`,
                    formula: `Sales Price = Total Amount - EPP Effective`,
                    subtext: `Basic Commission = Sales Price × Rate % = ${fmtNum(sales)} × ${ratePct}% = ${commValStr}` + clickTip
                };
            }
            return {
                title: `Basic Commission — ${custName}`,
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
    function renderAgentSummaryInline(agSummarySection, isOutsource, visibleAgents) {
        const headers = agSummarySection.headers;
        const agentColIdx = headers.findIndex(h => h.toLowerCase().trim() === "agent");
        const totalInvColIdx = getCountColumnIdx(headers);
        const overrideColIdx = headers.findIndex(h => h.toLowerCase().includes("override"));
        const referralFeeColIdx = headers.findIndex(h => h.toLowerCase().trim() === "referral fee");

        const userObj = USER_ROLES[state.currentUser];
        const originalRows = agSummarySection.rows;
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
            // Filter by search box (agent name match)
            if (!matchesSearch(tempCur, "")) return;
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
        headers.forEach(h => {
            const th = document.createElement("th");
            th.textContent = h;
            if (isNumericHeader(h)) th.classList.add("numeric");
            else if (isDateHeader(h)) th.classList.add("date");
            trHead.appendChild(th);
        });
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
        const cellGrid = [];
        for (let ri = 0; ri < N; ri++) {
            cellGrid.push(rows[ri].map(val => ({
                value: val === null || val === undefined || String(val).trim() === "" ? "-" : String(val),
                rowspan: 1, visible: true
            })));
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
                if (ci === agentColIdx && !agentVisible[ri]) continue;
                if (ci === totalInvColIdx && !totalInvVisible[ri]) continue;
                if (ci === referralFeeColIdx && !referralFeeVisible[ri]) continue;
                if (colsToMerge.includes(ci) && !cellGrid[ri][ci].visible) continue;
                const td = document.createElement("td");
                td.innerHTML = cellGrid[ri][ci].value;
                if (ci === agentColIdx && cellGrid[ri][ci].value !== "-") td.textContent = resolveAgentName(cellGrid[ri][ci].value);
                if (ci === agentColIdx && agentSpans[ri] > 1) td.rowSpan = agentSpans[ri];
                if (ci === totalInvColIdx && totalInvVisible[ri] && totalInvSpans[ri] > 1) td.rowSpan = totalInvSpans[ri];
                if (ci === referralFeeColIdx && referralFeeVisible[ri] && referralFeeSpans[ri] > 1) td.rowSpan = referralFeeSpans[ri];
                if (colsToMerge.includes(ci) && cellGrid[ri][ci].rowspan > 1) td.rowSpan = cellGrid[ri][ci].rowspan;
                const colHeader = headers[ci];
                if (isNumericHeader(colHeader)) td.classList.add("numeric");
                else if (isDateHeader(colHeader)) td.classList.add("date");
                if (ci === overrideColIdx && cellGrid[ri][ci].value !== "-") { td.style.fontWeight = "600"; td.style.color = "#1d4ed8"; }

                if (colHeader.toLowerCase().trim() === "commission price") {
                    const calcInfo = getCommissionCalcBreakdown(row, headers, fullAgentNames[ri], "", ri, rows);
                    if (calcInfo) {
                        td.addEventListener("mouseenter", (e) => showCommCalcTooltip(e, calcInfo));
                        td.addEventListener("mousemove", (e) => moveCommCalcTooltip(e));
                        td.addEventListener("mouseleave", hideCommCalcTooltip);
                    }
                }

                tr.appendChild(td);
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
        renderLegend();
        updateNoteBox();

        if (rm300FilterGroup) {
            rm300FilterGroup.style.display = state.activeSection === "basic_nfp" ? "" : "none";
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
            noDataView.classList.remove("hidden");
            rowCount.textContent = "0 rows"; totalAgents.textContent = "0"; totalCustomers.textContent = "0";
            renderSectionTotalCards();
            return;
        }

        let headers = [...sectionData.headers];
        if (state.activeSection === "basic_nfp" && state.specialCaseRowRefs.size === 0) {
            headers = headers.filter(h => h !== "Remarks");
        }
        const rows = sectionData.rows;
        const isOutsource = state.activeAgentType === "outsource";
        const agentIdx = headers.findIndex(h => h.toLowerCase().trim() === "agent");
        const customerIdx = headers.findIndex(h => h.toLowerCase().trim() === "customer");
        const commissionIdx = headers.findIndex(h => h.toLowerCase().trim() === "commission");
        const commissionPriceIdx = headers.findIndex(h => h.toLowerCase().trim() === "commission price");
        const overrideColIdx = headers.findIndex(h => h.toLowerCase().includes("override"));
        const totalInvColIdx = getCountColumnIdx(headers);
        const rm300ColIdx = headers.findIndex(h => h.toLowerCase().includes("rm300") || h.toLowerCase().includes("basic commission (rm"));
        const rm300FilterActive = state.activeSection === "basic_nfp" && state.filters.rm300Only && rm300ColIdx !== -1;
        const hasRm300Value = (rawRow) => {
            const v = String(rawRow[rm300ColIdx] || "").trim().toLowerCase();
            return v !== "" && v !== "-" && v !== "pending" && v !== "invoice before july";
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
            if (applySearch && rm300FilterActive && !hasRm300Value(p.rawRow)) return false;

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
            renderBasicNfpTotalCards(unsearchedProcessed, headers);
        } else {
            renderSectionTotalCards();
        }

        tableHeaders.innerHTML = "";
        headers.forEach(h => {
            const th = document.createElement("th");
            th.textContent = h;
            if (isNumericHeader(h)) th.classList.add("numeric");
            else if (isDateHeader(h)) th.classList.add("date");
            tableHeaders.appendChild(th);
        });
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

                if (hasAgentSummary) renderAgentSummaryInline(agSummarySection, isOutsource, visibleAgents);
            } else {
                if (hasAgentSummary) renderAgentSummaryInline(agSummarySection, isOutsource, visibleAgents);

                const legendCard = document.getElementById("legendCard");
                const legendVisible = legendCard && !legendCard.classList.contains("hidden");
                let actionsTarget = legendCard;
                if (!legendVisible) {
                    actionsTarget = document.createElement("div");
                    actionsTarget.className = "agent-summary-inline-actions";
                    tableContainer.insertBefore(actionsTarget, dataTable);
                }
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
                const isSpecialRow = state.specialCaseRowRefs.has(row);
                const tr = document.createElement("tr");
                if (rowClass) tr.className = rowClass;
                if (isSpecialRow) tr.classList.add("special-case-row");
                const groupAnchorIndex = specialCaseGroupAnchorByRowIndex[ri];
                const isGroupAnchor = groupAnchorIndex === ri;
                const isGroupFollower = groupAnchorIndex !== ri;

                for (let ci = 0; ci < headers.length; ci++) {
                    const cellVal = row[ci];
                    if (ci === agentIdx && !agentVisible[ri]) continue;
                    if (ci === totalInvColIdx && !totalInvVisible[ri]) continue;
                    if (colsToMerge.includes(ci) && !cellGrid[ri][ci].visible) continue;
                    if (isGroupFollower && specialCaseMergeCols.includes(ci)) continue;
                    const td = document.createElement("td");
                    const displayVal = cellGrid[ri][ci] ? cellGrid[ri][ci].value : (cellVal === null || cellVal === undefined ? "-" : String(cellVal));
                    td.innerHTML = displayVal;
                    if (ci === agentIdx && displayVal !== "-") td.textContent = resolveAgentName(displayVal);
                    if (ci === agentIdx && agentSpans[ri] > 1) td.rowSpan = agentSpans[ri];
                    if (ci === totalInvColIdx && totalInvVisible[ri] && totalInvSpans[ri] > 1) td.rowSpan = totalInvSpans[ri];
                    if (colsToMerge.includes(ci) && cellGrid[ri][ci] && cellGrid[ri][ci].rowspan > 1) td.rowSpan = cellGrid[ri][ci].rowspan;
                    if (isGroupAnchor && specialCaseMergeCols.includes(ci)) td.rowSpan = specialCaseGroupSpanByAnchor.get(ri) || 1;
                    const colHeader = headers[ci];
                    if (isNumericHeader(colHeader)) td.classList.add("numeric");
                    else if (isDateHeader(colHeader)) td.classList.add("date");
                    if (ci === overrideColIdx && displayVal !== "-") { td.style.fontWeight = "600"; td.style.color = "#1d4ed8"; }

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



                    // Item 4: Red remark TEXT for special case rows, but the cell's
                    // BACKGROUND matches the row's own tier color (same as the
                    // Agent Tiers Legend) instead of standing out — including when
                    // there's no real remark and the cell just shows "-".
                    if (isSpecialRow && remarksIdx !== -1 && ci === remarksIdx) {
                        td.textContent = (row.specialCaseData && row.specialCaseData.remarks) ? String(row.specialCaseData.remarks) : displayVal;
                        td.classList.add("new-commission-value");
                        td.classList.add("special-case-remarks");
                        td.classList.add("special-case-remarks-cell");
                        td.style.whiteSpace = "normal";
                        const tierColor = TIER_BG_COLORS[rowClass];
                        if (tierColor) td.style.setProperty("background-color", tierColor, "important");
                        else td.style.removeProperty("background-color");
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

                    // Factory: green Insert Profit Sharing / clickable commission price
                    if (!isTotalRow && !isSpecialRow && ci === commissionPriceIdx && isFactoryBasicCommission(row, headers, agentName, custName)) {
                        const factoryDisplay = getFactoryCellDisplay(row, headers, agentName, custName, "agent");
                        if (factoryDisplay) td.innerHTML = factoryDisplay;
                        if (userObj && !userObj.readOnly) {
                            td.style.cursor = "pointer"; td.title = "Click to set Factory profit sharing rate";
                            td.addEventListener("click", () => openFactoryRateModal(row, headers, agentName, custName, "agent"));
                        }
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
                    if (isAgentHeader(colHeader) && cellVal != null && String(cellVal).trim()) td.textContent = resolveAgentName(cellVal);

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

        // Table 3: Team Sales Details
        if (matchingDetailRows.length > 0) {
            wrapper.appendChild(createBonusSubTable("Production Bonus Summary by customer", detailHeaders, matchingDetailRows));
        }

        tableContainer.appendChild(wrapper);
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
            teamIdx === -1 ? [] : matchingT3Rows.map(row => String(row[teamIdx] || "").trim().toLowerCase())
        );
        const t1Restricted = !!(userObj && userObj.filterAgentName !== null) || !!state.filters.search;
        const t1Rows = (contestData.rows_t1 || []).filter(row => {
            if (!t1Restricted) return true;
            return visibleTeams.has(String(row[0] || "").trim().toLowerCase());
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

        if (t1Rows.length > 0) {
            wrapper.appendChild(createBonusSubTable("Team Championship and Team Achievement Bonus", contestData.headers_t1, t1Rows, captains));
        }
        if (matchingT3Rows.length > 0) {
            wrapper.appendChild(createBonusSubTable("Summary of Cases and Awards by Agent", t3Headers, matchingT3Rows, captains));
        }

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
            legend.innerHTML = `<em>Gold, Silver, Bronze badges indicate Rank 1, 2, and 3 respectively.</em>`;
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
                if (isAgentHeader(colHeader) && displayVal !== "-") td.textContent = resolveAgentName(displayVal);

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

        const userObj = USER_ROLES[state.currentUser];

        const matchingT2Rows = t2Rows.filter(row => {
            const agentVal = agentIdx !== -1 && row[agentIdx] ? String(row[agentIdx]) : "";
            const customerVal = customerIdx !== -1 && row[customerIdx] ? String(row[customerIdx]) : "";
            if (userObj && userObj.filterAgentName !== null) {
                if (agentVal.toLowerCase().trim() !== userObj.filterAgentName.toLowerCase().trim()) {
                    return false;
                }
            }
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
                if (isAgentHeader(colHeader) && displayVal !== "-") td.textContent = resolveAgentName(displayVal);

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
        return parseFloat(String(val).replace(/[^0-9.-]/g, "")) || 0;
    }

    function formatRM(value) {
        return `RM ${value.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
    }

    function getFactoryBaseRate(target) {
        if (target === "safwan") return 0.5;
        return state.activeAgentType === "outsource" ? 2.5 : 2.0;
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

    function getFactoryCellDisplay(row, headers, fullAgentName, customerName, target) {
        const salesIdx = headers.findIndex(h => h.toLowerCase().trim() === "sales price" || h.toLowerCase().trim() === "total amount");
        const sales = salesIdx !== -1 ? parseMoneyValue(row[salesIdx]) : 0;
        const rateData = findFactoryRate(fullAgentName, customerName);
        const profitSharing = getFactoryProfitSharingValue(rateData, target);
        if (profitSharing === null) return '<span class="profit-sharing-value">Insert Profit Sharing</span>';
        return formatRM(getFactoryCommissionValue(sales, profitSharing, target));
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
    // Dynamic Legend & Notes Box
    // -------------------------------------------------------------
    function renderLegend() {
        const legendCard = document.getElementById("legendCard");
        if (!legendCard) return;
        
        if (state.activeSection === "monthly_contest") {
            legendCard.innerHTML = "";
            legendCard.classList.add("hidden");
            return;
        }

        legendCard.classList.remove("hidden");

        if (state.activeAgentType === "all") {
            legendCard.innerHTML = `
                <div class="legend-details">
                    <span class="legend-label">Agent Tiers Legend</span>
                    <div class="legend-items" style="align-items: center;">
                        <span class="legend-item-group-title" style="font-weight: 600; color: #475569; margin-right: 2px;">Internal:</span>
                        <span class="legend-item"><span class="legend-dot" style="background-color: #E0F2FE; border: 1px solid #7DD3FC;"></span>Senior</span>
                        <span class="legend-item"><span class="legend-dot" style="background-color: #F0F9FF; border: 1px solid #bae6fd;"></span>Executive</span>
                        <span class="legend-divider" style="border-left: 1px solid #cbd5e1; margin: 0 8px; height: 16px; display: inline-block;"></span>
                        <span class="legend-item-group-title" style="font-weight: 600; color: #475569; margin-right: 2px;">Outsource:</span>
                        <span class="legend-item"><span class="legend-dot" style="background-color: rgba(30, 58, 138, 0.28); border: 1px solid rgba(30, 58, 138, 0.55);"></span>OGM</span>
                        <span class="legend-item"><span class="legend-dot" style="background-color: rgba(30, 58, 138, 0.16); border: 1px solid rgba(30, 58, 138, 0.4);"></span>OUM</span>
                        <span class="legend-item"><span class="legend-dot" style="background-color: rgba(30, 58, 138, 0.07); border: 1px solid rgba(30, 58, 138, 0.28);"></span>OSA</span>
                    </div>
                </div>
            `;
            return;
        }

        let showInternalTiers = state.activeAgentType === "internal";
        
        if (showInternalTiers) {
            legendCard.innerHTML = `
                <div class="legend-details">
                    <span class="legend-label">Agent Tiers Legend</span>
                    <div class="legend-items">
                        <span class="legend-item"><span class="legend-dot" style="background-color: #E0F2FE; border: 1px solid #7DD3FC;"></span>Senior</span>
                        <span class="legend-item"><span class="legend-dot" style="background-color: #F0F9FF; border: 1px solid #bae6fd;"></span>Executive</span>
                    </div>
                </div>
            `;
        } else {
            legendCard.innerHTML = `
                <div class="legend-details">
                    <span class="legend-label">Agent Tiers Legend</span>
                    <div class="legend-items">
                        <span class="legend-item"><span class="legend-dot" style="background-color: rgba(30, 58, 138, 0.28); border: 1px solid rgba(30, 58, 138, 0.55);"></span>OGM</span>
                        <span class="legend-item"><span class="legend-dot" style="background-color: rgba(30, 58, 138, 0.16); border: 1px solid rgba(30, 58, 138, 0.4);"></span>OUM</span>
                        <span class="legend-item"><span class="legend-dot" style="background-color: rgba(30, 58, 138, 0.07); border: 1px solid rgba(30, 58, 138, 0.28);"></span>OSA</span>
                    </div>
                </div>
            `;
        }
    }

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

        try {
            const res = await fetch("/api/special-cases", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify({
                    year: state.activeYear,
                    month: state.activeMonth,
                    agent_type: state.activeAgentType,
                    special_cases: specialCases,
                    deleted: deleted
                })
            });
            if (!res.ok) {
                console.error("Failed to save special cases to server");
                return;
            }
            // Only clear the queue once the server has accepted the removals,
            // so a failed request retries them on the next save.
            pendingSpecialCaseDeletes = pendingSpecialCaseDeletes.filter(
                d => !deleted.includes(d));
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
        modalRemarks.value = data.remarks || "";
        
        onSpecialCaseTypeChange();
        confirmModalBtn.textContent = "Save Changes";
        specialCaseModal.classList.remove("hidden");
    }

    function closeModal() {
        state.pendingAddRowRef = null;
        specialCaseModal.classList.add("hidden");
    }

    // Modal Input & Select Listeners
    [modalSalesPrice, modalRatePct, modalSystemPrice, modalNetFloorPrice, modalFeeWaiver, modalAdjustedSalesPrice].forEach(input => {
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

            // OGM override: Gan Lai Soon earns 0.75% of the sales price on every
            // OUM/OSA invoice (outsource_basic_commission.py), so a special case
            // booked under one of his agents has to fill his column too. He earns
            // nothing on his own invoices.
            const caseAgentType = state.activeAgentType === "all"
                ? String(state.rawData?.agentTypeMap?.[String(agent).toLowerCase()] || "")
                : state.activeAgentType;
            const ganLaiSoonComm = (caseAgentType === "outsource"
                && getOutsourceAgentTier(agent) !== "OGM")
                ? salesForCalc * 0.0075 : 0;
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
            const rowKind = state.specialCaseMode === "customer"
                ? "all" : (state.specialCaseRowKind || "all");
            // Exception: Adjusted Sales Price changes the figure BOTH
            // commissions are derived from (basic = sales × rate, NFP =
            // (sales − net floor price) × rate), so it always restates both —
            // whichever row it was raised from. Every other type touches only
            // one side of the calculation.
            const affectsBothCommissions = specialCaseType === "adjusted_sales_price";
            const wantsBasic = affectsBothCommissions || rowKind === "basic" || rowKind === "all";
            const wantsNfp = affectsBothCommissions || rowKind === "nfp" || rowKind === "all";

            const dataObject = {
                agent, customer, pkg, system, nfp, sales, rate, remarks,
                profitSharingPct, specialCaseType, feeWaiver, adjustedSalesPrice,
                caseType: state.specialCaseMode, rowKind
            };

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
            target
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
    }

    function closeFactoryModal() {
        state.editingFactoryRate = null;
        if (factoryRateModal) factoryRateModal.classList.add("hidden");
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
    // Sends the user to the Data page, on the Basic & Net Floor Price
    // Commission section, with the list already open on the same month
    // the report page was showing.
    function goToNfpListPage() {
        const month = String(state.activeMonth || "").padStart(2, "0");
        window.location.href = `/data?section=basic_nfp&showNfpList=1&month=${encodeURIComponent(month)}`;
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
        const otherCommIdx = headers.findIndex(h => h.toLowerCase().includes("other comm") || h.toLowerCase().includes("other commission"));

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

        if (customerGroups.size === 0) {
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

            const remarks = remarksIdx !== -1 && basicRow[remarksIdx] ? String(basicRow[remarksIdx]).trim() : "";
            if (remarks && remarks !== "-") {
                notesList.push(`Item No. ${itemNo} - ${remarks}`);
            }

            const refPerson = refPersonIdx !== -1 && basicRow[refPersonIdx] ? String(basicRow[refPersonIdx]).trim() : "-";
            const refRate = refRateIdx !== -1 ? parseVal(basicRow[refRateIdx]) : 0;
            const refFee = refFeeIdx !== -1 ? parseVal(basicRow[refFeeIdx]) : 0;
            const otherCommVal = otherCommIdx !== -1 ? parseVal(basicRow[otherCommIdx]) : 0;

            totalCommSum += basicCommVal + otherCommVal;

            const tr1 = document.createElement("tr");
            tr1.innerHTML = `
                <td style="text-align: center;">${itemNo}</td>
                <td><b>${escapeHtml(customerName)}</b></td>
                <td class="numeric">${sysPrice > 0 ? formatRM(sysPrice) : "-"}</td>
                <td class="numeric">${salesPrice > 0 ? formatRM(salesPrice) : "-"}</td>
                <td style="text-align: center;">${basicRate.toFixed(2)}%</td>
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


