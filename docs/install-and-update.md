# Commission Portal — Install & Update

A short guide for everyone using the Commission Portal — on **Windows or Mac**.
You install it **once**. After that it keeps itself up to date.

*(Once it is running, see [Using the Commission Portal](using-the-portal.md).)*

> A printable copy lives at
> [Commission-Portal-User-Guide.pdf](Commission-Portal-User-Guide.pdf). Its
> content is laid out by `10. Electron App/build_user_guide_pdf.py`, which does
> **not** read this file — edit both, then re-run that script so the two stay in
> step.

---

## Part 1 — Installing

Set aside about 5 minutes. There is nothing to install beforehand — the Portal
brings everything it needs with it.

### Step 1 — Download the Portal

1. Go to the [Commission Portal download page](https://github.com/NurulAqilahSaifulBahril/Commission/releases/latest).
2. Scroll down to the **Assets** list.
3. **Windows:** click `CommissionDashboard-Setup-….exe` to download it.
4. **Mac:** first check which chip your Mac has: Apple menu → **About This
   Mac**. If it says **Apple M1/M2/M3…**, download
   `CommissionDashboard-Setup-…-macos-arm64.dmg`. If it says **Intel**,
   download `CommissionDashboard-Setup-…-macos-intel.dmg`.

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

1. Open the `.dmg` file you just downloaded.
2. Drag **CommissionDashboard** onto the **Applications** shortcut next to it.
3. Open **Applications** and double-click **CommissionDashboard**.
4. **macOS will refuse the first time**, saying it is from an unidentified
   developer. This is normal for apps outside the App Store. Right-click
   **CommissionDashboard**, choose **Open**, then click **Open** again.
5. **On macOS Sequoia or newer** there is no Open button in that message. Go to
   Apple menu → **System Settings** → **Privacy & Security**, scroll down, and
   click **Open Anyway**.
6. **The first start sets itself up before the dashboard appears.** It unpacks
   about 700 MB and takes a minute or two; the window tells you what it is
   doing. Later starts are quick.

### Step 3 — Open the Portal

**Windows:** Start Menu → **Finance Commission Dashboard** (or the desktop
shortcut).

**Mac:** **Applications** → **CommissionDashboard**.

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

![The Software Update panel, showing a new version available with an Install Update button](img/update-panel.png)

**If you see an "Install Update" button:**

1. Click **Install Update**.
2. Click **OK** on the confirmation message.
3. Wait. A progress bar runs, the Portal restarts itself, and the page reloads
   on its own. This takes a minute or two.
4. **Do not close the window while it is working.**

**If you do not see a button**, the box says *"Ask an admin to install it."* —
there is nothing for you to do. Let your admin know.

### Things worth knowing

- **Nothing of yours is lost.** Your login, your Excel files, saved reports and
  any rates you edited all stay exactly as they are. An update only replaces
  the program itself.
- **You never download the installer again.** Steps 1–3 above are one time only.
- **To check your version:** look at the bottom-left corner of the menu.
- **If an update fails**, the Portal puts the old version back by itself and
  keeps working. Tell IT so they can look into it.

---

## If something goes wrong

| What you see | What to do |
|---|---|
| The Portal will not open | Tell IT — ask them to check `dashboard.log` in the Portal's folder |
| **Mac:** "cannot be opened because it is from an unidentified developer" | Expected once, the first time you open it. Right-click **CommissionDashboard** and choose **Open**, then **Open** again. On macOS Sequoia: System Settings → Privacy & Security → **Open Anyway**. |
| **Mac:** the window sits on "Setting up the dashboard for the first time" | That is the first launch unpacking about 700 MB. Give it a minute or two. |
| **Mac:** "you don't have permission" when dragging to Applications | Drop it into your own Applications folder instead: Finder → **Go** → **Home** → **Applications**. The Portal runs the same from either. |
| **Mac:** the Portal does not come back by itself after an update, and the page will not reload | Expected once, when updating from version 1.2.25 or earlier. Quit the Portal and open it again from **Applications** → **Commission Dashboard**. The update itself already installed correctly, and later updates restart on their own. |
| An update failed | Tell IT — ask them to check `dashboard.log` |
| Numbers look out of date | Wait a few minutes, or click **Sync Data** at the top right |
