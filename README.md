<p align="center">
  <img src="instabox_gateway/web/logo.svg" width="112" height="112" alt="InstaBox Gateway logo">
</p>
<h1 align="center">InstaBox Gateway</h1>
<p align="center">Multiple accounts. One local endpoint.</p>
<p align="center"><strong>English</strong> · <a href="README.zh-CN.md">简体中文</a></p>

A local app for managing multiple [InstaBox](https://instabox.sh) accounts and accessing their machines through one HTTP endpoint. Use the web interface to manage accounts, then connect from Codex, terminal scripts, or other local HTTP clients without giving each client your upstream API keys.

## Features

- **Multiple accounts:** encrypted credential storage, aggregated machines, account priorities, and protection rules.
- **Fixed targets:** choose a machine explicitly or by priority, then reuse its binding across requests.
- **Commands and files:** synchronous execution with exit codes, stdout/stderr, timeout and truncation flags, plus binary uploads/downloads.
- **Local access:** separate owner and execution tokens; listens on `127.0.0.1` by default.

Experimental MVP. The macOS HTTP path has been exercised against real machines; Windows/Linux runtime acceptance is pending. Native MCP and official CLI compatibility are planned separately.

## Quick start

Requires Python 3.9+; Python 3.11–3.13 is recommended.

```sh
git clone https://github.com/AllenXiao95/InstaBox-Gateway.git
cd InstaBox-Gateway
python -m venv .venv
```

Activate the environment with `source .venv/bin/activate` on macOS/Linux, or `.\.venv\Scripts\Activate.ps1` in Windows PowerShell. Then:

```sh
python -m pip install .
igw init
igw serve
```

Choose a storage passphrase of at least 12 characters during initialization; enter it again when starting the service. There is no default passphrase. **`init` displays both the local management token and execution token**; keep them private. `serve` starts the service in the background, waits for startup confirmation, and returns your terminal. Open **http://127.0.0.1:18761** and sign in with the management token. To display it again:

```sh
igw token admin
```

Sign in to the web interface, add your InstaBox API keys, and set any protected machines. Keys stay in the local encrypted store; product startup does not load a `.env` file.

The console has **Instances**, **Accounts**, **Execute & records**, and **Settings** views. From an instance's details, bind a running target, then submit a short command in the workbench. Account filters do not change that binding. Output stays in page memory and is cleared on refresh or sign-out; recent requests contain metadata only. Resuming a paused machine requires both the local policy and explicit confirmation.

Use the language selector on the sign-in page or console header to switch between **English** and **简体中文**. The default follows your browser's supported language, with English as fallback. Only the language preference is saved in browser storage; switching preserves the current target, draft, and response. Names, commands, and raw output are never translated.

## Use from a client

Obtain a local execution token with `igw token execution`. `igw-client` prompts for it when `IGW_TOKEN` is unset. For automation, supply `IGW_TOKEN` through your process environment; `IGW_URL` defaults to `http://127.0.0.1:18761`.

```sh
igw-client machines
igw-client select
```

`select` chooses an eligible running machine and returns `binding.id`. Reuse that value below; file transfer examples assume a local `hello.txt` already exists:

```sh
igw-client --binding BINDING_ID exec -- printf hello
igw-client --binding BINDING_ID upload ./hello.txt /data/home/igw-workspaces/demo/hello.txt
igw-client --binding BINDING_ID download ./received.txt /data/home/igw-workspaces/demo/hello.txt
```

For an explicit target, use `igw-client --account ACCOUNT_ID --machine MACHINE_ID select`. Replace the uppercase placeholders with returned IDs. Commands return the remote exit code; downloads refuse to overwrite an existing local file.

HTTP clients use `Authorization: Bearer <execution-token>`. The main routes are `GET /api/v1/machines`, `POST /api/v1/bindings`, `POST /api/v1/exec`, and `GET/PUT /api/v1/file`. Account management is under `/api/admin/` and requires the management token.

## Operating notes

- Data stays outside the repository: macOS `~/Library/Application Support/InstaBoxGateway`, Linux `$XDG_DATA_HOME/instabox-gateway` (default `~/.local/share/instabox-gateway`), Windows `%LOCALAPPDATA%\InstaBoxGateway`. Override with `igw --data-dir PATH ...`.
- Change the port with `igw serve --port 18762`. Only loopback access is supported.
- Use `igw status` to check the local service and `igw stop` to stop it; both verify the stored service identity and require the storage passphrase. Stopping the Gateway does not stop upstream machines or their processes. Use `igw serve --foreground` for terminal-managed operation (Ctrl+C to exit). Background mode does not install an auto-start service.
- Targets are shared, with no exclusive lease. A binding never silently moves to another account or machine.
- Resume requires both owner policy and explicit client permission. Queries do not actively resume machines; platform auto-wake races may still occur.
- A timeout does not guarantee background processes have stopped. Unknown writes are not automatically replayed.
- File transfers are limited to 8 MiB within the configured file root. This path rule is not a filesystem sandbox for shell commands or symlinks.

## Repository

```text
instabox_gateway/       Python service, CLI, client, and web assets
.github/workflows/     Package checks
pyproject.toml         Installation and package metadata
MANIFEST.in            Source-distribution boundaries
README.md              English introduction and usage
README.zh-CN.md        中文介绍与用法
```

Local verification tools, reports, designs, credentials, and runtime state are excluded from Git and package distributions.

## License

[MIT](LICENSE). Independent community project; not affiliated with InstaBox.
