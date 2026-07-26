# GRUNTMASTER 6000

A load testing tool for Microsoft Copilot Studio bots. It simulates many users having real conversations with your bot at the same time, measures how fast the bot responds, and tells you whether it can handle your target load.

---

## Table of Contents

1. [What this tool does](#1-what-this-tool-does)
2. [How it works](#2-how-it-works)
3. [Prerequisites — what you need before starting](#3-prerequisites)
4. [Setup — end to end](#4-setup)
5. [Running the load test](#5-running-the-load-test)
6. [Reading the results](#6-reading-the-results)
7. [How it handles problems](#7-how-it-handles-problems)
8. [Troubleshooting](#8-troubleshooting)
9. [Beta Testing](#9-beta-testing)
10. [File Reference](#10-file-reference)

---

## 1. What this tool does

Your Copilot Studio bot is designed to handle one user at a time from the browser. Before going to production, you need to know: what happens when 50 users are talking to it simultaneously? Does it slow down? Does it fail?

This tool answers that question by:

- **Simulating multiple users at once.** Each simulated user follows a script — a list of messages to send — and behaves like a real person (it pauses between messages the way a human would).
- **Automating sign-in.** Copilot Studio bots protected by Microsoft sign-in require a real user identity to respond. This tool signs in each simulated user using a real Microsoft 365 account, the same way a browser would — but fully automated, so you can run 50 users without 50 browser windows.
- **Measuring response time.** The tool records exactly how long the bot takes to reply to each message, from the moment the message is sent to the moment the bot finishes replying.
- **Live terminal dashboard.** While the test runs, a real-time dashboard shows requests per second, response times, per-user stats, and a feed of test events — all in the terminal window, no browser required.

---

## 2. How it works

### 2.1 How the tool talks to the bot

The tool communicates with your bot through a messaging channel called **Direct Line**. Think of Direct Line as the back-door API that web chat widgets use to talk to bots. When someone chats with your bot on a website, the website is already using Direct Line behind the scenes. This tool uses the same channel — just without the browser.

The tool sends messages to the bot over the internet (HTTPS) and listens for replies over a separate persistent connection called a **WebSocket**. A WebSocket is like keeping a phone line open so the bot can send replies the moment they are ready, rather than making the tool repeatedly ask "any reply yet?"

```
  GRUNTMASTER 6000                       Microsoft Cloud
  ─────────────────────────────────────────────────────────
  Sends message (HTTPS) ───────────────►  Direct Line API
                                                │
                                                ▼
  Listens for reply (WebSocket) ◄──────── Bot replies
                                                │
                                          Copilot Studio Bot
```

### 2.2 Why real user accounts are needed

If your bot requires users to sign in before they can use it, the bot will not respond to questions from an unknown, unsigned-in user. Every simulated user needs to prove its identity — the same way you prove yours when you sign in to an app with your Microsoft account.

The tool handles this by signing in to a real Microsoft 365 account once before the test starts. It saves a sign-in token (a digital proof of identity) securely on your machine. During the test, it uses that saved token automatically for every message the simulated user sends.

The sign-in process happens once per account, in a browser. After that, the tool handles renewals silently for up to 90 days.

This tool currently requires authentication to be configured — the bot must use Microsoft Entra ID sign-in. Public bots (no sign-in required) are not yet supported.

### 2.3 How the test works end-to-end

Here is what happens from the moment you start the tool to the moment results appear:

1. The tool checks that all credentials and sign-in tokens are in order.
2. It sends a single test message ("hi") to the bot to confirm everything is working.
3. You review and confirm the test settings (number of users, speed of ramp-up, etc.).
4. The test starts. The tool adds new simulated users one at a time at the speed you chose, until the peak number is reached.
5. Each simulated user follows a script. It sends the first message, waits for the bot to reply, pauses for a moment to simulate reading, then sends the next message.
6. Each user runs through all messages in the script exactly once and then stops.
7. The test ends when all users have finished — or when the safety cut-off time you set expires, or when you press Q.
8. If a user gets no response for too long, the tool records a timeout and moves on.
9. When the test finishes, the tool saves a results file and generates an HTML report automatically.

---

## 3. Prerequisites

Before you start, make sure you have the following:

| Requirement | Notes |
|---|---|
| **Windows 10/11 machine** | Required for the secure credential storage this tool uses. |
| **Python 3.10 or newer** | Download from python.org. During installation, tick "Add Python to PATH". |
| **Charm Gum CLI** | Required for the interactive menus. Install with `winget install charmbracelet.gum` (see Step 1 in Section 4). |
| **A published Copilot Studio bot** | The bot must be published and have the Direct Line channel enabled. |
| **Two test user accounts** | Real Microsoft 365 accounts in your tenant (e.g. `loadtest.user1@yourcompany.com`). These accounts will be used as simulated users. They need a Copilot Studio licence or a Teams licence. |
| **Azure portal access** | You need permission to register applications in Microsoft Entra ID. The "Application Developer" role is sufficient. |
| **The bot's DirectLine Secret or Token Endpoint URL** | From Copilot Studio → Settings → Channels → Direct Line. |

---

## 4. Setup

**Setup journey:** Step 1: Install the tool → Step 2: Create your Azure app → Step 3: Configure Copilot Studio → Step 4: Run the setup wizard → Step 5: Write your test scripts

---

### Step 1: Install the tool

> **Step 1 of 5**

#### 1a. Get the code

```
git clone https://github.com/kaul-vineet/GRUNTMASTER6000-CopilotLoadTesting.git
cd GRUNTMASTER6000-CopilotLoadTesting
```

> **New machine / new clone:** If you clone this repository to a different machine, you will need to re-run the setup wizard and re-authenticate each profile. Credentials and tokens are stored securely on the local machine — they are intentionally not included in the repository download (they would be a security risk if they were).

#### 1b. Create a Python virtual environment

This command creates a private, isolated space for GRUNTMASTER 6000's software packages. It keeps the tool's packages separate from anything else on your machine so nothing conflicts.

```
python -m venv .venv
.venv\Scripts\activate
```

After running `activate`, your terminal prompt will show `(.venv)` at the start. This tells you the isolated environment is active. All commands from now on will use it.

> **PowerShell note:** If you see an error saying "running scripts is disabled on this system", run this once in PowerShell as your normal user (not as administrator):
> ```
> Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
> ```
> Then try `.venv\Scripts\activate` again. This is a Windows security setting. The command above allows scripts you create on your own machine to run.

#### 1c. Install dependencies

```
pip install -r requirements.txt
pip install -e .
```

The first command installs all the software packages GRUNTMASTER 6000 needs. The second registers the `run-gruntmaster` command so you can launch the tool by name instead of typing `python run.py`. You only need to do this once.

After this, start the tool with:

```
run-gruntmaster
```

#### 1d. Install Charm Gum (TUI menus)

GRUNTMASTER 6000 uses a tool called [Charm Gum](https://github.com/charmbracelet/gum) to draw the interactive menus you navigate with arrow keys. Install it once:

```
winget install charmbracelet.gum
```

Or with Scoop: `scoop install charm-gum`

Gum is a standalone program. It has no effect on Python or the virtual environment.

#### 1e. Alternative: run in Docker (recommended for Windows-on-ARM / Linux / macOS)

On a **Windows-on-ARM** machine the native ARM64 Python has no prebuilt wheels for
`cryptography`, `geventhttpclient` and `brotli`, so `pip install` tries to compile
them from source and fails unless you install Rust and the MSVC C++ Build Tools.
Running the tool in a Linux container sidesteps this entirely — the container gets
prebuilt wheels for every architecture, and `gum` is installed for you.

**Prerequisites:** Docker Desktop installed and running.

The interactive wizard, device-code sign-in and live dashboard all need a terminal,
so use `docker compose run` (which allocates a TTY), not `docker compose up`.

```
# Build the image (first time, or after code/dependency changes)
docker compose build

# First-time setup + per-account sign-in (interactive wizard)
docker compose run --rm gruntmaster --setup

# Run a load test
docker compose run --rm gruntmaster
```

How state is handled in the container:

- **Credentials** come from your `.env` file (Compose reads it via `env_file`). Copy
  `.env.example` to `.env` and fill in the values from Steps 2–3 before running.
- The container has no OS keyring, so it uses a **file-based keyring backend**
  (`keyrings.alt`) stored under `profiles/.keyring/`, and encrypted sign-in tokens
  are stored under `profiles/.tokens/`. Both live on a mounted volume, so you only
  sign in once — tokens survive container restarts.
- **Volumes** map `profiles/`, `utterances/` and `report/` to your host, so your
  test scripts and generated HTML reports are edited/viewed directly on Windows.
- During `--setup`, MSAL prints a URL and a device code in the terminal; open the
  URL in your normal browser on the host to complete sign-in for each account.

> **Tip:** set `TOKEN_ENCRYPTION_PASSWORD` (16+ characters) in `.env` as a belt-and-
> suspenders fallback for token encryption when running in containers.

**What's next:** Step 2 walks through the Azure configuration your bot needs for authenticated testing.

---

### Step 2: Create your Azure app

> **Step 2 of 5**

This section walks through everything you need to create in Azure to make authenticated load testing work.

#### 2.1 Understand the goal

You are creating one Azure App Registration that represents the load test tool. The Copilot Studio bot already has its own App Registration (created automatically when you configure authentication in Copilot Studio). You need to connect them.

#### 2.2 Find the bot's existing App Registration (the Resource App)

1. Sign in to the Azure portal: https://portal.azure.com
2. In the search bar at the top, type **Microsoft Entra ID** and click on it.
3. In the left menu, click **App registrations**.
4. Click the **All applications** tab.
5. Search for an app that includes "CopilotStudio" or your bot's name in its name. It was created automatically by Copilot Studio.
6. Click on that app and copy its **Application (client) ID**. It looks like `a172951c-2123-4f0a-9a63-3c5477d034d5`. Save this as your `AGENT_APP_ID` — you will need it in the setup wizard.

> **Shortcut:** In Copilot Studio → Settings → Security → Authentication, there is usually a link called **View application** or the Client ID is shown directly. Copy that Client ID and use it to search in Azure portal instead of browsing the app list.

> **How to confirm it is the right app:** In the app's left menu, click **Expose an API**. You should see a scope listed that ends in `/access_as_user`. If you see that, this is the right app.

#### 2.3 Create the Load Test Client App Registration

This is the new app you are creating to represent the load test tool.

1. In Microsoft Entra ID → App registrations, click **New registration**.
2. Give it a name like `CopilotStudio-LoadTest-Client`.
3. Under **Supported account types**, choose **Accounts in this organizational directory only**.
4. Leave the Redirect URI blank.
5. Click **Register**.
6. On the overview page, copy the **Application (client) ID**. Save this as your `CLIENT_ID`.
7. Also copy the **Directory (tenant) ID** from the same page. Save this as your `TENANT_ID`.

#### 2.4 Make the load test app a "public client"

The load test tool uses a flow called "device code flow" where the user approves sign-in on a separate device/browser. This flow requires the app to be configured as a public client.

1. In your new app registration, click **Authentication** in the left menu.
2. Scroll down to **Advanced settings**.
3. Under **Allow public client flows**, toggle **Enable the following mobile and desktop flows** to **Yes**.
4. Click **Save**.

#### 2.5 Grant the load test app permission to call the bot's resource app

1. In your new app registration, click **API permissions** in the left menu.
2. Click **Add a permission**.
3. In the panel that opens, click the **APIs my organization uses** tab.
4. Search for the name of the bot's resource app (the one you found in Step 2.2, e.g. `CopilotStudioAuthApp`).
5. Click on it.
6. Under **Delegated permissions**, tick `access_as_user`.
7. Click **Add permissions**.
8. Back on the API permissions page, click **Grant admin consent for [your organisation]** and confirm. This step requires a Global Administrator or Privileged Role Administrator.

> **What admin consent means:** By granting admin consent, an administrator approves this permission on behalf of every user in the organisation. This means individual test users will NOT be shown a "Do you allow this app to access...?" pop-up when they sign in — the administrator has pre-approved it. Without admin consent, each user's first sign-in would require them to manually approve the permission in a browser, which defeats the purpose of automation.

> **What "delegated permissions" means:** The load test tool will act **on behalf of** a signed-in user. It does not act as itself. This is "delegated" access — the user's rights are delegated to the tool. This is distinct from "application permissions" where an app acts entirely on its own authority.

#### 2.6 Verify the scope

After completing section 2.5, the load test tool will request this specific OAuth 2.0 scope when signing in:

```
api://<AGENT_APP_ID>/access_as_user
```

Where `<AGENT_APP_ID>` is the client ID you copied in step 2.2. The tool fills this in automatically.

**What's next:** Step 3 covers the two settings you need to confirm in Copilot Studio before running the wizard.

---

### Step 3: Configure Copilot Studio

> **Step 3 of 5**

#### 3.1 Enable the Direct Line channel

Direct Line is the API channel this tool uses to talk to the bot.

1. Open Copilot Studio and select your bot.
2. Go to **Settings → Channels**.
3. Click **Direct Line**.
4. If it is not already enabled, enable it.
5. Under **Secret keys**, click **Show** next to one of the keys and copy it. Save this as your `DIRECTLINE_SECRET`.

> **Important:** Keep the DirectLine Secret private. Anyone with this secret can send messages to your bot and consume your bot's capacity. Do not commit it to version control.

#### 3.2 Confirm the bot's authentication mode

1. In Copilot Studio, go to **Settings → Security → Authentication**.
2. Check which mode is selected.

- **No authentication:** Public bots are not currently supported by this tool. The bot must require sign-in.
- **Authenticate with Microsoft:** See Section 3.2.1 below.
- **Authenticate manually:** See Section 3.2.2 below.

---

#### 3.2.1 Authenticate with Microsoft

This is the simpler of the two modes. Copilot Studio creates and manages the App Registration for the bot automatically.

**What you need from this screen:**
1. In Copilot Studio → Settings → Security → Authentication, make sure **Authenticate with Microsoft** is selected.
2. Copy the **Client ID** shown on this page. Save it as your `AGENT_APP_ID`.

That Client ID is the bot's resource app. You already granted your load test client app permission to call it in Step 2.5. No further configuration is needed in Copilot Studio.

---

#### 3.2.2 Authenticate manually

This mode means your bot uses a custom OAuth 2.0 configuration — you chose the service provider and entered the settings yourself rather than letting Copilot Studio create them automatically. Both modes ultimately use Entra ID; the difference is whether Copilot Studio manages the App Registration or you do.

**Step-by-step to configure Authenticate manually:**

1. In Copilot Studio → Settings → Security → Authentication, select **Authenticate manually**.

2. Fill in the fields as follows:

   | Field | Value |
   |---|---|
   | **Service provider** | Azure Active Directory v2 |
   | **Client ID** | The Application (client) ID of the bot's resource app from Azure (the one you found in Step 2.2 — the app that has `access_as_user` exposed) |
   | **Client secret** | A client secret from that same app registration. To create one: Azure portal → App registrations → [bot resource app] → Certificates & secrets → New client secret. Copy the **Value** (not the Secret ID). |
   | **Scopes** | `openid profile` — **plus** `Sites.Read.All Files.Read.All` if the agent uses a SharePoint or OneDrive knowledge source (see 3.2.3). |
   | **Token exchange URL (for SSO)** | For **unattended** load testing you must enable SSO so the tool can sign in silently: set this to the app's Application ID URI, e.g. `api://<AGENT_APP_ID>`. Leave blank only if you intend to sign in interactively (not suitable for load tests). |

3. Click **Save**.

4. Copy the **Client ID** you entered above. Save it as your `AGENT_APP_ID` for the setup wizard.

> **Why a client secret is needed here:** In "Authenticate manually" mode, Copilot Studio acts as a confidential client when exchanging tokens — it needs a secret to authenticate itself to Entra ID. In "Authenticate with Microsoft" mode, Copilot Studio handles this internally and you never see it.

> **Keep the client secret safe:** Add it to the bot's App Registration in Azure, but do not put it in the load test wizard — the wizard only needs the `AGENT_APP_ID` (the Client ID), not the secret.

#### 3.2.3 SharePoint / OneDrive knowledge sources — extra Graph permissions

If your agent answers from a **SharePoint** (or **OneDrive**) knowledge source *and* uses **Authenticate manually**, the bot searches SharePoint **as the signed-in user** via Microsoft Graph. Your custom app registration must be granted the Graph delegated permissions to do this — otherwise the bot returns generic fallbacks even though the same question works in the Copilot Studio **Test pane**.

**Symptom:** Over Direct Line (this tool) every answer is *"I'm sorry, I'm not sure how to help with that"*, but the **exact same question answers correctly in the Copilot Studio Test pane.**

**Fix:**

1. Azure portal → **App registrations** → open the **bot resource app** (the app whose Client ID is your `AGENT_APP_ID`) → **API permissions**.
2. **Add a permission → Microsoft Graph → Delegated permissions** → tick **`Sites.Read.All`** and **`Files.Read.All`** → **Add permissions**.
3. Click **Grant admin consent for [your organisation]** and confirm — both must show a green **✓ Granted**.
4. In Copilot Studio → **Settings → Security → Authentication** (Authenticate manually), set **Scopes** to:
   ```
   openid profile Sites.Read.All Files.Read.All
   ```
5. **Save**, then **Publish** the agent.

> **Why the Test pane works but Direct Line does not:** The Test pane uses Copilot Studio's own Microsoft-managed first-party app, which already holds every Graph scope. Over Direct Line with manual auth, the identity flows from *your* custom app, so that app must carry the SharePoint Graph scopes itself.

> **Managed vs manual trade-off:** "Authenticate with Microsoft" (managed) provides these Graph scopes automatically — but it disables the Direct Line channel this tool requires. "Authenticate manually" keeps Direct Line working, at the cost of configuring these scopes yourself.

**What's next:** Step 4 runs the interactive setup wizard, which stores all your credentials and test accounts securely.

---

#### 3.3 Switching to a different agent later

A common question: *"I already have this working against one agent. To point it at a different agent, do I just swap the DirectLine Secret?"*

**Yes — as long as the new agent uses the same authentication app registration as the old one** (same Bot Client ID / `AGENT_APP_ID`, same scope, same SSO connection). Each agent has its own Direct Line channel, so the secret always changes, but that is the only thing you need to change: your load-test client app, its `access_as_user` grant, admin consent, and each test user's cached token all stay the same and keep working.

**To switch:**

1. Copy the **DirectLine Secret** from the new agent (Copilot Studio → Settings → Channels → Direct Line).
2. Update it through the **setup wizard** (see the gotcha below — do not just edit a file).
3. Re-run and confirm the pre-flight `Target agent id` and `Agent` name match the new agent.

> **⚠ Critical gotcha — change credentials through the wizard, not by editing files.**
> The tool reads the **saved credential store first** and only falls back to `.env`/environment variables. If you change the DirectLine Secret by editing `.env` (or a file) by hand, the previously saved value **still wins** and you will keep hitting the **old** agent. Always update the secret via the setup wizard (or re-run setup), which writes to the secure store. (In Docker the store is a keyring persisted under the mounted `profiles/` volume — same rule applies.)

> **Verify you landed on the right agent.** The pre-flight check prints `Target agent id` and the agent's display name (`Agent …`) after it sends its first message. Confirm both match the agent you intended before trusting a run.

---

### Step 4: Run the setup wizard

> **Step 4 of 5**

Before running the wizard, put at least one CSV file in the `utterances/` folder. See Step 5 for the format. There is an example file `utterances/it_support.csv` already included.

Run:

```
python run.py
```

The first time you run this (or if not yet configured), the setup wizard opens automatically. The wizard saves all credentials into **Windows Credential Manager** — the same secure store that browsers use to save passwords. Nothing sensitive is written to a plain text file.

#### 4.1 The wizard menu

The wizard uses arrow-key navigation. Each row is a setting. Navigate to it and press Enter to edit:

```
      Tenant ID                          72f988bf-…                      ✓
      Client ID                          cea29e59-…                      ✓
      Bot Client ID (SSO)                (optional — blank = SSO disabled)
      DirectLine Secret                  ●●●●●●●● (saved)                ✓
      Token Endpoint                     (not set)

  ─  PROFILES  ─  Each profile is a real M365 account. Assign a scenario
     to control which utterances it sends. Multiple profiles = more load.

      Profile [0]: User 1                loadtest.user1@…  → it_support   ✓

  +  Add profile
  ✓  Save & continue
  ←  Back
  ✕  Exit
```

Navigate up/down with arrow keys. Press Enter to edit a field or take an action. Press **← Back** to leave without saving, **✕ Exit** to quit the tool.

#### 4.2 Field-by-field guide

**Tenant ID**
The unique identifier for your Microsoft 365 organisation in Azure.

Where to find it: Azure portal → Microsoft Entra ID → Overview → Tenant ID.

It looks like: `72f988bf-86f1-41af-91ab-2d7cd011db47`

---

**Client ID**
The identifier of the load test client app you created in Step 2.3.

Where to find it: Azure portal → App registrations → [your app] → Application (client) ID.

---

**Bot Client ID (SSO)**
The identifier of the bot's resource app — the one Copilot Studio created automatically when you configured authentication.

Where to find it: Copilot Studio → Settings → Security → Authentication → Client ID.

This field is required. The tool does not currently support bots that have no authentication configured.

---

**DirectLine Secret**
The secret key that gives this tool permission to talk to your bot over Direct Line.

Where to find it: Copilot Studio → Settings → Channels → Direct Line → Secret keys → Show.

The value is a long string of random characters. It is hidden as you type it.

---

**Token Endpoint URL**
If your organisation uses a Token Endpoint instead of a DirectLine Secret, paste the URL here. Leave blank if you are using the DirectLine Secret.

Where to find it: Copilot Studio → Settings → Channels → Direct Line → Token Endpoint URL.

After you paste the URL, the wizard asks:

> **"Does this Token Endpoint require an AAD Bearer token?"**

Answer **Yes** if your endpoint requires an Azure AD Authorization header. Answer **No** if it is open.

---

**Profiles**
A profile is a test user account. After the credential fields, you can add as many profiles as you need.

For each profile you need:
- **Username (UPN):** The full email address of the test account, e.g. `loadtest.user1@yourcompany.com`
- **Display name:** A short label shown in the terminal (e.g. `User 1`). Press Enter to accept the default.
- **Scenario (CSV name):** The name of the CSV file (without `.csv`) this profile will use. Leave blank to auto-assign.

After adding a profile, the wizard asks whether to add another.

#### 4.3 Authentication

After saving, the wizard checks whether each profile already has a valid saved sign-in token. For any that do not, it starts the device code sign-in process:

```
  ╭──────────────────────────────────╮
  │  EL4LXCF6H                       │
  ╰──────────────────────────────────╯
  Go to:  https://microsoft.com/devicelogin

  Waiting for sign-in…
```

Open a browser, go to that URL, enter the code shown, and sign in with the test user account. The tool waits. When sign-in completes, the token is saved securely to your machine. You will not need to do this again for that account for up to 90 days.

#### 4.4 Pre-flight check

Before showing the run configuration, the tool sends "hi" to the bot and waits for a reply. This confirms the credentials work and the bot is reachable. If this fails, an error message explains what to check.

> **Note:** The pre-flight check sends one real message to your bot. If you are testing a production bot, this will appear in the bot's analytics as one conversation. This is unavoidable and harmless — it is a single "hi" message sent once at startup.

**What's next:** Step 5 covers how to write the CSV scripts that simulated users follow during the test.

---

### Step 5: Write your test scripts (utterance files)

> **Step 5 of 5**

Utterance files are the scripts GRUNTMASTER 6000 follows when simulating users. Each file lives in the `utterances/` folder. Each file represents one type of user — one "scenario".

#### 5.1 Format

The file must be a CSV (comma-separated values) file with a header row containing the word `utterance`. Each row after that is one message to send to the bot, in order.

```csv
utterance
Hi, I need help with my password.
I can't log in to my email.
What are the steps to reset a password?
Please escalate this to a human.
```

The tool sends these messages one at a time in order. It waits for the bot to reply before sending the next one.

#### 5.2 What makes a good test script

- **Cover the full journey.** Include the greeting, the main questions, and the closing. A realistic conversation has 5–10 turns.
- **Include escalations and edge cases.** Test what happens when the bot is asked something it does not know, or when the user asks to speak to a human.
- **Match real usage patterns.** If analytics show most users ask 3 questions per session, keep scripts to 3 utterances.
- **One script per scenario.** If your bot handles both HR and IT topics, create `utterances/hr.csv` and `utterances/it_support.csv` separately.

#### 5.3 Assigning profiles to scenarios

If you have two CSV files and two profiles, assign each profile to one scenario in the wizard (the "Scenario" field when adding a profile). That profile's simulated users will only use messages from that scenario's script.

If you have more CSV files than profiles, profiles are reused in rotation across scenarios.

**What's next:** Setup is complete. Go to Section 5 to run your first load test.

---

## 5. Running the Load Test

### 5.1 Start the tool

Make sure the virtual environment is active (you see `(.venv)` in your terminal prompt). Then run:

```
python run.py
```

If already configured, the wizard is skipped. The tool checks credentials, verifies sign-in tokens, and runs the pre-flight check, then shows the **Run Configuration** menu.

### 5.2 Run Configuration menu

All test settings are shown with their current values. Navigate to any row and press Enter to change it:

```
  ✦  RUN CONFIGURATION  ✦

  Select any setting to change it, then start the test.

▸   Peak users                           10     users     Total users spawned — each runs N message(s) then leaves
    Spawn rate                           5      users/min New users per minute — 1 user every 12s
    Think time                           30     seconds   How long each user pauses between messages
    Reply timeout                        30     seconds   Abort if bot has not started responding within this long (min 15s)
    Max run time  (safety cap)           20     min       Test force-stops here even if users are still running
    ────────────────────────────────────────────────────────────────────────────────
      ↳ Est. ramp-up                     2.0    min       Time until all 10 users are active (10 ÷ 5/min)
      ↳ Est. script / user               2.9–7.2 min      N msg × (think 30s + 5–30s response)
      ↳ Est. total duration              4.9–9.2 min       Ramp-up + last user's script
    Silence window                       15     seconds   Fixed — wait after bot's last message before recording done
    Protocol                             WebSocket 🔒      DirectLine WebSocket over TLS — traffic is encrypted
    Notes                                (none)           Free-text label embedded in the HTML report for this run
    ▶  Start test
    ?  Help
    ✕  Exit
```

Select **▶ Start test** when ready.

**Peak users** — How many simulated users will run in total. Each user sends all the messages in the script once and then stops. With think time set to 30 seconds, 10 users will send roughly 1 message every 3 seconds combined. To generate 1 message per second, you need roughly 30 users. This matches the pace of real human conversations.

**Spawn rate** — How quickly users are added. A rate of 5 users/min means one new user starts every 12 seconds. With a peak of 10 users, the test reaches full load after 2 minutes. A slower ramp gives the bot time to warm up. A faster ramp stresses it sooner.

**Think time** — How long each simulated user pauses between messages to simulate a real person reading the reply. The actual pause varies randomly to avoid all users sending at exactly the same moment.

**Reply timeout** — How long the tool waits for the bot to start replying before giving up. If the bot does not send its first word within this time, the request is recorded as a timeout. The minimum is 15 seconds.

> ⚠️ **Authenticated SharePoint / OneDrive agents are slow to answer.** When the agent retrieves generative answers from a SharePoint knowledge source over the On-Behalf-Of flow (see 3.2.3), the first authenticated reply typically takes **55–90 seconds**. The default 30s Reply timeout will record these as false timeouts. Set **Reply timeout to at least 120 seconds** for SharePoint-backed agents. For headless runs (`locust -f run.py --headless`), set the environment variable **`GRUNTMASTER_RESPONSE_TIMEOUT=120`** since the interactive Run Configuration menu is skipped.

**Max run time (safety cut-off)** — The test will force-stop at this number of minutes even if users are still in the middle of their scripts. This is a backstop for situations where many requests are timing out and scripts are taking much longer than expected. Set it higher than the "Est. total duration" shown below. Under normal conditions the test ends earlier on its own, and this cut-off is never reached.

**Est. ramp-up / Est. script / user / Est. total duration** — These rows are calculated automatically and update as you change the settings above them. They give you a rough preview of how long the test will take. You cannot edit them directly.

**Silence window** — After the bot sends its first reply, the tool waits this long for any follow-up messages (some bots send multiple reply cards). This is fixed at 15 seconds and cannot be changed.

**Notes** — Free text you can attach to a run — a description, ticket number, or anything that helps identify it later. The notes appear in the HTML report.

**Recommended approach for first runs:**
1. Start with 1 user, confirm the bot responds correctly.
2. Step up to 5 users, watch for errors.
3. Step up to 10, 20, 50 users incrementally.

### 5.3 Understanding the rate limit ceiling

Direct Line (the messaging channel the tool uses) has a hard rate limit of approximately 8,000 messages per minute. You cannot exceed this regardless of how many users you run. In practice, the more likely bottleneck for a typical Copilot Studio deployment is the bot's message capacity setting in Power Platform Admin Center, not this ceiling.

### 5.4 Live dashboard

The test runs entirely in the terminal. A live dashboard updates every half-second:

```
╭──────────────────────────────────────────────────────────────────────╮
│  GRUNTMASTER 6000  ·  LIVE                     ● HEALTHY    00:03:24  │
╰──────────────────────────────────────────────────────────────────────╯
  SPAWNING  ▓▓▓▓▓▓▓▓▓░  9 / 10 users
  Peak: 10 users   Ramp: 5/min   Run time: 5 min
  RPS: 0.3/s   Errors: 0.0%   p95: [████████░░] 1820ms / 2000ms

  RAMP STEPS  (2 completed)
   Ramp   Users   Requests   RPS   p50   p95   p99   T/O   429
   ──────────────────────────────────────────────────────────────
       1       5         4  0.07  1100  1400  1400     0     0
       2      10        12  0.20  1340  1820  2100     0     0
     ▶ 3       9         6  0.30  1200  1620  1820     0     0
       13:05:01  ▶  Ramp 3 started — 9 users
  RAMP TREND  Users ▁▂▄  Req ▁▂▄  RPS ▁▂▃  p50 ▁▃▄  p95 ▁▄▆  p99 ▁▄▇  T/O ▁▁▁

  PROFILE STATS
   User · Scenario        n    Typical  Tail p95   Worst   T/O   Latency trend / 10m
   ─────────────────────────────────────────────────────────────────────────────────
   Alice · It Support     22    1.3s     (1.8s)     2.1s     0    ▁▂▂▃▃▄▃▄▁▁▁▁▁▁▁▁▁▁▁▁
   ALL USERS              22    1.3s     (1.8s)     2.1s     0    ▁▂▂▃▃▄▃▄▁▁▁▁▁▁▁▁▁▁▁▁
  Typical = median (outlier-proof) · Tail p95 in (parens) = too few samples to trust · Worst = single slowest reply
  Trend bar = p95 latency per 10-minute window · taller = slower · ▁ low  █ high
  ⚠ Alice: 22 samples — trust Typical 1.3s; worst 2.1s — possibly a fluke at this sample size.
  ▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁  error rate  (bar height = errors in bucket)

  UTTERANCES
   Profile        Utterance              p50   p95   Count   Bot Response
   ───────────────────────────────────────────────────────────────────────
   Alice #1       How do I reset my…    1620  2100       8   To reset your password…
   Alice #2       I can't log in…       1400  1900       7   Please visit the login…
  ── fastest ──
   Alice #3       Hi                    1100  1400       9   Hello! How can I help…
   Alice #4       Thank you              800  1200       6   You're welcome! Let me…

  EVENTS
  13:05:01  R3  ▶  Ramp 3 started — 9 users
  13:04:00  R2  ▶  Ramp 2 started — 5 users

  ╭────────────────────────────────────────────────────────────────╮
  │  p50 = median   p95 = 95th percentile   p99 = 99th             │
  │  T/O = Timeout   RPS = Requests / second                       │
  ╰────────────────────────────────────────────────────────────────╯
  Press Q to stop test and go to New Run
```

**Health indicator (top right):**
- `● STARTING` — no replies recorded yet
- `● HEALTHY` — response times are well within your target
- `● DEGRADED` — response times are close to your target
- `● CRITICAL` — response times have exceeded your target

**SPAWNING bar** — Shows how many users have been started so far versus the peak you set. The bar fills up as the ramp proceeds.

**RPS / Errors / p95 bar** — The current rate of messages per second, the current error rate, and a progress bar showing how close the p95 response time is to your target.

**RAMP STEPS table** — The test is divided into 60-second windows called ramp steps. Each row covers one minute. The current in-progress minute is marked with `▶` in cyan. The 429 column shows how many times the bot said "slow down" during that minute. Events (ramp starts, errors) appear as indented rows under the minute they happened in.

**RAMP TREND line** — A line of tiny bars showing how metrics evolved across all ramp steps. Each bar is one completed minute. If the p95 bars grow faster than the Users bars, the bot is slowing down under load.

**PROFILE STATS** — One row per test user account. Shows how many messages were sent, the response time at the 50th, 95th, and 99th percentile, the timeout count, and a small sparkline chart showing how the 95th percentile changed over time during the test. A sparkline that rises from left to right means the bot got slower as the test progressed.

**UTTERANCES** — Shows the 4 slowest messages (highlighted in red) and the 4 fastest (highlighted in green) in one table, separated by a dim divider. The "Bot Response" column shows the actual reply the bot gave on the slowest recorded call for each message.

**EVENTS** — A timestamped log of the last 8 things that happened: ramp steps starting, timeouts, rate-limit hits, and errors. This is useful for understanding when things went wrong during the test.

Press **Q** at any time to stop the test early.

### 5.5 After the test

When the test ends (all users finish their scripts, the safety cut-off expires, or you press Q):

1. An HTML report is generated automatically in `report/report_YYYYMMDD_HHMMSS.html`.
2. A post-run menu appears.

```
  ▶  New Run         — go back to Run Configuration and start again
  ⚙  Edit Settings   — open the setup wizard to change credentials or profiles
  ✕  Exit            — close the tool
```

All results are automatically saved to `report/detail_YYYYMMDD_HHMMSS.csv`.

---

## 6. Reading the Results

### 6.1 Live dashboard metrics explained

**Response time (latency)**
Think of it like a stopwatch. Every time a simulated user sends a message, the tool starts a stopwatch. It stops the moment the bot finishes all its replies. That elapsed time — measured in milliseconds — is the response time. 1000 ms equals 1 second. A well-performing bot usually replies in under 2000 ms.

**p50 — the median**
Sort every response time you have measured from fastest to slowest. The value exactly in the middle is the p50. Half of all requests were faster than this number, half were slower. It represents the typical experience — what most users feel most of the time.

**p95 — the 95th percentile**
Sort all response times again and go 95% of the way through the list. That value is the p95. It means "95 out of every 100 requests finished within this time or faster." The remaining 5 out of 100 took longer. The p95 is the most important number to watch. It tells you how bad the worst realistic experience is, not just the average. A p95 of 2000 ms means almost everyone gets a reply within 2 seconds, with only rare exceptions.

**p99 — the 99th percentile**
The same idea pushed further: 99 out of 100 requests finished within this time. The p99 captures the very slow outliers. If your p99 is 5000 ms, one request in every hundred takes five seconds or more.

**The live PROFILE STATS columns: Typical / Tail p95 / Worst**
The live table deliberately shows only the numbers you can act on, and tells you which to trust:
- **Typical** is the median (p50). It is *outlier-proof* — one freakishly slow reply cannot move it — so it always represents the experience most users actually get. When in doubt, this is the number to trust.
- **Tail p95** is the 95th percentile (see below). It is shown **in parentheses** (e.g. `(1.8s)`) when the row has **fewer than 100 samples**, meaning there aren't yet enough data points for the tail to be statistically reliable — treat a parenthesised value as indicative only.
- **Worst** is the single slowest reply (the max). Comparing it to Typical is the fastest way to spot an outlier: if `Worst` is far above `Typical` but `Typical` itself is low, one reply was slow and it did **not** distort the typical experience.

**The verdict line (which value makes sense)**
Below the table, each row gets one plain-English sentence that names the number to trust and classifies the shape of the data — for example *"trust Typical 1.3s; worst 2.1s = a few isolated slow replies; Typical unaffected"*, or *"~40% of replies much slower — a real slow group, not a fluke"*, or *"latency is trending slower over time (degrading)"*. It is computed from the sample count and percentile ratios, and it directly answers "is this number skewed by one value?".

**p50 / p75 / p85 / p95 / p99 — the full percentile set**
The complete percentile ladder is preserved in the **HTML report** and the **detail CSV** for drill-down. In the report, each profile/scenario row also gains a **Worst (s)** column and the same **Read (which value to trust)** verdict.

**RPS — requests per second**
How many messages are being sent to the bot per second right now across all simulated users. With a 30-second think time between messages, 30 concurrent users generate roughly 1 message per second.

**T/O — timeout**
A request where the bot did not reply within the Reply Timeout setting. The tool gave up waiting and counted it as a failure. A rising T/O count is a strong sign the bot is struggling to keep up.

**Error rate**
The percentage of requests that either timed out or returned an error. Keep this below 1%.

**The p95 / 10m buckets sparkline (▁▂▄▇█▆▄▂)**
Each tiny bar in the PROFILE STATS table represents one 10-minute window of the test. The taller the bar, the slower the p95 was during that window. A sparkline that rises from left to right means the bot got slower as load increased. A flat sparkline means the bot handled load consistently.

**Error rate sparkline**
Shown as a single red line below PROFILE STATS. Each bar covers one 10-minute window. A flat line at ▁ means zero errors throughout. Any bar taller than ▁ means errors occurred in that window.

**RAMP STEPS table**
One row per 60-second window of the test. The current in-progress window is highlighted in cyan. The 429 column shows rate-limit responses per window. These rows let you pinpoint the load level where things started to slow down — for example, "response times were fine at 5 users but started climbing in the third minute when load hit 15 users."

**RAMP TREND line**
A single line of labeled sparklines where each bar is one completed 60-second window. It shows Users, Requests, RPS, p50, p95, p99, and timeouts across the full test. If the p95 bars grow faster than the Users bars, the bot is degrading under load.

**UTTERANCES table**
Shows the 4 slowest messages (highlighted red) and 4 fastest (highlighted green) in a single table, separated by a dim divider. With 50 messages in your script, you still only see 8 rows — the extremes most worth your attention.

### 6.2 The detail CSV

Every bot reply is recorded to `report/detail_YYYYMMDD_HHMMSS.csv`. One file is created per test run.

The file contains one row per message exchange: which test user sent the message, which conversation it belonged to, what the message was, what the bot replied, when it was sent, how long it took to reply (in milliseconds), whether it timed out, and how many users were active at that moment.

You can open this file in Excel, Power BI, or any analysis tool to produce your own charts and summaries.

### 6.3 HTML report

After every test run, an HTML report is automatically generated in `report/report_YYYYMMDD_HHMMSS.html`. It has four tabs:

**Tab 1 — Summary**
The top-line results at a glance. Shows total message count, test duration, error rate, and whether the p95 response time passed or failed your target. Below that: a ramp steps table (one row per minute of the test), a breakdown of error types, per-user stats, and a comparison of how different user accounts performed.

**Tab 2 — Response Time Distribution**
Charts that show the spread of response times per user account and per scenario. A narrow spread means the bot responded consistently. A wide spread means some users waited much longer than others. Also includes a heatmap showing how response times changed over time for each individual message in your script.

**Tab 3 — Utterance Analysis**
A detailed table of every message in your script with its response time stats. You can type in the filter box to search for a specific message. If you run a second test after a baseline test, this tab shows percentage changes so you can see whether things got faster or slower.

**Tab 4 — Config**
A record of all the settings used for this test run: peak users, spawn rate, think time, reply timeout, safety cut-off, and p95 target. Useful for sharing results and knowing exactly what conditions produced them.

All tables in the report are sortable — click any column header to sort.

#### Reading the response time charts

**Box/whisker chart**
Imagine lining up all the response times for one scenario from fastest to slowest, then folding the line into a shape:

- The **box** covers the middle 50% of all requests.
- The **line through the middle of the box** is the median (p50) — the typical response time.
- The **whiskers** extend out to show faster and slower extremes.
- Dots beyond the whiskers are individual unusually fast or slow requests.

A narrow box means consistent performance. A wide box means high variability — some users waited much longer than others.

**Latency heatmap**
A grid where each row is a different message and each column is a 30-second window during the test. Each cell is colour-coded: lighter colours are fast, darker colours are slow.

Use the heatmap to spot patterns:
- A single row that is always dark — one particular message is consistently slow no matter what.
- Columns that turn dark towards the right side — the bot starts slowing down as more users join.
- A random speckled pattern — occasional slowdowns, probably network noise rather than a real capacity issue.

**Per-utterance anomaly flag**
Some rows in the utterance table may be flagged with a dot (•). This means that message is unusually slow compared to all the others in your script. The flag uses a statistical method that is not thrown off by a few extreme outliers.

**Per-utterance p99.9 projection**
This is a statistical estimate of how slow the very worst 1-in-1000 request might be, extrapolated from the data you collected. It is labelled with `~` to remind you it is an estimate, not a measured value.

**Profile comparison**
Shows the percentage difference in median response time between any two user accounts. If one account's messages consistently take longer than another's, it may indicate the bot behaves differently depending on the user — worth investigating.

### 6.4 Interpreting results

**The bot passes the performance test if:**
- The 95th-percentile response time stays below 2000 ms (or your configured target)
- Error rate stays below 1%

**Signs of trouble:**
- Response times climbing steadily — the bot is under capacity pressure. Add message capacity in Power Platform Admin Center.
- Error rate above 1% — the bot may be throttling or returning errors. Check Copilot Studio analytics.
- T/O count rising — the bot is taking too long to respond. Increase the Reply Timeout in Run Configuration or reduce peak users.
- 429 column rising in RAMP STEPS — the bot is rate-limiting. Reduce concurrent users or increase think time.

---

## 7. How it handles problems

**If the bot does not reply in time (timeout):** The tool waits up to your Reply Timeout setting for the first word from the bot. Once the bot starts replying, it waits another 15 seconds for any follow-up messages. If nothing arrives in time, the request is recorded as a timeout.

**If the same user gets two timeouts in a row:** The tool assumes the conversation is stuck and starts a fresh one for that user. The event is logged in the EVENTS feed on the dashboard.

**If the bot returns a rate-limit error (429):** The tool stops all users for 60 seconds to let the bot recover, then resumes automatically. A red banner appears on the dashboard while users are paused:

```
⚡ CIRCUIT OPEN — DirectLine rate limit (429) hit — all users paused — resuming in Xs
```

This prevents a flood of failing requests from distorting your results.

**Retrying failed connections:** When starting a new conversation, the tool tries up to 3 times before giving up. Each retry waits a little longer than the last. When sending a message, the tool tries up to 2 times before recording the failure.

**Connection reuse:** All simulated users share a single pool of HTTPS connections to Direct Line. The pool is sized automatically based on how many users you are running. Connections are reused across requests so the test does not need to set up a new connection for every single message.

---

## 8. Troubleshooting

### "This agent is currently unavailable. It has reached its usage limit."

This is a Copilot Studio capacity issue, not an authentication problem. The bot has exceeded the number of messages allocated to its Power Platform environment.

Fix: Go to Power Platform Admin Center → select your environment → Capacity → increase the message capacity assigned to this environment.

This is expected when running load tests with a trial or developer environment.

---

### "AADSTS650057: Invalid resource"

This means the token was requested for a resource (application) that has not been configured to accept delegated permissions.

Likely causes:
1. The `Bot Client ID (SSO)` field in the wizard is set to the wrong value (e.g., it matches the Client ID instead of the Agent App ID).
2. The `access_as_user` API permission was not added in Azure (see Step 2.5).
3. Admin consent was not granted after adding the permission.

---

### "AADSTS90009: Application is requesting a token for itself"

This means the Client ID and the Agent App ID are the same value. They must be different — one is the load test client, the other is the bot's resource app.

Fix: Re-run the wizard and enter the correct value for `Bot Client ID (SSO)` — it must be the client ID from the bot's existing app registration, not the load test app's client ID.

---

### "IntegratedAuthenticationNotSupportedInChannel"

The bot's authentication is set to **"Authenticate with Microsoft"** (managed), which **disables the Direct Line channel** this tool requires.

Fix: In Copilot Studio → **Settings → Security → Authentication**, switch the agent to **Authenticate manually** (see 3.2.2), then **Publish** the agent — Direct Line only picks up the change after a publish. If "Authenticate manually" is greyed out, first disconnect the agent's Microsoft 365-integrated channels (Teams, Microsoft 365 Copilot, SharePoint, Power Apps) from the **Channels** page, which unlocks the option.

---

### "No valid token for [username]"

The cached token has expired and could not be refreshed silently. This happens if the refresh token has expired (typically after 90 days of inactivity) or if the account's password was changed.

Fix: Re-run the wizard, navigate to the profile, and choose "Re-authenticate now".

---

### Bot gives "sign in" prompt during the test

The SSO token exchange is not completing. The tool sent the sign-in proof but the bot did not accept it.

Likely causes:
1. `Bot Client ID (SSO)` is blank or wrong — the tool cannot acquire a token for the bot's scope.
2. The token scope does not match. The tool uses `api://<AGENT_APP_ID>/access_as_user`. Verify this scope exists in the bot's app registration (Azure portal → the bot's app → Expose an API).
3. The Token Exchange URL in the bot's OAuth connection does not match the bot's app ID.

---

### Bot answers "I'm not sure how to help" for everything (SharePoint knowledge)

Every Direct Line answer is a generic fallback, but the **same question answers correctly in the Copilot Studio Test pane.**

Cause: The agent uses a **SharePoint** or **OneDrive** knowledge source under "Authenticate manually", but your custom app registration lacks the Microsoft Graph delegated permissions needed to search SharePoint on the signed-in user's behalf. The Test pane works because it uses Copilot Studio's Microsoft-managed first-party app, which already has those scopes.

Fix: See **section 3.2.3** — grant the bot's app the Graph delegated permissions `Sites.Read.All` + `Files.Read.All` (with admin consent), add `Sites.Read.All Files.Read.All` to the Copilot Studio auth connection **Scopes**, then **Publish**.

---

### Still talking to the old agent after changing the DirectLine Secret

You updated the secret but the bot's answers (or the pre-flight `Target agent id` / `Agent` name) still show the previous agent.

Cause: The tool reads the **saved credential store first** and only falls back to `.env`/environment variables. Editing `.env` (or any file) by hand does not take effect because the previously saved credential overrides it.

Fix: Change the DirectLine Secret through the **setup wizard** (or re-run setup), which writes to the secure store. Then re-run and confirm the pre-flight `Target agent id` and `Agent` name match the agent you intended. See **section 3.3** for the full agent-switch checklist. (In Docker the store is a keyring under the mounted `profiles/` volume — the same "wizard, not file" rule applies.)

---

## 9. Beta Testing

### HTTP Transport (experimental)

By default GRUNTMASTER 6000 receives bot replies over a persistent WebSocket connection. An alternative HTTP polling transport is implemented but not yet enabled in the UI — it polls the DirectLine REST endpoint for replies instead of reading from a WebSocket stream.

To activate it for testing, set the `GRUNTMASTER_TRANSPORT` environment variable before running:

**PowerShell:**
```powershell
$env:GRUNTMASTER_TRANSPORT = "http"
python run.py
```

**Command Prompt:**
```cmd
set GRUNTMASTER_TRANSPORT=http
python run.py
```

When active, the Run Configuration screen will show:

```
  Protocol                     HTTP ⚠ TEST MODE      set by GRUNTMASTER_TRANSPORT env var
```

To revert to WebSocket, unset the variable or open a new terminal:

```powershell
$env:GRUNTMASTER_TRANSPORT = ""
```

> The HTTP transport is not selectable through the normal UI. This env var is the only way to activate it. Once testing confirms it is stable, it will be promoted to a standard Run Configuration option.

---

## 10. File Reference

| File | Purpose |
|---|---|
| `run.py` | The main file. Run this to start the tool. |
| `requirements.txt` | The list of software packages the tool needs. Install them with `pip install -r requirements.txt`. |
| `.env.example` | A template showing advanced configuration options. Copy to `.env` only if you need to override settings without using the wizard. |
| `utterances/*.csv` | Your test script files. One CSV per scenario. Drop any CSV file here and it becomes a scenario automatically. |
| `profiles/profiles.json` | The list of test user accounts created by the wizard. Not included when you share the repository. |
| `profiles/.tokens/` | Saved sign-in tokens, one per user account. Encrypted and stored locally. Not included when you share the repository. |
| `profiles/profiles.example.json` | An example showing what a profiles file looks like, for reference. |
| `report.py` | The HTML report generator. Runs automatically after each test. You can also run it on its own with `python report.py`. |
| `report/detail_*.csv` | Per-run results files. One CSV per test run. Not included when you share the repository. |
| `report/events_*.csv` | Per-run event log (ramp starts, errors, rate-limit hits, etc.). Paired with the detail CSV by timestamp. Not included when you share the repository. |
| `report/report_*.html` | Auto-generated HTML reports. One file per test run. Not included when you share the repository. |
| `report/ci_*.json` | A summary file written after every test containing key metrics (pass/fail, p95, error rate). Set the environment variable `GRUNTMASTER_CI=1` to also print this to the terminal and exit with a success or failure code — useful for automated pipelines. |
