// Hide / show the left menu panel. One small button on the panel's edge; the
// choice is remembered in this browser (localStorage) and shared by every page
// with a sidebar, so hiding it on one page keeps it hidden on the next. The
// styling travels with the script, so a cached styles.css cannot leave the
// button unstyled.
(function () {
    const KEY = "sidebarHidden";
    const sidebar = document.querySelector(".app-container > .sidebar, aside.sidebar");
    if (!sidebar) return;

    const style = document.createElement("style");
    style.textContent = `
        body.sidebar-hidden aside.sidebar { display: none !important; }
        .sidebar-toggle-btn {
            position: fixed; top: 14px; z-index: 900;
            width: 26px; height: 26px; padding: 0;
            display: flex; align-items: center; justify-content: center;
            border-radius: 50%; border: 1px solid #cbd5e1;
            background: #ffffff; color: #334155;
            font-size: 15px; line-height: 1; cursor: pointer;
            box-shadow: 0 1px 4px rgba(15, 23, 42, 0.18);
        }
        .sidebar-toggle-btn:hover { background: #f1f5f9; }
        /* Inside the dark panel: a quiet light-on-dark button. */
        .sidebar-toggle-btn.in-panel {
            background: rgba(255, 255, 255, 0.08); color: #e2e8f0;
            border-color: rgba(255, 255, 255, 0.25); box-shadow: none;
        }
        .sidebar-toggle-btn.in-panel:hover { background: rgba(255, 255, 255, 0.18); }
        @media print { .sidebar-toggle-btn { display: none !important; } }
    `;
    document.head.appendChild(style);

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "sidebar-toggle-btn";
    document.body.appendChild(btn);

    function read() {
        try { return localStorage.getItem(KEY) === "1"; } catch (e) { return false; }
    }
    function write(hidden) {
        try { localStorage.setItem(KEY, hidden ? "1" : "0"); } catch (e) { /* private mode */ }
    }

    // Shown: the button sits inside the panel, in its top-right corner.
    // Hidden: at the very left of the page.
    function place() {
        const hidden = document.body.classList.contains("sidebar-hidden");
        btn.classList.toggle("in-panel", !hidden);
        btn.style.left = hidden ? "8px" : `${Math.max(8, sidebar.getBoundingClientRect().right - 26 - 10)}px`;
        btn.textContent = hidden ? "›" : "‹";
        btn.title = hidden ? "Show menu" : "Hide menu";
        btn.setAttribute("aria-label", btn.title);
    }

    function apply(hidden) {
        document.body.classList.toggle("sidebar-hidden", hidden);
        place();
        // Charts and wide tables size themselves on resize; let them re-measure.
        window.dispatchEvent(new Event("resize"));
    }

    btn.addEventListener("click", () => {
        const hidden = !document.body.classList.contains("sidebar-hidden");
        write(hidden);
        apply(hidden);
    });
    window.addEventListener("resize", place);
    apply(read());
})();
