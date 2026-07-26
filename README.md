# GRUNTMASTER 6000

Load-testing tool for **Microsoft Copilot Studio** agents. It drives many simulated
users through real conversations over the **Direct Line** channel, signs each one in
with a real Microsoft 365 account, and measures how fast the agent replies under load.

- Concurrent, authenticated users — no browser windows required.
- Automated device-code sign-in per account (tokens cached ~90 days).
- Live terminal dashboard + auto-generated HTML report and detail CSV.

> Requires an agent with **Entra ID sign-in** and the **Direct Line** channel enabled.
> Public (no-auth) agents are not supported.

---

## Quick start (Docker — recommended)

Docker avoids native build issues on Windows-on-ARM/macOS/Linux and installs `gum` for you.

```bash
git clone https://github.com/kaul-vineet/GRUNTMASTER6000-CopilotLoadTesting.git
cd GRUNTMASTER6000-CopilotLoadTesting

cp .env.example .env         # fill in values from Setup below
docker compose build
docker compose run --rm gruntmaster --setup   # first-time wizard + sign-in
docker compose run --rm gruntmaster           # run a load test
```

Use `docker compose run` (allocates a TTY), **not** `docker compose up`. Credentials come
from `.env`; the container uses a file keyring under `profiles/.keyring/` and encrypted
tokens under `profiles/.tokens/`. `profiles/`, `utterances/` and `report/` are mounted to
your host. Set `TOKEN_ENCRYPTION_PASSWORD` (16+ chars) in `.env` as a token-encryption fallback.

<details>
<summary><b>Local install (Windows, no Docker)</b></summary>

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install -e .
winget install charmbracelet.gum   # or: scoop install charm-gum
run-gruntmaster
```

If PowerShell blocks activation: `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`.
Requires Python 3.10+ and Windows Credential Manager for secure storage.
</details>

---

## Setup

You connect **two** Entra app registrations: the **load-test client** you create, and the
agent's **resource app** (created by Copilot Studio when you enable auth).

### 1. Azure — load-test client app

1. **Entra ID → App registrations → New registration.** Single tenant, no redirect URI.
   Copy its **Client ID** (`CLIENT_ID`) and **Tenant ID** (`TENANT_ID`).
2. **Authentication → Allow public client flows → Yes** (needed for device-code sign-in).
3. **API permissions → Add → APIs my organization uses →** find the agent's resource app →
   **Delegated → `access_as_user` → Add → Grant admin consent.**

Find the agent's resource app under **App registrations → All applications** (name contains
"CopilotStudio" or your agent), or via Copilot Studio → Settings → Security → Authentication →
Client ID. Confirm it exposes `.../access_as_user` under **Expose an API**. Save its Client ID
as `AGENT_APP_ID`. The tool requests `api://<AGENT_APP_ID>/access_as_user`.

### 2. Copilot Studio

1. **Settings → Channels → Direct Line →** enable, copy a **Secret key** (`DIRECTLINE_SECRET`).
   Keep it private. (Or use a **Token Endpoint URL** instead.)
2. **Settings → Security → Authentication** must be **Authenticate manually** (Azure AD v2).
   *"Authenticate with Microsoft" disables Direct Line and will not work.* After changing auth,
   **Publish** — Direct Line only picks up the change on publish.
   - **Token exchange URL (SSO):** `api://<AGENT_APP_ID>` — required for silent/unattended sign-in.
   - **Scopes:** `openid profile` — add `Sites.Read.All Files.Read.All` if the agent uses a
     **SharePoint/OneDrive** knowledge source (see below).

**SharePoint/OneDrive agents:** the agent searches Graph *as the signed-in user*, so the
resource app needs Graph **delegated** `Sites.Read.All` + `Files.Read.All` (with admin consent),
plus those scopes in the auth connection, then **Publish**. Symptom if missing: every Direct Line
answer is a generic fallback, yet the same question works in the Copilot Studio Test pane.

### 3. Wizard (`--setup`)

The wizard stores everything in the secure credential store and signs in each profile via
device code (open the printed URL, enter the code). Fields:

| Field | Value |
|---|---|
| Tenant ID | `TENANT_ID` |
| Client ID | `CLIENT_ID` (load-test client) |
| Bot Client ID (SSO) | `AGENT_APP_ID` (resource app) — **required** |
| DirectLine Secret | from Direct Line channel |
| Token Endpoint | alternative to the secret; leave blank if using the secret |
| Profiles | one per M365 test account (UPN, display name, scenario CSV) |

> **⚠ Change credentials via the wizard, not by editing `.env`.** The saved store wins over
> `.env`, so hand-editing a file (e.g. the DirectLine Secret) silently keeps hitting the old
> agent. Switching agents only requires the new secret *if* the new agent shares the same
> `AGENT_APP_ID`/scope/SSO connection.

### 4. Utterance scripts

Drop CSV files in `utterances/`. Each file is one scenario; each row after the `utterance`
header is one message, sent in order. Assign a scenario to each profile in the wizard.

```csv
utterance
Hi, I need help with my password.
I can't log in to my email.
What are the steps to reset a password?
```

---

## Running

```bash
docker compose run --rm gruntmaster     # or: run-gruntmaster  /  python run.py
```

After a pre-flight "hi" (confirms auth + prints the target agent id/name), the **Run
Configuration** menu appears. Key settings:

| Setting | Meaning |
|---|---|
| **Mode** | **Concurrent** (hold N users for a set run time) or **Pipeline** (each user runs its script once, then leaves). |
| **Peak / Total users** | How many simulated users. |
| **Spawn rate** | New users per minute (ramp-up). |
| **Think time** | Pause between a user's messages (randomised). |
| **Reply timeout** | Give up if the first word doesn't arrive in time (min 15s). |
| **Warm-up cap** | Max priming turns to burn off cold-start fallbacks (see below). |
| **Notes** | Free text embedded in the HTML report. |

> **SharePoint/OneDrive agents are slow** (first authenticated reply ~55–90s). Set **Reply
> timeout ≥ 120s**, or `GRUNTMASTER_RESPONSE_TIMEOUT=120` for headless runs.

**Cold-start warm-up:** Copilot Studio fast-fallbacks the first 1–2 turns of a fresh
conversation while greeting/SSO/knowledge orchestration spin up. Before measuring, each user
replays its own utterances (discarded, **not** counted) until it gets a real (non-fallback)
reply, capped by *Warm-up cap*. Tune with `GRUNTMASTER_WARMUP_TURNS` and, if your agent's
fallback wording differs, `GRUNTMASTER_WARMUP_FALLBACK_MARKERS` (semicolon-separated).

Press **Q** to stop early (skips report generation). Direct Line caps at ~8,000 msg/min; the
agent's Power Platform message capacity is usually the real ceiling.

---

## Reading results

**Live dashboard** (updates every ~0.5s): health indicator, ramp-step table, per-profile
**PROFILE STATS**, an extremes **UTTERANCES** table, and an **EVENTS** feed.

PROFILE STATS columns:
- **Typical** — median (p50); outlier-proof, the number to trust.
- **p85 / Tail p95** — percentiles; p95 shown in `(parens)` when <100 samples (not yet reliable).
- **Worst** — single slowest reply; far above Typical = an isolated outlier.
- **Shape** — self-relative distribution skew (Bowley): *Mostly fast* / *Balanced* / *Mostly slow*.

Each row also gets a plain-English verdict naming which number to trust and whether latency is
trending slower.

**Files** (in `report/`, one set per run):
- `report_*.html` — 4 tabs: Summary, Response-time distribution (box/whisker + heatmap),
  Utterance analysis (filterable, baseline diff), Config. Sortable tables.
- `detail_*.csv` — one row per message exchange (open in Excel/Power BI).
- `events_*.csv` — ramp/error/rate-limit log. `ci_*.json` — pass/fail summary
  (`GRUNTMASTER_CI=1` prints it and sets the exit code).

**Pass criteria:** p95 under your target (default 2000 ms) and error rate under 1%.

**Resilience:** two consecutive timeouts restart that user's conversation; a 429 pauses all
users 60s (circuit breaker) then resumes; connection/send retries with backoff; a shared,
auto-sized HTTPS connection pool.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| *"This agent is currently unavailable… reached its usage limit."* | Power Platform message capacity exhausted. Admin Center → environment → Capacity → increase. |
| **AADSTS650057** Invalid resource | `access_as_user` not added, admin consent missing, or `Bot Client ID (SSO)` wrong (Step 1.3). |
| **AADSTS90009** requesting a token for itself | `Client ID` = `Bot Client ID` — they must differ. Re-run wizard with the resource app's ID. |
| **IntegratedAuthenticationNotSupportedInChannel** | Agent is on "Authenticate with Microsoft" (disables Direct Line). Switch to **Authenticate manually**, then **Publish**. |
| *"No valid token for [user]"* | Cached token expired (>90d) or password changed. Re-run wizard → profile → re-authenticate. |
| Agent shows a "sign in" prompt mid-test | SSO exchange failing: check `Bot Client ID (SSO)`, the `api://<AGENT_APP_ID>/access_as_user` scope, and the Token Exchange URL. |
| Every answer is *"I'm not sure how to help"* but Test pane works | SharePoint/OneDrive knowledge missing Graph delegated scopes — grant `Sites.Read.All` + `Files.Read.All`, add to Scopes, Publish. |
| Still hitting the old agent after changing the secret | The saved store overrides `.env`. Change the secret via the **wizard**, then confirm the pre-flight agent id/name. |

---

## Environment variables

| Variable | Purpose |
|---|---|
| `GRUNTMASTER_RESPONSE_TIMEOUT` | Reply timeout (s) for headless runs. |
| `GRUNTMASTER_WARMUP_TURNS` | Cold-start warm-up cap (default 4). |
| `GRUNTMASTER_WARMUP_FALLBACK_MARKERS` | Semicolon-separated fallback phrases used to detect a "not warm yet" reply. |
| `GRUNTMASTER_TRANSPORT` | `http` selects the experimental HTTP-polling transport (default WebSocket). |
| `GRUNTMASTER_CI=1` | Print the `ci_*.json` summary and exit with a pass/fail code. |
| `TOKEN_ENCRYPTION_PASSWORD` | Token-encryption fallback (recommended in containers). |

---

## File reference

| Path | Purpose |
|---|---|
| `run.py` | The tool (wizard, load engine, live dashboard). |
| `report.py` | HTML report generator (runs automatically; also standalone). |
| `requirements.txt` / `pyproject.toml` | Dependencies and `run-gruntmaster` entry point. |
| `.env.example` | Config template — copy to `.env`. |
| `utterances/*.csv` | Test scripts; one CSV per scenario. |
| `profiles/profiles.json` | Test accounts (wizard-created; git-ignored). |
| `profiles/.tokens/`, `profiles/.keyring/` | Encrypted tokens / file keyring (local only). |
| `report/report_*.html`, `detail_*.csv`, `events_*.csv`, `ci_*.json` | Per-run outputs (git-ignored). |
