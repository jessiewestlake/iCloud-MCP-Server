# iCloud MCP Server — Agent Playbook

This document captures the practical steps that keep the helper tools, server, and diagnostics running smoothly across the environments we touch most often. Keep it close whenever you need to replay a workflow or unblock yourself quickly.

## Core Tasks (All Environments)

- **Start server**: `python server.py` from the repo root; expect the FastMCP server to listen on `http://127.0.0.1:8800/mcp`.
- **Verify port in use**: connect via `http://127.0.0.1:8800/` in a browser or run a quick health request with `curl http://127.0.0.1:8800/mcp/health` (Windows users can rely on `Invoke-WebRequest`).
- **Diagnostics toolbox** (repo root):
  - `devtools/check_message.py` → fetches the newest message and prints both structured and raw content.
  - `devtools/peek_imap.py` → prompts for a UID and fetches raw IMAP payload slices via the `_peek_imap` tool.
- **OAuth setup**: populate `OAUTH_CONSENT_PASSWORD` in `.env` before starting the server. When ChatGPT performs dynamic client registration it will redirect you to `http://127.0.0.1:8800/oauth/consent?...` to approve the client; registrations persist in `oauth_clients.json`.
- **Compile-time sanity check**: `python -m compileall server.py` guards against syntax slips before running the server.
- **Shutdown and cleanup**: take note of the PID (printed by PowerShell/terminal on error) or follow the environment-specific guidance below to stop lingering processes.

## Windows + VS Code + Copilot (PowerShell default shell)

- **Activate virtual environment** (if present): `.\.venv\Scripts\Activate.ps1`. Confirm with `Get-Command python` that you are using the venv interpreter.
- **Run the server in foreground**: `python server.py`. PowerShell shows the PID right in the prompt (`[pid] python server.py`).
- **Kill a stuck server**:
  - `Get-Process python` to list live interpreters with IDs.
  - `Stop-Process -Id <PID>` to terminate; add `-Force` if the process is stubborn.
  - If you only know the port, use `Get-NetTCPConnection -LocalPort 8800 | Select-Object -First 1 -ExpandProperty OwningProcess` to locate the PID first.
- **Check listening port**: `Test-NetConnection -ComputerName 127.0.0.1 -Port 8800` (alias `tnc`).
- **Run helper scripts**: `python devtools/check_message.py` etc.; no path tweaks required because scripts live inside the repo.
- **Tail logs**: If you redirected output (`python server.py *> server.log`), inspect with `Get-Content server.log -Wait`.
- **Environment variables**: rely on `.env`; ensure VS Code terminal inherits environment or run `Get-ChildItem Env:` to confirm.
- **Common pitfalls**:
  - PowerShell execution policy may block scripts; bypass once with `powershell.exe -ExecutionPolicy Bypass -File ...` or adjust policy if needed.
  - When multiple VS Code terminals are open, make sure only one instance of `python server.py` is running; rerun `Get-Process python` before starting a new session.

## Windows Subsystem for Linux (Ubuntu or similar)

- **Activate environment**: `source .venv/bin/activate` if you keep a separate venv inside WSL.
- **Server start**: `python server.py`. Confirm binding on localhost by curling from WSL: `curl -s http://127.0.0.1:8800/mcp/health`.
- **Expose to Windows side**: If you need Windows tools to reach the WSL server, run `netsh interface portproxy add v4tov4 listenport=8800 listenaddress=127.0.0.1 connectport=8800 connectaddress=<WSL_IP>`, or simply start the server in Windows when possible.
- **Kill process**: `ps -A | grep server.py` followed by `kill <PID>` (or `pkill -f server.py`). If the process ignores `SIGTERM`, escalate to `kill -9`.
- **Inspect port usage**: `ss -tulpn | grep 8800` shows which PID owns the port.
- **File synchronization**: ensure the repo lives on the Windows filesystem (`/mnt/c/...`) when using VS Code remote; reduces path confusion when swapping between shells.

## macOS or Generic Unix Shells

- **Virtual environment**: `source .venv/bin/activate` (or `python3 -m venv .venv` if missing).
- **Start server**: `python3 server.py`; `lsof -i :8800` confirms port ownership.
- **Terminate server**: `pkill -f server.py` or `kill $(lsof -t -i :8800)`.
- **Launch diagnostics**: `python3 devtools/check_message.py` etc.; make sure you installed requirements via `pip3 install -r requirements.txt`.
- **Watch logs**: pipe output to a file (`python3 server.py | tee server.log`) and tail with `tail -f server.log`.

## Remote Containers / Codespaces

- **Environment detection**: run `python --version` to ensure the container uses the expected interpreter (often pre-created venvs reside under `/workspaces/.../.venv`).
- **Run server**: `python server.py` (typically port 8800). Publish the port in the Codespaces UI if you need external access.
- **Kill process**: `pkill -f server.py` or `kill $(pidof python)` within the container.
- **Port forwarding**: Codespaces automatically forwards published ports; local dev containers may need `devcontainer.json` updates.
- **Dependency updates**: `pip install --upgrade -r requirements.txt` inside the container; note that container restarts may reset the environment unless you persist the venv in the workspace volume.

## Workflow Reminders

- **Before coding**: run `pip install -r requirements.txt` after repo updates to stay aligned with dependencies.
- **During debugging**: keep one terminal dedicated to the server and another for helper scripts; avoid mixing to prevent losing server output.
- **After crashes**: always verify the port is free (`Get-Process`, `ss`, or `lsof` depending on OS) before restarting—the server will not bind if the port is still held.
- **Version control hygiene**: helper scripts now live under `devtools/`; avoid recreating them at the root to keep the project tidy.
- **Log verbosity**: when needed, instrument `server.py` temporarily with `logging.basicConfig(level=logging.DEBUG)` and revert once the issue is solved.

Add new tips as you discover smoother flows or recurring pitfalls—this playbook is meant to evolve.
