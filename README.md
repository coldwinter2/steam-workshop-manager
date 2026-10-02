# Steam 游戏 Mod 下载同步工具（服务端）

使用 Python + FastAPI 编写的 Steam 创意工坊 Mod 管理同步工具。
通过 Web 管理端配置游戏与 Mod，利用 `steamcmd` 手动触发下载，
并对外提供文件分发接口（兼容 demo 客户端）。

## 功能

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

## 目录结构

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
│   ├── proxy.py             # socks5 代理配置（仅 Steam 网页访问）
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
├── demo/                    # 参考示例（请勿改动）
├── config.toml              # 服务端运行时配置
├── client_config.json       # 客户端运行期配置（生成）
├── requirements.txt         # 服务端依赖
└── README.md
```

## 安装与运行

项目自带本地虚拟环境 `venv/`（Windows 下解释器为 `venv\Scripts\python.exe`）。

```bash
# 1. 安装依赖（使用项目本地 venv）
venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. 准备 steamcmd
#    Windows: 下载 https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip 并解压
#    Linux:   curl -sqL https://steamcdn-a.akamaihd.net/client/steamcmd.zip | bsdtar -xvf-  && chmod +x steamcmd.sh
#    首次运行 steamcmd 会自行更新。

# 3. 配置 steamcmd 路径（在 Web「设置」中填写，或编辑 config.toml）
#    Windows 例: C:/steamcmd/steamcmd.exe
#    Linux   例: /opt/steamcmd/steamcmd.sh

# 4. 启动服务
venv\Scripts\python.exe -m app.server
# 浏览器打开 http://<host>:8080
# 首次访问使用默认账号 admin / admin123 登录（请在设置中修改密码）
```

> 若使用系统 Python，也可：`python -m venv venv && venv/bin/pip install -r requirements.txt`
> （Linux/macOS 下解释器为 `venv/bin/python`）。依赖含 `fastapi / uvicorn / tomlkit / PySocks / cryptography`。

## 使用流程

1. 打开 Web 管理端，使用 `admin` / `admin123` 登录。
2. **设置** → 填写 `steamcmd 路径` 与 `存储目录`，保存。
3. （可选）**设置** → 配置并启用 **Steam 账号**：填用户名/密码，点「测试登录」；
   若提示需要设备授权，把收到的 Guard 码填入后保存再测试，直到登录成功。
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

> **批量下载（一次登录）**：同步/更新按 **游戏分组** 执行——每组只起**一次 steamcmd**、
> 只 **`login` 一次**，随后在同一会话里顺序执行多个 `workshop_download_item`，最后 `quit`。
> 即 N 个 Mod 只需 1 次登录（跨 G 个游戏则 G 次），显著更快且降低 Steam 限流风险。
> 批内每个 Mod 的成败在批结束后**按产物目录逐个判定**；失败原因优先归因到含该 itemid 的错误行。
>
> **批量以 `+runscript` 脚本方式执行**（不再是拼接命令行参数）：不受命令行长度限制、
> 无需转义，脚本即完整可复查的执行计划。生成的脚本形如
> `force_install_dir <dir>` → `login ...` → 每个 Mod 一行 `workshop_download_item` → `quit`。
> 注意 **`force_install_dir` 必须排在 `login` 之前**，否则 steamcmd 会打印
> `Please use force_install_dir before logon!` 并可能忽略该设置（`download_item` 已同步修正）。
>
> **超时与容错（重要）**
> - 旧实现的 `timeout` **形同虚设**：日志读取 `for line in proc.stdout` 会阻塞到 EOF，
>   `timeout` 只作用于其后的 `proc.wait()`，因此批量下载慢/卡死时**既不超时也不结束**。
>   现改为**双超时**：总墙钟 `DEFAULT_TIMEOUT + PER_ITEM_TIMEOUT × N`，外加
>   `DEFAULT_IDLE_TIMEOUT`（默认 600s 无任何新输出即判定卡死），超时杀**进程树**并报出最近日志。
> - **分块**：每批最多 `BATCH_CHUNK`（默认 5）个 Mod，避免单批过长、失败影响面过大。
> - **失败降级**：整批失败（登录/网络/超时）时自动**降级为逐个下载重试**，单个卡住不再拖垮整批。

> **`+runscript` 脚本执行**：除命令行参数拼接外，`SteamCMD.run_script(script_content)` 支持
> 直接传入**脚本文本**——内部先落盘为临时脚本文件（UTF-8 无 BOM，默认系统临时目录，
> 可用 `script_dir` 指定），再以 `+runscript <file>` 执行，结束后默认删除该文件
> （`keep_script=True` 可保留便于排查）。脚本每行一条命令且**不带 `+` 前缀**（写了会自动剥离），
> `//` 为注释；末尾无 `quit` 时自动追加（`quit_after=False` 可关闭），确保 steamcmd 执行完退出。
> 返回 `{"returncode", "errors", "logs", "script_path"}`，超时策略与 `download_item` 一致。
> 适用于长命令、多步流程或含特殊字符的场景。
> - **日志节流**：连续相似的进度行折叠为一行并计数，避免大批量下载时日志撑爆内存/拖慢前端轮询。
> - 并发数：始终为 **1**（单进程顺序下载），不并发以避免 Steam 限流；瓶颈通常是单批总量
>   （本项目 8 个 Mod 约 1.5GB），这也是批量比单个更容易「卡住/超时」的直接原因。

## 日志中的 ANSI 颜色序列（去色）

steamcmd 在部分平台（如 Ubuntu）会向 stdout 写入 ANSI 转义序列来呈现颜色/加粗，例如
`ESC[0m`（重置）、`ESC[1m`（加粗）。这些序列在**终端**里才是颜色，**一旦被管道捕获、写入
日志文件或 systemd journal** 就会变成可见的 `[0m`、`[1m` 乱码，影响阅读与后续解析。

**已做的处理**：

- 统一在 `app/steamcmd_runner.py` 的捕获处（`_run`）对每一行调用 `app/ansi.strip_ansi()`
  后再存入日志列表与回调，保证写入任务日志、Web 日志面板、导出文件的内容**始终是纯文本**，
  且原文本、级别、时间戳字段完整保留（仅剔除转义序列本身）。纯转义行去净后为空则跳过。
- 服务端自身启动横幅（`Steam Mod 同步工具已启动…` 等）通过 `app/ansi.colored()` 上色，
  是否上色由 `[logging] color` 策略决定（见下），管道/journal 下自动不上色。

**颜色策略配置（`config.toml` 的 `[logging]` 段，或通过 Web「设置」保存 `log_color`）**：

```toml
[logging]
color = "auto"   # auto（默认）| always | never
```

| 值 | 行为 |
| --- | --- |
| `auto` | 仅当标准错误是**终端（TTY）**时上色；管道、`systemd journal`、文件均**不上色**（最安全） |
| `always` | 始终上色（仅在确实连到彩色终端时使用） |
| `never` | 永远不上色（日志文件 / journal / 管道场景用这个彻底关闭） |

> 说明：任务日志（steamcmd 输出）的去色与 `color` 配置**无关**——无论 `color` 取何值，
> 捕获到的内容都会被清洗为纯文本。`color` 只影响服务端启动横幅等自身输出。

**验证日志里不再含转义序列**：

```bash
# 1) 直接对纯文本断言（无 ESC / 0x9b 字节、无 [0m [1m 类残留）
python tests/test_ansi.py        # 覆盖 strip_ansi / use_color / _run 集成

# 2) 跑一次真实同步，抓取日志后用 grep 确认（Ubuntu 上）
journalctl -u game_sync --no-pager | grep -aP '\x1b\['   # 应为空
# 或直接 grep 可见残留
journalctl -u game_sync --no-pager | grep -aE '\[0m|\[1m|\[32m'   # 应为空

# 3) 远程到日志文件 / 导出文件
grep -aP '\x1b' /var/log/gamesync/sync.log   # 应为空

# 4) 单元级快速自检
python - <<'PY'
from app import ansi
sample = "\x1b[1mDownloading\x1b[0m item \x1b[32;1mOK\x1b[0m"
assert "\x1b" not in ansi.strip_ansi(sample)
assert ansi.strip_ansi(sample) == "Downloading item OK"
print("ANSI 清洗 OK")
PY
```

> 注意：某些日志查看器会把不可见的 `ESC` 字节渲染成空，从而只显示出 `[0m`/`[1m` 这类
> “半截”序列——这正是本项目要消除的现象；清洗后整条 `ESC[...]` 都会被移除，不再有残留。

## 自动更新

可在 Web「设置 → 自动更新」配置，或在 `config.toml` 的 `[auto_update]` 段直接编辑。
**两种触发条件可分别开关，均只处理尚未下载的 Mod**（已下载的一律跳过，不覆盖、不强制更新、不重复下载）。

```toml
[auto_update]
enabled = false             # 总开关（关闭时两种触发都不生效）
delay_enabled = true        # 触发条件一：新增 Mod 后延时触发
delay_minutes = 5           # 延时时长（分钟）
scan_enabled = true         # 触发条件二：定时扫描
scan_interval_hours = 1     # 扫描间隔（小时）
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

> 参数范围：延时 `0.1~10080` 分钟，扫描间隔 `0.01~720` 小时，超出返回 400。

## 下载路径说明

Mod 下载后位于：
`<存储目录>/<appid>/steamapps/workshop/content/<appid>/<itemid>/`

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
  1. **P0 硬编码默认地址**——只要它可达就使用它（换服务端端口改这一行即可）。
  2. **P1 广播发现地址**——仅当 P0 不可达时启用；运行期持续接收服务端 UDP 广播（`type=gamesync_server`），广播地址变化即自动更新。
  3. 后台每 `RECHECK_INTERVAL`（默认 10s）复核一次：默认地址恢复即**自动切回 P0**；当前地址失效则改用最新广播；全部不可达时标记断开并持续重试。下载/删除进行中不切换服务端。界面只读展示当前地址与来源（默认地址 / 广播发现）。
- **下载**：勾选一个或多个 Mod → **下载所选**（逐个下载，含其传递依赖），或 **全部下载**。已存在的文件按大小跳过。
- **删除**：勾选 Mod → **删除所选**，弹窗列出「将删除」与「将保留的共享依赖」；删除遵循**仅删孤儿依赖**——仍被其它已下载 Mod 依赖的共享依赖会保留。
- **只读服务端**：客户端对服务端仅做 `GET`（`/mods` `/files` `/download`），所有写操作只发生在本地路径，**任何客户端操作都不影响服务端文件**（已由端到端测试校验）。

**界面操作**：启动自动连接 →（必要时「⟳ 重连」/「🔄 刷新」）→（全选/清空/反选 勾选）→ 下载所选/全部下载 → 删除所选；底部进度条与日志实时显示。

**服务端需提供**：公开接口 `GET /mods/{appid}`（分组清单 + 依赖 + 安装方式）、`GET /install/types`、`GET /download/{appid}/{path}`，均无需登录。

## Mod 安装方式（下载后安装流程）

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

## 下载压缩传输（gzip）

参考 `demo/` 下载模块的思路：下载时**流式 gzip 压缩**下行，降低传输字节数、省流量并提速。

**采用方式：HTTP 标准 `Accept-Encoding` 协商（gzip / DEFLATE）**

- **服务端**（`app/server.py` 的 `/download`）：请求头含 `Accept-Encoding: gzip` **且**该文件值得压缩时，用 `zlib.compressobj(9, DEFLATED, 16+MAX_WBITS)` **边读边压**，经 `StreamingResponse` 下行，并回 `Content-Encoding: gzip` + `X-Original-Size`（压缩前大小，供客户端算进度）。否则返回 `FileResponse` 原始文件。
- **客户端**（`client/api.py` 的 `download_file`）：请求带 `Accept-Encoding: gzip`；若响应 `Content-Encoding: gzip` 则用 `zlib.decompressobj(16+MAX_WBITS)` **流式解压**后写盘；进度总量取 `X-Original-Size`（原始大小），保证进度条按解压后的真实大小推进。

**兼容性**：不带 `Accept-Encoding` 的旧客户端 / demo 客户端 → 服务端返回原始文件，行为与改造前完全一致；浏览器自带 `Accept-Encoding` 并自动解压，也正常。压缩是**可选增强，非破坏性改动**。

**适用场景（何时省流量 / 何时跳过）**：
- ✅ **受益**：可压缩内容——`copy` 类的文本/脚本/未压缩数据、日志、JSON 等（实测 150 KB 文本压到 519 B，约 0.3%）。
- ⛔ **跳过**：已是高压缩比格式（`.zip .gz .7z .rar .png .jpg .mp4 .mp3` 等）再压几乎无收益、只增 CPU，服务端对这些扩展名直接返回原始文件。`extract` 类下发的 `.zip` 即属此类，不会二次压缩。
- 中间：`.vpk` 等按内容而定（内容可压则受益，否则收益有限但无害）。

> 说明：gzip 是**通用、无损、逐流**压缩，适合"边下边解"的实时下载；对已压缩媒体收益低，故用扩展名白名单跳过。若后续需要更高压缩比，可换 zstd，但 gzip 兼容性最好（浏览器/客户端普遍支持）。

## 代理配置

为 **Steam 网页访问（商店 / 社区 / API）** 指定出口代理，**必须支持 socks5**（亦兼容 http / https）。配置可通过 `config.toml` 的 `[proxy]` 段或环境变量指定地址与端口，**发起请求时自动生效**。

**生效范围**

- ✅ **生效**：Python 侧发起的 Steam 网页与 API 请求（获取游戏名、Mod 名称、工坊依赖解析）。
- ❌ **不生效**：**steamcmd 子进程（Mod 下载）不使用本代理**，不注入任何代理环境变量，保持 steamcmd 自身网络行为——避免代理链路不稳定导致下载卡死/超时（批量更新曾因此失败）。

**生效方式**

- **Steam 网页/API（Python urllib）**：通过 PySocks 的 `SocksiPyHandler` 构造 opener，使所有请求经 SOCKS5 代理并**远程解析 DNS**（避免本地域名污染）。构造时会一并传入 `ProxyHandler({})` **屏蔽系统环境变量代理**——否则默认 `ProxyHandler`（order=100）会抢在 `SocksiPyHandler`（order=500）之前生效，导致 SOCKS5 形同虚设（典型症状：`SSL: UNEXPECTED_EOF_WHILE_READING`）。
- **稳定性**：请求带 `Accept-Encoding: gzip`（响应体从几十 KB 压到几 KB，降低不稳定链路被截断的概率），网络类失败自动重试 2 次（退避 1s/2s），错误信息会注明实际出口（如 `socks5://192.168.3.10:1080`）。

**方式一：配置文件（`config.toml`）**

```toml
[proxy]
enabled = true          # 是否启用
type = "socks5"         # socks5 / http / https
host = "127.0.0.1"      # 代理地址
port = 1080             # 代理端口
username = ""           # 代理账号（可选）
password = ""           # 代理密码（可选）
```

也可在 Web「设置 → 代理设置」面板中修改，**保存后立即生效，无需重启**。

**方式二：环境变量（优先级更高，便于部署时覆盖）**

| 环境变量 | 说明 | 示例 |
|----------|------|------|
| `STEAM_PROXY_ENABLED` | 启用代理 | `1` / `true` |
| `STEAM_PROXY` | 地址端口（可带协议） | `127.0.0.1:1080` 或 `socks5://127.0.0.1:1080` |
| `STEAM_PROXY_HOST` | 仅代理地址 | `127.0.0.1` |
| `STEAM_PROXY_PORT` | 仅代理端口 | `1080` |
| `STEAM_PROXY_TYPE` | 代理类型 | `socks5` |

> 环境变量优先级高于 `config.toml`；两者都未启用代理时保持原有直连行为（不受影响）。
>
> 代理仅影响 Python 侧的 Steam 网页请求；**steamcmd 下载始终直连**（如需让 steamcmd 走代理，请在其所在机器/系统层面自行配置）。

**排障：代理自检**

```bash
# 直连基线（未启用代理时）
venv\Scripts\python.exe -m app.proxy

# 指定代理自检（覆盖 config.toml）
set STEAM_PROXY_ENABLED=1 && set STEAM_PROXY_HOST=192.168.3.10 && set STEAM_PROXY_PORT=1080 && set STEAM_PROXY_TYPE=socks5
venv\Scripts\python.exe -m app.proxy
```

输出会给出「生效代理」与三个 Steam 域名的握手结果，可快速区分三层问题：

| 现象 | 定位 |
|------|------|
| TCP/握手失败（`ConnectionRefused`/`timeout`） | 代理地址或端口不对、代理未启动 |
| 握手成功但读数据 `IncompleteRead`/`RemoteDisconnected` | 代理**出口链路**问题（与本地代码无关），用 `curl --socks5-hostname` 对照复现；需换代理节点/协议 |
| 请求提示走的是 `http://127.0.0.1:xxxx` 而不是配置的 SOCKS5 | 系统环境变量代理抢占（已在本版修复） |

## 接口一览

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
