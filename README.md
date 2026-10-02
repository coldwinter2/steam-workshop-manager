# Steam 游戏 Mod 下载同步工具

使用 Python + FastAPI 编写的 Steam 创意工坊 Mod 管理同步工具。
通过 Web 管理端配置游戏与 Mod，利用 `steamcmd` 手动触发下载，
并对外提供文件分发接口。

## 服务端

### 功能

- **Web 管理后台（带登录鉴权）**：仪表盘、游戏/Mod 管理、设置、已下载清单
  - 默认账号 `admin` / `admin123`，保存在 `config.toml`
  - Web 登录密码使用 **PBKDF2 加盐哈希**存储（不可逆）
- **Steam 账号登录**：可在设置中配置 Steam 账号，由 steamcmd 以账号登录下载
  - 支持首次登录的 **设备授权（Steam Guard）**：填入邮箱/手机验证码（Guard 码）后重新测试登录，steamcmd 会本地记住登录态
  - Steam 密码使用 **Fernet 对称加密**存储（需可解密回传 steamcmd）
- **steamcmd 下载**：匿名或账号登录 + `workshop_download_item` 下载指定 Mod
- **手动同步**：在 Web 端点击「更新全部」，顺序下载所有已配置 Mod；「触发自动更新」只补下载未下载的 Mod
- **Mod 清单**：扫描本地已下载内容，展示文件与大小
- **分游戏配置文件**：每个游戏生成独立 TOML（`games/<AppID>.toml`）存放其 Mod 列表
- **Mod 订阅链接**：添加 Mod 时可填纯 ID 或直接粘贴创意工坊订阅链接，自动解析 ID
- **商店页地址添加游戏**：添加游戏可直接粘贴 Steam 商店页地址（`.../app/<AppID>`），自动解析 AppID
- **Steam 名称自动获取**：添加游戏 / Mod 时点击「获取名称」，从 Steam 商店页 / 创意工坊页自动填充名称（留空也会在提交时尝试自动获取）
- **定向更新**：支持**单独更新一个 Mod** 或**更新某个游戏下的全部 Mod**（无需全量重下）
- **Mod 依赖管理**：
  - 添加 Mod 时自动解析其 Steam 创意工坊**依赖项**并一并加入（级联，递归深度上限 6 层）
  - 明确区分**「手动」**（用户添加）与**「自动」**（依赖自动引入）两种来源，界面以标签区分
  - 删除主体 Mod 时，仅清理**不再被任何手动 Mod 依赖**的「自动」依赖；用户手动单独添加的 Mod 即使依赖关系消失也**保留**，不会被自动删除
  - 删除 Mod / 游戏时**同步删除已下载文件**（Mod 目录与 extract 的 zip 缓存；删除游戏则清空整个下载目录）；同步任务运行中删除返回 409
  - 若把被自动引入的依赖手动添加，则**提升为「手动」**（不再被自动清理）
- **文件分发**：`/list`、`/files/{game}`、`/download/{game}/{path}`（兼容 demo 客户端，**公开**）
- **局域网广播**：可选 UDP 广播，便于客户端自动发现
- **代理支持（socks5）**：仅 Steam 网页访问（商店 / 社区 / API）走代理；steamcmd 下载不使用本代理，详见下方「代理配置」
- **TOML 配置**：主配置 `config.toml` + 各游戏独立 `games/<AppID>.toml`

### 目录结构

```
game_sync/
├── app/
│   ├── server.py            # FastAPI 主服务与路由
│   ├── config_manager.py    # TOML 配置读写
│   ├── steamcmd_runner.py   # steamcmd 封装（下载 / 登录 / +runscript 脚本执行）
│   ├── steam_meta.py        # 从 Steam 获取游戏/Mod 名称、解析依赖、解析地址
│   ├── sync_manager.py      # 同步任务与清单扫描（含"仅未下载"目标计算）
│   ├── auto_updater.py      # 自动更新调度（延时触发 + 定时扫描，仅未下载）
│   ├── broadcast.py         # 局域网广播
│   ├── file_dist.py         # 文件分发 + 分组清单/下载解析
│   ├── install.py           # 服务端安装类型（copy/rename/extract，模块化）
│   ├── proxy.py             # socks5/http/https 代理配置（仅 Steam 网页访问）
│   └── templates/index.html # Web 管理界面
├── client/                  # 桌面客户端（tkinter，纯标准库）
│   ├── config.py            # 硬编码 AppID + 默认服务端地址 + 运行期配置（本地路径）
│   ├── api.py               # 只读访问服务端（分组清单/download，不代理）
│   ├── deps.py              # 依赖图：下载闭包 / 删除孤儿依赖
│   ├── store.py             # 下载→安装管线 + 状态记录（不碰服务端）
│   ├── install/             # 客户端安装处理器（copy/rename/extract，模块化）
│   ├── discovery.py         # 局域网广播解析 / 发现服务端
│   ├── endpoint.py          # 服务端地址管理：默认地址优先 + 广播动态更新 + 自动重连
│   ├── ui.py                # 图形界面
│   └── __main__.py          # 入口：python -m client
├── requirements.txt         # 服务端依赖
└── README.md
```

### 安装与运行

```bash
# 1. 安装依赖（使用项目本地 venv）
python -m pip install -r requirements.txt

# 2. 准备 steamcmd
#    Windows: 下载 https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip 并解压
#    Linux:   curl -sqL https://steamcdn-a.akamaihd.net/client/steamcmd.zip | bsdtar -xvf-  && chmod +x steamcmd.sh
#    首次运行 steamcmd 会自行更新。

# 3. 配置 steamcmd 路径（在 Web「设置」中填写，或编辑 config.toml）
#    Windows 例: C:/steamcmd/steamcmd.exe
#    Linux   例: /opt/steamcmd/steamcmd.sh
#    注意事项：经过实际测试，steamcmd需要完成一次手动登录，首次登录需要手动完成登录验证，不要在设置中填写密码，仅填写用户名，否则后续每次使用steamcmd时可能都会需要验证。

# 4. 启动服务
python -m app.server
# 浏览器打开 http://<host>:8080
# 首次访问使用默认账号 admin / admin123 登录（请在设置中修改密码）
```

> 若使用系统 Python，也可：`python -m venv venv && venv/bin/pip install -r requirements.txt`
> （Linux/macOS 下解释器为 `venv/bin/python`）。依赖含 `fastapi / uvicorn / tomlkit / PySocks / cryptography`。

### 使用流程

1. 打开 Web 管理端，使用 `admin` / `admin123` 登录。
2. **设置** → 填写 `steamcmd 路径` 与 `存储目录`，保存。
3. （可选）**设置** → 配置并启用 **Steam 账号**：填用户名/密码，点「测试登录」；
   若提示需要设备授权，把收到的 Guard 码填入后保存再测试，直到登录成功。(**注意:**实际测试发现，最好的方式是手动登录，设置中只填写账号，不要填写密码，首次登录成功后后续登录无需密码即可。)
4. **游戏 / Mod**：添加游戏——可填 AppID 或直接粘贴 Steam 商店页地址（`.../app/<AppID>`），
   点「获取名称」自动从 Steam 填充；添加后生成 `games/<AppID>.toml`。
   在每个游戏下添加 Mod——可填 ID 或粘贴创意工坊订阅链接（自动解析），
   同样支持「获取名称」自动填充 Mod 名。添加时会自动解析该 Mod 的创意工坊**依赖项**
   并一并加入，依赖以「自动」标签标记；界面会显示每个 Mod 的依赖数量。
5. **更新**：在游戏卡片点 **🔄 更新全部** 更新该游戏所有 Mod；
   在每个 Mod 旁点 **🔄 更新** 单独更新该 Mod；日志在仪表盘查看。
6. **仪表盘** 也可点 **更新全部** 做全量更新；点 **触发自动更新** 则按自动更新的规则
   只更新尚未下载的 Mod。**已下载清单** 查看结果。
7. **删除级联**：移除某个 Mod 时，若其「自动」依赖不再被任何**手动**添加的 Mod 依赖，则一并清理；
   手动添加的 Mod 始终保留。把自动依赖手动添加一次即可将其「提升」为手动（不再被自动清理）。


### 自动更新

可在 Web「设置 → 自动更新」配置，或在 `config.toml` 的 `[auto_update]` 段直接编辑。
**两种触发条件可分别开关，均只处理尚未下载的 Mod**（已下载的一律跳过，不覆盖、不强制更新、不重复下载）。

```toml
[auto_update]
enabled = false             # 总开关（关闭时两种触发都不生效）
delay_enabled = true        # 触发条件一：新增 Mod 后延时触发
delay_minutes = 5           # 延时时长（分钟）
scan_enabled = true         # 触发条件二：定时扫描
scan_interval = 5     # 扫描间隔（分钟）
```

**行为要点**

| 项目 | 说明 |
|------|------|
| 延时触发 | 新增 Mod 后延迟 N 分钟触发一次；倒计时期间再次新增 → **合并为同一次触发并重新计时** |
| 定时扫描 | 每 M 小时扫描一次，**仅在存在未下载 Mod 时**触发。**服务启动不再触发下载**：启动时会先扫描一次，但只登记待下载数量并排期（`_scan_once(trigger=False)`），真正的下载由下一个到点的扫描周期执行，避免每次重启都立刻开跑 |
| 更新范围 | 两种触发都只下载未下载的 Mod（判定：产物目录不存在或为空） |
| 并发控制 | 同一时间只允许一个更新任务；任务执行中的触发请求**直接跳过**（不排队堆积），并计入跳过次数 |
| 失败处理 | 失败只记录，不影响下一次触发；失败明细（名称 + 原因）保留在状态里 |
| 生效方式 | 保存后立即应用（重算扫描排期）；持久化到 `config.toml`，重启后仍生效 |
| 手动触发 | 仪表盘「⚡ 触发自动更新」按钮：立即执行一次同样的「仅未下载」流程（忽略总开关，仍受单任务约束） |
| 计时重置 | **任何更新任务完成时**（延时触发 / 定时扫描 / 手动触发 / 更新全部），都清空延时倒计时并从完成时刻重排定时扫描 |

**计时重置机制**：`sync_manager` 提供 `set_finish_hook()`，`server.py` 启动时把
`auto_updater.on_sync_finished` 注册为完成钩子。任务一结束（无论来源）即调用
`AutoUpdater.reset_timers()` —— 清空 `_pending_at`、把 `_next_scan_at` 重排为
`当前时间 + scan_interval_hours`。因此界面上的「下次自动更新」总以**最近一次更新结束时刻**
为基准重新起算，时间间隔配置本身不变。前端在 `pollTask()` 检测到任务结束时立刻 `loadAuto()`
刷新该时间（另有 5s 轮询 + 1s 本地倒计时重绘兜底）。

**接口**：`GET /api/auto_update`（配置 + 运行时状态）、`POST /api/auto_update`（保存并立即应用）、
`POST /api/auto_update/trigger`（手动触发一次；无未下载返回 200 + `task_id: null`，
任务运行中返回 409）；`/api/status` 也返回 `auto_update` 摘要（开关 + 待下载数）。

**界面**：设置页可配置总开关、两个触发开关与对应时间；仪表盘「自动更新」面板展示开启状态、
未下载数、**下次触发时间与剩余倒计时**、上次结果（成功/失败）、失败明细与**计时重置记录**；
仪表盘卡片也有状态徽标。仪表盘同步区两个按钮：「🚀 更新全部」（全量同步所有已配置 Mod）、
「⚡ 触发自动更新」（仅补下载未下载的 Mod）。

> 参数范围：延时 `0.1~10080` 分钟，扫描间隔 `1~10080` 分钟，超出返回 400。

### 下载路径说明

Mod 下载后位于：
`<存储目录>/<appid>/steamapps/workshop/content/<appid>/<itemid>/`



### 接口一览

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/login` | 登录（公开） |
| POST | `/api/logout` | 登出（公开） |
| GET  | `/api/status` | 系统状态（需登录） |
| GET  | `/api/config` | 完整配置（需登录） |
| POST | `/api/games` | 添加游戏（支持 AppID/商店页地址 + 安装方式，需登录） |
| PUT  | `/api/games/{appid}` | 修改游戏（需登录） |
| PUT  | `/api/games/{appid}/install` | 设置游戏安装方式（rename/extract/copy，需登录） |
| DEL  | `/api/games/{appid}` | 删除游戏（需登录） |
| POST | `/api/games/{appid}/mods` | 添加 Mod（支持 ID 或订阅链接，需登录） |
| DEL  | `/api/games/{appid}/mods/{itemid}` | 删除 Mod（需登录） |
| POST | `/api/games/{appid}/update` | 更新该游戏下所有 Mod（需登录） |
| POST | `/api/games/{appid}/mods/{itemid}/update` | 单独更新一个 Mod（需登录） |
| GET  | `/api/steam/game/{appid}` | 从 Steam 获取游戏名称（需登录） |
| GET  | `/api/steam/mod/{itemid}` | 从 Steam 获取 Mod 名称（需登录） |
| POST | `/api/settings` | 更新基础设置（需登录） |
| POST | `/api/auth` | 更新 Web 登录账号（需登录） |
| POST | `/api/steam` | 更新 Steam 账号（需登录） |
| POST | `/api/steam/login` | 测试 Steam 登录（需登录） |
| POST | `/api/sync` | 触发全量同步（需登录） |
| GET  | `/api/sync/{task_id}?since=<seq>` | 同步/更新进度与日志（增量游标，需登录） |
| GET  | `/api/sync/current` | 当前/最近一次任务 id（刷新后重建连接） |
| GET  | `/api/manifest` | 已下载清单（需登录） |
| GET  | `/list` `/files/{game}` `/download/{game}/{path}` | 文件分发（公开，兼容 demo） |
| GET  | `/mods/{appid}` | 分组清单：modid→{元数据+安装方式+文件列表}（公开，供客户端） |
| GET  | `/install/types` | 可用安装类型（公开） |



## 桌面客户端

独立的 tkinter 图形客户端（**纯 Python 标准库**，无需安装第三方依赖），
从服务端拉取指定游戏的 Mod 列表并下载到本地安装路径。

```bash
# 客户端需要带 tkinter 的 Python（本机系统 Python 即可）
C:\app\Python313\python.exe -m client
# 或在已装 tkinter 的任意 Python 下：python -m client
```

**要点**

- **硬编码 AppID**：启动前在 `client/config.py` 顶部改 `APPID = "..."`，客户端只拉取该游戏的 Mod。
- **本地安装路径**：在界面里填写/浏览选择，保存到 `client_config.json`；Mod 落盘为 `<本地路径>/<itemid>/...`，可直接指向游戏 workshop content 目录。
- **服务端地址（自动，无需手填）**：`client/config.py` 里**硬编码** `DEFAULT_SERVER_URL`（默认 `http://127.0.0.1:8080`），客户端**启动即自动连接**，没有「连接」按钮。地址优先级：
  1. **P0 配置文件中保存的地址**——只要它可达就使用它。
  2. **P1 广播发现地址**——仅当 P0 不可达时启用；接收服务端 UDP 广播（`type=gamesync_server`），接收到地址后确认可连接即保存到配置文件中。
  3. **硬编码地址**——仅当本地配置文件尚未配置地址时使用
- **下载**：选中一个或多个 Mod → **下载所选**（逐个下载，含其传递依赖），或 **全部下载**。已存在的文件按大小跳过。
- **删除**：勾选 Mod → **删除所选**，弹窗列出「将删除」与「将保留的共享依赖」；删除遵循**仅删孤儿依赖**——仍被其它已下载 Mod 依赖的共享依赖会保留。
- **只读服务端**：客户端对服务端仅做 `GET`（`/mods` `/files` `/download`），所有写操作只发生在本地路径，**任何客户端操作都不影响服务端文件**（已由端到端测试校验）。
- **右键菜单**: 单个mod支持右键菜单操作，针对上行带宽较小的情况，增加了gzip压缩传输的方式，即选择**压缩下载**。默认使用不压缩的方式。
**界面操作**：启动自动连接 →（必要时「⟳ 重连」/「🔄 刷新」）→（全选/清空/反选 勾选）→ 下载所选/全部下载 → 删除所选；底部进度条与日志实时显示。

**服务端需提供**：公开接口 `GET /mods/{appid}`（分组清单 + 依赖 + 安装方式）、`GET /install/types`、`GET /download/{appid}/{path}`，均无需登录。

### Mod 安装方式（下载后安装流程）

服务端**区分**安装方式并决定如何下行内容；客户端**执行**安装。两侧各自模块化，便于扩展自定义安装方式。

**文件列表结构**：`modid -> 该 Mod 的文件列表`（分组清单 `GET /mods/{appid}`）。单文件 Mod 命名为 `modid + 扩展名`。

| 安装方式 | 服务端下行 | 客户端安装 | 示例 |
|----------|-----------|-----------|------|
| `copy`（默认） | 原样文件 `modid/...` | 移到 `<安装根>/<modid>/` | 通用 |
| `rename` | **下载时直接下行改名后的文件** `modid.vpk` | 放到 `<安装根>/modid.vpk` | 求生之路2（appid 550）→ `.vpk` |
| `extract` | 打包为 `modid.zip`（带缓存） | **解压**到 `<安装根>/<modid>/` | 饥荒联机版（appid 322330） |
| `custom` | 两侧各自 `register()` 扩展 | 对应处理器 | 自定义 |

**服务端**（`app/install.py`）：`build_files` 决定呈现哪些文件、`resolve` 决定下行什么内容（rename 给主文件改名下行、extract 给缓存 zip）。配置存 `games/<appid>.toml` 的 `[install]`（游戏级默认，Mod 可 `install_type` 覆盖）；Web「游戏卡片 → 安装方式」或 `PUT /api/games/{appid}/install` 设置。

**客户端**（`client/install/`）：`store.download_and_install` 先把文件下到 `.staging/`，再按 `install.type` 分发到处理器落地，最后记录到 `<安装根>/.installed.json`（类型 + 产物路径），用于状态检测与删除。新增安装方式：服务端 `app/install.py` 注册呈现/下行，客户端 `client/install/` 注册安装处理器。

### 下载压缩传输（gzip）

下载时**流式 gzip 压缩**下行，降低传输字节数、省流量并提速。

**采用方式：HTTP 标准 `Accept-Encoding` 协商（gzip / DEFLATE）**

- **服务端**（`app/server.py` 的 `/download`）：请求头含 `Accept-Encoding: gzip` **且**该文件值得压缩时，用 `zlib.compressobj(9, DEFLATED, 16+MAX_WBITS)` **边读边压**，经 `StreamingResponse` 下行，并回 `Content-Encoding: gzip` + `X-Original-Size`（压缩前大小，供客户端算进度）。否则返回 `FileResponse` 原始文件。
- **客户端**（`client/api.py` 的 `download_file`）：请求带 `Accept-Encoding: gzip`；若响应 `Content-Encoding: gzip` 则用 `zlib.decompressobj(16+MAX_WBITS)` **流式解压**后写盘；进度总量取 `X-Original-Size`（原始大小），保证进度条按解压后的真实大小推进。

**兼容性**：不带 `Accept-Encoding` 的客户端 → 服务端返回原始文件，行为与改造前完全一致；浏览器自带 `Accept-Encoding` 并自动解压，也正常。压缩是**可选增强，非破坏性改动**。

### 代理配置

为 **Steam 网页访问（商店 / 社区 / API）** 指定出口代理，兼容 http / https(socks5目前测试经常遇到问题，不建议使用)。配置可通过 `config.toml` 的 `[proxy]` 段或环境变量指定地址与端口，**发起请求时自动生效**。

**生效范围**

- ✅ **生效**：Python 侧发起的 Steam 网页与 API 请求（获取游戏名、Mod 名称、工坊依赖解析）。
- ❌ **不生效**：**steamcmd 子进程（Mod 下载）不使用本代理**(事实上也无法生效)。


