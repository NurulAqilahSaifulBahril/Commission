# Commission Portal — Install & Update

A short guide for everyone using the Commission Portal — on **Windows or Mac**.
You install it **once**. After that it keeps itself up to date.

*(Once it is running, see [Using the Commission Portal](using-the-portal.md).)*

> The PDF version of this guide is generated from this file by
> `build_user_guide_pdf.py`. Edit this file, then re-run that script so the two
> stay in step.

---

## Part 1 — Installing

Set aside about 5 minutes. There is nothing to install beforehand — the Portal
brings everything it needs with it.

### Step 1 — Download the Portal

1. Go to the [Commission Portal download page](https://github.com/NurulAqilahSaifulBahril/Commission/releases/latest).
2. Scroll down to the **Assets** list.
3. **Windows:** click `CommissionDashboard-Setup-….exe` to download it.
4. **Mac:** click `CommissionDashboard-Setup-…-macos.zip` to download it.

### Step 2 — Install

**Windows**

1. Open the file you just downloaded.
2. **If Windows shows a blue "Windows protected your PC" box:** click
   **More info**, then **Run anyway**. This is normal — Windows shows it for any
   app it has not seen before.
3. Click **Next** through the screens. Tick **Create a desktop shortcut** if you
   would like one.
4. Click **Install**.

**Mac**

1. Open the `.zip` file you just downloaded (it usually unzips by itself).
2. Drag **Commission Portal.app** into your **Applications** folder.
3. **If macOS says the app is from an unidentified developer:** right-click the
   app, choose **Open**, then click **Open** again. This is normal for apps
   outside the App Store.

### Step 3 — Add your access keys

The Portal needs keys to reach the company data. **Ask IT for your access keys** —
they will send you a few lines of text that look like `SOMETHING=a-long-code`.

**Windows**

1. Open the Portal's folder. The installer shows you where it is — the folder is
   called **Commission Dashboard**.
2. Find the file named `.env` and open it with Notepad
   (right-click → **Open with** → **Notepad**).
3. Go to the end of the file and paste in the lines IT sent you, each on its own
   line.
4. Save (**Ctrl+S**) and close Notepad.

**Mac**

1. Open **Finder** → **Applications**.
2. Right-click **Commission Portal.app** → **Show Package Contents**.
3. Go to `Contents/Resources/` and open the **Commission Dashboard** folder.
4. Open `.env` with **TextEdit** (right-click → **Open With** → **TextEdit**).
5. Go to the end of the file and paste in the lines IT sent you, each on its own
   line.
6. Save (**Cmd+S**) and close TextEdit.

> Keep these keys private. Do not email them or send them in a chat message.
>
> **Note for IT:** accounts live in the shared database and are issued before
> the install, so the installer never asks for one. Use
> `8. Web Dashboard\create_admin.py` to add or reset an account, and seed `.env`
> during the install rather than leaving the keys to the user.

### Step 4 — Open the Portal

**Windows:** Start Menu → **Finance Commission Dashboard** (or the desktop
shortcut).

**Mac:** **Applications** → **Commission Portal**.

Log in with the **username and password IT gave you**. There is no account to
create — yours already exists. The first load takes a moment while it fetches
the year's data. **You are done.**

---

## Part 2 — Updating

**Short version: you don't have to do anything.** The Portal checks for new
versions by itself and tells you when one is ready. This works the same on
Windows and Mac.

### When an update is ready

A **Software Update** box appears at the **bottom of the left-hand menu**,
showing the new version number.

![The Software Update panel, showing a new version available with an Install Update button](update_panel_screenshot.png)

**If you see an "Install Update" button:**

1. Click **Install Update**.
2. Click **OK** on the confirmation message.
3. Wait. A progress bar runs, the Portal restarts itself, and the page reloads
   on its own. This takes a minute or two.
4. **Do not close the window while it is working.**

**If you do not see a button**, the box says *"Ask an admin to install it."* —
there is nothing for you to do. Let your admin know.

### Things worth knowing

- **Nothing of yours is lost.** Your login, your access key, your Excel files,
  saved reports and any rates you edited all stay exactly as they are. An update
  only replaces the program itself.
- **You never download the installer again.** Steps 1–4 above are one time only.
- **To check your version:** look at the bottom-left corner of the menu.
- **If an update fails**, the Portal puts the old version back by itself and
  keeps working. Tell IT so they can look into it.

---

## If something goes wrong

| What you see | What to do |
|---|---|
| "Token expired" | Ask IT for a new access key, then redo Step 3 |
| The Portal will not open | Tell IT — ask them to check `dashboard.log` in the Portal's folder |
| **Mac:** "cannot be opened because it is from an unidentified developer" | Right-click **Commission Portal.app** and choose **Open**, then click **Open** again. You only need to do this the first time. |
| **Mac:** "Commission Portal is damaged and can't be opened" | The download did not finish cleanly. Delete the app, download the `-macos.zip` again from the release page, and open it with right-click → **Open**. |
| An update failed | Tell IT — ask them to check `dashboard.log` |
| Numbers look out of date | Wait a few minutes, or click **Sync Data** at the top right |
