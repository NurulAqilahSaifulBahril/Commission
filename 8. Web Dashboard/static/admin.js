(function () {
    "use strict";

    async function api(path, options) {
        const res = await fetch(path, options);
        if (res.status === 401) {
            window.location.href = "/login?next=/admin";
            throw new Error("Not authenticated");
        }
        if (res.status === 403) {
            document.body.innerHTML = "<p style='padding:32px;font-family:sans-serif;'>Admin access required. <a href='/'>Back to dashboard</a></p>";
            throw new Error("Not authorized");
        }
        return res;
    }

    function fmtDate(iso) {
        if (!iso) return "-";
        try {
            return new Date(iso).toLocaleString();
        } catch (e) {
            return iso;
        }
    }

    async function loadMe() {
        const res = await api("/api/me");
        const me = await res.json();
        document.getElementById("accountWhoami").textContent = `${me.username} (${me.role})`;
        return me;
    }

    async function loadUsers() {
        const res = await api("/api/admin/users");
        const users = await res.json();
        document.getElementById("userCountBadge").textContent = `${users.length} user${users.length === 1 ? "" : "s"}`;
        const tbody = document.getElementById("usersBody");
        tbody.innerHTML = "";
        users.forEach((u) => {
            const tr = document.createElement("tr");
            if (!u.is_active) tr.classList.add("inactive-row");
            tr.innerHTML = `
                <td>${escapeHtml(u.username)}</td>
                <td>${escapeHtml(u.role)}</td>
                <td>${u.is_active ? "Active" : "Deactivated"}</td>
                <td>${fmtDate(u.created_at)}</td>
                <td class="row-actions">
                    <button class="btn btn-secondary" data-action="role" data-id="${u.id}" data-role="${u.role}">Toggle role</button>
                    <button class="btn btn-secondary" data-action="reset" data-id="${u.id}">Reset password</button>
                    ${u.is_active ? `<button class="btn btn-danger" data-action="deactivate" data-id="${u.id}">Deactivate</button>` : ""}
                </td>
            `;
            tbody.appendChild(tr);
        });

        tbody.querySelectorAll("button[data-action]").forEach((btn) => {
            btn.addEventListener("click", () => handleUserAction(btn));
        });
    }

    async function handleUserAction(btn) {
        const id = btn.getAttribute("data-id");
        const action = btn.getAttribute("data-action");

        if (action === "deactivate") {
            if (!confirm("Deactivate this user? They will no longer be able to log in.")) return;
            await api(`/api/admin/users/${id}`, { method: "DELETE" });
            loadUsers();
            loadAudit();
            return;
        }

        if (action === "role") {
            const currentRole = btn.getAttribute("data-role");
            const newRole = currentRole === "admin" ? "staff" : "admin";
            if (!confirm(`Change role to '${newRole}'?`)) return;
            await api(`/api/admin/users/${id}`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ role: newRole })
            });
            loadUsers();
            loadAudit();
            return;
        }

        if (action === "reset") {
            const password = prompt("New password (min 6 characters):");
            if (!password) return;
            if (password.length < 6) {
                alert("Password must be at least 6 characters.");
                return;
            }
            await api(`/api/admin/users/${id}`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ password })
            });
            alert("Password reset.");
            loadAudit();
        }
    }

    async function addUser() {
        const username = document.getElementById("newUsername").value.trim();
        const password = document.getElementById("newPassword").value;
        const role = document.getElementById("newRole").value;
        const errorBox = document.getElementById("userFormError");
        errorBox.textContent = "";

        if (!username || password.length < 6) {
            errorBox.textContent = "Username is required and password must be at least 6 characters.";
            return;
        }

        const res = await api("/api/admin/users", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ username, password, role })
        });
        const data = await res.json();
        if (!res.ok) {
            errorBox.textContent = data.error || "Failed to create user";
            return;
        }
        document.getElementById("newUsername").value = "";
        document.getElementById("newPassword").value = "";
        loadUsers();
        loadAudit();
    }

    async function loadAudit() {
        const entityType = document.getElementById("entityFilter").value;
        const qs = entityType ? `?entity_type=${encodeURIComponent(entityType)}` : "";
        const res = await api(`/api/admin/audit-log${qs}`);
        const entries = await res.json();
        document.getElementById("auditCountBadge").textContent = `${entries.length} entr${entries.length === 1 ? "y" : "ies"}`;
        const tbody = document.getElementById("auditBody");
        tbody.innerHTML = "";
        entries.forEach((entry) => {
            const tr = document.createElement("tr");
            const details = entry.before_json || entry.after_json
                ? `<details><summary>view</summary><div class="audit-diff">Before: ${escapeHtml(entry.before_json || "-")}\n\nAfter: ${escapeHtml(entry.after_json || "-")}</div></details>`
                : "-";
            tr.innerHTML = `
                <td>${fmtDate(entry.created_at)}</td>
                <td>${escapeHtml(entry.username)}</td>
                <td>${escapeHtml(entry.action)}</td>
                <td>${escapeHtml(entry.entity_type)}</td>
                <td>${escapeHtml(entry.entity_summary || "")}</td>
                <td class="audit-detail-cell">${details}</td>
            `;
            tbody.appendChild(tr);
        });
    }

    async function loadLoginLog() {
        const res = await api("/api/admin/login-log");
        const entries = await res.json();
        document.getElementById("loginCountBadge").textContent = `${entries.length} entr${entries.length === 1 ? "y" : "ies"}`;
        const tbody = document.getElementById("loginLogBody");
        tbody.innerHTML = "";
        entries.forEach((entry) => {
            const tr = document.createElement("tr");
            tr.innerHTML = `
                <td>${fmtDate(entry.created_at)}</td>
                <td>${escapeHtml(entry.username)}</td>
                <td>${escapeHtml(entry.role || "-")}</td>
                <td>${escapeHtml(entry.host || "-")}</td>
                <td>${escapeHtml(entry.ip_address || "-")}</td>
            `;
            tbody.appendChild(tr);
        });
    }

    function escapeHtml(str) {
        return String(str)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
    }

    document.getElementById("addUserBtn").addEventListener("click", addUser);
    document.getElementById("entityFilter").addEventListener("change", loadAudit);
    document.getElementById("logoutBtn").addEventListener("click", async () => {
        await fetch("/logout", { method: "POST" });
        window.location.href = "/login";
    });

    loadMe().then(() => {
        loadUsers();
        loadLoginLog();
        loadAudit();
    }).catch(() => {});
})();
