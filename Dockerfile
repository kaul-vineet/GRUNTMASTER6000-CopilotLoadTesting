# GRUNTMASTER 6000 — container image
#
# Why Docker: on Windows-on-ARM the native ARM64 Python has no prebuilt wheels
# for cryptography / geventhttpclient / brotli, so `pip install` tries to compile
# from source (needs Rust + MSVC Build Tools). A Linux image gets prebuilt wheels
# for every architecture, so the install is fast and needs no compilers.
#
# The tool is fully Linux-compatible: all Windows-only code paths (msvcrt keyboard
# handling, `cls`) are guarded by `os.name == "nt"`. Credentials fall back from the
# OS keyring to environment variables, and token encryption falls back to
# TOKEN_ENCRYPTION_PASSWORD — so no Windows Credential Manager is required.

FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1

# ── System deps ───────────────────────────────────────────────────────────────
# - gum: the Charm TUI binary used for the interactive wizard/menus
# - ncurses-bin: provides `clear` (used by the UI) and terminal capabilities
# - curl/gnupg/ca-certificates: needed to add the Charm apt repo
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl gnupg ca-certificates ncurses-bin \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://repo.charm.sh/apt/gpg.key | gpg --dearmor -o /etc/apt/keyrings/charm.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/charm.gpg] https://repo.charm.sh/apt/ * *" \
        > /etc/apt/sources.list.d/charm.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gum \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python deps (separate layer for better caching) ───────────────────────────
# keyrings.alt provides a file-based keyring backend so the setup wizard can
# save credentials without a desktop secret service.
COPY requirements.txt pyproject.toml ./
RUN pip install -r requirements.txt keyrings.alt

# ── App source ────────────────────────────────────────────────────────────────
COPY . .
RUN pip install -e .

# Force a file-based keyring backend and keep its store under the mounted
# profiles/ volume so saved credentials survive container restarts.
ENV PYTHON_KEYRING_BACKEND=keyrings.alt.file.PlaintextKeyring \
    XDG_DATA_HOME=/app/profiles/.keyring

ENTRYPOINT ["python", "run.py"]
