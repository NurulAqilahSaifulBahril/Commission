// "Hi Name!" under "Commission Portal" in the sidebar of every page. The Name
// is the one set for the account on the Admin page; an account without one
// is greeted by its username.
(function () {
    const el = document.getElementById("sidebarGreeting");
    fetch("/api/me")
        .then((res) => (res.ok ? res.json() : null))
        .then((user) => {
            if (user) hideUnavailablePages(user.available_pages);
            if (!el) return;
            const name = String((user && (user.name || user.username)) || "").trim();
            if (!name) return;
            el.textContent = `Hi ${name}!`;
            el.hidden = false;
        })
        .catch(() => {});

    // Menu links and buttons to pages this account may not open are hidden
    // (Page Access on the Admin page); the server refuses them anyway.
    const COMMISSION = ["basic_nfp", "anp", "ega_esa", "production_bonus", "monthly_contest"];
    function pageOf(href) {
        let url;
        try { url = new URL(href, window.location.origin); } catch (e) { return null; }
        if (url.origin !== window.location.origin) return null;
        if (url.pathname === "/") return "overview";
        if (url.pathname === "/sales-report") return "sales_report";
        if (url.pathname === "/data") return "data";
        if (url.pathname === "/admin") return "admin";
        if (url.pathname === "/report") return url.searchParams.get("section") || "report";
        return null;
    }
    function hideUnavailablePages(pages) {
        if (!Array.isArray(pages)) return;
        const ok = (key) => key === "report" ? COMMISSION.some((k) => pages.includes(k)) : pages.includes(key);
        const sweep = () => {
            document.querySelectorAll("a[href]").forEach((a) => {
                const key = pageOf(a.getAttribute("href"));
                if (!key || ok(key)) return;
                const item = a.closest("li") || a;
                item.style.display = "none";
            });
            // The report page builds its section list itself.
            document.querySelectorAll("#sectionList li[data-section]").forEach((li) => {
                const key = li.getAttribute("data-section");
                if (COMMISSION.includes(key) && !pages.includes(key)) li.style.display = "none";
            });
            const dataGroup = document.getElementById("dataGroup");
            if (dataGroup && !pages.includes("data")) dataGroup.style.display = "none";
        };
        sweep();
        const list = document.getElementById("sectionList");
        if (list) new MutationObserver(sweep).observe(list, { childList: true });
    }
})();
