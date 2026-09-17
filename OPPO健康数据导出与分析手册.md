# OPPO 健康数据导出与分析手册

> **项目名**：oppo-health-data-toolkit ｜ **版本**：V1.1。
> **适用**：雷电 14（Android 14）——当前版本仅在此平台完成全链路实测；**MuMu 12+（Android 15）支持暂缓**，手册保留其差异说明（su 垫片、端口 16384、6.5 差异根源等）供后续版本使用，最新代码未在 MuMu 上实测；脚本自定位，整体目录搬家/改名零修改。

## 📖 目录

- ⚡ 快速开始（A 一键执行 · B 30 秒速览与预期值 · C 下一步去哪）
- ⚠️ 必坑清单（A 模拟器卡死 · B Root 权限 · C 数据错误 · D 白跑一场 · E 隐私红线）
- 第一部分 环境准备与依赖（1.1 要求 · 1.2 路径 · 1.3 模拟器 · 1.4 安装登录 · 1.5 Frida · 1.6 依赖 · 1.7 模块 · 1.8 su 垫片 · 1.9 多模拟器端口）
- 第二部分 数据导出与生成流程（2.1 检查清单 · 2.2 连接 · 2.3 一键导出 · 2.4 仅导出 · 2.5 目录结构 · 2.6 验证 · 2.7 从零 11 步速通）
- 第三部分 数据分析步骤与输出格式（3.1 12 维报告 · 3.2 评分权重 · 3.3 字段单位 · 3.4 常用表速查 · 3.5 上传后端 · 3.6 易错点）
- 第四部分 常见错误与排查（问题 1-15）
- 第五部分 使用方法、参数配置与系统维护（5.1 脚本用法 · 5.2 参数配置 · 5.3 系统维护 · 5.4 网页报告 · 5.5 按天拆分）
- 第六部分 技术原理详解（6.1 加密机制 · 6.2 密钥体系 · 6.3 Frida 取密钥 · 6.4 电脑端解密 · 6.5 双平台差异根源）
- 第七部分 双平台可移植性实测报告
- 第八部分 占位符与待填项一览
- 第九部分 把导出数据交给其他 AI 分析（不依赖 WorkBuddy 的路径）
- 第十部分 模拟器闪退专项排查与解决方案（10.1 典型现象 · 10.2 原因按可能性排序 · 10.3 逐步排查与处置 · 10.4 四级验证 · 10.5 防复发 10 条）
- 附录A 摘要模板 · 附录B 换环境速查 · 附录C 自动化自检 · 附录D 已知边界 · 附录E 环境维护守则 · 附录F 版本说明

---

## ⚡ 快速开始（先读这一节）

> 环境未就绪（未装模拟器 / Frida 服务端 / Python 依赖）→ 请直接跳到 **第一部分：环境准备与依赖**。本节只给"已具备环境"的最短路径与预期值基线。

### A. 一键执行（最快路径，3-5 分钟）

```cmd
:: 第1步：启动模拟器（雷电；MuMu 用：MuMuManager control -v 0 launch）
"<模拟器目录>\ldconsole.exe" launch --index 0
:: 也可改为启动主窗口进程（效果等同，二选一）：start "" "<模拟器目录>\dnplayer.exe"
:: 第2步：连接 ADB（MuMu 端口为 127.0.0.1:16384）
<工作目录>\platform\adb.exe connect 127.0.0.1:5555
:: 第3步：一键导出 + 分析
cd /d <工作目录>
python auto_export_and_analyze.py
```

- **预计耗时**：3-5 分钟；
- **输出位置**：`<工作目录>\DB\combined\`（合并全量库，覆盖式；按天日库由 `split_db_by_day.py` 产出 `DB\YYYY-MM-DD\`）；
- **预期**：返回码 0，产出解密库、CSV / JSON、`csv_by_date`、类型化 SQLite 与 12 维分析报告；
- **⚠️ 前置检查**：模拟器刚重启后确认 OPPO 健康能正常启动（见问题 8）；Zygisk 开启会导致 App 启动失败、导出旧数据。

### B. 30 秒速览（命令卡片 + 关键预期值）

**B1. 完整导出 + 分析**：见上方 A。

**B2. 纯代码自动化测试（不需要模拟器，约 1 分钟）：**

```cmd
cd /d <工作目录>
python config.py
python test_auto_export.py
python data_validation.py
```

- `python config.py`：配置自检（打印配置有效性与 ADB / 模拟器 / Frida CLI 探测结果）。
- `python test_auto_export.py`：预期 74 个测试 0 失败 0 错误（可选：改代码后自检）。
- `python data_validation.py`：预期 0 ERROR（自动优先校验 `DB\combined`；**需已至少成功导出一次**，否则无库可校验）。

**B3. 上传链路（两个终端；本机测试必须用环境变量覆盖 URL）：**

```cmd
:: 终端A（服务端，首次启动自动在 sink\ 重建空库）：
cd /d <工作目录>\server && python upload_server.py

:: 终端B（客户端）：
set OPPO_HEALTH_URL=http://127.0.0.1:5000/api/upload
python upload_data.py
```
预期：约 46~47 张表上传（随 App 版本与设备传感器浮动，6.7.19 版为 47）、HTTP 200（响应体 `{"sections":N,"status":"ok"}`）。

**关键预期值速查**

| 项 | 预期值 |
|----|--------|
| 单元测试 | 74 个测试，0 失败 0 错误（可选） |
| 解密库表数量 | **约 100~101 张**（用户 100 张 + 内部 1 张 `sqlite_sequence`；该内部表仅在库中存在 AUTOINCREMENT 表时由 SQLite 自动生成，故总数在 100~101 间浮动，实测 101 张）。同一 App 版本在任何设备上表结构相同，可自行用 `PRAGMA table_info` 核对；其中相当一部分为空表属正常，非空表数随 App 版本与设备传感器浮动 |
| CSV 文件数 | **约 46~47 个**（仅非空表，随 App 版本与设备传感器浮动；6.7.19 版为 47 个）；另有 `csv_by_date\` 按数据日期分类 |
| Python 解释器 | ≥3.10 均可（核心依赖 sqlcipher3 / frida；上传链路 flask / requests / mcp；GUI 导出 Excel 另需 openpyxl——详见 1.6） |

### C. 下一步去哪

| 你的情况 | 去看 |
|---|---|
| ⚠️ **动手前先扫一眼坑（强烈建议）** | **⚠️ 必坑清单**（模拟器卡死 / Root 权限 / 数据错误 / 白跑 / 隐私，共 5 组 27 条） |
| 还没装环境（模拟器/Frida/依赖） | **第一部分**（1.1 要求 · 1.3 模拟器 · 1.4 安装登录 · 1.5 Frida · 1.6 依赖） |
| 环境就绪，想跑全流程 | 上方 A，或逐步操作看 **第二部分 2.7（11 步速通）** |
| 跑出数据，想看怎么分析 | **第三部分**（12 维口径与字段单位） |
| 报错了 | **第四部分**（问题 1-15） |
| 想交给其他 AI 分析 | **第九部分** |

---

## ⚠️ 必坑清单（动手前扫一眼：这些操作会让模拟器卡死、让你白跑或泄隐私）

> 以下每一条都是**已被实测证实的高危操作**（模拟器整机冻结、导出链死锁、数据偏旧等后果均已复现）。动手前扫一眼，能省 1-2 小时。

### A. 会让模拟器卡死 / 整机冻结（最高危）

| # | 坑 | 后果 | 正确做法 | 详见 |
|---|---|---|---|---|
| A1 | `adb shell setenforce 1` | **雷电整机冻结**，只能重启模拟器 | 不要执行；确需关闭 SELinux 用 `setenforce 0` | 问题12 |
| A2 | `adb reboot` | 模拟器/真机重启**挂起**（可能永久不返回） | 用模拟器自带重启：雷电 `ldconsole quit` 后重新 `launch` | 问题13 |
| A3 | `am start -W`（等待启动完成） | **永久阻塞**（App 起不来时尤其） | 用普通 `am start`（脚本已内置） | 问题13 |
| A4 | 在 `adb shell` 里**前台**跑常驻进程（如 frida-server） | adb shell 永不返回 → **整条导出链死锁**（实测卡 6.5 分钟零产出） | 必须三件套：`>/dev/null 2>&1 </dev/null &`（stdout + stderr + **stdin** 缺一不可） | 1.5 |
| A5 | 强杀模拟器进程 / 断电式关闭（任务管理器结束进程、直接关机） | 数据库 `-wal` 未 checkpoint → **数据偏旧或库损坏** | 用 `ldconsole quit` 正常关闭；导出前由脚本 `am force-stop` App 再 pull | 2.5 |

### B. Root 权限相关（雷电 与 MuMu **完全不同**，最容易搞错）

| # | 坑 | 后果 | 正确做法 | 详见 |
|---|---|---|---|---|
| B1 | 对着雷电用 `adb root` | 报错/无效果，误判"没 root" | **雷电**：Root 走 **KernelSU 动态挂载 su**，直接用 `su -c`；**MuMu**：无 su，必须 `adb root` + 放 su 垫片 | 1.8 · 6.5 |
| B2 | 卸载 KernelSU / 管理器 App | 动态 su 失效，所有 `su -c` 全废 | 卸载前先备份管理器 APK（`adb pull` 保存），见问题5 | 问题5 |
| B3 | 为了"消掉 Root 弹窗"乱改系统 | 可能把 App 彻底弄坏、数据停更 | 弹窗点"好的"继续即可，**不影响导出**（脚本直接读数据库文件） | 问题5 |
| B4 | 给来路不明的 App 授 root | 安全风险 | 本流程只需 adb 侧一条 `su -c`，**无需给任何 App 授权** | — |
| B5 | Zygisk 开着还硬推 | OPPO 健康起不来 → 只能导出旧数据 | 先按问题8 跑 `disable_zygisk.sh` 关 Zygisk，关完**必须硬重启**（`ldconsole quit` + `launch`，禁用 `adb reboot`，见问题13）才生效 | 问题8 |

### C. 会让数据错误 / 白分析的坑

| # | 坑 | 后果 | 正确做法 | 详见 |
|---|---|---|---|---|
| C1 | 手动只 pull `database.db`（不带 `-wal`/`-shm`） | 丢失尚未 checkpoint 的最新数据 | 用一键脚本（已自动带 `-wal`/`-shm` 并校验） | 2.5 |
| C2 | 上次运行残留的本地 `-wal` 混进新库 | 数据偏旧/混杂 | 脚本**拉取前自动清理残留**，无需手动 | 2.5 |
| C3 | App 运行中直接 pull | 库处于写入中间态 | 脚本会先 `am force-stop` 再 pull（步骤 5） | 2.3 |
| C4 | 临近午夜（如 23:57）就断言"今天数据差" | 当天数据本就不完整 | 报告窗口已前移到**锚点日的前一天**；指定日期用 `OPPO_DATA_DATE` | 5.4 · 附录D |
| C5 | `sport_mode` 用 -3 / 压力按时长算 / `fall_asleep` 不取模 | 数值全错（压力会偏小约 270 倍） | 严格按 3.3 单位表 + 3.6 易错点 | 3.3 · 3.6 |
| C6 | 换设备/重装 App 后沿用旧密钥 | 解密失败（`file is not a database`） | 密钥随 App 运行时由 Frida 取，换环境重新取 | 6.2 |

### D. 会让自动化白跑的坑（环境/工具）

| # | 坑 | 后果 | 正确做法 | 详见 |
|---|---|---|---|---|
| D1 | 多台模拟器同时连着 | `more than one device/emulator` | 脚本已自动 `-s`；手动命令务必指定序列号或设 `ANDROID_SERIAL` | 问题7 |
| D2 | 雷电端口动态变化 / 设备 offline | 连不上 | `ldconsole list2` 查进程；改用 `emulator-5554`（端口无关）；`kill-server && start-server` 重连 | 问题10 |
| D3 | 工作目录含**中文或空格** | adb / frida / python 各种诡异报错（路径转义问题） | 用**纯英文短路径**（示例 `D:\oppo-health-data-toolkit`） | — |
| D4 | 杀毒/安全软件拦截 | `frida-server` 或 `adb.exe` 被隔离、删除，报"找不到文件" | 把工作目录与 `platform\` 加入白名单（或临时关实时防护），用后恢复 | — |
| D5 | 本机 5000 端口被占用 | 上传服务端起不来 | `netstat -ano \| findstr :5000` 查占用；关掉占用进程，或 `set OPPO_HEALTH_PORT=5001` 换端口（客户端 `OPPO_HEALTH_URL` 同步改端口） | 3.5 |
| D6 | 电脑端 frida 与 `frida-server` 版本不一致 | attach 失败 / `unable to load libart.so` | 版本必须一致（实测 **17.17.0** 配套）；`dist\` 分卷还原的就是配套版 | 1.5 |
| D7 | 模拟器系统时间与主机不同步 | 日期判定异常（未来日期告警、窗口错位） | 校准模拟器时间后重跑 | 附录D |
| D8 | Python 依赖不全 | 解密失败（缺 sqlcipher3）/ 上传失败（缺 mcp）/ GUI 导出 Excel 失败（缺 openpyxl） | `pip install -r requirements.txt`（openpyxl 为 GUI Excel 的可选项） | 1.6 |

### E. 隐私红线（别把个人数据带出去）

| # | 坑 | 后果 | 正确做法 | 详见 |
|---|---|---|---|---|
| E1 | 把 `db_key.txt` / 解密库 / 报告 / `server\sink\` 提交进 git | **个人健康数据泄露** | 以上均已写入 `.gitignore`；提交前 `git status` 复核一遍 | README 隐私说明 |
| E2 | 把原始数据直接丢给第三方 AI | 隐私外泄（含 ssoid / open_id 等身份列） | 先按第九部分 9.2 删身份列与裁剪时间范围再上传 | 9.2 |
| E3 | 上传载荷明文留在本地 | 本地明文留痕 | 上传过程**不落盘**任何明文载荷，无需操作 | 3.5 |

---

## 第一部分：环境准备与依赖

### 1.1 硬件与软件要求

| 项目 | 要求 |
|------|------|
| 操作系统 | Windows 10/11 |
| 模拟器 | 雷电 14（Android 14），x86_64 —— **当前版本验证基线（已全链路实测）**；MuMu 12+（Android 15）支持暂缓——发布前曾在 MuMu 完成历史实测（记录见第七部分），最新代码未复测，差异说明见 6.5 |
| OPPO 健康版本 | 最新版 **6.7.19**（实测可完整配套；自适应 hook 已适配）或 6.6.7 旧版（回退备选） |
| Python | 3.10+（依赖见 1.6，另见仓库 requirements.txt） |
| 磁盘空间 | 至少 500MB 可用 |

> ⚠️ **App 版本与升级兼容性**：随 App 升级，需留意以下兼容性风险（实测可完整配套的版本见上表；两版取密钥 hook 均已适配）：
> - **取密钥 hook 失效**——App 更换加密类或方法名时需跟进适配：脚本已内置新旧两套方法名并自动切换，适配方法见 6.3 节（改 `frida_get_dbkey.js` 一个文件即可全链路生效）；
> - **字段错位**——App 改版增删数据库列会导致字段映射失效：生成器已内置缺表/缺列保护，缺失数据降级为"无数据"而不中断，但口径需人工复核；
> - **frida-server 版本不匹配**——与 App 版本无关，只需保证电脑端 frida 与模拟器内 frida-server 同版本（见必坑清单 D6）。

### 1.2 关键路径与配置

```
工作目录：        <工作目录>\                # 你的工作目录
ADB 工具：        <工作目录>\platform\adb.exe
Frida 服务端：    <工作目录>\dist\frida-server（push 到模拟器 /data/local/tmp/）
                  ⚠️ 仓库内是分卷 dist\frida-server.part0/.part1，需先 `python tools\rejoin.py`
                  还原为 dist\frida-server 后才能 push（根目录没有 frida-server）
导出数据目录：    <工作目录>\DB\combined\（合并全量库，每次导出覆盖旧文件）
Flask 后端：      <工作目录>\server\upload_server.py
ADB 设备序列号：  雷电 127.0.0.1:5555 或 emulator-5554；MuMu 127.0.0.1:16384
                  （可用环境变量 ANDROID_SERIAL / config.py adb_device 覆盖）
ADB 端口注意：    雷电 14 重启后 TCP 端口可能变化，用 ldconsole list2 查看，
                  或直接用 emulator-5554 序列号（端口无关，推荐）
模拟器目录探测：  config.py 自动探测雷电默认安装位 C:\leidian\LDPlayer*（示例）；
                  自动探测仅覆盖 C/D/E/F 四盘符的常见安装根（含 1\、soft\、tools\、
                  Program Files\ 等一层子目录）；装在更深目录或其他盘符时，
                  直接设环境变量 OPPO_EMULATOR_DIR 指定（最可靠）
Flask 服务地址：  默认 http://127.0.0.1:5000（环境变量 OPPO_HEALTH_URL 覆盖）
Flask 监听端口：  默认 5000（环境变量 OPPO_HEALTH_PORT 覆盖；改端口后客户端
                  OPPO_HEALTH_URL 需同步修改）
Flask Token：     CHANGE_ME_TOKEN（公开仓库占位值，使用前请改成你自己的字符串）
```

### 1.3 模拟器配置

**雷电 14**：设置 → 其他设置 → **Root 权限开启**、**ADB 调试开启** → **重启模拟器使设置生效**。
**MuMu 12+**：设置 → 其他 → **Root 权限开启**；ADB 默认开启（16384）。
启动方式（控制台最稳）：
```cmd
雷电："<模拟器目录>\ldconsole.exe" launch --index 0
MuMu："<MuMu 安装目录>\nx_main\MuMuManager.exe" control -v 0 launch
```
> ⚠️ OPPO 健康可能报"检测到 Root"，点"好的"继续即可——脚本直接读数据库文件，不需要 App 全功能运行。自动化环境中模拟器启动即死（沙箱杀子进程）的处置见问题 2。

### 1.4 OPPO 健康安装与登录

1. 安装 APK：`adb -s <序列号> install -r OPPO健康最新版.apk`（6.6.7 旧版为回退备选，两版 hook 均已适配）
   > **APK 未随仓库分发**（版权与合规考虑）：OPPO 健康从 OPPO 官方渠道（官网/软件商店）获取。
2. 启动 App：`adb -s <序列号> shell "am start -n com.heytap.health/.oobe.LaunchActivity"`
3. **登录账号**（人工步骤；Root 警告点"好的"继续）
4. 等待 2~5 分钟数据同步；确认 App 里能看到健康数据
5. 登录证据：`adb shell "su -c 'ls -la /data/data/com.heytap.health/databases/'"`——database.db 应从 4KB 长到数 MB

### 1.5 Frida 服务端配置

```cmd
:: 第 0 步：还原分卷（仓库内只有 .part0/.part1，必须先拼成完整文件，只需做一次）
python <工作目录>\tools\rejoin.py

:: 推送（首次）——注意源路径是 dist\frida-server，不是工作目录根
<工作目录>\platform\adb.exe -s <序列号> push <工作目录>\dist\frida-server /data/local/tmp/frida-server
<工作目录>\platform\adb.exe -s <序列号> shell "chmod 755 /data/local/tmp/frida-server"

:: 启动（雷电，有 su）
<工作目录>\platform\adb.exe -s <序列号> shell "su -c '/data/local/tmp/frida-server >/dev/null 2>&1 </dev/null &'"
:: 启动（MuMu，无 su：先 adb root 提权）
<工作目录>\platform\adb.exe -s <序列号> root
<工作目录>\platform\adb.exe -s <序列号> shell "nohup /data/local/tmp/frida-server >/dev/null 2>&1 </dev/null &"

:: 验证
<工作目录>\platform\adb.exe -s <序列号> shell "ps -A | grep frida"
```
- **每次重启模拟器后都要重新启动**（不持久）。
- **末尾 `</dev/null` 不可省**：只重定向 stdout/stderr 时，交互终端会因 stdin 挂起（脚本内经 run_cmd 的 taskkill 机制不受影响）。
- 版本必须与电脑端 frida 匹配（实测 17.17.0 配套）。
- **MuMu 禁止经 `MuMuManager sh` 启动**（nemuinit 上下文会报 `unable to load libart.so`），必须 adb root 态经 adb shell 启动。

### 1.6 Python 依赖检查

```cmd
python -c "import sqlcipher3; print('sqlcipher3 ok')"
python -c "import flask; print('flask ok')"
python -c "import requests; print('requests ok')"
python -c "import frida; print('frida ok')"
```
缺失：`pip install -r requirements.txt`（核心：frida / frida-tools / sqlcipher3；上传与 MCP 链路：flask / requests / mcp；GUI 导出 Excel 另需 openpyxl，可选）。sqlcipher3 Windows 下可能需预编译 wheel。

### 1.7 模块说明

| 模块 | 文件名 | 功能 |
|------|--------|------|
| 集中配置 | `config.py` | 38 项配置集中管理（自定位，改这里即可） |
| 通用工具 | `utils.py` | 日志、进度条、完整性校验、安全类型转换 |
| 单元测试 | `test_auto_export.py` | 74 个单元测试（含 5 条实现/质量守卫 + 18 条边界/异常输入用例） |
| 数据校验 | `data_validation.py` | 8 维度数据合理性测试（自动优先校验 DB\combined） |

### 1.8 无 su 环境放置 su 垫片（MuMu 必做；脚本依赖 `su -c`）

```cmd
<工作目录>\platform\adb.exe -s <序列号> root
<工作目录>\platform\adb.exe -s <序列号> shell "printf '#!/system/bin/sh\n[ \"$1\" = \"-c\" ] && shift\nexec /system/bin/sh -c \"\$@\"\n' > /system/bin/su && chmod 755 /system/bin/su"
:: 预期 uid=0(root)
<工作目录>\platform\adb.exe -s <序列号> shell "su -c id"
```
> 原理：adbd 已是 root，垫片只做参数转发；开启"可写系统盘"时写入经 OverlayFS 落盘持久（块设备显示 ro 不影响写入）。雷电 14 的 KernelSU 会自行拦截 `su` 提权到 `u:r:su:s0` 上下文，效果等同。

### 1.9 多模拟器并存与 ADB 端口管理

- 多台并存时所有 adb 命令必须带 `-s <序列号>`，或运行脚本前 `set ANDROID_SERIAL=<序列号>`（config 支持）。
- 雷电 14 的 TCP 端口重启后可能变化；`emulator-5554` 序列号与端口无关，推荐优先使用。
- 实测双模拟器并存（雷电 + MuMu 同时在线）下，`ANDROID_SERIAL=127.0.0.1:16384 python export_health_data.py` 路由正确。

---

## 第二部分：数据导出与生成流程

### 2.1 执行前检查清单

- [ ] 模拟器已启动，能看到安卓桌面
- [ ] Root 权限已开启（su 或 adb root 可用）
- [ ] ADB 已连接（`adb devices` 有 device）
- [ ] OPPO 健康已安装并**登录账号**
- [ ] OPPO 健康里有数据（database.db 已长大）
- [ ] frida-server 已在模拟器上运行
- [ ] Python 依赖已安装

### 2.2 连接 ADB

```cmd
:: 雷电；MuMu 为 127.0.0.1:16384
<工作目录>\platform\adb.exe connect 127.0.0.1:5555
<工作目录>\platform\adb.exe devices
```
**预期**：`127.0.0.1:5555 device`、`emulator-5554 device`（双条目并存属正常，脚本已自动处理）。
5555 连不上：`ldconsole list2` 确认端口 / `kill-server && start-server` / 用 emulator-5554。

### 2.3 运行一键导出+分析脚本

```cmd
cd /d <工作目录>
python auto_export_and_analyze.py
```
**主脚本自动完成 6 个阶段**：① 检查 ADB → ② 启动 OPPO 健康 → ③ 等待 120 秒数据同步 → ④ 调用 `export_health_data.py`（内部步骤编号 1~10，含 7.5/8.5 子步）→ ⑤ 定位最新导出目录（优先 DB\combined）→ ⑥ 生成 12 维分析报告。

**`export_health_data.py` 内部步骤（编号 1~10，另含 7.5 / 8.5 两个增强子步，合计 12 个执行段）**：
```
[1] 检查 ADB 连接        [6]  Pull 加密数据库（连同 -wal/-shm，含完整性校验）
[2] 检查 Frida 服务端    [7]  解密数据库（候选密钥逐个尝试）
[3] 确保 OPPO 健康运行   [7.5] 完整性校验（enable_integrity_check 开关）
[4] Frida 获取密钥       [8]  导出 CSV（46~47 非空表，随版本浮动）
    （自适应 hook，失败回退占位密钥）    [8.5] 按日期分类 csv_by_date
[5] 杀掉 OPPO 健康       [9]  导出 JSON
                         [10] 生成带类型推断的 SQLite
```

### 2.4 只导出数据（不分析）

```cmd
python export_health_data.py
```

### 2.5 导出目录结构（DB\combined\）

```
database_encrypted.db      # 原始加密数据库（数 MB）
database_decrypted.db      # 解密后的 SQLite 数据库（数 MB）
oppo_health_full.db        # 带类型推断的 SQLite（附加步骤：类型推断失败时本文件不生成，不影响其它产物）
health_data.json           # JSON 全量数据（十余 MB，随数据量增长）
db_key.txt                 # 数据库密钥（Frida 实取）
get_key.js                 # Frida 取密钥脚本（自动生成；内容为取密钥脚本副本，无敏感数据，可随手删）
csv\                       # CSV（每表一个，~47个，随版本浮动）
csv_by_date\               # 按数据日期分类（YYYYMMDD\<表>.csv；目录名为 8 位纯数字日期，注意与单日库目录 DB\YYYY-MM-DD\ 的带连字符格式不同）
analysis_report_enhanced_*.md  # 12 维分析报告（auto 版，2 个时间窗口：昨晚 + 最近7天，见 3.1）
```
> 另：项目根目录会有 `logs\`（首次 import `config` 即自动创建；当前版本主流程日志仅终端输出，**文件日志为预留能力未启用**，`log_to_file` 等配置项当前不生效）——**预期行为**，已 gitignore，克隆后不存在属正常。

### 2.6 验证导出成功

1. 退出码 0，日志出现 `🎉 数据导出完成！`
2. 关键文件存在且大小合理（decrypted 数 MB、json 十余 MB，随数据量增长）
3. SQL 抽查：`SELECT COUNT(*) FROM DBSleepMainStat` 等核心表应有数据

### 2.7 从零开始 11 步速通（实操序列）

> 各步骤的深入说明与排错见第一部分对应小节与第四部分；此处为可直接照抄的命令序列。

```cmd
:: 步骤1 安装模拟器（雷电 14 / MuMu 12+）
:: 步骤2 开启 Root 与 ADB（雷电需重启生效）
:: 步骤3 启动并连接
"<模拟器目录>\ldconsole.exe" launch --index 0
:: MuMu 为 127.0.0.1:16384
<工作目录>\platform\adb.exe connect 127.0.0.1:5555

:: 步骤4 安装 OPPO 健康 APK（APK 来源见 1.4 节声明）
<工作目录>\platform\adb.exe -s <序列号> install -r OPPO健康最新版.apk
<工作目录>\platform\adb.exe -s <序列号> shell "pm list packages -3 | grep heytap"
:: 预期：package:com.heytap.health

:: 步骤5 登录账号并等待同步（人工；见 1.4）
<工作目录>\platform\adb.exe -s <序列号> shell "am start -n com.heytap.health/.oobe.LaunchActivity"

:: 步骤6 frida-server（见 1.5，双平台命令不同）
:: 步骤7 无 su 环境放 su 垫片（MuMu 必做，见 1.8）
:: 步骤8 pip install -r requirements.txt（见 1.6）

:: 步骤9 一键导出
cd /d <工作目录>
:: ⚠️ set 行尾不能跟 :: 注释——实测整段会被吞进变量值（ANDROID_SERIAL 变成
::   "127.0.0.1:5555        :: 雷电"），后续 adb 全部失配。注释必须独立成行。
:: 雷电序列号 127.0.0.1:5555；MuMu 为 127.0.0.1:16384
set ANDROID_SERIAL=127.0.0.1:5555
:: 成功标准：退出码 0 + 🎉 数据导出完成！；退出码 2 = 导出数据不完整（JSON 缺表，
::   报告已按现有数据生成）；退出码 1 = 失败。
python auto_export_and_analyze.py

:: 步骤10 SQL 抽查（示例）
python -c "import sqlite3; con=sqlite3.connect(r'DB\combined\database_decrypted.db'); [print(t, con.execute('SELECT COUNT(*) FROM '+t).fetchone()[0]) for t in ['DBSleepMainStat','DBSportDataStat','DBStressTable']]"

:: 步骤11 网页报告
python generate_html_report.py
:: 产出：根目录 OPPO健康数据分析报告.html + DB\combined\ 同名副本（目录存在时；
::       数据源优先级见 5.4——combined 优先，单日报告需显式 --single-day）
```

---

## 第三部分：数据分析步骤与输出格式

### 3.1 增强版分析报告（12 维）

睡眠分析 / OSA与打鼾 / 心率 / HRV / 运动 / 压力 / 血氧 / 综合健康评分 / Top3 健康问题 / 7天改善计划 / 就医建议 / 一句话总结。
（前 7 项为健康数据维度，后 5 项为结论与建议板块，合计 12 章，故称"12 维"。）

> **时间窗口口径**：12 维 MD 报告仅含 **2 个窗口**——昨晚睡眠（`date=今天`，起床日语义）+ 最近 7 天（完整日）；而网页版报告（`generate_html_report.py`）为昨天 / 近3天 / 近7天 / 近30天 **4 个窗口**。两者口径不同，数值勿直接互相对比。

### 3.2 综合健康评分权重

睡眠 25% · 心率 15% · HRV 15% · 运动 15% · 压力 10% · 血氧 10% · OSA 10%。
**动态归一化**：维度不可靠/缺失时自动排除并重新归一化；有效权重 <50% 时显示"有效数据不足"。
**等级**：85-100 优秀 🌟 / 70-84 良好 ✅ / 60-69 一般 ⚠️ / 0-59 需关注 🔴

### 3.3 关键字段单位说明（重要！脚本已自动处理）

| 表名 | 字段 | 原始单位 | 换算规则 | 显示单位 |
|------|------|---------|---------|---------|
| DBSportDataStat | total_calories / total_static_cal | 0.001 千卡（毫千卡） | ÷1000 | 千卡 |
| DBSportDataStat | total_duration | 毫秒 | ÷60000 | 分钟 |
| DBSportDataStat | sedentary_total_duration | **分钟**（实测确认：数万行样本中该列取值全部小于 1440；而同表 total_duration 为百万级、确系毫秒，两者单位**不同**） | 按数值数量级自动判定：>1440 才按毫秒 ÷60000，否则按分钟原样使用 | 分钟 |
| DBSportDataStat | sport_mode | 用 **-2**（精确值），不要用 -3 | - | - |
| DBSportDataStat | total_distance | 米 | ÷1000 | 公里 |
| DBStressDataStatTable | relax/normal/middle/high_stress_total_time | 采样点个数 | ×2分钟 | 分钟 |
| DBStressDataStatTable | average_hr | **并非真实心率**（该列取值远低于人体静息心率下限，属 App 字段本身的特性，勿按心率解读；脚本在取值异常时会自动打印提示） | - | - |
| DBSleepDataStatTable | fall_asleep | 分钟（从昨天0点起算） | %1440 | 时:分 |
| DBStressTable | sdnn/rmssd | ms | 直接使用 | ms |
| DBStressTable | stress_value | 0-100（无量纲压力指数，逐采样点明细） | 直接使用 | 0-100 |
| DBBreathRate | value | 0.1 次/分 | ÷10 | 次/分 |
| DBWeightBodyFatTable | weight | 克（**TEXT 存储**；时间列为 `measurement_timestamp`，**本表无 `date` 列**） | float 后 ÷1000 | kg |
| DBHeartRateDataStatTable | rest_hr / average_hr / min_hr / max_hr | bpm | 直接使用 | bpm |
| DBBloodOxygenSaturationDataStat | average_blood_oxygen_saturation / min_blood_oxygen_saturation | % | 直接使用 | % |
| DBWristTemperatureStat | day_baseline_value / value / min_value / max_value | 0.01 ℃ | ÷100 | ℃ |

> 注：呼吸率数据在明细表 **DBBreathRate.value**（日汇总表 DBBreathRateStat 无 breath_rate 列）；生成器已 ÷10 并渲染。体重字段 SQLite 内 `/1000` 是整数除法，必须 Python 侧转 float。

> ⚠️ **按本表写 SQL 前必看——两个列名陷阱**：
> 1. **血氧表的列名很长**：真实列名是 `average_blood_oxygen_saturation` / `min_blood_oxygen_saturation`，
>    写 SQL 请照抄，不要简写成 `avg_spo2` / `min_spo2`（不是真实列名，会报 `no such column`）；
> 2. **体重表 `DBWeightBodyFatTable` 没有 `date` 列**：它的时间列是 `measurement_timestamp`（毫秒时间戳）。
>    其余表通用的"按 `date` 过滤"写法在这张表上不成立，需先转换时间戳再比对。

> **阈值分级说明（重要）**：两类产物的阈值**分工不同**——**网页报告**的"需关注 / 待改善"为**提醒级**（如静息心率 >85 bpm、最低血氧 ≤93%），提示你留意趋势；**分析报告"就医建议"章**为**就医级**（如静息心率 >100 bpm、平均血氧 <90%），达到该级别才建议挂号就诊。两者用途不同，不可互相替代。

> **深睡 / REM 占比的口径差异（重要）**：两份产物对"深睡占比"采用**不同分母**，数值与参考区间各按自身口径计算——
> - **网页报告**：分母 = **日均总睡眠时长**（日汇总表 `total_sleep_time`，**含白天小睡**），深睡参考区间 **13~23%**；
> - **Markdown 分析报告**：分母 = **主睡时长**（**不含小睡**，避免小睡稀释分母），深睡参考区间 **15~25%**。
>
> 想对比两份报告时，请**只比同一份报告内部的趋势**，不要把两份的百分比直接对照。

### 3.4 常用表速查（Navicat / SQL）

`DBSleepDataStatTable`（睡眠日汇总）· `DBSleepMainStat`（夜间主睡）· `DBHeartRateDataStatTable`（心率日汇总）· `DBSportDataStat`（运动，sport_mode=-2）· `DBStressDataStatTable`（压力日汇总）· `DBBloodOxygenSaturationDataStat`（血氧日汇总）· `DBOsaResult`（OSA，AHI）· `DBStressTable`（压力明细含 HRV）· `DBBreathRate`（呼吸率明细）· `DBWeightBodyFatTable`（体重）

### 3.5 上传数据到 Flask 后端（进阶可选——普通使用跳过本节）

> 这条链路面向"想让支持 MCP 的 AI 直接查询数据"的进阶场景：**"服务器"就是你自己电脑上的一个小程序**（默认只监听 127.0.0.1，数据不出本机），不是任何云端服务。只做"导出 → 网页报告"的普通使用**完全不需要本节**。

```cmd
:: 默认监听 127.0.0.1:5000
cd /d <工作目录>\server && python upload_server.py
:: 每表最新 500 行
cd /d <工作目录> && python upload_data.py
```
鉴权 Token 默认 `CHANGE_ME_TOKEN`（**公开仓库占位值**，客户端/服务端一致，使用前请改成你自己的值）；两端均支持环境变量 `OPPO_HEALTH_TOKEN` 覆盖（默认值不变，改环境变量即无需改源码）；`OPPO_HEALTH_URL`/`OPPO_HEALTH_HOST`/`OPPO_HEALTH_PORT` 见第八部分占位符表与 5.2 节 B。

> **上传范围说明**：上传为"分节快照"——**每表最多 500 行**（按时间列倒序取最新；无时间列则按 rowid 倒序）。因此 **sink 库行数通常少于本地源库**（实测 sink 库为数十表、数千行量级，明显少于源库），这是设计取向（面向 AI 查询的近期快照），**不是数据丢失**；本地 CSV/JSON/SQLite 才是全量。上传与 MCP（`server\mcp_server.py`）共用 `sink\health.db`，跨通道幂等去重；上传过程**不落盘**任何明文载荷（隐私最小化）。另注：受分节文本（逗号分隔）格式约束，数据**值**中的英文逗号与换行会被统一替换为空格（客户端与两端服务端同规则，保证双通道行哈希一致）——含逗号的文本字段上传后存在轻微失真，属已知取舍。
>
> **⚠️ 传输安全**：上传链路使用 **HTTP 明文**。默认监听 `127.0.0.1`（本机回环，不经过网卡，无泄露风险）；但若通过 `OPPO_HEALTH_URL` 指向**远程主机**，健康数据与 Token 将以**明文跨网络传输**，可被同网段抓包——请仅在**内网/可信网络**使用，或自建 TLS 反向代理（Nginx + HTTPS）后再对外暴露，并配合强 Token（见第八部分 `OPPO_HEALTH_TOKEN`）。

### 3.6 数据计算易错点（写 SQL 前必看）

1. sport_mode 用 -2 不用 -3（-3 是舍入值且日期为其子集，会漏算）；
2. 压力档位字段是**采样点个数**（每点约 2 分钟），按秒算偏小约 270 倍；
3. `fall_asleep` 从昨天 0 点起算，常 >1440，需 %1440 取模；
4. HRV 在 `DBStressTable` 的 sdnn/rmssd（数千条真实值），不要误判"缺失"；
5. **日汇总表 date 字段比实际日期晚一天**（如 date=20260102 对应 01-01 数据；即"起床日"口径，见附录 D）；
6. 类型化 SQLite（`oppo_health_full.db`）已自动推断字段类型；早期全 TEXT 存储的旧库做聚合（SUM）查询需 CAST。

---

## 第四部分：常见错误与排查

### 问题1：ADB 连不上模拟器
`kill-server && start-server` 后重连；检查模拟器 ADB 调试开关；雷电端口动态变化用 `ldconsole list2` 或 `emulator-5554`。

### 问题2：模拟器没真正启动 / 启动即死

**先看进程**：只有 `ldplayerservice.exe` 而没有 `dnplayer.exe` + `Ld9BoxHeadless.exe`＝虚拟机根本没起来。
判别要点：`Ld9BoxHeadless.exe` 才是真正跑 Android 的进程（占 200MB+ 内存），只有它起来了 ADB 才连得上。

| 症状 | 原因 | 处置 |
|---|---|---|
| 只有 `ldplayerservice.exe`，没有 `dnplayer.exe` | 上次异常退出留下僵尸状态 | 杀掉 `dnplayer*` / `Ld9Box*` 进程后重启；检查 Hyper-V / 内核隔离冲突 |
| 模拟器起来了但几秒后自己退出 | 内存不足或显卡渲染冲突 | 雷电设置里把分辨率/帧率调低，或改用 DirectX 渲染 |
| **在 AI 助手 / 自动化脚本里启动后闪退** | **不是模拟器的问题**：调用方在命令结束时回收整棵子进程树 | 见下方「WMI 启动法」 |

#### ⚠️ WMI 启动法（自动化场景必看）

在 AI 助手、CI、或被父进程托管的环境里启动模拟器时，**`Start-Process` 和 `start ""` 都无效**
——实测：命令一结束，模拟器就被连同进程树一起回收，表现为「启动即闪退」。

原因是这类环境把子进程纳入统一的进程管理（Job Object），父命令结束即整树终止。
**可行做法是用 WMI 启动**——进程由 WMI 服务（winmgmt）创建，不在那棵进程树里：

```powershell
Invoke-CimMethod -ClassName Win32_Process -MethodName Create `
  -Arguments @{ CommandLine='"<模拟器目录>\ldconsole.exe" launch --index 0';
                CurrentDirectory='<模拟器目录>' }
```

返回值 `ReturnValue=0` 即启动成功，随后照常用 `adb connect 127.0.0.1:5555` 连接即可，
模拟器会在命令结束后继续存活。

> 实测对照：同一环境下分别用三种方式启动常驻进程，命令结束后
> `Start-Process` 起的进程在 6 秒内被终止、任务计划方式未能拉起，
> **WMI 方式存活**，模拟器与 frida-server 均保持运行。

**备选做法（WMI 不可用时的退路）**：把「启动 → 等开机 → 连接 ADB → 推送 frida-server → 运行导出」**全部写进同一条命令 / 同一个脚本里一次跑完**。
只要整条链路不跨越命令边界，模拟器就不会被中途回收。缺点是中途失败要整段重跑。

```powershell
# 一次跑完，不要拆成多条命令
powershell -ExecutionPolicy Bypass -File run_all.ps1   # 内含启动→开机→连接→导出
```

> 完整的闪退排查与解决方案见 **第十部分**。

### 问题3：Frida 拿不到密钥（`DB_KEY_ERROR`）
① 确认 App 前台运行（attach 模式需 App 先启动）；② 确认 frida-server 运行中；③ **最新版 App 方法名已变**——确保 `export_health_data.py` 为自适应 hook 版（g()→getInstance()、b()→deCryptData()）；④ 用方法枚举脚本核对新签名（见 6.3 节）；⑤ 脚本有密钥回退机制，但**仓库内回退密钥为公开零占位，经实测无法解密正常登录账号的库**——新装/换账号必须 Frida 实取。

### 问题4：数据库解密失败（`file is not a database` / HMAC 错误）
① 密钥不对——未登录时 Keystore 无 db_key，回退密钥必然失败，**先登录再导出**；② 确认 pull 前已杀 App、连 -wal 一起 pull；③ SQLCipher 用默认配置不要手改。

### 问题5：OPPO 健康报"检测到 Root"

点"好的"继续即可，**不影响导出**（脚本直接读数据库文件），弹窗可关闭、不阻塞导出链路。

**已确认生效的 3 个检测点 + 中和办法**：①Magisk 运行时（magiskd+su）②`ro.debuggable=1` ③/system 可写。对应办法：关 Zygisk（见问题8，`disable_zygisk.sh`）+ `setprop ro.debuggable 0`（运行时生效，重启后需重设）+ /system 只读。

**第 4 触发源疑似 KernelSU（雷电 14 特有，尚未实机验证）**：新装雷电 14 预置 KernelSU（管理器 `me.weishu.kernelsu`、数据目录 `/data/adb/ksu` 与 `/data/adb/ksud`）；旧环境用普通 su 文件、稳定配置下**无弹窗**——新旧环境关键差异即 KernelSU，为头号嫌疑。已排除"检测结果缓存"假设（`pm clear com.heytap.health` 后仍弹）。

> ⚠️ **注意：以下步骤为理论排查思路，尚未在雷电 14 上实机验证。**
> 
> 涉及卸载/移动 Root 管理器相关文件，操作前必须：
> 1. 确认 `adb root` 兜底可用：`adb -s <序列号> root && adb -s <序列号> shell whoami` → 预期返回 `root`；
> 2. 备份管理器 APK：`adb pull <pm path 返回的路径> kernelsu_backup.apk`；
> 3. 截基线存证：`adb exec-out screencap -p > baseline.png`；
> 4. 建议先在**测试环境/不重要的模拟器实例**上验证，确认无副作用后再写入主环境。
> 
> 若对 Root 管理器操作没有把握，可跳过本段，直接关闭弹窗继续导出（弹窗不影响数据库读取）。

**单变量递进排查**（每步复测：`am force-stop com.heytap.health` → `am start` → 等 7 秒 → `adb exec-out screencap -p > expN.png`）：

| 步骤 | 操作 | 弹窗消失则说明 |
|---|---|---|
| R1 | `adb shell "pm uninstall --user 0 me.weishu.kernelsu"`（仅卸管理器） | 触发源 = 管理器包名扫描 |
| R2 | 仍弹 → `mv /data/adb/ksu /data/adb/ksu.bak && mv /data/adb/ksud /data/adb/ksud.bak`（root 态） | 触发源 = /data/adb 路径扫描 |
| R3 | 仍弹 → `ps -A \| grep -i ksud` 确认守护进程已无，若在则 kill 后复测 | 触发源 = ksud 进程 |
| R4 | 全部排除后仍弹 | KernelSU 嫌疑排除，转下方兜底 |

> 编号说明：本表用 **R1~R4**（R = Root 排查），与第一部分「必坑清单」的 E 编号互不相干——
> 两处此前都用 E1 起头，实测造成查阅混淆，已错开。

**回滚**：卸载管理器后若 `su -c` 失效，先 `adb root` 即可正常运行（**不影响导出**），或 `adb install kernelsu_backup.apk` 重装；移走的目录 `mv` 回原名即可。

**兜底（KernelSU 排除后仍弹）**：对比"无弹窗旧环境"与"有弹窗新环境"的全量 `getprop` 做 diff（重点 SELinux / build / oem 类属性），锁定差异属性后逐个 `setprop` 复测。

### 问题6：脚本运行到一半卡住
Ctrl+C 中断；检查模拟器是否卡死（雷电偶发）；重启模拟器重跑——脚本幂等。

### 问题7：ADB 报 "more than one device"
脚本已自动处理（-s 参数 + ANDROID_SERIAL）；手动命令需指定设备。多模拟器并存时用 `ANDROID_SERIAL` 切换目标（实测有效）。

### 问题8：Magisk Zygisk 损坏导致 App 起不来
症状：App `failed to attach` → `start timeout`，数据停在旧时间。根治：推送 `disable_zygisk.sh` 执行（改 magisk.db zygisk=1→0）→ **不要用 adb reboot**，用 `ldconsole quit` 等待 10 秒再 launch → 验证 `zygisk|0` → App 正常启动后重跑导出。Magisk 配置目标态：zygisk=0（脚本所改）、magiskhide=1、denylist=0、sulist=0（后三项为经验目标值，脚本不改，需自行核对）。

### 问题9：导出的数据截止到昨天
App 没起来就没同步。按问题 8 关 Zygisk → 硬重启 → 手动打开 App 等 2-5 分钟同步 → 确认 App 有今天数据 → 重跑。报告对滞后数据自动适配（动态完整日窗口/睡眠回退/数据不足保护）。

### 问题10：雷电 ADB 端口动态变化 / 设备 offline
重启后端口可能变（5555→其他）。`ldconsole list2` 查看进程；改用 `emulator-5554` 序列号（端口无关）；`kill-server && start-server` 后重连。

### 问题11：MuMu 无 su，脚本 `su -c` 报 not found
`adb root` 提权（adbd 直接变 root），再放 **su 垫片**（见 1.8 节）。开启"可写系统盘"时写入经 OverlayFS 持久；块设备显示 ro 不影响写入。

### 问题12：`setenforce 1` 导致模拟器整机冻结
**红线：雷电/MuMu 上禁止 setenforce 1**。Permissive 是这类 ROM 的构建前提，Enforcing 后 root adbd 无法 exec /system/bin/sh，且状态**跨软重启持久**；只能 `ldconsole quit`（宿主杀进程）硬恢复。恢复流程：quit → 确认 list2 停止 → launch → 等待 boot（脏盘首次引导可能数分钟）。

### 问题13：`adb reboot` / `am start -W` 永久挂起
已知坑。重启模拟器用 `ldconsole quit + launch`；启动 App 用 `am start`（不带 -W）。

### 问题14：日志文件被文本工具判为"二进制"
sqlcipher3 原生 stderr 输出 UTF-16LE，与脚本 UTF-8 日志混排所致；用 Python 按编码解码查看，不影响功能。

### 问题15：Frida "Java is not defined"
Frida 17 已知问题：用 frida CLI（`frida -D <序列号> -p <PID> -l frida_get_dbkey.js`，`-l` 传仓库内的实际脚本名），不要用 Python 绑定直接执行。

---

## 第五部分：使用方法、参数配置与系统维护

### 5.1 各脚本功能与用法

| 脚本 | 功能 | 用法 |
|------|------|------|
| `auto_export_and_analyze.py` | 一键导出+分析（主入口） | `python auto_export_and_analyze.py`；返回码 0=成功 / 2=导出数据不完整（JSON 缺表，报告已按现有数据生成）/ 1=失败 |
| `export_health_data.py` | 仅导出（自适应取密钥→pull→解密→校验→CSV/JSON/SQLite） | `python export_health_data.py`；返回码口径同上（0/2/1） |
| `generate_html_report.py` | 网页报告（四个时间窗口 × 六模块单文件 HTML，纯标准库） | `python generate_html_report.py`（默认当天；`OPPO_DATA_DATE` 或首参指定历史日） |
| `split_db_by_day.py` | 合并库按天拆分（自定位，产出 DB\YYYY-MM-DD\） | `python split_db_by_day.py`；目标目录已存在 `database_decrypted.db` 时**默认跳过**，加 `--force` 则先备份为 `.bak` 再覆盖 |
| `json_to_sqlite.py` | JSON → 带类型推断的 SQLite（数值列可直接 SUM/AVG） | `python json_to_sqlite.py`（自定位：默认 DB\combined\health_data.json，找不到时回退 DB 下最新含该文件的目录） |
| `disable_zygisk.sh` | 关闭 Magisk Zygisk（OPPO 健康起不来时用，见问题 8） | 推送到设备后在 adb shell 里执行 `sh disable_zygisk.sh`；完成后必须硬重启模拟器才生效 |
| `data_validation.py` | 数据合理性校验（自动优先校验 DB\combined） | `python data_validation.py [db路径]` |
| `upload_data.py` / `server\upload_server.py` | 上传链路 | 见 3.5 节 |
| `server\mcp_server.py` | MCP 接口（health_ingest / health_query / health_stats / health_watermark / health_list_tables / health_schema，共 6 个工具，详见 mcp-tools.json） | stdio 由 MCP 客户端拉起 |
| `health_export_gui.py` | GUI 按时间范围/格式导出 | `python health_export_gui.py` |
| `test_auto_export.py` | 单元测试 74 项（可选） | `python test_auto_export.py` |
| `tools\rejoin.py` | dist 分卷还原 + SHA256 校验 | `python tools\rejoin.py` |

### 5.2 参数配置

**A. config.py**：`target_daily_steps`(8000)、`wait_data_sync`(120)、`adb_device`、分析阈值等。
**B. 环境变量**：`ANDROID_SERIAL`（目标设备）、`OPPO_HEALTH_URL`、`OPPO_HEALTH_HOST`、`OPPO_HEALTH_PORT`（上传服务监听端口，默认 5000）、`OPPO_HEALTH_TOKEN`（上传鉴权，两端共读）、`OPPO_FRIDA_CLI`、`OPPO_EMULATOR_DIR`、`OPPO_DATA_DATE`（网页报告锚定日期）。
**C. 报告口径**："昨晚"=起床日为今天；"最近7天"=最近 7 个**完整日**（动态探测）；运动取 sport_mode=-2。
**D. 可调常量（代码内的具名常量，改完建议重跑 `test_auto_export.py` 自检）**：

| 常量 | 所在文件 | 作用 | 注意 |
|---|---|---|---|
| `MED_THRESHOLDS` | `auto_export_and_analyze.py` | 就医建议阈值共 13 键：9 个核心临床阈值（AHI 重度/轻度、血氧低值/正常、静息心率高值/正常、RMSSD 低值/正常、综合评分低值）+ 4 个分档辅助键（ahi_moderate/rest_hr_low_normal/rest_hr_elevated/rmssd_mid） | 报告里的提示文案由同一常量插值生成，**改阈值文案自动跟着变**，不会口径分裂 |
| `WEB_ALERT` | `generate_html_report.py` | 网页报告的「提醒级」阈值（静息心率关注线 85 bpm、低血氧 ≤93%） | **独立于 `MED_THRESHOLDS`**（后者是「就医级」）。两套阈值有意分层（见下文「阈值分级说明」），改其一不影响另一套；网页报告所有提醒级判据由该常量插值，不散落硬编码 |
| `SLEEP_SCORE_WEIGHTS` | `generate_html_report.py` | 医学睡眠评分六维满分权重（时长 30 / 深睡 20 / REM 15 / 节律 15 / 连续性 10 / 效率 10） | **六项合计必须为 100**，否则 0~100 分量表失效（已有测试守卫） |
| `MAX_ROWS_PER_TABLE` | `upload_data.py` | 上传时每表取最新行数的上限（默认 500） | 与 3.5 节 / 附录D 的"分节快照"口径一致 |

### 5.3 系统维护

上传每表限最新 500 行（具名常量 `MAX_ROWS_PER_TABLE`，见 upload_data.py；与 3.5 节/附录 D 口径一致）。

### 5.4 网页报告生成器

**调用**：`python generate_html_report.py [锚点日] [--single-day]` → 双写：根目录主文件 + `DB\combined\OPPO健康数据分析报告.html` 副本（该目录存在时）；`OPPO_DATA_DATE=YYYY-MM-DD` 或首参可锚定历史日；加 `--single-day`（可放任意参数位置）强制使用锚点日单日库出单日深度报告（终端与报告页头均显著提示，四个多日窗口将为空，属预期）；数据库缺失时非零退出、不生成空报告。
**更新数据**：全自动导出会自动落盘 combined 并可直接生成；手动替换则将 `database_decrypted.db` 放入 `DB\combined\` →（可选）`python split_db_by_day.py` 按天拆分 → 运行生成器。
**故障排查**：报告 0KB/全"—"=误读空库，删空库重跑；中文乱码=`chcp 65001`；"昨天"显示不完整当天=设 OPPO_DATA_DATE 为昨天。
**能力**：呼吸率（DBBreathRate.value÷10）与睡眠血氧渲染进心率与血氧模块；空窗口自动显示"该窗口日期范围内没有导出数据（全库最新数据日：…）"琥珀色提示条。

> ⚠️ **数据源优先级（盲审 P1-1 修订）**：生成器**默认永远优先 `DB\combined\` 全量合并库**——
> 四个统计窗口（锚点日前 N 天、不含锚点日当天）需要跨多日数据；即使跑过 `split_db_by_day.py`
> 建立了日库，多日报告也不会再被单日库"抢走"数据源（旧版"日库优先"会把约 200KB 的完整报告
> 静默降级成约 45KB 的全空报告且退出码仍为 0，已修正）。**需要单日深度报告**（只看某一天）
> 时，显式加 `--single-day` 参数（如 `python generate_html_report.py 2026-09-09 --single-day`）：
> 此时终端打印醒目警告、报告页头标注"⚠️ 单日库"，四个多日窗口将为空（预期行为，报告内有说明）。
> `DB\combined\` 不存在时，才回退使用锚点日日库。

### 5.5 按天拆分（split_db_by_day.py）

读 `DB\combined\` 合并库 → 产出 `DB\YYYY-MM-DD\` 独立日库（仅新建缺失日，已有跳过）→ 自动校验各分区表行数与源库一致。脚本自定位（基于 `__file__`），整个目录搬家/改名无需改代码。

---

## 第六部分：技术原理详解

### 6.1 数据库加密机制

OPPO 健康本地库用 **SQLCipher** 整库加密：AES-256-CBC，页大小 4096，KDF 迭代 256000，HMAC-SHA512（SQLCipher 4 默认）。直接十六进制打开是乱码，需正确密钥。

### 6.2 密钥体系与存储

`db_key` 为随机字符串（格式 `32位十六进制 + "db_key"`），**按账号生成并云端同步**（双平台实测同账号同密钥），经 Android Keystore 非对称密钥加密后存于 App 私有目录；使用时先经 Keystore 解密得到 db_key，再打开 SQLCipher 库。

### 6.3 Frida 取密钥原理与最新版适配

目标类 `com.heytap.health.base.encrypt.AesGcmAndroidKeyStore`：attach 进程 → 取单例 → 调解密方法传别名 `"db_key"` → 返回明文密钥。

**最新版 API 变化**：

| 用途 | 6.6.7 | 最新版 |
|---|---|---|
| 取单例 | `g()` | `getInstance()` |
| 解密 db_key | `b("db_key", null)` | `deCryptData("db_key", null)` |
| 别名换算 | （内部） | `getRealAliasKey(String, String)` |

旧 hook 在最新版上报 `DB_KEY_ERROR: not a function`（方法混淆改名）。**自适应 hook** 先试旧签名、失败自动切新签名，两版通吃（已统一维护在根目录 **`frida_get_dbkey.js`**——`export_health_data.py` 主流程优先加载该文件，内嵌副本仅作文件缺失时的兜底；**适配新版本 App 时改这一个文件即可全链路生效**）。⚠️ 手工用 frida CLI 单独运行本脚本会在终端输出**明文密钥**（JS→Python 的机器通道必须保留完整值），请勿在共享屏幕/录屏场景使用。排查新版本时用**方法枚举脚本**列出全部方法签名：

```js
Java.perform(function() {
  var Cls = Java.use("com.heytap.health.base.encrypt.AesGcmAndroidKeyStore");
  var ms = Cls.class.getDeclaredMethods();
  for (var i = 0; i < ms.length; i++) console.log("  " + ms[i].toString());
  try {
    var C = Java.use("com.heytap.health.base.encrypt.AesGcmAndroidKeyStore$Companion");
    var cms = C.class.getDeclaredMethods();
    for (var i = 0; i < cms.length; i++) console.log("  [Companion] " + cms[i].toString());
  } catch(e) {}
});
```

### 6.4 为什么电脑端解密、为什么用 Frida

- **电脑端解密**：App 内置 SQLCipher 原生库为 ARM64（最新版 APK 无 x86_64 so，靠模拟器 ARM 转译运行），在模拟器内解密常因 native 库加载失败；电脑 x86 版 sqlcipher3 完全兼容且更快。
- **为什么 Frida**：Frida 手动 attach、无需 Zygisk；雷电上手动部署的 Magisk 缺 magiskinit 导致 Zygisk 无法注入。曾评估并放弃 Xposed 模块路线（需 LSPosed 框架，雷电/MuMu 上经验证不可行），Frida 是唯一稳定路线。

### 6.5 MuMu 与雷电的差异根源

| 维度 | 雷电 14 | MuMu 12+ |
|---|---|---|
| Root 机制 | KernelSU（动态挂载 su） | 自带（adb root 提权，无 su 文件） |
| ADB 端口 | 5555 起但**动态** | 16384 固定 |
| /system 写入 | "可写系统盘"开 + OverlayFS 上层 | 同左（块设备显 ro 但写入成功） |
| Frida 启动 | `su -c` | **必须 adb root 后 adb shell 启动** |
| SELinux | 必须 Permissive（Enforcing 即冻结） | 同左 |

---

## 第七部分：双平台可移植性实测报告（全新环境）

> 注：本部分为**历史实测记录**（发布前在雷电 14 与 MuMu 12+ 上完成）；当前版本 MuMu 支持暂缓，本记录保留作技术参考。

### 7.1 测试环境

| 项 | 雷电 14 | MuMu 12+ |
|---|---|---|
| 安装位置 | 各自默认/自选安装目录（不作记录） |
| Android / ABI | 14 / x86_64 | **15** / x86_64 |
| ADB 端口 | 5555（动态） | 16384（固定） |
| Root 方式 | KernelSU | 自带（adb root，无 su） |
| SELinux | Permissive（必须） | Permissive |
| OPPO 健康 / 账号 | 最新版 / 已登录 | 最新版 / 已登录 |

### 7.2 逐环节验证结果

> 注：下表为**历史环境实测记录**（✅ 均为当时结果）；当前版本 MuMu 支持暂缓、未在最新代码上复测（见开篇"适用"与 7.5）。

| 环节 | 雷电 14 | MuMu |
|---|---|---|
| 启动 / ADB / Root | ✅ / ✅ / ✅ su 或 adb root | ✅ / ✅ 16384 / ✅ adb root |
| frida-server | ✅ | ✅（须 adb root 态启动） |
| 安装 OPPO 健康 APK（来源见 1.4） | ✅ Success | ✅ Success |
| App 启动 | ✅ | ✅ |
| 登录验证 | ✅ db 由初始数 KB 增长到数 MB | ✅ 同左 |
| Frida 取密钥 | ✅ `<你的数据库密钥>`（自适应 hook） | ✅ 同密钥 |
| Pull / 解密 / 完整性校验 | ✅ 数 MB / 数 MB / 通过 | ✅ 同左 |
| CSV 46~47 表（数万行，随 App 版本浮动）+ csv_by_date（按有数据的每一天拆分） | ✅ | ✅ |
| JSON（十余 MB 量级）/ 类型化 SQLite | ✅ | ✅ |
| **退出码** | **0** | **0** |

### 7.3 实测发现（8 项）

1. **密钥跟随账号**（双平台同账号同密钥）；
2. **最新版方法名变更**：g()→getInstance()、b()→deCryptData()，自适应 hook 兼容新旧两版（已实测）；
3. **雷电 ADB 端口动态**，推荐 emulator-5554 序列号；
4. **MuMu 无 su**：adb root + su 垫片（OverlayFS 持久）；
5. "可写系统盘"开启时块设备显 ro 但写入成功（OverlayFS）；
6. **SELinux 必须 Permissive**：Enforcing 冻结且跨软重启持久，只能宿主硬杀恢复；
7. **禁止 adb reboot / am start -W**（永久挂起）；
8. 最新版 APK 无 x86_64 so（ARM 转译），原生 hook 不可行。

### 7.4 遗留事项与排查建议

| 事项 | 状态 | 建议 |
|---|---|---|
| Root 检测弹窗 | 可关闭，不阻塞导出 | 三检测点中和后仍弹，第四触发源疑似 KernelSU——排查思路见问题5 |
| 网页报告 | 已实测 ✅ | 按第五部分 5.4 使用 |

### 7.5 结论

**雷电 14（Android 14）：全新环境可移植性验证通过**——从空目录复制 → APK 安装 → 登录 → Frida 取密钥 → 解密 → 三格式导出 → 网页/拆分/校验，全部环节退出码 0、数据经 SQL 抽查确认。
**MuMu 12+（Android 15）**：本部分为历史环境的实测记录（见本部分开头注）；当前版本 MuMu 支持暂缓、未在最新代码上复测，生产使用以雷电 14 为准。

---

## 第八部分：占位符与待填项一览

> 本仓库以占位符形式提供以下可配置项，逐项说明含义、作用与填写方法。

| # | 占位符 | 出现位置 | 含义与作用 | 如何获取 / 填写 |
|---|---|---|---|---|
| 1 | `<工作目录>` | 全文命令 | 脚本所在目录（自定位，无需改代码） | 克隆仓库后的实际路径（示例 `D:\oppo-health-data-toolkit`） |
| 2 | `<序列号>` | adb 命令 | 目标模拟器设备 | 雷电：`127.0.0.1:5555` 或 `emulator-5554`；MuMu：`127.0.0.1:16384` |
| 3 | `<模拟器目录>` | 启动命令 | 模拟器安装目录 | 雷电常见默认 `C:\leidian\LDPlayer*`（随安装选择浮动）；不确定就填多开器里看到的路径，或设 `OPPO_EMULATOR_DIR` |
| 4 | `CHANGE_ME_TOKEN` | upload_data.py / server\upload_server.py / 手册 | 上传链路鉴权（两端一致才可通过） | 推荐设环境变量 `OPPO_HEALTH_TOKEN`（两端共读，默认即此占位值，改环境变量无需改源码）；或在 `upload_data.py` 与 `server\upload_server.py` 中，将两处 Token 常量改为相同的新值 |
| 5 | `0000…0db_key`（32 个零 + "db_key" 后缀共 38 字符的占位密钥，在 export_health_data.py 内） | Frida 失败时的回退候选密钥 | 公开仓库占位值，经实测无法解密正常登录账号的库 | **无需填写**：正常流程由 Frida 自动实取（结果见 `DB\combined\db_key.txt`）；如确需回退，填你账号的密钥 |
| 6 | `<你的数据库密钥>` | 第七部分 7.2 节逐环节验证结果表 | 导出运行时实取密钥的示例位 | 自动生成，无需手填 |
| 7 | `OPPO_DATA_DATE=YYYY-MM-DD` | 网页报告 | 锚定要分析的历史日期 | 填你要分析的那天（如数据截止昨天则填昨天） |
| 8 | `YYYYMMDD_HHMMSS` / `YYYYMMDD\<表>.csv` | 产物命名 | 时间戳/日期模板 | 自动生成 |
| 9 | MIT | LICENSE 文件 | 允许任何人自由使用、复制、修改、再分发、**商用**；唯一义务是保留版权声明 | 无需填写；完整许可文本见 `LICENSE`，说明见下方附注 |
| 10 | `OPPO_HEALTH_URL` | upload_data.py（环境变量覆盖） | 上传服务地址（客户端指向） | 默认 `http://127.0.0.1:5000/api/upload`；指向其他主机时设该变量 |
| 11 | `OPPO_HEALTH_HOST` | server\upload_server.py | 服务端监听地址 | 默认 `127.0.0.1`（仅本机）；设为**任何非回环地址**（`0.0.0.0` 或局域网 IP）对外开放时，必须同时设强 `OPPO_HEALTH_TOKEN`（否则拒绝启动） |
| 12 | `OPPO_HEALTH_PORT` | server\upload_server.py | 服务端监听端口 | 默认 `5000`；端口被占用时改设其他值，客户端 `OPPO_HEALTH_URL` 同步修改 |

### 附注：关于 MIT 许可证（对应上表第 9 项）

**MIT** 是最宽松、最通用的开源许可证：任何人可以**自由使用、复制、修改、再分发、商用**，唯一义务是**在副本中保留原作者的版权声明与许可文本**；作者不承担任何担保责任。

对你的影响：
- **你自己**：完全自由——使用、修改、分享，包括商用，无任何额外义务（除保留版权声明）；
- **别人**：同样自由；如果别人拿它做出商业产品，你分不到收益，但也不承担任何责任——对个人工具项目，这通常是好事（传播最大化、纠纷最小化）；
- **合规义务**：LICENSE 文件已包含上游 [foxlesbiao/oppo-health-export](https://github.com/foxlesbiao/oppo-health-export)（MIT）的版权声明保留段——因为本项目 MCP server 部分基于其 MIT 代码改编，MIT 要求衍生品保留其声明；
- **注意**：OPPO 健康 APK 版权归其权利人（未随仓库分发），不在 MIT 覆盖范围内（README 已注明）。

完整许可文本见 `LICENSE`。

---

## 第九部分：把导出数据交给其他 AI 分析（不依赖 WorkBuddy 的路径）

> 适用场景：①你没有 WorkBuddy 这类**能直接跑脚本**的 AI Agent（或不想安装）；②你已有导出数据，只想交给通用 AI（ChatGPT / Claude / Gemini / DeepSeek / Kimi / 豆包 / 通义等）帮忙解读；③你觉得自带网页报告的角度不够，想按自己的目标（减脂 / 增肌 / 睡眠改善 / 运动表现 / 就医准备）换角度分析。

### 9.1 先决定"投喂什么"：三种方式

| 方式 | 给 AI 的文件 | 适合 | 体积与注意 |
|---|---|---|---|
| **A. 轻量问答**（隐私最优） | 手抄/复制的关键数值：近 7 天睡眠均值、日均步数、静息心率、最低血氧… | 只问几个具体问题 | 最小；不含任何身份列 |
| **B. 表格分析**（推荐） | `DB\combined\csv\` 下按需要挑表，例如 `DBSleepDataStatTable.csv`、`DBSportDataStat.csv`、`DBHeartRateDataStatTable.csv`、`DBStressDataStatTable.csv`、`DBBloodOxygenSaturationDataStat.csv` | 让 AI 做统计、趋势、图表 | 每表几百 KB～1 MB；**CSV 含 ssoid/open_id 等身份列，投喂前必须删列**（见 9.2） |
| **C. 全量分析** | `DB\combined\health_data.json`（十余 MB 量级）或 `database_decrypted.db` | 想让 AI 全面、自由地分析 | JSON 常超聊天窗口附件上限 → 用 `python split_db_by_day.py` 按天拆分后**分段**投喂；SQLite 适合让 AI 写 SQL（见模板 3） |

### 9.2 投喂前必做：隐私处理（重要）

健康数据属敏感个人信息，**先本地脱敏再上传**：

1. **删除身份列**（各表通用）：`ssoid`、`open_id`、`sub_account`、`device_unique_id`、`sn`、`user_tag_id`、`old_user_tag_id`、`metadata`、`residence`、`occupation` 等——这些列对分析无用，却直接暴露你是谁、用什么设备。
   一行 Python 批量处理（把 `你的导出目录` 换成实际路径）：
   ```python
   import pandas as pd, glob, os
   # 与项目 utils.IDENTITY_COLUMNS 保持一致（共 10 列，含 residence/occupation）
   DROP = {"ssoid","open_id","sub_account","device_unique_id","sn","user_tag_id",
           "old_user_tag_id","residence","occupation","metadata"}
   for f in glob.glob(r"你的导出目录\csv\*.csv"):
       df = pd.read_csv(f, dtype=str)
       df = df.drop(columns=[c for c in df.columns if c in DROP], errors="ignore")
       df.to_csv(f.replace(".csv", "_clean.csv"), index=False, encoding="utf-8-sig")
       print("已脱敏:", os.path.basename(f))
   ```
   （没装 pandas 也可用 Excel 手动删列；或用 `csv` 标准库逐列过滤。）

   > ⚠️ **口径注意**：上例是**删除整列**，适用于"整理后发给第三方 AI / 他人"的场景。而代码内置的 `utils.scrub_identity_columns()`（上传 / MCP 通道所用）是**只清值、保留列**——两通道的 row_hash 依赖列序一致，幂等去重才生效。若你**手动删列后**再经 `upload_data.py` 上传，同一行会与 MCP 通道算出**不同**哈希，导致重复入库。两个场景两种做法，勿混用。
2. **只发需要的表与时间范围**：建议近 30～90 天，够看趋势且体积小。
3. **绝不上传**：`db_key.txt`（数据库密钥）、`database_encrypted.db`（加密原库）。
4. **看平台政策**：优先选"不用于模型训练"的选项（部分平台需企业版/API 才有）；不确定时用方式 A。

### 9.3 提示词模板（可直接复制，按需改）

**模板 1 · 整体健康分析（附 5 张 CSV）**
```
你是我的健康数据分析助手。附件是我从 OPPO 健康导出的近 N 天数据，共 5 张 CSV：
· DBSleepDataStatTable = 每日睡眠（total_sleep_time 总睡眠分钟、total_deep_sleep_time 深睡、total_rem_time REM、sleep_score 评分、fall_asleep 入睡、sleep_out 起床）
· DBSportDataStat = 每日运动（需先筛 sport_mode = -2 的日汇总行；total_steps 步数、total_distance 距离(米)、total_calories 卡路里(÷1000=千卡)、total_duration 活动时长(毫秒)）
· DBHeartRateDataStatTable = 每日心率（min_hr/average_hr/max_hr/rest_hr，单位 bpm）
· DBStressDataStatTable = 每日压力（0-100）
· DBBloodOxygenSaturationDataStat = 每日血氧（%）
字段口径：date 均为 YYYYMMDD；睡眠时长单位=分钟；0 或空值表示当天未佩戴/无监测，不是 0 值本身。
请按以下结构输出：
① 数据概况（覆盖天数、有效天数、缺失情况；先复述一遍确认你读对了）
② 五个维度各自的均值 / 趋势 / 异常值
③ 综合评分（0-100）与打分理由
④ 最需要关注的 3 个问题 + 可执行的改善建议（具体到每天做什么）
⑤ 还需要哪些数据才能判断更准
约束：不得编造缺失数据；结论要带上对应数值，便于复核。
```

**模板 2 · 单维度深挖（示例：睡眠）**
```
角色：你是睡眠医学与行为干预方向的分析师。
目标：用附件数据帮我找出睡眠变差的原因并给出 4 周改善计划。
数据：DBSleepDataStatTable（口径同模板 1）+ DBSportDataStat（看晚间运动是否影响入睡）。
输出：① 我的睡眠基线画像（时长/效率/规律性）② 影响最可能的 3 个因素（用数据支撑）③ 4 周计划（每周只改 1 件事）④ 判断改善成功的指标。
```

**模板 3 · 让 AI 写 SQL（附 .db 或表结构）**
```
你是 SQLite 专家。附件是健康数据库（或见下表结构）。请写出并解释 SQL：
目标问题：最近 30 天平均睡眠时长、深睡占比、静息心率趋势、日均步数。
约束：只写 SELECT 只读查询，不要任何修改语句；日期列 date 为 YYYYMMDD 整数；运动表取 sport_mode = -2 的日汇总行；表名/列名区分大小写。
输出：SQL 语句 + 每句的作用说明 + 预期结果长什么样。
```

**模板 4 · 零文件纯数值（隐私最优）**
```
你是健康顾问。以下是我最近 7 天的数据（表格式，可空值）：日期 | 睡眠(分) | 深睡(分) | 静息心率 | 步数 | 压力 | 最低血氧
请判断哪些指标偏离正常范围、优先改善哪一项、给我 3 条今天就能做的具体动作。
（附：我的年龄/性别/主要目标：____）
```

### 9.4 三条经验（决定分析质量）

1. **一定要先给"字段字典"**：AI 不认识 OPPO 的列名与单位（毫千卡、毫秒、0=无监测）。不给口径，结果必错——直接把 9.3 模板 1 的口径段或本手册第三部分粘给它。
2. **让它先复述数据概况**：一旦它读错文件/单位，复述环节立刻暴露，比事后核对 30 个数字省事。
3. **结论必须带数值、可复核**：让它把"结论 ↔ 对应数值"列出来，你用 Excel/`sqlite3` 一查即知——这与本项目网页报告坚持的"数值可复核"原则一致。

### 9.5 与自带网页报告怎么分工

| | 网页报告（`generate_html_report.py`） | 通用 AI |
|---|---|---|
| 强项 | 标准化评分、四个时间窗口横向对比、图表、长期趋势、离线可看 | 自由提问、多轮追问、跨领域建议（饮食/训练/就医）、按你的目标定制 |
| 弱项 | 角度固定，不能追问 | 可能算错/编造，需你复核 |
| 建议 | 先看网页报告拿"客观面"（哪些指标在变差） | 再拿 AI 做"个性化解读与行动方案"，并用数值复核 |

> 如果 AI 平台不支持附件：把表格拆成若干块**分块粘贴**（每块标注表名与列头）；或让它**先给你一段本地 Python 脚本**，你在自己电脑上跑出结果（数据不出本机，隐私最优）。

### 9.6 把「整个任务」交给其他 AI 时怎么描述（避免它编造数据）

9.1–9.5 讲的是"**把已导出的数据**投喂给 AI 做分析"。本节讲另一种更常见、也更容易翻车的场景：
**你还没导出，直接让另一个 AI（Claude / Codex / 各类 Agent）替你跑完整流程并生成网页报告。**

**⚠️ 一句话说错，AI 就会去做"手册介绍页"，并为了填满版面而编造你的健康数据。**

**实测踩坑**：曾用如下描述交给某 AI——

> ❌ 「请依据手册与 README 的内容与流程进行操作……并生成一个结构清晰、内容完整的网页，
> **用于展示该手册的核心说明、数据导出流程与分析方法**，确保页面信息准确。」

结果：AI 交付了一个标题为《OPPO 健康数据导出与分析手册 · 网页版》的页面——
它忠实执行了字面要求（**把手册做成网页**），并自行编造了示例健康数值来充实版面。

**问题出在哪**：整句的宾语是「**该手册**」而不是「**你的数据**」。AI 没有任何理由去做你的数据报告。

| | 错误说法 | 正确说法 |
|---|---|---|
| 目标 | 生成网页**展示该手册的**核心说明与分析方法 | 生成网页**展示我的健康数据**分析结果 |
| 动作 | 依据文档内容与流程进行**操作** | **执行导出流程**：启动模拟器 → 连 ADB → 跑 `auto_export_and_analyze.py` |
| 手段 | （未提） | **运行 `generate_html_report.py`** 产出报告，不要自己另写生成脚本 |
| 约束 | （未提） | 数值必须来自真实数据库，**禁止编造/占位**；导出失败就如实报告，不许凑数据 |

**可直接复制的完整提示词**见仓库内的 **`AI执行提示词.md`**（独立文档，替换 2 处占位符后即可整段丢给 AI；内含「完整流程」与「每日增量」两段提示词）。

> **自查口诀**：说清楚三件事——① **执行什么**（导出我的数据）② **用什么命令**（`auto_export_and_analyze.py` / `generate_html_report.py`）③ **页面上的数字从哪来**（真实数据库，禁止编造）。

---

## 第十部分：模拟器闪退专项排查与解决方案

> 本节专门解决「模拟器启动或运行过程中闪退 / 一闪就没 / 跑着跑着消失」的问题。
> 文中所有命令与结论均在雷电 14 / Android 14 / Windows 11 环境实测验证过。
> 若你只想看结论：**先做步骤 0 判定类型，90% 的自动化场景闪退直接跳到步骤 1**。

### 10.1 典型现象与触发条件

| 编号 | 现象 | 触发条件 | 一眼判别要点 |
|------|------|---------|-------------|
| 现象 A | 双击/命令行启动后窗口一闪而过，进程消失 | 手动启动，或上次异常退出后重启 | `dnplayer.exe` 与 `Ld9BoxHeadless.exe` 都没起来 |
| 现象 B | 窗口正常出现，几秒到一分钟后自己退出 | 开机过程中、或刚进桌面 | 曾出现 `Ld9BoxHeadless.exe`，随后消失 |
| 现象 C | **在 AI 助手 / 脚本里启动一切正常，但那条命令一执行完模拟器就没了** | 由父进程（AI 助手、CI、批处理）拉起 | 手动双击启动完全正常，**只有被脚本拉起才闪退** |
| 现象 D | 开机进度条卡在 90%~99% 后崩溃 | 冷启动、或刚改过配置 | 卡住时内存占用持续走高 |
| 现象 E | 运行中途闪退，常见于拉数据库 / Frida attach 时 | 导出流程执行到某一步 | 退出前内存或 CPU 打满 |

> 现象 C 在自动化场景中最为常见，也是**最容易被误判为"模拟器坏了"**的一种
> ——手动启动明明没问题，于是反复折腾模拟器设置，其实根因在调用方。

### 10.2 可能原因（按可能性由高到低）

| 排名 | 原因 | 典型特征 | 如何确认 |
|------|------|---------|---------|
| 1 | **父进程 / 调用方在命令结束时回收整棵子进程树** | 只有被脚本、AI 助手、CI 拉起时才闪退；手动启动正常 | 现象 C。用 10.3 步骤 0 的心跳法 30 秒即可判定 |
| 2 | 分配给模拟器的内存 / CPU 不足 | 现象 B、D、E；退出前卡顿 | 雷电设置里看分配值；任务管理器看内存是否打满 |
| 3 | 显卡渲染模式不兼容（OpenGL / DirectX） | 冷启动即崩，或与分辨率/帧率调整同时出现 | 切换渲染模式后是否恢复 |
| 4 | Hyper-V / 内核隔离（内存完整性）与 VirtualBox 冲突 | 现象 A、D；新装系统或开启过 WSL 后首次启动 | `bcdedit` 查看 `hypervisorlaunchtype` |
| 5 | 上次异常退出留下僵尸进程或锁文件 | 现象 A；杀进程后重启即可恢复 | 存在残留 `dnplayer` / `Ld9Box*` 进程 |
| 6 | 杀毒软件隔离了 `dnplayer.exe` 或虚拟机文件 | 现象 A；某次突然开始，之前一直正常 | 杀软隔离区有记录；加白名单后恢复 |
| 7 | 磁盘空间不足 | 现象 D、E | 系统盘剩余空间 < 5GB |
| 8 | 雷电版本与系统不兼容 / 版本过旧 | 现象 A；升级系统后首次使用 | 官网对比版本号 |
| 9 | **受管环境安全策略拦截 WMI / 任务计划** | 现象 C；调用 WMI 时报「WMI/CIM process creation is equivalent to Start-Process」，或 `schtasks` 报「PROGRAM BLOCKED BY SECURITY POLICY」 | 同一脚本在其他非受管机器上可正常拉起；手动双击启动正常 |

### 10.3 逐步排查与处置

#### 步骤 0：先判定属于哪一类（30 秒）

**核心判别命令——看 `Ld9BoxHeadless.exe` 在不在**：

```cmd
tasklist | findstr /I "dnplayer Ld9BoxHeadless"
```

- `Ld9BoxHeadless.exe` **才是真正跑 Android 的虚拟机进程**（正常占 200MB+ 内存）。
- 只有 `dnplayer.exe` 而没有它 ＝ 虚拟机没起来，ADB 必然连不上。
- 两个都在 ＝ 模拟器健康，请转查 ADB / Frida（见第四部分问题 1、问题 3）。

**若怀疑是现象 C，用心跳法确认**（起一个常驻进程，看命令结束后是否活着）：

做法：起一个**会活 5 分钟的探针进程**，然后**另起一条命令**看它还在不在。
这里用 `ping`（零依赖，任何 Windows 都有）当探针：

```powershell
# 第 1 条命令：起一个约 5 分钟后才结束的探针进程
Start-Process -FilePath "ping.exe" -ArgumentList "-n","300","127.0.0.1"
```

```powershell
# 第 2 条命令：务必「另起一次」执行，不要接在上面那条后面
Get-Process ping -ErrorAction SilentlyContinue
```

判据：

| 结果 | 结论 | 下一步 |
|------|------|--------|
| 列不出 `ping` 进程 | **确认是原因 1（进程被回收）** | 直接跳到步骤 1 |
| 能列出 `ping` 进程 | 进程可以跨命令存活，问题不在回收 | 转步骤 2 及以后 |

> 关键点：第 2 条命令必须**单独执行一次**。
> 如果把「启动」和「检查」写在同一条命令里，永远看不出进程是否被回收。

#### 步骤 1：自动化场景闪退 —— 改用 WMI 启动（最高发，实测有效）

**原因**：这类环境会把拉起的子进程纳入统一的进程管理（Windows Job Object），
父命令一结束就整树终止。模拟器本身没问题，是被"连坐"了。

> 实测对照（同一环境三种方式各起一个常驻进程）：

| 启动方式 | 命令结束后的存活情况 |
|---------|-------------------|
| `Start-Process` | **6 秒内被终止** |
| `cmd /c start ""` | 同样被终止 |
| 任务计划 `schtasks /run` | 未能拉起（部分受管环境中 schtasks 会被安全策略拦截——**schtasks 可用性随环境而异**，另一类受管环境可正常使用，见下方备选一；被拦截时走备选二/三） |
| **WMI `Win32_Process.Create`** | **存活** ✅ |

**处置：用 WMI 启动**（进程由 WMI 服务 `winmgmt` 创建，不在被回收的进程树里）：

```powershell
$ld = "<模拟器目录>"   # 改成你的雷电安装目录

Invoke-CimMethod -ClassName Win32_Process -MethodName Create `
  -Arguments @{ CommandLine="`"$ld\ldconsole.exe`" launch --index 0";
                CurrentDirectory=$ld }
```

返回 `ReturnValue = 0` 即启动成功。随后照常连接：

```cmd
<工作目录>\platform\adb.exe connect 127.0.0.1:5555
```

**备选一（WMI 被安全策略拦截时）**：改用**任务计划程序**启动——进程由计划任务服务拉起，
不挂在当前命令行下，同样能脱离进程树：

> ⚠️ **环境差异说明**：`schtasks` 自身也可能被某些受管环境拦截
> （报 `PROGRAM BLOCKED BY SECURITY POLICY`，见 10.2 原因 9）。因此上文实测对照表中
> "schtasks 未能拉起"与这里的"实测可用"**并不矛盾——是两个不同受管环境的策略差异**。
> 判定方法很简单：跑一次下方命令，被拦截即改走备选二。

```powershell
$ld = "<模拟器目录>"
schtasks /create /tn "OPPO_LDLaunch" /tr "`"$ld\ldconsole.exe`" launch --index 0" /sc ONCE /st 23:59 /f
schtasks /run /tn "OPPO_LDLaunch"
schtasks /delete /tn "OPPO_LDLaunch" /f     # 立即删任务定义，已拉起的进程不受影响
```

验证是否生效（**务必换一条新命令 / 新窗口执行**）：

```powershell
Get-Process -Name dnplayer,Ld9BoxHeadless -ErrorAction SilentlyContinue
```

> 实测：`LastTaskResult=0`，换调用后 `dnplayer.exe` + `Ld9BoxHeadless.exe` 均存活。
> 注意某些受管环境会直接拦截 WMI 创建进程（报"WMI/CIM process creation is equivalent to
> Start-Process"），此时本方法仍可用。

**备选二（以上都不可用时的退路）**：把「启动 → 等开机 → 连 ADB → 推 frida-server → 跑导出」
全部写进同一个脚本，**一次执行完**，不跨越命令边界：

```powershell
powershell -ExecutionPolicy Bypass -File run_all.ps1
```

**相关代码位置**（若需要让脚本自己找模拟器）：

| 位置 | 说明 |
|------|------|
| `config.py` → `_resolve_emulator()`（约 L64–L94） | 模拟器路径探测。优先读环境变量 `OPPO_EMULATOR_DIR`，否则在 `C:\` `D:\` `E:\` `F:\` 的常见安装根（含 `1\` `soft\` `tools\` `Program Files\`）下扫描 `LDPlayer*` |
| `config.py` → `CONFIG["adb_device"]` | ADB 序列号，可用环境变量 `ANDROID_SERIAL` 覆盖 |
| `auto_export_and_analyze.py` → 步骤 1（约 L209–L236） | 设备检测与 `adb connect` 失败回显（会打印 adb 原始输出，便于判断是端口问题还是模拟器没起来） |

装在非标准位置时，直接指定最省事：

```cmd
set OPPO_EMULATOR_DIR=<模拟器目录>
```

#### 步骤 1b：WMI / 任务计划也被受管环境拦截时的兜底

如果 步骤 1 的 WMI 和 schtasks 均被安全策略拦截（分别报「WMI/CIM process creation is equivalent to Start-Process」或「PROGRAM BLOCKED BY SECURITY POLICY」），只剩最后一条路：**把「启动模拟器 → 等开机 → 连 ADB → 推/起 frida-server → 跑导出 → 生成报告」整条链路写进同一次调用**，让父进程在导出完成前一直活着，从而撑住模拟器进程。

本质：既然环境不允许「启动」与「使用」分两次调用，就不分两次。

**最小可运行示例**（保存为 `run_all.ps1`，与你的项目目录同级）：

```powershell
$ErrorActionPreference = "Stop"
$workDir  = "<工作目录>"          # 改成你的实际工作目录
$leiDian  = "<模拟器目录>"        # 改成你的雷电安装目录（常见默认 C:\leidian\LDPlayer14，自定义安装则位置不同；见 1.2 节 OPPO_EMULATOR_DIR）
$adb      = "$workDir\platform\adb.exe"

Set-Location $leiDian
.\ldconsole.exe launch --index 0
Start-Sleep 120                    # 等模拟器完全开机

Set-Location $workDir
if (-not (Test-Path "$workDir\dist\frida-server")) {
    python tools\rejoin.py
}

& $adb connect 127.0.0.1:5555
& $adb -s 127.0.0.1:5555 push $workDir\dist\frida-server /data/local/tmp/frida-server
& $adb -s 127.0.0.1:5555 shell "chmod 755 /data/local/tmp/frida-server"
& $adb -s 127.0.0.1:5555 shell "su -c '/data/local/tmp/frida-server >/dev/null 2>&1 </dev/null &'"
Start-Sleep 5

python auto_export_and_analyze.py
```

**判据**：

- 同一次 PowerShell 调用期间，`dnplayer.exe` + `Ld9BoxHeadless.exe` 持续存在；
- `auto_export_and_analyze.py` 返回码为 0；
- `DB\combined\database_decrypted.db` 生成且大小为若干 MB。

**注意**：

- 这个方案只能用于「一次性跑完全程」的验证或自动化，不适合「启动后保持常驻待机」；如需常驻，只能换到非受管环境。
- 导出完成后，命令结束，模拟器进程仍会被环境回收；这是预期现象，不是模拟器故障。

#### 步骤 2：内存与 CPU 资源不足

雷电默认配置可能偏低。建议 **CPU 4 核 / 内存 4096MB / 分辨率 720×1280 DPI 320**：

```cmd
"<模拟器目录>\ldconsole.exe" modify --index 0 --cpu 4 --memory 4096 --resolution 720,1280,320
```

也可以在雷电界面改：**设置 → 性能设置 → CPU / 内存**，改完**必须重启模拟器**才生效。

> 参考：正常运行时的实测占用为 `dnplayer.exe` 约 150MB、`Ld9BoxHeadless.exe` 约 250MB。
> 若宿主机可用内存低于 8GB，不建议把模拟器内存调到 4096MB 以上。

#### 步骤 3：显卡渲染设置不当

**雷电界面**：设置 → 性能设置 → **渲染模式**，在 `OpenGL+` 与 `DirectX` 之间切换试一次；
同时把**帧率**从 60 降到 30，分辨率调低。

独显笔记本还需确认雷电用的是独显：**Windows 设置 → 系统 → 显示 → 图形 → 添加 `dnplayer.exe` → 选项选「高性能」**。

#### 步骤 4：Hyper-V / 内核隔离冲突

查看当前状态：

```cmd
bcdedit /enum | findstr /I "hypervisorlaunchtype"
```

若显示 `Auto` 或 `On` 且模拟器起不来，可关闭后重启电脑：

```cmd
bcdedit /set hypervisorlaunchtype off
shutdown /r /t 0
```

同时检查：**Windows 安全中心 → 设备安全性 → 内核隔离 → 内存完整性**，临时关闭后重启再试。

> 注意：关闭 Hyper-V 会影响 WSL2 / Docker Desktop。如果日常要用它们，
> 优先试步骤 2、3，把步骤 4 留作最后的验证手段，确认是此原因后再决定是否长期关闭。

#### 步骤 5：僵尸进程与锁文件残留

```powershell
Get-Process -Name dnplayer,Ld9BoxHeadless,Ld9BoxSVC -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 3
# 确认已清空
tasklist | findstr /I "dnplayer Ld9Box"
```

清干净后按步骤 1 的 WMI 方式重新启动。

#### 步骤 6：杀毒软件拦截

把以下两项加入杀软白名单（Windows 安全中心 → 病毒和威胁防护 → 管理设置 → 排除项 → 添加）：

- 雷电安装目录：`<模拟器目录>\`
- 项目目录（含 `platform\adb.exe` 与 `dist\frida-server`）：`<工作目录>\`

> `frida-server` 常被误判为木马而被隔离，表现为 frida 突然连不上或启动即死。

#### 步骤 7：磁盘空间不足

```cmd
wmic logicaldisk get caption,freespace,size
```

系统盘（通常是 C 盘）建议保留 **5GB 以上**。模拟器运行时需要写临时页文件，空间不足会直接崩溃。

#### 步骤 8：雷电版本与系统兼容性

确认雷电版本 ≥ 14，Android 版本为 14（本项目的验证基线）。
版本过旧时到官网下载最新版，或新建一个模拟器实例（多开器 → 新建）对比测试，
以区分是"版本问题"还是"这个实例的配置坏了"。

### 10.4 验证问题是否解决

#### 10.4.1 进程级判别（必要条件）

```cmd
tasklist | findstr /I "dnplayer Ld9BoxHeadless"
```

期望：**两个进程都在**，且 `Ld9BoxHeadless.exe` 内存 ≥ 200MB。

#### 10.4.2 ADB 连通性

```cmd
<工作目录>\platform\adb.exe devices
<工作目录>\platform\adb.exe -s 127.0.0.1:5555 shell getprop sys.boot_completed
```

期望：`devices` 列出 `127.0.0.1:5555 device`（通常还会有 `emulator-5554`），
`boot_completed` 返回 `1`。

#### 10.4.3 稳定性观察（针对现象 C，关键）

**必须换一条新的命令窗口 / 新的工具调用**去检查，而不是在启动命令里顺便查——
否则看不出进程是否被回收。

```cmd
"<模拟器目录>\ldconsole.exe" list2
```

期望：输出形如 `0,雷电模拟器,3015984,1116346,1,19548,17596,900,1600,320`，
其中**第 5 个字段为 `1`**（运行中），且后面两个 PID 非零。

建议再等 3 分钟复测一次，确认不是"暂时活着"。

#### 10.4.4 端到端验收

```cmd
cd /d <工作目录>
python auto_export_and_analyze.py
```

期望：全程无报错，结尾出现 `🎉 自动导出与分析完成！`，退出码 `0`。
正常产出参考（大小随账号数据量浮动）：`DB\combined\` 下 `database_decrypted.db` 约 10MB、`health_data.json` 约 16MB、
分析报告 `.md` 一份。

### 10.5 避免复发的注意事项

1. **自动化场景一律用 WMI 启动**，不要用 `Start-Process`、`start ""` 或 `cmd /c`。
   这是实测中唯一可靠的方式，其余方式都会在命令结束时被回收。
2. **每次跑流程前先看 `Ld9BoxHeadless.exe`**，不要只看 `dnplayer.exe`。
   只有窗口进程在 ＝ 虚拟机没起来，一定会失败。
3. **把整条链路放进一个脚本一次跑完**，减少跨命令边界的次数；
   万一 WMI 也不可用，这是最有效的退路。
4. **关闭模拟器用 `ldconsole quit`，不要直接杀进程**，避免留下僵尸进程与锁文件：
   ```cmd
   "<模拟器目录>\ldconsole.exe" quit --index 0
   ```
5. **改完 CPU / 内存 / 渲染 / 分辨率必须重启模拟器**才生效，改了不重启等于没改。
6. **把内存与渲染配置固定下来并单独记在一个备忘文件里**，换电脑或重装后照抄即可，
   不要每次都从默认值重新试。
7. **给杀软加白名单要包含 `dist\frida-server`**，它是被误杀的高频目标。
8. **保留一份"健康基线"**：正常状态下记录 `list2` 输出、`adb devices` 结果、
   导出产物大小。出问题时跟基线一比，能立刻定位是模拟器、ADB 还是解密环节。
9. **不要同时开多个模拟器实例**，端口冲突与资源竞争都会表现为随机闪退。
10. **模拟器长期不用时正常退出**，避免系统休眠/睡眠唤醒后虚拟机状态错乱。

## 附录A：输出报告摘要模板（脚本成功后输出）

```
✅ 导出成功！
📁 导出目录：<工作目录>\DB\combined\
📄 生成文件：database_encrypted.db / database_decrypted.db / health_data.json /
            oppo_health_full.db / csv\(46~47) / csv_by_date\(天数随数据跨度)
📊 核心数据：综合评分 / 睡眠 / 步数 / 静息心率 / HRV / 血氧
📝 分析报告：analysis_report_enhanced_*.md
```

## 附录B：换环境速查清单（换电脑/重装系统）

| 类别 | 清单 |
|---|---|
| 必装 | 雷电 14 或 MuMu、Python 3.10+、Navicat（可选）；ADB 无需另装（platform-tools 精简版已随仓库 platform\ 目录分发） |
| Python 库 | `pip install -r requirements.txt` |
| 必备文件 | 工作目录整套脚本 + frida-server（`dist\` 分卷还原）+ OPPO 健康 APK（自备，来源见 1.4 节）+ 本手册 |
| 配置步骤 | 模拟器开 Root+ADB → 装 App 登录等同步 → 连 ADB → 启动 frida-server → （MuMu：adb root + su 垫片）→ `python export_health_data.py` |

## 附录C：自动化自检

- `python test_auto_export.py` → **74 个测试 0 失败 0 错误**（含 18 条边界/异常输入用例；**可选**——日常导出无需运行，改代码后自检用）；
- `python data_validation.py` → 0 ERROR（自动优先校验 DB\combined）；

**GUI 人工冒烟清单**（`health_export_gui.py` 无法自动化——需在有 tkinter 的机器手工跑一遍，约 3 分钟）：

| # | 步骤 | 预期 |
|---|---|---|
| 1 | `python health_export_gui.py` 打开界面 | 窗口正常显示，无报错 |
| 2 | 点「浏览」选 `DB\combined\database_decrypted.db` → 「加载」 | 显示表数量/总行数/时间范围，表列表勾选框全选 |
| 3 | 时间范围选「自定义」，开始日期填 `2026-9-1`（非法格式） | 弹窗提示"格式应为 8 位数字 YYYYMMDD"，**不进入导出** |
| 4 | 时间范围选「最近30天」，格式选 CSV → 「开始导出」 | 进度条推进、日志逐表输出、结束弹"导出完成" |
| 5 | 格式改 SQLite / JSON 各导出一次 | 生成 `.db` / `.json`；SQLite 日志提示"各列均为 TEXT 类型" |
| 6 | 导出期间点窗口关闭按钮 | 无崩溃（后台线程安全退出） |

## 附录D：已知边界（勿误判为 bug）

- 跨天/设备滞后导致评分变化 = 动态完整日窗口的**预期行为**（今日不完整数据不参与均值）。
- `DBSpaceInfo` 表无可信时间列，sink 库中其 `__t` 为 NULL 属预期。
- 上传每表最多 500 行（按时间列倒序取**最新**），超出打印截断警告；需要全量可调整 LIMIT。
  → 因此 **sink 库行数通常明显少于本地源库**（实测 sink 库为数十表、数千行量级，明显少于源库），属**设计而非数据丢失**；上传与 MCP 链路定位是"给 AI 提供近期快照"，不是全量备份。
- `health_export_gui.py` 已验证代码可编译、依赖可解析，**未做界面级运行验证**；使用前建议按附录C 的「GUI 人工冒烟清单」跑一遍。
- `DBSleepDataStatTable.date` 语义为**起床日**；"昨晚"= date=今天的记录；小睡分钟计入"浅睡"。
- 打鼾明细全 0 + AHI 近恒定时，OSA 维度自动判定为不可靠并排除出综合评分（保守策略）。
- 生成器默认 DATA_END=本机当天：当天未导出新数据时"昨天"窗口为 0 条并显示琥珀色提示条（属预期，可设 OPPO_DATA_DATE 锚定历史日）。

## 附录E：环境维护守则

**1. 纯净初始态**：目录只应包含代码与脚本、文档、`dist\` 分卷与 `platform\` 工具（即克隆后的原始状态）；`server\sink\` 由服务端首次运行自动创建。以下均为**可再生数据**，测试后可清理：`DB\` 导出目录、`OPPO健康数据分析报告.html`（工作目录根与数据源目录双写）、`analysis_report_enhanced_*.md`、`oppo_health_full.db`、`server\sink\health.db*`、`__pycache__`、运行日志、`dist\frida-server`（分卷还原产物，`tools\rejoin.py` 一键再生）。
**2. 不可删除项**：全部代码与脚本、本手册。
**3. 测试后复位**：先停止 upload_server 进程，再删 `server\sink\health.db*`，服务下次启动自动重建空库。

## 附录F：版本说明

- **V1.1（当前版本）**：面向公开发布的修订版——上传链路含安全守卫（对外开放监听时强制强 Token、启动时提醒更换公开占位 Token、扫描可疑身份列），并新增上传服务端口环境变量 `OPPO_HEALTH_PORT`。功能与用法以本文档为准。
- **V1.0（首个正式发布版）**：项目定名 **oppo-health-data-toolkit**，包含完整链路：数据库解密导出（CSV / JSON / 类型化 SQLite 三格式）、12 维分析报告、网页报告、按天拆分、上传与 MCP 接口，以及"把导出数据交给其他 AI 分析"的指引（第九部分）。

---

*本文档为详细操作手册，与 `README.md`（项目概览/快速开始）和 `AI执行提示词.md`（交给 AI 代跑）配套使用；个人数据（数据库/密钥/报告/日志）未随仓库分发。*
*最后更新：2026-09-16*
