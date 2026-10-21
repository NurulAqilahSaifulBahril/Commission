// "Hi Name!" under "Commission Portal" in the sidebar of every page. The Name
// is the one set for the account on the Admin page; an account without one
// is greeted by its username.
(function () {
    const el = document.getElementById("sidebarGreeting");
    if (!el) return;
    fetch("/api/me")
        .then((res) => (res.ok ? res.json() : null))
        .then((user) => {
            const name = String((user && (user.name || user.username)) || "").trim();
            if (!name) return;
            el.textContent = `Hi ${name}!`;
            el.hidden = false;
        })
        .catch(() => {});
})();
