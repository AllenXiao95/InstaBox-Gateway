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
igw serve
```

初始化时自行设置至少 12 字符的存储口令，每次启动时再次输入；应用没有默认口令。**`init` 会直接显示本地管理 Token 和执行 Token**，请妥善保存。`serve` 默认后台启动，确认启动成功后返回终端。打开 **http://127.0.0.1:18761**，使用管理 Token 登录；需要再次查看时运行：

```sh
igw token admin
```

在网页中登录、添加 InstaBox API Key，并设置需要保护的机器。Key 保存在本地加密存储中；产品启动不读取 `.env` 文件。

控制台包含**实例、账号、执行与记录、设置**四个视图。从实例详情固定一台运行机器后，可在工作台提交短命令；账号筛选不会改变绑定。本次输出仅保留在页面内存，刷新或退出会清除，最近请求只保存元数据。恢复暂停机器需要本地策略开启，并单独明确确认。

登录页和控制台顶栏均可切换 **English / 简体中文**，默认跟随浏览器支持的语言，其他语言回退至英语。浏览器只保存语言偏好；切换时保留当前目标、草稿和结果，不翻译实例名称、命令或原始输出。

## 客户端调用

运行 `igw token execution` 获取本地执行凭据。未设置 `IGW_TOKEN` 时，`igw-client` 会隐藏提示输入。自动化场景可通过进程环境传入 `IGW_TOKEN`，`IGW_URL` 默认是 `http://127.0.0.1:18761`。

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

HTTP 客户端使用 `Authorization: Bearer <执行凭据>`。主要入口为 `GET /api/v1/machines`、`POST /api/v1/bindings`、`POST /api/v1/exec` 和 `GET/PUT /api/v1/file`；`/api/admin/` 下的账号管理入口需要管理凭据。

## 使用边界

- 数据位于仓库外：macOS 为 `~/Library/Application Support/InstaBoxGateway`，Linux 为 `$XDG_DATA_HOME/instabox-gateway`（默认 `~/.local/share/instabox-gateway`），Windows 为 `%LOCALAPPDATA%\InstaBoxGateway`。可用 `igw --data-dir PATH ...` 指定。
- 使用 `igw serve --port 18762` 修改端口，仅支持本机 loopback 访问。
- 使用 `igw status` 查看本地服务，`igw stop` 停止本地服务；两者核对保存的服务身份，并要求输入存储口令。停止 Gateway 不会停止上游机器或其中的进程。需要前台运行时使用 `igw serve --foreground`，按 Ctrl+C 退出。后台模式不安装开机自启服务。
- 机器共享使用，不提供独占租约；已有绑定不会静默切换账号或机器。
- 恢复需要所有者策略和客户端显式许可。查询不主动恢复机器，但仍存在平台隐式唤醒竞态。
- 请求超时不保证后台进程已终止；结果未知的写操作不会自动重放。
- 每次文件传输最多 8 MiB，路径须在配置的根目录内；这项路径约束不构成 Shell 或符号链接的文件系统沙箱。

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
