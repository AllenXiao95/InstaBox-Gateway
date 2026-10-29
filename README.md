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
- **Workspace browser:** browse the fixed instance’s persistent root or a registered workspace, and download files through the existing streaming API.
- **Google Drive:** private selections, on-demand reads, recursive workspace snapshots and uploads into approved folders. OAuth credentials stay local.
- **Local access:** separate owner and execution tokens; listens on `127.0.0.1` by default.
- **Background tasks:** detached workers, persistent status and logs, explicit checkpoint resume, and local completion events.
- **Workspace guards:** fixed machine and directories, checked file paths, test context, and read-only Git queries. These guards are not a Shell sandbox.

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
igw serve --open
```

Choose a storage passphrase of at least 12 characters during initialization; enter it once when starting the service. There is no default passphrase. **`init` displays both local tokens**; keep them private. `serve --open` starts the service in the background and opens a signed-in console. To open it later without copying a token:

```sh
igw open
```

Sign in to the web interface, add your InstaBox API keys, and set any protected machines. Keys stay in the local encrypted store; product startup does not load a `.env` file.

The console has **Instances**, **Accounts**, **Execute & records**, **Background tasks**, and **Settings** views. Bind a running target in Instances before saving a workspace or executing a short command. Filters never move a fixed target. Synchronous output stays in page memory; background task logs stay on the target. Resuming a paused machine requires both policy and explicit permission.

Browser sessions survive refresh for up to eight hours, ending on sign-out or service restart. A single-use, 60-second ticket opens the session; long-lived management tokens are not put in URLs or browser storage. Manual token login remains available with `igw token admin`.

Use the language selector on the sign-in page or console header to switch between **English** and **简体中文**. The default follows your browser's supported language, with English as fallback. Only the language preference is saved in browser storage; switching preserves the current target, draft, and response. Names, commands, and raw output are never translated.

## Use from a client

Pair once with the running, unlocked Gateway. Pairing saves an execution credential in the owner-only application data directory, without storing the vault passphrase or upstream keys:

```sh
igw-client login
```

Use the same `--data-dir` as the service if customized. An explicit `IGW_TOKEN` still overrides pairing; without either, the client prompts. `IGW_URL` defaults to the paired URL or `http://127.0.0.1:18761`. `igw-client logout` removes the local profile, without revoking the shared execution token.

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

Downloads stream with bounded memory and support large files. To check a trusted SHA256 or refresh SSH metadata:

```sh
igw-client --binding BINDING_ID download ./package.tar.gz /data/home/igw-workspaces/package.tar.gz --sha256 EXPECTED_SHA256
igw-client --binding BINDING_ID connection-info
```

The first binding discovers and caches SSH metadata (host, port, username and host CA); `connection-info` refreshes it. Discovery does not issue login credentials, connect over SSH, or prove SSH reachability. Missing SSH permissions do not block binding or independent HTTP capabilities.

Downloads write to a uniquely named `.part` file beside the destination and publish it only after receiving all declared bytes and passing any supplied checksum. Interrupted or checksum-failed files are retained. Automatic resume and HTTP Range are not supported. `--timeout` sets socket inactivity timeout (default 75 seconds); total transfer time is limited to two hours. Without `--sha256`, the reported checksum is computed, not independently verified.

HTTP clients use `Authorization: Bearer <execution-token>`. The main routes are `GET /api/v1/machines`, `POST /api/v1/bindings`, `POST /api/v1/exec`, and `GET/PUT /api/v1/file`. Account management is under `/api/admin/` and requires the management token.

`GET /api/v1/file` streams downloads. `GET /api/v1/connection-info` accepts the same binding or explicit account/machine target.

## Workspaces and background tasks

Save one workspace after choosing a binding. Replace IDs and paths with your own. Commands below assume your project contains the referenced scripts:

```sh
igw-client --binding BINDING_ID workspace add --name demo --remote-root /data/home/igw-workspaces/demo --local-root "$PWD"
igw-client workspace list
igw-client use WORKSPACE_ID
igw-client exec -- pwd
igw-client test -- python3 -m unittest
igw-client git -- status --porcelain=v1
igw-client upload ./hello.txt hello.txt
```

The profile supplies the fixed workspace. Its remote file paths are relative to `remoteRoot`; local upload/download paths must stay inside `localRoot`. Execution checks the actual cwd and repository root. Git queries support `status --porcelain=v1`, `diff --stat`, and `rev-parse HEAD`. The raw binding APIs remain available for compatibility and do not inherit workspace guards.

```sh
igw-client task start --submission-id demo-build-001 --argv '["python3","build.py"]' --preflight-argv '["python3","preflight.py"]' --verify-argv '["python3","verify.py"]' --resume-argv '["python3","build.py","--resume"]'
igw-client task status TASK_ID
igw-client task logs TASK_ID --channel stdout --offset 0
igw-client task cancel TASK_ID
igw-client task resume TASK_ID --submission-id demo-resume-001
igw-client task verify TASK_ID --submission-id demo-verify-001
```

For a simple background command:

```sh
igw-client exec --mode background_task --submission-id demo-build-002 -- python3 build.py
```

Submit returns after preflight and a short launch-confirmation window, with a task ID and dispatch state. On a lost response, reuse the **same submission ID** or query the original task; never invent a new ID to retry an unknown write. `resume` requires a checkpoint-aware command declared at submission. It creates a linked attempt on the same workspace and machine. Without a verification command, exit zero means execution succeeded, not artifact verification.

To monitor an existing Builder without starting it again:

```sh
igw-client task watch --submission-id existing-build-001 --status-path work/state/run.json --stdout-path work/logs/build.log
igw-client task events
```

Watched tasks are read-only; verification is labeled as producer-reported. Use the original Builder controls for them. Logs are paged (64 KiB); the response offset continues the next read. The Gateway monitors tasks without a running Codex turn. Stopping the Gateway stops monitoring, not remote workers; reopen and unlock it to reconcile. No automatic machine wake, job replay, or resource cleanup occurs.

Ordinary background tasks should run their work in the foreground under the runner. If a Builder's `start` command detaches another process, monitor its state with `task watch`; a successful launch command does not establish that the build finished.

Completion, failure and blocking events appear in **Background tasks**. macOS notifications are submitted from the local service; OS permission and Focus settings determine display. Linux uses `notify-send` if available; Windows desktop notifications are currently unavailable. Unsupported or denied desktop delivery is recorded in the task event, with the task result retained. No notification requires an open browser or automatically starts a Codex turn.

## Files and Google Drive

Open **File manager** in the console. The left pane browses directories on the computer running Gateway; the right pane selects a running InstaBox machine and browses `/data`. No workspace registration is needed, and browsing is independent of the execution `fileRoot`. Both panes have their own path navigation and pagination. Click **Download to local** to save a remote file into the directory shown on the left when the download is accepted.

Downloads run in the local Gateway background. Closing the page or switching machines does not redirect or cancel an accepted download. The transfer list shows byte progress, results, and the SHA256 of the received bytes. Transfers use bounded buffers without a Gateway file-size cap. Existing files are never overwritten, and incomplete temporary files are not published. Stopping Gateway interrupts transfers; HTTP Range/automatic resume and folder archives are not included. Remote symlinks are not followed, browsing never actively resumes a machine, and machine protection rules still apply.

Local browsing, remote `/data` browsing, and downloads to local disk use `/api/admin/files/` and require an owner management session or admin token. Execution clients retain their existing `fileRoot` and fixed-workspace boundaries.

Private Google Drive access uses OAuth on Google's website. Once the app identity is configured, click **Sign in to Google** to open authorization. The console automatically detects success, denial, or expiration; no manual refresh is needed. If a new tab is blocked, use **Continue Google authorization**. Advanced setup is secondary and does not need to be repeated for normal sign-ins.

The maintainer configures the app identity once. This checkout contains no real OAuth app credentials. An unconfigured installation shows **Set up Google sign-in → Maintainer setup (advanced)**.

1. Create or select a Google Cloud project and enable the [Google Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com).
2. In **Google Auth Platform**, configure **Branding**, **Audience**, and **Data Access**. Personal Google accounts use an External audience; add your account under Test users during testing. Recursive reads use `https://www.googleapis.com/auth/drive.readonly`. Upload-folder approval separately requests `https://www.googleapis.com/auth/drive.file` and requires the **Google Picker API** enabled in the same project.
3. Create a **Desktop app** OAuth client under [Clients](https://console.cloud.google.com/auth/clients) and download its JSON. Do not select Web application. Keep client credentials and Google tokens outside Git.
4. Import the JSON once in the console's advanced maintainer setup. The configuration and tokens are encrypted in the existing local store. This setup does not replace Google's consent step.
5. Click **Sign in to Google**, select your account, and grant read access on Google. The console then browses My Drive or Shared with me. Explicitly approve read selections. Upload-folder approval opens Google Picker and requires the same account and exact destination folder.

Maintainers can also provision the application identity at the first startup instead of importing JSON through the web UI. On macOS/Linux, use your own absolute path outside the repository:

```bash
chmod 600 /absolute/private/google-drive-client.json
IGW_GOOGLE_OAUTH_CLIENT_FILE=/absolute/private/google-drive-client.json igw serve --open
```

Preserve any existing `IGW_UPSTREAM_PROXY`, `SSL_CERT_FILE`, or other required startup settings. The file must be outside Git repositories with owner-only POSIX permissions; restrict its ACL on Windows. Import occurs only when no encrypted OAuth configuration already exists. Existing client configuration and connections are never replaced by this environment setting. After successful import, subsequent launches use encrypted storage without the variable. Invalid provisioning blocks Drive sign-in while other Gateway features remain available.

One maintainer-registered app can serve its permitted users without each user creating a Google Cloud project; each local installation still needs the maintainer-provided app configuration. Public distribution and restricted scopes are subject to [Google OAuth policies](https://developers.google.com/identity/protocols/oauth2/policies) and [Drive scope requirements](https://developers.google.com/workspace/drive/api/guides/api-specific-auth). Testing mode or organizational policy may limit which accounts can sign in.

Online reads, file downloads, background recursive snapshots to a fixed workspace, and new-file uploads to approved folders are supported. Trusted execution clients see only approved selections and never receive Google tokens. Overwrite, delete, and move operations are not exposed.

`drive.readonly` permits reading the entire Drive at the Google authorization layer; selection restrictions are enforced by this application. `drive.file` also permits modifications to app-authorized files, but this application exposes only new-file uploads. Google still enforces folder permissions; a denial does not trigger broader authorization. Disconnecting removes local tokens; revoke the Google account grant separately in Google’s security settings. Online reads connect from the local Gateway directly to Google; only snapshots are then sent to the remote workspace.

There is **no Gateway-imposed file-size cap**. Reads and Gateway uploads stream with bounded buffers; Google uploads use resumable chunks. Native Google export limits, provider quotas, available disk space and inactivity timeouts still apply. Text previews display up to 256 KiB; the full file remains downloadable. Docs export as TXT, Sheets as XLSX, and Slides as PDF. Online reads do not persist disk copies and are not a filesystem mount or document editor. Shortcuts are not followed. Shared-with-me items remain subject to owner download permissions; Shared Drives have not received separate live acceptance.

Snapshots run in the local Gateway’s background worker and write a manifest under `inputs/google-drive/<task-id>/` on the fixed remote workspace. Closing the page does not cancel a snapshot. Stopping the local service interrupts its transfers; resume explicitly after restarting. Completed remote files are checked by SHA256 before reuse. Unknown upload outcomes retain the original Drive file ID and an encrypted resumable session. Re-submit the same file with the same Idempotency-Key to query the accepted offset and send only missing Google chunks, or reconcile an already completed upload. The browser retains the key for the same selected file while the page is open; scripts should retain their own stable key. If the session was lost before its URL was recorded, reconcile rather than blindly creating another file. Upload validation and snapshots use private temporary local staging, so sufficient local disk space is required.

HTTP clients use the existing local authentication. Owner-only routes configure OAuth, browse all accessible Drive metadata and approve selections. Execution routes include:

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/api/v1/persistent-files?bindingId=…&path=.` | Browse the configured persistent root |
| GET | `/api/v1/workspaces/{id}/files?path=.` | Browse a registered workspace |
| GET | `/api/v1/persistent-file?bindingId=…&path=…&download=1` | Stream a persistent file |
| GET | `/api/v1/file?workspaceId=…&path=…&download=1` | Stream a workspace file |
| GET | `/api/v1/drive/selections` | List approved Drive selections |
| GET | `/api/v1/drive/selections/{id}/files` | List a selected folder (`parentId` / `pageToken` optional) |
| GET | `/api/v1/drive/selections/{id}/content?fileId=…` | Read/download selected content |
| POST | `/api/v1/drive/imports` | Background snapshot; JSON `selectionId`, `workspaceId` |
| POST | `/api/v1/drive/selections/{id}/upload?name=…` | Add binary content to an approved folder |

Snapshot and upload requests require a stable `Idempotency-Key`. Query `/api/v1/drive/imports` for import/upload records. Import control uses `/api/v1/drive/imports/{id}/cancel` or `/resume`; reconcile an upload using `/api/v1/drive/uploads/{id}/verify`. Native console downloads use the existing HttpOnly login session; credentials never appear in download URLs.

The Google integration has passed isolated local tests with synthetic API responses. Real Google OAuth/private Drive transfer and the new remote directory browser still require live acceptance; upstream documentation alone does not establish runtime compatibility.

## Operating notes

- Data stays outside the repository: macOS `~/Library/Application Support/InstaBoxGateway`, Linux `$XDG_DATA_HOME/instabox-gateway` (default `~/.local/share/instabox-gateway`), Windows `%LOCALAPPDATA%\InstaBoxGateway`. Override with `igw --data-dir PATH ...`.
- Change the port with `igw serve --port 18762`. Only loopback access is supported.
- Use `igw status` and `igw stop` without another unlock on newly started services; both verify the owner-only service identity. Legacy instances fall back to passphrase-based control. Stopping the Gateway does not stop upstream machines or processes. Use `igw serve --foreground` for terminal-managed operation. Background mode does not install an auto-start service.
- Targets are shared, with no exclusive lease. A binding never silently moves to another account or machine.
- Resume requires both owner policy and explicit client permission. Queries do not actively resume machines; platform auto-wake races may still occur.
- A timeout does not guarantee background processes have stopped. Unknown writes are not automatically replayed.
- File uploads and downloads stream without a Gateway file-size cap. Paths must stay within the configured file root. This path rule is not a filesystem sandbox for shell commands or symlinks.
- Workspace guards prevent accidental misrouting; arbitrary Shell code, Docker and Codex's direct local tools still use their own OS permissions. Files are not automatically synchronized. `.igw/` inside the remote workspace contains runner state and logs; application credentials and profiles stay outside the checkout. Keep secrets out of command arguments and task logs.
- New task/login/workspace behavior is covered by isolated local acceptance; real InstaBox background-task and Windows/Linux acceptance remain pending. Desktop command submission is not proof of visible notification delivery.

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
