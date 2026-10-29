<p align="center">
  <img src="instabox_gateway/web/logo.svg" width="112" height="112" alt="InstaBox Gateway 标志">
</p>
<h1 align="center">InstaBox Gateway</h1>
<p align="center">多个账号，一个本地入口。</p>
<p align="center"><a href="README.md">English</a> · <strong>简体中文</strong></p>

一个在本机运行的 [InstaBox](https://instabox.sh) 多账号管理与统一访问应用。通过网页管理账号，再让 Codex、终端脚本或其他本地 HTTP 客户端访问机器，无需向每个客户端分别提供上游 API Key。

## 功能

- **多账号管理：** 加密保存凭据，聚合机器，设置账号优先级和机器保护规则。
- **固定目标：** 显式选机或按优先级自动选择，后续请求复用同一绑定。
- **命令与文件：** 同步执行并保留退出码、双路输出、超时和截断标志，支持二进制上传下载。
- **本地访问：** 管理凭据与执行凭据分离，默认监听 `127.0.0.1`。
- **后台任务：** 独立进程、持久状态与日志、显式检查点续跑和本地完成事件。
- **工作区约束：** 固定机器与目录，检查文件路径、测试上下文和只读 Git 查询；不构成 Shell 安全沙箱。

当前为实验性 MVP。macOS 的 HTTP 路径已进行真实机器验证，Windows/Linux 运行验收尚待完成；原生 MCP 和官方 CLI 兼容另行规划。

## 快速开始

需要 Python 3.9+，推荐 Python 3.11–3.13。

```sh
git clone https://github.com/AllenXiao95/InstaBox-Gateway.git
cd InstaBox-Gateway
python -m venv .venv
```

macOS/Linux 使用 `source .venv/bin/activate` 激活环境；Windows PowerShell 使用 `.\.venv\Scripts\Activate.ps1`。然后运行：

```sh
python -m pip install .
igw init
igw serve --open
```

初始化时设置至少 12 字符的存储口令，每次启动输入一次；应用没有默认口令。**`init` 会显示本地管理 Token 和执行 Token**，请妥善保存。`serve --open` 后台启动并打开已登录控制台。以后无需复制 Token，运行：

```sh
igw open
```

在网页中登录、添加 InstaBox API Key，并设置需要保护的机器。Key 保存在本地加密存储中；产品启动不读取 `.env` 文件。

控制台包含**实例、账号、执行与记录、后台任务、设置**五个视图。先在实例详情固定运行机器，再保存工作区或提交短命令。筛选不改变绑定；同步输出只保留在页面内存，后台任务日志保留在目标机器。恢复暂停机器仍需策略允许及显式授权。

浏览器会话刷新后保留，最长八小时，退出或服务重启后失效。打开控制台使用 60 秒有效、一次性的登录票据，长期管理 Token 不写入 URL 或浏览器存储。仍可运行 `igw token admin` 使用兼容的手工登录。

登录页和控制台顶栏均可切换 **English / 简体中文**，默认跟随浏览器支持的语言，其他语言回退至英语。浏览器只保存语言偏好；切换时保留当前目标、草稿和结果，不翻译实例名称、命令或原始输出。

## 客户端调用

与正在运行且已解锁的 Gateway 配对一次：

```sh
igw-client login
```

配对只将执行凭据保存在应用数据目录的所有者权限文件中，不保存存储口令或上游 Key。若服务使用自定义数据目录，客户端也传入相同的 `--data-dir`。显式 `IGW_TOKEN` 优先；没有配对和环境凭据时才隐藏提示输入。`IGW_URL` 默认使用配对地址或 `http://127.0.0.1:18761`。`igw-client logout` 删除本地配置，不撤销共用执行 Token。

```sh
igw-client machines
igw-client select
```

`select` 选择符合条件的运行机器，返回 `binding.id`。后续复用这个值；文件传输示例需要本地已有 `hello.txt`：

```sh
igw-client --binding BINDING_ID exec -- printf hello
igw-client --binding BINDING_ID upload ./hello.txt /data/home/igw-workspaces/demo/hello.txt
igw-client --binding BINDING_ID download ./received.txt /data/home/igw-workspaces/demo/hello.txt
```

显式选机使用 `igw-client --account ACCOUNT_ID --machine MACHINE_ID select`。将大写占位符替换成返回的 ID；命令保留远端退出码，下载默认不覆盖已有本地文件。

下载采用有界内存的流式传输，支持大文件。可指定可信 SHA256，或重新读取 SSH 连接信息：

```sh
igw-client --binding BINDING_ID download ./package.tar.gz /data/home/igw-workspaces/package.tar.gz --sha256 EXPECTED_SHA256
igw-client --binding BINDING_ID connection-info
```

首次绑定会读取并缓存 SSH 地址、端口、用户名和主机 CA 等元数据，`connection-info` 可重新读取。此过程不签发登录凭据、不建立 SSH 连接，也不代表 SSH 可达性已经验证；缺少 SSH 权限不会阻断绑定或独立的 HTTP 能力。

下载先写入目标目录中带随机名称的 `.part` 文件，收齐声明的字节并通过指定摘要校验后才生成目标文件。中断或摘要不匹配时保留临时文件，暂不支持自动续传和 HTTP Range。`--timeout` 指定 socket 无数据超时，默认 75 秒；单次下载总时长最多两小时。未指定 `--sha256` 时只计算摘要，不代表已与来源校验。

HTTP 客户端使用 `Authorization: Bearer <执行凭据>`。主要入口为 `GET /api/v1/machines`、`POST /api/v1/bindings`、`POST /api/v1/exec` 和 `GET/PUT /api/v1/file`；`/api/admin/` 下的账号管理入口需要管理凭据。

`GET /api/v1/file` 采用流式下载；`GET /api/v1/connection-info` 接受相同的绑定或显式账号/机器参数。

## 工作区与后台任务

选择绑定后保存工作区，将 ID 和路径替换为实际值。以下执行示例假设项目内已有对应脚本：

```sh
igw-client --binding BINDING_ID workspace add --name demo --remote-root /data/home/igw-workspaces/demo --local-root "$PWD"
igw-client workspace list
igw-client use WORKSPACE_ID
igw-client exec -- pwd
igw-client test -- python3 -m unittest
igw-client git -- status --porcelain=v1
igw-client upload ./hello.txt hello.txt
```

配置会补充固定工作区。远端文件路径相对于 `remoteRoot`；本地上传/下载路径必须在 `localRoot` 内。受管执行检查真实 cwd 与仓库根。Git 查询支持 `status --porcelain=v1`、`diff --stat`、`rev-parse HEAD`。兼容的原始 binding 接口不自动获得工作区约束。

```sh
igw-client task start --submission-id demo-build-001 --argv '["python3","build.py"]' --preflight-argv '["python3","preflight.py"]' --verify-argv '["python3","verify.py"]' --resume-argv '["python3","build.py","--resume"]'
igw-client task status TASK_ID
igw-client task logs TASK_ID --channel stdout --offset 0
igw-client task cancel TASK_ID
igw-client task resume TASK_ID --submission-id demo-resume-001
igw-client task verify TASK_ID --submission-id demo-verify-001
```

简单后台命令也可使用：

```sh
igw-client exec --mode background_task --submission-id demo-build-002 -- python3 build.py
```

提交经过预检及短暂启动确认窗口后返回任务 ID 和 dispatch 状态。启动响应丢失时，复用**原提交 ID** 或查询原任务，不使用新 ID 重试未知写操作。`resume` 只接受提交时声明的检查点续跑命令，在原工作区和机器创建关联的新尝试。未配置验证命令时，退出 0 只表示执行成功，不表示产物已经验证。

只读接管已有 Builder，无需重新启动：

```sh
igw-client task watch --submission-id existing-build-001 --status-path work/state/run.json --stdout-path work/logs/build.log
igw-client task events
```

接管任务只读，验证结果标为生产者报告；控制操作继续使用原 Builder。日志每次最多 64 KiB，用返回的 offset 继续读取。监视由本地 Gateway 完成，不占用 Codex 轮次。停止 Gateway 只停止监视，重新启动并解锁后按原任务对账；不自动唤醒机器、重放任务或清理资源。

普通后台任务应在执行器下以前台方式完成实际工作。如果 Builder 的 `start` 自行脱离并启动另一个进程，应使用 `task watch` 监视其状态，不能把启动命令成功当作构建完成。

完成、失败和阻断事件显示在“后台任务”。macOS 从本地服务提交桌面通知，实际显示受系统权限和勿扰模式影响；Linux 有 `notify-send` 时尝试发送，Windows 桌面通知暂不可用。不可用状态会记录到事件中，任务结果保留。提醒不依赖打开浏览器，也不会自动启动 Codex 新轮次。

## 文件浏览与 Google Drive

在控制台打开 **工作区文件**，选择已登记工作区，或先在实例页固定机器，再选择该机器的持久化根目录（`fileRoot`）。支持目录导航和文件下载，复用现有流式下载 API，交给浏览器下载管理器处理，使用有界缓冲区，不设 Gateway 单文件大小上限。目录不自动打包、不跟随符号链接、不主动恢复机器，也暂不支持 HTTP Range 续传。

私有 Google Drive 的连接步骤：

1. 在自己的 Google Cloud 项目启用 Drive API，配置同意页面和测试用户，创建 **Desktop app** 类型 OAuth 客户端。
2. 将客户端 JSON 下载到仓库外，在 **Google Drive → 首次连接设置** 选择并保存。配置和 Google 令牌使用已有本地存储加密保存。
3. 点击 **登录 Google → 继续 Google 授权**，在系统浏览器完成授权，再刷新连接。初次连接仅请求只读权限。
4. 浏览“我的云端硬盘”或“与我共享”，明确批准读取文件/目录。指定已有上传文件夹时，会进入单独的 Google Picker 授权（仅 `drive.file`），需要确认同一 Google 账号与原目标目录；此步骤还需在同一 Cloud 项目启用 Google Picker API。受信执行客户端只能访问已批准的内容，无法取得 Google 令牌。
5. 支持按需在线读取、单文件下载、后台递归快照到固定工作区，以及向批准文件夹新增本地文件。没有覆盖、删除和移动入口。

`drive.readonly` 在 Google 授权层可读取整个 Drive，所选范围限制由 Gateway 实施。`drive.file` 本身还允许修改应用获准访问的文件，但 Gateway 本轮只提供新增上传。目标目录仍受 Google 权限约束；拒绝时明确失败，不自动扩大 scope。断开连接会清除本地令牌，Google 账号侧授权需在其安全设置中撤销。在线读取由本地 Gateway 直接连接 Google，只有快照才会进一步发送到 InstaBox 工作区。

**不设 Gateway 单文件大小上限**。读取和网关上传采用流式处理，Google 上传使用 resumable 分块会话；Google 自身的导出限制、上游配额、可用磁盘及空闲超时仍适用。文本预览最多显示 256 KiB，完整文件仍可下载。Docs 导出 TXT、Sheets 导出 XLSX、Slides 导出 PDF。在线读取不保存磁盘副本，不提供透明挂载或在线文档编辑器。Shortcut 不跟随，“与我共享”的内容仍受拥有者下载限制；Shared Drives 尚未单独实机验收。

快照由本地 Gateway 的后台线程传输，固定到远端工作区 `inputs/google-drive/<任务ID>/`，保存来源和 SHA256 清单。关闭页面不取消；停止本地服务会中断传输，重启后需显式续跑并校验已有文件。上传结果未知时保留原 Drive file ID 和加密的续传会话；使用同一文件、同一 Idempotency-Key 重新提交，会先查询 Google 已接收偏移，只补传缺失分块。也可先核对已完成的原上传。浏览器仅在当前页面保留同一文件的请求键，脚本需自行保存稳定请求键。若会话 URL 在记录前丢失，应先人工核对，不盲目新建。上传校验和快照使用本地私有临时文件暂存，需要足够本地磁盘；传输后自动清除暂存。

HTTP 脚本沿用现有本地鉴权：执行接口提供 `/api/v1/drive/selections`、选择范围内的 `/files`、`/content`，以及 `/api/v1/drive/imports` 和向选择目录的 `/upload`。连接设置、全盘元数据浏览、选择批准仅开放给管理入口。快照/上传需要稳定 `Idempotency-Key`。目录浏览提供 `/api/v1/persistent-files` 与 `/api/v1/workspaces/{id}/files`，下载提供 `/api/v1/persistent-file` 与现有 `/api/v1/file`；完整参数见英文 README 的接口表。

Google 集成已通过模拟 API 的本地隔离验证，真实 Google 登录/私有 Drive 传输及新的远端目录浏览仍待实机验收。不会把官方文档支持写成已实测成功。

## 使用边界

- 数据位于仓库外：macOS 为 `~/Library/Application Support/InstaBoxGateway`，Linux 为 `$XDG_DATA_HOME/instabox-gateway`（默认 `~/.local/share/instabox-gateway`），Windows 为 `%LOCALAPPDATA%\InstaBoxGateway`。可用 `igw --data-dir PATH ...` 指定。
- 使用 `igw serve --port 18762` 修改端口，仅支持本机 loopback 访问。
- 新启动的服务可直接使用 `igw status`、`igw stop`，无需再次解锁；它们校验所有者权限文件中的服务身份。旧实例回退到口令控制。停止 Gateway 不会停止上游机器或进程。前台运行使用 `igw serve --foreground`；后台模式不安装开机自启服务。
- 机器共享使用，不提供独占租约；已有绑定不会静默切换账号或机器。
- 恢复需要所有者策略和客户端显式许可。查询不主动恢复机器，但仍存在平台隐式唤醒竞态。
- 请求超时不保证后台进程已终止；结果未知的写操作不会自动重放。
- 文件上传和下载均采用流式传输，不设 Gateway 单文件大小上限。路径须在配置的根目录内；这项路径约束不构成 Shell 或符号链接的文件系统沙箱。
- 工作区约束防止误操作；任意 Shell、Docker 和 Codex 直接使用的本地工具仍受各自 OS 权限控制，不自动同步文件。远端工作区的 `.igw/` 保存执行器、状态和日志；应用凭据与客户端配置保存在仓库外。命令参数及日志不要包含秘密。
- 新任务、登录和工作区功能完成隔离本地验收；真实 InstaBox 后台任务以及 Windows/Linux 验收仍待补充。通知命令提交成功不等于已显示给用户。

## 目录结构

```text
instabox_gateway/       Python 服务、CLI、客户端及网页资源
.github/workflows/     包检查
pyproject.toml         安装与包元数据
MANIFEST.in            源码发行包边界
README.md              英文介绍与用法
README.zh-CN.md        中文介绍与用法
```

本地验证工具、测试报告、设计文档、凭据和运行状态不进入 Git 或发行包。

## 许可

[MIT](LICENSE)。本项目为独立社区项目，与 InstaBox 官方无隶属关系。
