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
                    <button class="btn btn-secondary" data-action="password" data-id="${u.id}" data-username="${escapeHtml(u.username)}" data-active="${u.is_active ? "1" : "0"}">Set password</button>
                    ${u.is_active
                        ? `<button class="btn btn-danger" data-action="deactivate" data-id="${u.id}">Deactivate</button>`
                        : `<button class="btn btn-primary" data-action="reactivate" data-id="${u.id}">Reactivate</button>`}
                </td>
            `;
            tbody.appendChild(tr);
        });

        tbody.querySelectorAll("button[data-action]").forEach((btn) => {
            btn.addEventListener("click", () => handleUserAction(btn));
        });
    }

    // These calls used to ignore the response, so anything the server
    // rejected -- a password under six characters, a name already taken --
    // still looked like it had worked.
    async function sendUserUpdate(id, payload, method) {
        const options = { method: method || "PUT" };
        if (payload) {
            options.headers = { "Content-Type": "application/json" };
            options.body = JSON.stringify(payload);
        }
        const res = await api(`/api/admin/users/${id}`, options);
        let data = {};
        try {
            data = await res.json();
        } catch (e) {
            // some responses carry no body; the status is what matters
        }
        if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
        return data;
    }

    async function handleUserAction(btn) {
        const id = btn.getAttribute("data-id");
        const action = btn.getAttribute("data-action");

        if (action === "password") {
            openPasswordModal(id, btn.getAttribute("data-username"),
                              btn.getAttribute("data-active") === "1");
            return;
        }

        let payload = null;
        let method = "PUT";

        if (action === "deactivate") {
            if (!confirm("Deactivate this user? They will no longer be able to log in.")) return;
            method = "DELETE";
        } else if (action === "reactivate") {
            if (!confirm("Reactivate this user? They will be able to log in again with their existing password.")) return;
            payload = { is_active: true };
        } else if (action === "role") {
            const currentRole = btn.getAttribute("data-role");
            const newRole = currentRole === "admin" ? "staff" : "admin";
            if (!confirm(`Change role to '${newRole}'?`)) return;
            payload = { role: newRole };
        } else {
            return;
        }

        btn.disabled = true;
        try {
            await sendUserUpdate(id, payload, method);
        } catch (e) {
            alert(e.message);
            return;
        } finally {
            btn.disabled = false;
        }
        loadUsers();
        loadAudit();
    }

    // ── Set-password dialog ──────────────────────────────────────────────────
    // A dialog rather than prompt(): the Portal runs inside Electron, which
    // does not implement window.prompt(), so the old Reset password button
    // did nothing at all on the desktop app.
    let pwUserId = null;

    function setPwVisibility(show) {
        const type = show ? "text" : "password";
        document.getElementById("pwNew").type = type;
        document.getElementById("pwConfirm").type = type;
    }

    function openPasswordModal(id, username, isActive) {
        pwUserId = id;
        document.getElementById("pwTargetName").textContent = username;
        document.getElementById("pwNew").value = "";
        document.getElementById("pwConfirm").value = "";
        document.getElementById("pwShow").checked = false;
        setPwVisibility(false);
        document.getElementById("pwError").textContent = "";
        // Reactivating someone and handing them a new password is one errand,
        // so a deactivated account offers both in the same dialog.
        document.getElementById("pwReactivateRow").style.display = isActive ? "none" : "flex";
        document.getElementById("pwReactivate").checked = !isActive;
        document.getElementById("pwModal").style.display = "flex";
        document.getElementById("pwNew").focus();
    }

    function closePasswordModal() {
        document.getElementById("pwModal").style.display = "none";
        // Do not leave the typed password sitting in the DOM.
        document.getElementById("pwNew").value = "";
        document.getElementById("pwConfirm").value = "";
        pwUserId = null;
    }

    async function savePassword() {
        if (pwUserId === null) return;
        const password = document.getElementById("pwNew").value;
        const confirmation = document.getElementById("pwConfirm").value;
        const errorBox = document.getElementById("pwError");
        errorBox.textContent = "";

        if (password.length < 6) {
            errorBox.textContent = "Password must be at least 6 characters.";
            return;
        }
        if (password !== confirmation) {
            errorBox.textContent = "The two passwords do not match.";
            return;
        }

        const payload = { password };
        const reactivate = document.getElementById("pwReactivateRow").style.display !== "none"
            && document.getElementById("pwReactivate").checked;
        if (reactivate) payload.is_active = true;

        const saveBtn = document.getElementById("pwSaveBtn");
        saveBtn.disabled = true;
        try {
            await sendUserUpdate(pwUserId, payload);
        } catch (e) {
            errorBox.textContent = e.message;
            return;
        } finally {
            saveBtn.disabled = false;
        }

        closePasswordModal();
        loadUsers();
        loadAudit();
        alert(reactivate
            ? "Password set, and the account can log in again."
            : "Password set.");
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
    document.getElementById("pwSaveBtn").addEventListener("click", savePassword);
    document.getElementById("pwCancelBtn").addEventListener("click", closePasswordModal);
    document.getElementById("pwModalClose").addEventListener("click", closePasswordModal);
    document.getElementById("pwModal").addEventListener("click", (e) => {
        if (e.target.id === "pwModal") closePasswordModal();
    });
    document.getElementById("pwShow").addEventListener("change", (e) => setPwVisibility(e.target.checked));
    ["pwNew", "pwConfirm"].forEach((id) => {
        document.getElementById(id).addEventListener("keydown", (e) => {
            if (e.key === "Enter") savePassword();
        });
    });
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && document.getElementById("pwModal").style.display !== "none") {
            closePasswordModal();
        }
    });
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
