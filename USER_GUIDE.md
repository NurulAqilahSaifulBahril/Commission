# Commission Portal — Install & Update

A short guide for everyone using the Commission Portal.
You install it **once**. After that it keeps itself up to date.

*(Once it is running, see [Using the Commission Portal](docs/using-the-portal.md).)*

---

## Part 1 — Installing

Set aside about 15 minutes. Most of it is waiting.

### Step 1 — Install Python (one time only)

The Portal needs a free program called Python to run.

1. Go to **<https://www.python.org/downloads/windows/>**
2. Click the big yellow **Download Python** button.
3. Open the file you downloaded.
4. **Important:** on the first screen, tick the box **"Add python.exe to PATH"** at
   the bottom. It is easy to miss.
5. Click **Install Now** and wait for it to finish.

> Already have Python? Skip this step. The Commission installer will tell you if
> it is missing.

### Step 2 — Download the Portal

1. Go to the **[Commission Portal download page](https://github.com/NurulAqilahSaifulBahril/Commission/releases/latest)**
2. Scroll down to the **Assets** list.
3. Click the file named **`CommissionDashboard-Setup-….exe`** to download it.

### Step 3 — Run the installer

1. Open the file you just downloaded.
2. **If Windows shows a blue "Windows protected your PC" box:** click
   **More info**, then **Run anyway**. This is normal — Windows shows it for any
   app it has not seen before.
3. Click **Next** through the screens. Tick **Create a desktop shortcut** if you
   would like one.
4. Click **Install**.

### Step 4 — Wait for the black window

A black window with scrolling text opens by itself. It is setting the Portal up.

- **This takes a few minutes.** Text scrolling past is normal.
- **Do not close it.** Let it finish on its own.

### Step 5 — Create your login

The black window asks you to create an account. Type a **username** and a
**password** and press Enter. Write them down — this is how you log in.

### Step 6 — Add your access keys

The Portal needs keys to reach the company data. **Ask IT for your access keys** —
they will send you a few lines of text that look like `SOMETHING=a-long-code`.

1. Open the Portal's folder. The installer shows you where it is — the folder is
   called **Commission Dashboard**.
2. Find the file named **`.env`** and open it with Notepad
   (right-click → **Open with** → **Notepad**).
3. Go to the end of the file and paste in the lines IT sent you, each on its own
   line.
4. Save (**Ctrl+S**) and close Notepad.

> Keep these keys private. Do not email them or send them in a chat message.
>
> **Note for IT:** the account-creation step above (Step 5) already needs
> `PG_MIRROR_TOKEN` to reach the shared database, so the keys have to be in
> place *before* that step, not after. In practice, seed `.env` during the
> install rather than leaving it to the user.

### Step 7 — Open the Portal

**Start Menu → Finance Commission Dashboard** (or the desktop shortcut).

Log in with the username and password from Step 5. The first load takes a moment
while it fetches the year's data. **You are done.**

---

## Part 2 — Updating

**Short version: you don't have to do anything.** The Portal checks for new
versions by itself and tells you when one is ready.

### When an update is ready

A **Software Update** box appears at the **bottom of the left-hand menu**,
showing the new version number.

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
- **You never download the installer again.** Steps 1–6 above are one time only.
- **To check your version:** look at the bottom-left corner of the menu.
- **If an update fails**, the Portal puts the old version back by itself and
  keeps working. Tell IT so they can look into it.

---

## If something goes wrong

| What you see | What to do |
|---|---|
| "Token expired" | Ask IT for a new access key, then redo Step 6 |
| The Portal will not open | Check Python is installed (Step 1), then tell IT |
| An update failed | Tell IT — ask them to check `dashboard.log` |
| Numbers look out of date | Wait a few minutes, or click **Sync Data** at the top right |
