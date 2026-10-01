# dsh-ctl

[English](README.md) | 简体中文

[DeepSeek Harness](https://www.npmjs.com/package/@deepseek-ai/dsh)（`dsh`）的 Windows **系统托盘控制器**。
在托盘图标上一键启动 / 停止 / 重启 web 服务——同时覆盖 Windows 本体与 WSL 内实例——外加 NapCat QQ OneBot 桥控制、
实时日志查看器，以及带图标校验门禁的 PyInstaller 构建流水线。

最终产物是单文件 `dsh-ctl.exe`（约 31 MB，无控制台窗口），常驻系统托盘。

---

## 功能

| 目标 | 动作 | 控制方式 |
|---|---|---|
| **Windows dsh** | 启动 / 停止 / 重启 / 状态 | 在*隐藏*控制台内启动 `node ... dsh bin.js web --no-open`；通过监听 `:3080` 的 PID 定位服务 |
| **WSL dsh** | 启动 / 停止 / 重启 / 状态 | bash 脚本经 stdin 喂给 `wsl -d <distro> -- bash -s`；用 `setsid nohup` 脱离会话；用保活客户端防止 WSL2 VM 闲置关机 |
| **NapCat（QQ 机器人桥）** | 启动 / 停止 / 重启 / 状态 | 用 NapCat 注入器拉起 QQNT，支持按 QQ 号快登；幂等设计，杜绝双开 |
| **日志** | 查看日志 | 多标签 tkinter 查看器，跟踪 `dsh-web.log`、`dsh-web.log.err`、`dsh-ctl.log`（+ 经 UNC 读取的 WSL 日志），2 秒自动刷新 |

其他特性：

- **单实例锁** —— 第二个副本尝试绑定 `127.0.0.1:47632` 失败后静默退出。
- **动作串行化** —— 快速连点菜单不会拉起两个 dsh 实例争抢 3080 端口。
- **UI 打开权收归一家（含 0.1.5 入口鉴权）** —— dsh 以 `--no-open` 运行；只有托盘控制器负责打开窗口，永远不会多出一个重复的浏览器标签页。**重启**会先优雅关闭当前 PWA 窗口（只发 `WM_CLOSE`，绝不杀进程，日常浏览器分毫不动），服务就绪后再拉起一个全新窗口。
  DSH ≥ 0.1.5 起 `dsh web` 只在 stdout 打印一次带入口 token 的 URL（`http://127.0.0.1:3080/?token=...`），裸 `/` 一律 401。本程序把 dsh 的 stdout 重定向到 `dsh-web.log`，因此能把这个 token 抓下来：启动就绪后用它打开一个 **Chrome 应用窗口**（`--app=<tokenURL>`，且与 PWA 快捷方式共享同一个 profile），token 换来的签名 cookie 因此也落进 PWA，此后 PWA 自己打开同样免鉴权（cookie 有效期内）。托盘新增 **Open Web UI**，会用**实测可用**的 token 重开窗口（token 只属于打印它的那个进程，故逐个探测后再用）。
- **启动前清理僵尸写锁** —— DSH ≥ 0.1.5 在 profile boot 期间持有带 deadline 的文件写锁；`taskkill /F` 无法触发其清理，残留锁会让下一次启动直接失败（`atomic-write: timed out waiting for the writer lock`）。启动/停止时会自动清掉「内容是一个已消失 PID」的锁；`task-board/ledger-v2.lock` 是 JSON，绝不触碰。
- **子进程环境净化** —— 传给 dsh 的 PATH 取自注册表（避免继承开发/agent shell 的 PATH，否则灵枢 bridge 的 `python -m aeis.mcp.server` 会解析到没有 `aeis` 的解释器、每次启动都握手失败）；并清除继承来的 `HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/NO_PROXY`（DSH ≥ 0.1.5 开始遵循它们）与 `NODE_OPTIONS`。
- **路径推导不再静默降级** —— 环境变量缺失时回退到真实绝对目录（此前 `APPDATA` 为空会把日志目录变成相对路径 `DeepSeekHarness`）。
- **NAT 模式双实例共存** —— Windows dsh 与 WSL dsh 可同时运行，各占独立回环。
- **审计追踪** —— 每个动作都追加写入 `dsh-ctl.log`。

## 托盘菜单

```
State: Running (PID 12345)        <- 动态
WSL State: Not running            <- 动态
NapCat: Running (PID 6789)        <- 动态
──────────────────────────────
Start dsh / Stop dsh / Restart dsh / Status
WSL dsh  >  Start / Stop / Restart / Status
NapCat   >  Start / Stop / Restart / Status
──────────────────────────────
View Logs
──────────────────────────────
Exit
```

## 安全守卫

所有停止操作都拒绝击杀「长得不像」的目标进程：

- Windows dsh 停止只杀镜像名为 `node.exe` / `cmd.exe` 的 PID；
- WSL dsh 停止只杀 `/proc/<pid>/cmdline` 含 `node` 的 PID；
- NapCat 停止只杀镜像名为 `qq.exe` 的 PID。

如果有无关进程占用了受管端口，本工具会选择上报并拒绝，而不是盲目 `taskkill`。

## 环境要求

- Windows 10 / 11
- Python 3.10+（实测 3.13），依赖 `pystray>=0.19`、`pillow`
- 通过 npm 安装的 DeepSeek Harness（`%APPDATA%\npm\node_modules\@deepseek-ai\dsh`）
- 可选：装有 `dsh` CLI 的 WSL2 发行版（位于 `~/.local/node-v*/bin/`）
- 可选：NapCat shell + QQNT（QQ 桥）

## 构建

```bat
build.bat
```

执行流程：

1. 缺失时安装锁定版本的构建依赖；
2. 修复残留的 `TCL_LIBRARY` / `TK_LIBRARY`（从 PyInstaller onefile 应用内部构建时会踩坑）；
3. 记录受保护图标资产的 SHA-256；
4. 经 `dsh-ctl.spec` 构建 `dist\dsh-ctl.exe`（onefile、无窗口）;
5. 运行**图标门禁**（`verify_icon.py`）：抽取 exe 图标与权威基准 `icon-check.png`
   逐像素比对——0 字节差异才放行，否则不替换桌面副本；
6. 把 exe 复制到真实桌面（正确处理 OneDrive 重定向）。

## 配置（可选环境变量）

| 变量 | 用途 | 默认值 |
|---|---|---|
| `DSH_NAPCAT_DIR` | NapCat shell 安装目录 | `%USERPROFILE%\.dsh\napcat\napcat-shell` |
| `DSH_NAPCAT_QQ` | 快登用的机器人 QQ 号 | 从 `config/napcat_<qq>.json` 自动探测 |

## 占用的端口

| 端口 | 含义 |
|---|---|
| 3080 | dsh web（Windows 与 WSL 各自独立监听） |
| 3001 | NapCat OneBot WebSocket |
| 6099 | NapCat WebUI |
| 3081 | 指向 WSL dsh 的固定 portproxy 条目（首选 UI 地址） |
| 47632 | dsh-ctl 单实例锁（仅本地） |

## 日志与数据

- `%APPDATA%\DeepSeekHarness\dsh-web.log(.err)` —— Windows dsh 输出（保留 4 轮轮转）
- `%APPDATA%\DeepSeekHarness\dsh-ctl.log` —— 审计记录（`时间戳 \| 动作 \| OK/FAIL \| 详情`）
- WSL 日志经 `\\wsl$\<distro>\home\<user>\.dsh\` 实时读取

## 仓库结构

```
main.py           入口：线程模型、队列事件泵、单实例锁
dsh_control.py    核心逻辑：进程/端口管理、NapCat、WSL 支持
tray.py           pystray 图标 + 菜单（回调只负责入队动作）
logviewer.py      多标签日志窗口
verify_icon.py    构建门禁：exe 图标逐像素比对
build.bat         一条命令完成构建 + 部署到桌面
*.spec            PyInstaller 配置（正式版 / 测试版）
test_wsl_*.py     WSL 流程与双实例并行集成测试
backup/           dsh-ctl 出现之前的批处理脚本存档
```

## 线程模型（硬规则）

> tkinter 只住主线程；pystray 回调只往队列塞动作；
> 耗时动作跑在守护线程上并由锁串行化；气泡通知与菜单刷新
> 一律经 `root.after` 回到主线程执行。
