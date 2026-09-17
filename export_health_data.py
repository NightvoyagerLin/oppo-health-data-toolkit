# SPDX-License-Identifier: MIT
"""
OPPO 健康数据一键导出工具
==========================
功能：自动完成 获取密钥 → pull数据库 → 解密 → 导出为SQLite + CSV

使用方法：
    python export_health_data.py

前提条件：
    1. 模拟器已启动，OPPO 健康已登录并有数据
    2. ADB 已连接模拟器
    3. Frida 服务端已在模拟器上运行
    4. Python 已安装 sqlcipher3 库
"""

import os
import sys

# 强制 UTF-8 输出，避免 Windows 控制台/重定向乱码（V1.1 修复）
from utils import ensure_utf8_stdout
ensure_utf8_stdout()

# 导入集中配置
try:
    from config import CONFIG
except ImportError:
    CONFIG = {"work_dir": os.path.dirname(os.path.abspath(__file__))}
import io
import json
import time
import subprocess
import sqlite3
from datetime import datetime
from utils import atomic_write, safe_fs_name, sanitize_adb_serial, smart_decode

# ==================== 配置区（从 config.py 读取） ====================
WORK_DIR = CONFIG.get("work_dir", os.path.dirname(os.path.abspath(__file__)))
#   与手册 5.2"改 config.py 即可"矛盾。现优先读配置，config 缺失时回退默认路径。
# 此处原本二次 `from config import CONFIG as _CFG`，
#   与文件头部的 CONFIG 导入重复（同一模块两次导入、两份引用并存），
#   后续维护者改一处会漏另一处。统一复用头部的 CONFIG。
ADB_PATH = CONFIG.get("adb_path") or os.path.join(WORK_DIR, "platform", "adb.exe")
# 模拟器同时存在 127.0.0.1:5555 与 emulator-5554 两个条目，
# 不指定 -s 时 adb 会报 "more than one device/emulator"，故显式指定设备。
ADB_DEVICE = sanitize_adb_serial(CONFIG.get("adb_device", os.environ.get("ANDROID_SERIAL", "127.0.0.1:5555")))
os.environ["ANDROID_SERIAL"] = ADB_DEVICE

# CSV / JSON 导出的分批大小。原实现整表 fetchall()
#   驻留内存，大库会 OOM；改为 fetchmany 流式写出后，内存占用由此常量控制。
CSV_BATCH = 5000
JSON_BATCH = 2000
# 相对化改造：frida_cli 由 config 自动探测（可能为 None），
#   探测不到时步骤4 直接走回退密钥，不再依赖任何硬编码绝对路径。
FRIDA_CLI = CONFIG.get("frida_cli")
PYTHON_PATH = CONFIG.get("python_path", sys.executable)

# 导出文件统一存放到 DB\combined\（合并全量库），每次导出覆盖同名旧文件；
# 「按天拆分」由 split_db_by_day.py 负责，产出 DB\YYYY-MM-DD\ 独立日库。
OUTPUT_DIR = os.path.join(WORK_DIR, "DB", "combined")

PACKAGE_NAME = CONFIG.get("package_name", "com.heytap.health")
# 该 ROM 没有 monkey 命令，必须用 am start 显式启动
LAUNCH_ACTIVITY = CONFIG.get("launch_activity", "com.heytap.health/.oobe.LaunchActivity")
# C-P2-6：包名统一走 PACKAGE_NAME（config 可改）——硬编码路径与 PACKAGE_NAME 漂移时
#   pull/ls 会静默失配（换渠道包/包名调整场景）。
DB_PATH_IN_DEVICE = f"/data/data/{PACKAGE_NAME}/databases/database.db"

# 公开**零占位**回退密钥：仅作 Frida 取钥失败时的最后候选（实测无法解密
# 正常登录账号的库，见手册"问题 3"）。**严禁把真实密钥填入本行**——
# 真实密钥只应存在于 db_key.txt（已 gitignore）或运行时内存中。
DEFAULT_DB_KEY = "00000000000000000000000000000000db_key"

# Frida 获取密钥的脚本（**仅作兜底**：正常情况以根目录 frida_get_dbkey.js 为准，见 load_frida_script）
_FRIDA_GET_KEY_SCRIPT_FALLBACK = r'''
Java.perform(function() {
    try {
        var AesGcmClass = Java.use("com.heytap.health.base.encrypt.AesGcmAndroidKeyStore");
        // 取单例：6.6.7 为 g()；最新版混淆后为 getInstance()（2026-09-11 实测适配）
        var instance = null;
        try { instance = AesGcmClass.g(); } catch (e) {}
        if (instance === null || instance === undefined) {
            instance = AesGcmClass.getInstance();
        }
        // 解密 db_key：6.6.7 为 b(alias, ssoid)；最新版为 deCryptData(alias, ssoid)
        var key = null;
        try { key = instance.b("db_key", null); } catch (e) {}
        if (key === null || key === undefined || key === "") {
            key = instance.deCryptData("db_key", null);
        }
        console.log("DB_KEY_START:" + key + ":DB_KEY_END");
    } catch(e) {
        console.log("DB_KEY_ERROR:" + e.message);
    }
});
'''

def load_frida_script():
    """取密钥 Frida 脚本的**唯一来源**：优先读根目录 frida_get_dbkey.js。

    主流程优先加载 frida_get_dbkey.js；文件缺失/不可读时才回退内嵌副本，
    并打印明确告警——改一处即全链路生效。
    """
    js_path = os.path.join(WORK_DIR, "frida_get_dbkey.js")
    try:
        with open(js_path, "r", encoding="utf-8") as f:
            src = f.read()
        if "DB_KEY_START" in src:
            return src
        print(f"  ⚠️  {js_path} 内容不含 DB_KEY_START 标记，疑似被改写，回退内嵌脚本")
    except Exception as e:
        print(f"  ⚠️  读取 {js_path} 失败（{e}），回退内嵌取密钥脚本")
    return _FRIDA_GET_KEY_SCRIPT_FALLBACK


# ==================== 工具函数 ====================
# SQL 表名统一走标识符转义（`"` 翻倍），
#   与 utils.DataIntegrityChecker._q_ident / json_to_sqlite.q_ident /
#   mcp_server._check_ident 同一标准。原实现用 f"FROM '{table}'" 单引号包裹，
#   表名虽来自 sqlite_master，但异常库/异常名字（含单引号）会破坏语句。
def _q_ident(name):
    """把表名/列名安全地包成 SQLite 标识符（剔除 NUL、双引号翻倍）。"""
    return '"' + str(name).replace('"', '""').replace("\x00", "") + '"'


def run_cmd(cmd, timeout=30):
    """执行命令并返回输出。
    原用 subprocess.run(shell=True)，超时只杀直接子进程
    cmd.exe——持有 stdout 管道的孙进程（如 frida-server 启动命令派生的 adb.exe）不被杀，
    超时后的第二次 communicate() 永久阻塞，实测整条导出链死锁 6.5 分钟零产出。
    现改为 Popen + 超时后 `taskkill /F /T` 杀整个进程树，任何情况下都不会悬挂。"""
    try:
        # 盲审 U-1：bytes 模式 + smart_decode——cmd.exe 的报错（如
        #   "'adb' 不是内部或外部命令"）是 GBK 字节，固定 UTF-8 解码会让
        #   提示变成一串问号；命令的正常输出又是 UTF-8。smart_decode 两类通吃。
        p = subprocess.Popen(
            cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except Exception as e:
        return f"ERROR: {str(e)}"
    try:
        out, err = p.communicate(timeout=timeout)
        return smart_decode(out) + smart_decode(err)
    except subprocess.TimeoutExpired:
        # 原无条件调用 Windows 专有 taskkill，Linux/macOS 上
        #   超时即抛 FileNotFoundError。现按平台选择终止方式（本工具主战场为 Windows）。
        try:
            if os.name == "nt":
                # Windows: /T 连同全部子进程（adb.exe 等）一起终止
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                               capture_output=True, timeout=10)
            else:
                p.kill()
        except Exception:
            pass
        try:
            out, err = p.communicate(timeout=5)
            return "TIMEOUT" + smart_decode(out) + smart_decode(err)
        except Exception:
            return "TIMEOUT"
    except Exception as e:
        return f"ERROR: {str(e)}"

def adb(cmd):
    """执行 ADB 命令（显式指定设备，避免多设备歧义）"""
    return run_cmd(f'"{ADB_PATH}" -s {ADB_DEVICE} {cmd}')

def print_step(step, msg):
    """打印步骤信息"""
    print(f"\n{'='*60}")
    print(f"  [{step}] {msg}")
    print(f"{'='*60}")

# ==================== 主流程 ====================
# C-P2-2：JSON 阶段有失败表时置位——main 仍返回 True 让报告照常生成，
#   __main__ 据此以退出码 2 收尾（0=完整成功 / 2=数据不完整 / 1=硬错误）。
_JSON_INCOMPLETE = False

def main():
    # 盲审 U-2：主入口接入配置自检——轻量（仅路径 exists 与阈值检查）、无交互，
    #   配置错误时早停（跑下去必然失败得更难看）。局部 import 避免拖慢测试导入。
    from config import validate_config
    _ok, _warns = validate_config()
    if not _ok:
        print("❌ 配置自检未通过，中止导出。先解决以下问题（或运行 python config.py 查看完整自检）：")
        for _w in _warns:
            print(f"  - {_w}")
        return False
    print("=" * 60)
    print("  OPPO 健康数据一键导出工具")
    print(f"  导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # 创建输出目录
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print(f"\n输出目录: {OUTPUT_DIR}")

    # JSON 导出失败的表清单（L7 修复：在 main 作用域提前初始化，
    #   保证后面无论走哪条分支都能安全引用，且最终据此决定退出码）
    json_failed_tables = []

    # ========== 步骤1: 检查 ADB 连接 ==========
    print_step(1, "检查 ADB 连接")
    output = adb("devices")
    print(output)
    # X1修复：原条件 `"emulator" not in output and "device" not in lines[1] if len>1 else True`
    #   是优先级混乱的三元表达式，且行内只要含 "emulator"/"device"/"127.0.0.1" 即算连接，
    #   offline/unauthorized 也会被误判为正常（与 auto_export main() 的 E4 同根因）。
    #   统一改为：仅当存在状态为 device 的行才判定可用。
    def _has_ready_device(lines):
        for l in lines:
            parts = l.strip().split()
            if len(parts) >= 2 and parts[-1] == "device":
                return True
        return False
    device_lines = [l for l in output.split("\n") if l.strip() and "List of devices" not in l]
    if not _has_ready_device(device_lines):
        #   用户看不到 "failed to connect to '127.0.0.1:5555'" 这类关键原因。现自动尝试 connect
        #   并把 adb 原始输出（stdout+stderr）回显，便于定位（端口错/模拟器未起/权限）。
        print("❌ 未检测到可用设备（需状态为 device），尝试自动 adb connect ...")
        # 盲审复审 L2：使用 ADB_DEVICE 变量而非硬编码——用户配置 MuMu 端口
        #   （127.0.0.1:16384）或 emulator-5554 时，硬编码会连错目标。
        print(adb(f"connect {ADB_DEVICE}"))
        # 重新枚举设备，连接成功则继续流程（不中断）
        output = adb("devices")
        print(output)
        device_lines = [l for l in output.split("\n") if l.strip() and "List of devices" not in l]
        if _has_ready_device(device_lines):
            print("✅ ADB 自动连接成功")
        else:
            print("   仍不可用，请确认模拟器已启动（雷电/MuMu）且 ADB 端口正确")
            return False
    print("✅ ADB 连接正常")

    # ========== 步骤2: 检查 Frida 服务端 ==========
    print_step(2, "检查 Frida 服务端")
    # ps|grep 检查在 adb 瞬时不稳时会返回空/TIMEOUT，
    #   被误判为"未运行"而误走启动分支；现用 pidof 交叉复核，两次确认未运行才启动。
    def _frida_running():
        out = adb('shell "ps -A | grep frida"')
        if "frida-server" in out:
            return True
        out2 = adb('shell "pidof frida-server"')
        return bool(out2 and out2.strip().strip('"').isdigit())
    if _frida_running():
        print("✅ Frida 服务端正在运行")
    else:
        print("⚠️  Frida 服务端未运行，正在启动...")
        # L1修复：frida-server 是常驻进程，输出不重定向会让 adb shell 永不返回
        #   （此前正是这里触发了 6.5 分钟全链死锁）。重定向到 /dev/null 后命令立即返回。
        adb('shell "su -c \'/data/local/tmp/frida-server >/dev/null 2>&1 &\'"')
        time.sleep(3)
        if _frida_running():
            print("✅ Frida 服务端启动成功")
        else:
            print("❌ Frida 服务端启动失败，请手动启动")
            return False

    # ========== 步骤3: 确保 OPPO 健康正在运行 ==========
    print_step(3, "确保 OPPO 健康正在运行")
    output = adb(f'shell "pidof {PACKAGE_NAME}"')
    if output.strip():
        print(f"✅ OPPO 健康正在运行 (PID: {output.strip()})")
    else:
        print("⚠️  OPPO 健康未运行，正在启动...")

        # 该 ROM 无 monkey，直接用 am start 启动 Launcher Activity
        # 注意：不要用 am start -W，-W 会一直阻塞到启动完成，App 起不来时永久挂住
        print(adb(f'shell "am start -n {LAUNCH_ACTIVITY}"'))
        time.sleep(10)
        output = adb(f'shell "pidof {PACKAGE_NAME}"')

        if output.strip():
            print(f"✅ OPPO 健康启动成功 (PID: {output.strip()})")
        else:
            # 不中断：数据库文件本身已在 /data/data 下，用已知密钥仍可解密
            print("⚠️  OPPO 健康无法启动（多为 Magisk Zygisk 导致进程 attach 超时）")
            print("   继续使用设备上已有的数据库文件 + 已知密钥解密")

    # ========== 步骤4: 用 Frida 获取数据库密钥 ==========
    print_step(4, "用 Frida 获取数据库密钥")
    
    # 写入临时 Frida 脚本
    frida_script_path = os.path.join(OUTPUT_DIR, "get_key.js")
    with atomic_write(frida_script_path, encoding="utf-8") as f:
        f.write(load_frida_script())
    
    # 获取 OPPO 健康 PID
    pid = adb(f'shell "pidof {PACKAGE_NAME}"').strip()
    print(f"OPPO 健康 PID: {pid}")

    db_key = None

    # 相对化改造配套：frida_cli 自动探测不到（None）时跳过 Frida，
    #   直接走已知回退密钥，流程不中断。
    if not FRIDA_CLI or not os.path.isfile(FRIDA_CLI):
        print("⚠️  未找到 frida.exe（探测顺序：环境变量 OPPO_FRIDA_CLI > PATH > 常见安装位置）")
        print("   跳过 Frida 取密钥，解密阶段使用内置回退密钥")
    # 没有 PID 就没法 attach，直接跳过 Frida，走已知密钥
    elif not pid or not pid.isdigit():
        print("⚠️  OPPO 健康未运行，跳过 Frida，解密阶段使用已知密钥")
    else:
        # 运行 Frida 获取密钥
        # deadline 15→40 秒，且未取到时重启 App 整体重试 1 次——
        #   全新环境实测 attach 冷启动可能超过 15 秒，一次失败即回退占位密钥会导致
        #   本次导出必然失败（解密"file is not a database"）。
        for _attempt in (1, 2):
            print(f"正在调用 Frida 获取密钥（第 {_attempt} 次尝试）...")

            # 用更简单的方式：直接运行 frida，然后读取输出
            proc = None  # C-P2-3：预绑定——Popen 本身抛异常时 finally 可安全判空
            try:
                import threading
                import queue

                # stdin 接 DEVNULL：frida CLI 在 stdin EOF 时会退出，避免 REPL 挂起
                # C-P2-5：-D <序列号> 替代 -U（USB）——本流程经 ADB（模拟器/TCP），
                #   ADB_DEVICE 已由 sanitize_adb_serial 白名单校验（L46），可安全入参。
                proc = subprocess.Popen(
                    [FRIDA_CLI, "-D", ADB_DEVICE, "-p", pid, "-l", frida_script_path],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    encoding='utf-8', errors='replace', stdin=subprocess.DEVNULL
                )

                # 后台读线程把输出行放入队列，主线程带 deadline
                #   取行，超时兜底 kill 进程并回退已知密钥，任何情况下都不会挂死。
                #   X9：stderr 并入 stdout——此前 stderr 管道从不
                #   读取，缓冲写满理论上会阻塞子进程。
                _q = queue.Queue()

                def _pump():
                    try:
                        for _line in proc.stdout:
                            _q.put(_line)
                    except Exception:
                        pass
                    finally:
                        _q.put(None)  # EOF 哨兵

                _t = threading.Thread(target=_pump, daemon=True)
                _t.start()

                deadline = time.time() + 40
                while time.time() < deadline:
                    try:
                        line = _q.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    if line is None:  # 子进程输出流已结束
                        break
                    # 密钥行脱敏后再输出——终端回滚/
                    #   截图/终端日志不再留存完整密钥；完整密钥仅写入 db_key.txt。
                    if "DB_KEY_START:" in line and ":DB_KEY_END" in line:
                        _s = line.index("DB_KEY_START:") + len("DB_KEY_START:")
                        _e = line.index(":DB_KEY_END")
                        _k = line[_s:_e]
                        _masked = (_k[:6] + "…" + _k[-4:]) if len(_k) > 12 else "****"
                        print(f"  {line[:_s]}{_masked}{line[_e:]}")
                    else:
                        print(f"  {line.strip()}")
                    if "DB_KEY_START:" in line and ":DB_KEY_END" in line:
                        # C-P2-3：双标记锚定——只有半截 DB_KEY_START（无 END）的行会在
                        #   line.index(":DB_KEY_END") 抛 ValueError，被外层 except 误报成
                        #   "Frida 执行出错"。与上方掩码块（同款条件）口径一致。
                        start = line.index("DB_KEY_START:") + len("DB_KEY_START:")
                        end = line.index(":DB_KEY_END")
                        db_key = line[start:end]
                        break
                    if "DB_KEY_ERROR:" in line:
                        print(f"⚠️  获取密钥失败: {line}")
                        break

                proc.kill()

                if db_key:
                    break
            except Exception as e:
                print(f"⚠️  Frida 执行出错: {str(e)}，解密阶段将回退到已知密钥")
                db_key = None
            finally:
                # C-P2-3：兜底杀进程——正常路径已 kill；Popen 后中途抛异常等路径
                #   此前会把存活子进程遗留到下一轮尝试。
                if proc is not None and proc.poll() is None:
                    proc.kill()

            if _attempt == 1 and not db_key:
                print("⚠️  首次未取到密钥，重启 OPPO 健康后重试一次（冷启动 attach 可能超时）")
                adb(f'shell "am force-stop {PACKAGE_NAME}"')
                time.sleep(2)
                adb(f'shell "am start -n {LAUNCH_ACTIVITY}"')
                time.sleep(12)
                pid = adb(f'shell "pidof {PACKAGE_NAME}"').strip()
                if not pid or not pid.isdigit():
                    print("⚠️  重启后 OPPO 健康仍未运行，放弃重试")
                    break

        if not db_key:
            print("⚠️  Frida 未能取到密钥，解密阶段将回退到已知密钥")

    if db_key:
        # V1.0 隐私加固：成功提示同样脱敏（完整密钥仅存 db_key.txt，已被 gitignore）
        print(f"✅ 数据库密钥获取成功: {db_key[:6]}…{db_key[-4:]}（完整密钥已写入 db_key.txt，不入库）")
        # db_key.txt 改用 atomic_write（临时文件+os.replace），
        #   避免写入中途中断留下半截密钥文件（同文件其他写入已用 atomic_write）。
        with atomic_write(os.path.join(OUTPUT_DIR, "db_key.txt"), encoding="utf-8") as f:
            f.write(db_key)
    else:
        # 不再完整明文打印密钥（与终端不留存密钥的加固口径一致）。
        print(f"ℹ️  回退使用已知密钥: {DEFAULT_DB_KEY[:4]}…{DEFAULT_DB_KEY[-4:] if len(DEFAULT_DB_KEY) > 8 else ''}(已脱敏)")

    # ========== 步骤5: 杀掉 OPPO 健康，确保数据库完整 ==========
    print_step(5, "杀掉 OPPO 健康，确保数据库完整")
    adb(f'shell "am force-stop {PACKAGE_NAME}"')
    time.sleep(2)
    print("✅ OPPO 健康已停止")

    # ========== 步骤6: Pull 数据库到宿主机 ==========
    print_step(6, "Pull 数据库到宿主机")
    
    # 连同 -wal / -shm 一起取出，否则会丢失尚未 checkpoint 的最新数据
    print(adb(f'shell "su -c \'ls -la {DB_PATH_IN_DEVICE}*\'"'))
    adb('shell "su -c \'mkdir -p /sdcard/oppo_db\'"')
    for ext in ("", "-wal", "-shm"):
        cp_out = adb(f'shell "su -c \'cp {DB_PATH_IN_DEVICE}{ext} /sdcard/oppo_db/database.db{ext}\'"')
        # 设备端 cp 失败不再静默丢弃
        if "TIMEOUT" in cp_out or cp_out.startswith("ERROR"):
            print(f"  ⚠️  设备端复制 database.db{ext} 异常: {cp_out.strip()[:80]}")
    #   个别 ROM 上目录失去 x 位后无法被枚举/进入，后续 ls、pull 会取不到文件。
    #   改为：目录保持 755（可进入），仅把目录下的普通文件设为 644。
    adb('shell "chmod 755 /sdcard/oppo_db 2>/dev/null; chmod 644 /sdcard/oppo_db/* 2>/dev/null; true"')

    # W1修复：确认设备端中转目录里实际有哪些文件（区分"设备端本来没有 -wal"
    #   与"有 -wal 但没拉下来"——前者正常，后者意味着最新数据丢失）
    ls_out = adb('shell "ls /sdcard/oppo_db/"')
    remote_wal = "database.db-wal" in ls_out

    # Pull 到宿主机
    db_local_path = os.path.join(OUTPUT_DIR, "database_encrypted.db")
    wal_local_path = db_local_path + "-wal"
    # 拉取前清除本地上一次运行残留的 -wal/-shm——
    #   本次设备端无 -wal 时（App 被 force-stop 后干净 checkpoint），陈旧 WAL 会
    #   留在新拉取的库旁被 SQLite 应用，导致报告数据偏旧/混杂且无任何提示。
    for _ext in ("-wal", "-shm"):
        _stale = db_local_path + _ext
        if os.path.exists(_stale):
            try:
                os.remove(_stale)
                print(f"  已清除上次运行残留: {os.path.basename(_stale)}")
            except OSError as _e:
                print(f"  ⚠️  残留 {os.path.basename(_stale)} 清除失败: {_e}")
    pull_result = adb(f'pull /sdcard/oppo_db/database.db "{db_local_path}"')
    print(pull_result)
    for ext in ("-wal", "-shm"):
        pull_out = adb(f'pull /sdcard/oppo_db/database.db{ext} "{db_local_path}{ext}"')
        print(pull_out)
    # L7修复：不再打印过期的output变量（那是之前pidof的结果）

    if not os.path.exists(db_local_path):
        print("❌ 数据库 Pull 失败")
        return False
    print(f"✅ 主数据库 Pull 成功 ({os.path.getsize(db_local_path)/1024/1024:.2f} MB)")

    # W1修复：-wal 是未 checkpoint 的最新数据，静默缺失 = 解密回退到上次
    #   checkpoint、报告数值偏旧且无任何提示（四轮复审确认的隐蔽后果）。
    #   设备端有而本地无 → 重试 1 次；仍失败则醒目警告数据边界。
    if remote_wal and not os.path.exists(wal_local_path):
        retry_out = adb(f'pull /sdcard/oppo_db/database.db-wal "{wal_local_path}"')
        print(f"  重试 pull -wal: {retry_out.strip()[:80]}")
    if remote_wal and not os.path.exists(wal_local_path):
        print("🔴 警告：database.db-wal 未能拉取到本地！")
        print("   解密将基于上次 checkpoint 的数据，最新数据可能缺失（报告数值可能偏旧）。")
        print("   建议：检查模拟器/ADB 状态后重新运行导出。")
    elif remote_wal:
        print("✅ WAL 文件已一并拉取（最新数据完整）")
    else:
        print("ℹ️  设备端无 -wal 文件（数据已全部 checkpoint），按主文件解密即可")

    # ========== 步骤7: 解密数据库 ==========
    print_step(7, "解密数据库")
    
    try:
        import sqlcipher3
    except ImportError:
        print("❌ 未安装 sqlcipher3，请先安装: pip install sqlcipher3")
        return False
    
    decrypted_db_path = os.path.join(OUTPUT_DIR, "database_decrypted.db")
    
    # 解密：attach 一个新数据库，然后复制所有表
    # 候选密钥：Frida 取到的 > 之前保存的 db_key.txt > 内置已知密钥
    candidates = []
    if db_key:
        candidates.append(db_key)
    prev_key_file = os.path.join(OUTPUT_DIR, "db_key.txt")
    if os.path.exists(prev_key_file):
        try:
            prev = None
            with io.open(prev_key_file, encoding="utf-8") as _pf:
                prev = _pf.read().strip()
            if prev and prev not in candidates:
                candidates.append(prev)
        except Exception:
            pass
    if DEFAULT_DB_KEY not in candidates:
        candidates.append(DEFAULT_DB_KEY)

    conn = None
    table_count = 0
    used_key = None
    for idx, key in enumerate(candidates):
        c = None  # C-P2-4：预绑定——connect 抛异常时 except 里的 c.close() 安全判空
        try:
            c = sqlcipher3.connect(db_local_path)
            # 密钥转义后拼接。本环境的 sqlcipher3 不支持
            #   PRAGMA 参数绑定（实测 'PRAGMA key = ?' 报 near "?": syntax error），
            #   故不能改用占位符；改为把单引号翻倍，防密钥异常时破坏 SQL/注入。
            _safe_key = str(key).replace("'", "''")
            c.execute(f"PRAGMA key = '{_safe_key}'")
            n = c.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            # 原打印 key[:8]，把真实密钥前 8 位留在终端
            #   （截图/录屏/日志会外泄）。密钥前几位对定位问题没有价值——候选序号足够。
            print(f"  候选密钥 #{idx+1}: {n} 张表")
            if n > 0:
                conn, table_count, used_key = c, n, key
                break
            c.close()
        except Exception as e:
            print(f"  候选密钥 #{idx+1} 失败: {str(e)}")
            try:
                c.close()
            except Exception:
                pass

    if conn is None:
        print("❌ 所有候选密钥均无法解密数据库")
        return False

    print(f"  数据库包含 {table_count} 张表（密钥已实取，完整值仅写入 db_key.txt）")
    # db_key.txt 改用 atomic_write（同 get_key.js / 报告类产物的原子写策略，
    #   避免写一半被中断留下损坏的密钥文件）
    with atomic_write(os.path.join(OUTPUT_DIR, "db_key.txt"), encoding="utf-8") as f:
        f.write(used_key)

    # 创建解密后的数据库
    try:
        if os.path.exists(decrypted_db_path):
            os.remove(decrypted_db_path)

        # 解密核心：ATTACH 一个空库，用 sqlcipher_export 把加密库的全部表复制过去
        # 路径直插单引号，工作目录名含单引号时 SQL 语法错误。
        _esc = str(decrypted_db_path).replace("'", "''")
        conn.execute(f"ATTACH DATABASE '{_esc}' AS decrypted KEY ''")
        conn.execute("SELECT sqlcipher_export('decrypted')")
        conn.execute("DETACH DATABASE decrypted")

        file_size = os.path.getsize(decrypted_db_path) / 1024 / 1024
        print(f"✅ 数据库解密成功 ({file_size:.2f} MB)")

        # X6修复：清理设备上的临时数据库副本（/sdcard/oppo_db）。
        #   那是健康数据的明文度较高的副本（仅加密库，但密钥同机可得），
        #   长期残留既占空间又有隐私风险；解密失败时保留以便排查重试。
        adb('shell "su -c \'rm -rf /sdcard/oppo_db\'"')
        print("✅ 已清理设备上的临时副本 /sdcard/oppo_db")

    except Exception as e:
        print(f"❌ 数据库解密失败: {str(e)}")
        return False
    finally:
        # L-1修复：解密任一步（ATTACH/export/DETACH）失败时，加密库连接此前不会关闭
        #   （sqlcipher 句柄泄漏）。统一在 finally 关闭。
        try:
            conn.close()
        except Exception:
            pass

    # ========== 步骤7.5: 数据完整性校验（排查报告） ==========
    # P3修复：config.enable_integrity_check 与 utils.DataIntegrityChecker 此前仅被
    #   单元测试引用、生产流程从不调用（死配置）。现接入：解密成功后自动执行一次，
    #   结果仅打印提示，不阻断导出、不影响返回码；深度合理性校验仍以 data_validation.py 为准。
    if CONFIG.get("enable_integrity_check", True):
        print_step("7.5", "数据完整性校验（enable_integrity_check=True）")
        try:
            from utils import DataIntegrityChecker
            _checker = DataIntegrityChecker(decrypted_db_path, config=CONFIG)
            try:
                _ok, _report = _checker.check_all()
                print(_report)
                print("✅ 完整性校验通过" if _ok else "⚠️  完整性校验发现问题（不阻断导出，可运行 data_validation.py 复核）")
            finally:
                _checker.close()
        except Exception as _e:
            print(f"⚠️  完整性校验执行失败（不阻断导出）: {_e}")

    # ========== 步骤8: 导出为 CSV ==========
    print_step(8, "导出为 CSV 文件")
    
    csv_dir = os.path.join(OUTPUT_DIR, "csv")
    os.makedirs(csv_dir, exist_ok=True)
    
    conn = sqlite3.connect(decrypted_db_path)
    # 连接关闭移入 finally（异常路径不再泄漏）。
    try:
        cursor = conn.cursor()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [row[0] for row in cursor.fetchall()]

        total_rows = 0
        exported_tables = 0

        for table in tables:
            if table in ("sqlite_sequence", "room_master_table"):
                continue

            try:
                cursor.execute(f"SELECT * FROM {_q_ident(table)}")
                columns = [desc[0] for desc in cursor.description]

                #   GB 级大库（如 DBHeartRate 千万行）会 OOM 或极慢。改为游标流式分批写：
                #   先 fetchone() 判空（等效于原来的 `if not rows: continue`），
                #   再按 CSV_BATCH 分批取，内存占用恒定。
                first_row = cursor.fetchone()
                if first_row is None:
                    continue

                # C-P2-12：表名经 safe_fs_name 白名单化——异常库的表名可能含路径
                #   分隔符/非法字符，直接拼接会目录穿越或写出错位文件。
                csv_path = os.path.join(csv_dir, f"{safe_fs_name(table)}.csv")
                with atomic_write(csv_path, encoding="utf-8-sig") as f:
                    # 写入表头
                    # 表头此前未走转义，列名含逗号/引号时
                    #   表头列数与数据行列数不一致，RFC4180 解析器会错位。
                    f.write(",".join(
                        ('"' + str(_c).replace('"', '""') + '"') if ("," in str(_c) or '"' in str(_c)) else str(_c)
                        for _c in columns) + "\n")
                    # 写入数据（流式分批，常量内存）
                    def _row_line(row):
                        values = []
                        for val in row:
                            if val is None:
                                values.append("")
                            elif isinstance(val, bytes):
                                values.append(val.hex())
                            else:
                                s = str(val).replace('"', '""')
                                #   不含逗号时，翻倍后的引号未加外层引号，RFC4180 解析器会错位。
                                #   现按规范：含 引号/逗号/换行/回车 任一即整体加引号。
                                if any(ch in s for ch in (',', '\n', '\r', '"')):
                                    s = f'"{s}"'
                                values.append(s)
                        return ",".join(values) + "\n"

                    f.write(_row_line(first_row))
                    n_rows = 1
                    while True:
                        batch = cursor.fetchmany(CSV_BATCH)
                        if not batch:
                            break
                        for row in batch:
                            f.write(_row_line(row))
                        n_rows += len(batch)

                total_rows += n_rows
                exported_tables += 1
                print(f"  ✅ {table}: {n_rows} 行")

            except Exception as e:
                print(f"  ⚠️  {table}: 导出失败 - {str(e)}")
    finally:
        conn.close()
    print(f"\n✅ CSV 导出完成: {exported_tables} 张表, {total_rows} 行数据")

    # ========== 步骤8.5: 按数据所属日期分类导出（csv_by_date） ==========
    # 用户需求：一次导出包含多天数据时，文件按"数据所属日期"分类，
    #   便于按天查看与归档。输出：csv_by_date\YYYYMMDD\<表名>.csv
    # 口径：仅拆分带 date 列（YYYYMMDD 日汇总口径）的表（睡眠/心率/运动/压力/血氧等
    #   核心分析表）；明细表（毫秒时间戳口径、无 date 列）保持整表导出，不按天拆分，
    #   否则文件数量爆炸且失去明细意义。每日一个子目录，目录名 8 位补零。
    print_step("8.5", "按数据所属日期分类导出（csv_by_date）")
    by_date_dir = os.path.join(OUTPUT_DIR, "csv_by_date")
    os.makedirs(by_date_dir, exist_ok=True)
    by_date_files = 0
    by_date_days = set()

    # 修复：步骤8 结束时已 conn.close()，本段不能复用其 cursor——
    #   首跑实测全部报 "Cannot operate on a closed database"。改用独立连接，
    #   与前后步骤的生命周期完全解耦。
    conn_b = sqlite3.connect(decrypted_db_path)
    # 连接关闭移入 finally（异常路径不再泄漏）。
    cursor_b = conn_b.cursor()

    def _csv_cell(val):
        """CSV 单元格转义（与步骤8 完全一致的规则，None->空/bytes->hex/特殊字符加引号）"""
        if val is None:
            return ""
        if isinstance(val, bytes):
            val = val.hex()
        s = str(val).replace('"', '""')
        if any(ch in s for ch in (',', '\n', '\r', '"')):
            s = f'"{s}"'
        return s

    try:
        for table in tables:
            if table in ("sqlite_sequence", "room_master_table"):
                continue
            try:
                cursor_b.execute(f"PRAGMA table_info({_q_ident(table)})")
                cols = [r[1] for r in cursor_b.fetchall()]
                if "date" not in cols:
                    continue
                cursor_b.execute(
                    f"SELECT DISTINCT date FROM {_q_ident(table)} WHERE date IS NOT NULL ORDER BY date")
                dates = [r[0] for r in cursor_b.fetchall()]
                for d in dates:
                    # 目录名：8 位零补齐字符串（date 列通常为 20260908 整数）
                    try:
                        day = str(int(str(d).strip())).zfill(8)
                    except (ValueError, TypeError):
                        day = str(d).strip()
                    # C-P2-12：day 的 except 分支保留原串（可能含任意字符），同走白名单
                    day_dir = os.path.join(by_date_dir, safe_fs_name(day))
                    # 参数绑定传原始值 d，避免 INTEGER 列与字符串比较不匹配
                    cursor_b.execute(f"SELECT * FROM {_q_ident(table)} WHERE date = ?", (d,))
                    d_cols = [desc[0] for desc in cursor_b.description]
                    # 原 fetchall() 把单日整表
                    #   驻留内存（百万行明细表内存峰值高），改为 fetchone 流式写出，
                    #   产物字节与原实现完全一致。
                    _first = cursor_b.fetchone()
                    if _first is None:
                        continue
                    os.makedirs(day_dir, exist_ok=True)
                    fp = os.path.join(day_dir, f"{safe_fs_name(table)}.csv")
                    with atomic_write(fp, encoding="utf-8-sig") as f:
                        # 表头此前用 ",".join(d_cols) 裸拼，
                        #   未走与数据行一致的 _csv_cell 转义。列名含逗号/引号/换行时
                        #   表头列数会与数据行错位（数据行已修、表头漏修）。
                        f.write(",".join(_csv_cell(c) for c in d_cols) + "\n")
                        _row = _first
                        while _row is not None:
                            f.write(",".join(_csv_cell(v) for v in _row) + "\n")
                            _row = cursor_b.fetchone()
                    by_date_files += 1
                    by_date_days.add(day)
            except Exception as e:
                print(f"  ⚠️  {table}: 按日期分类导出失败 - {str(e)}")
    finally:
        conn_b.close()
    print(f"✅ 按日期分类导出完成: {len(by_date_days)} 个日期, {by_date_files} 个文件"
          f"（{os.path.join('csv_by_date', 'YYYYMMDD', '<表>.csv')}）")

    # ========== 步骤9: 导出为 JSON ==========
    print_step(9, "导出为 JSON 文件")
    
    json_path = os.path.join(OUTPUT_DIR, "health_data.json")
    
    conn = sqlite3.connect(decrypted_db_path)
    # 连接关闭移入 finally（异常路径不再泄漏）。
    try:
        cursor = conn.cursor()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [row[0] for row in cursor.fetchall()]

        #   再 json.dump(indent=2)——峰值内存约为数据本身的 2~3 倍，GB 级大库必然 OOM。
        #   改为「边查边写」的流式 JSON：内存占用恒定，输出格式与 json.dump(indent=2) 一致。
        #   注意：atomic_write 在块内异常时会删除临时文件，不会产生半截 JSON。
        with atomic_write(json_path, encoding="utf-8") as f:
            f.write("{\n")
            first_table = True
            # 累计 JSON 导出失败的表（清单在 main 开头初始化）。
            #   用户可能拿着**缺表**的 JSON 去分析却浑然不知。现累计并在结束时显著汇总，
            #   存在失败表时按"数据不完整"上报（退出码 2），与 CSV 步骤的逐表告警口径一致。
            for table in tables:
                if table in ("sqlite_sequence", "room_master_table"):
                    continue

                opened = False
                try:
                    # 先取行数：① 空表跳过（M20：与 CSV 步骤8 口径一致）
                    #           ② 作为写入前的 count 值，无需把行攒在内存里
                    n = cursor.execute(f"SELECT COUNT(*) FROM {_q_ident(table)}").fetchone()[0]
                    if not n:
                        continue

                    cursor.execute(f"SELECT * FROM {_q_ident(table)}")
                    columns = [desc[0] for desc in cursor.description]

                    if not first_table:
                        f.write(",\n")
                    first_table = False
                    opened = True

                    f.write('  %s: {\n' % json.dumps(table, ensure_ascii=False))
                    f.write('    "count": %d,\n' % n)
                    f.write('    "columns": %s,\n' % json.dumps(columns, ensure_ascii=False))
                    f.write('    "rows": [\n')

                    first_row = True
                    while True:
                        batch = cursor.fetchmany(JSON_BATCH)
                        if not batch:
                            break
                        for row in batch:
                            row_dict = {}
                            for i, col in enumerate(columns):
                                val = row[i]
                                if isinstance(val, bytes):
                                    val = val.hex()
                                row_dict[col] = val
                            if not first_row:
                                f.write(",\n")
                            first_row = False
                            f.write("      " + json.dumps(row_dict, ensure_ascii=False))
                    f.write("\n    ]\n  }")
                    opened = False
                except Exception as e:
                    # 表结构已开写时补上收尾括号，保证产出的仍是合法 JSON
                    # （atomic_write 只会在异常冒泡出 with 块时才丢弃整个文件）
                    if opened:
                        try:
                            f.write("\n    ]\n  }")
                        except Exception:
                            pass
                    print(f"  ⚠️  {table}: 导出失败 - {str(e)}")
                    json_failed_tables.append(table)
            f.write("\n}\n" if not first_table else "}\n")
    finally:
        conn.close()

    file_size = os.path.getsize(json_path) / 1024 / 1024
    if json_failed_tables:
        # 显著汇总（__main__ 以退出码 2 上报"数据不完整"，见文件尾）
        print(f"❌ JSON 导出不完整：{file_size:.2f} MB，但有 {len(json_failed_tables)} 张表失败：")
        for _t in json_failed_tables:
            print(f"     - {_t}")
        print(f"   ⚠️  该 JSON 缺少上述表，**请勿直接用于分析**；"
              f"建议按手册问题 6 排查后重跑导出。")
    else:
        print(f"✅ JSON 导出完成 ({file_size:.2f} MB)")

    # ========== 步骤10: 生成带类型推断的 SQLite（可选增强）============
    # D7修复：手册 A.1-12 称 json_to_sqlite 会"自动推断类型"，但此前未被任何脚本引用（死代码）。
    # 此处作为附加步骤调用，产出 oppo_health_full.db（数值列为 INTEGER/REAL，可直接 SUM/AVG）。
    print_step(10, "生成带类型推断的 SQLite（oppo_health_full.db）")
    try:
        from json_to_sqlite import convert as convert_typed
        typed_db = convert_typed(OUTPUT_DIR)
        if typed_db:
            print(f"✅ 类型推断数据库已生成: {typed_db} ({os.path.getsize(typed_db)/1024/1024:.2f} MB)")
        else:
            print("⚠️  类型推断数据库生成跳过（缺少 JSON）")
    except Exception as e:
        # 附加步骤失败不影响主流程（CSV/JSON 已成功）
        print(f"⚠️  类型推断数据库生成失败（不影响主导出）: {str(e)}")

    # ========== 完成 ==========
    print("\n" + "=" * 60)
    if json_failed_tables:
        # L7 修复：存在失败表时不谎报成功（__main__ 以退出码 2 上报"数据不完整"）
        print(f"  ⚠️  数据导出**不完整**（{len(json_failed_tables)} 张表失败）")
    else:
        print("  🎉 数据导出完成！")
    print("=" * 60)
    print(f"\n输出目录: {OUTPUT_DIR}")
    print(f"\n生成的文件:")
    print(f"  📄 db_key.txt              - 数据库密钥")
    print(f"  📄 database_encrypted.db   - 原始加密数据库")
    print(f"  📄 database_decrypted.db   - 解密后的 SQLite 数据库")
    print(f"  📄 health_data.json        - JSON 格式完整数据")
    print(f"  📁 csv/                    - CSV 格式（每表一个文件）")
    print(f"\n数据统计:")
    print(f"  非空表数: {exported_tables}　[有数据且成功导出的表；全部表总数以上方完整性校验报告为准]")
    # 本处统计只覆盖**导出的数据表**；而稍早的
    #   「完整性校验报告」统计的是**全部表**（含 sqlite_sequence / room_master_table）。
    #   两者相差十几行，此前都叫"总行数"，实测被误读为"数据不一致"。现显式标注口径。
    print(f"  总行数: {total_rows}　[数据表口径]")
    print(f"\n提示:")
    print(f"  - 用 Navicat 打开 database_decrypted.db 查看数据")
    print(f"  - 用 Excel 打开 csv/ 目录下的 CSV 文件")
    print(f"  - 用任何文本编辑器打开 health_data.json")

    # 导出产物是全列导出，含 ssoid / open_id /
    #   device_unique_id / sn 等**身份列**。本机自用无妨，但直接分享给第三方 AI
    #   或上传时会连带外泄身份信息。手册 9.2 已要求"投喂前删身份列"，但此前这条
    #   提醒只写在手册里，导出环节完全没提示。此处补一条醒目告警。
    print(f"\n" + "!" * 60)
    print(f"⚠️  隐私提醒：上列产物为**全列导出**，含身份列（ssoid / open_id /")
    print(f"    device_unique_id / sn 等）。本机自用无妨；**对外分享或投喂给第三方 AI 前**")
    print(f"    请先删除这些列——做法见手册第九部分 9.2「投喂前必做：隐私处理」。")
    print(f"    另：db_key.txt 含**明文数据库密钥**，拿到它即可解开本库，")
    print(f"    切勿随数据目录或任何数据包一并外发（手册 9.2 第 3 条同款要求）。")
    print(f"!" * 60)

    # 返回值语义（盲审 P2-2 修正）：JSON 有失败表 ≠ 流程失败——CSV/解密库/报告
    #   均已按现有数据产出，属"部分成功"。此处一律 return True（硬错误路径在
    #   main 内部各自 return False），是否"数据不完整"由 _JSON_INCOMPLETE 标志
    #   传递，__main__ 据此区分退出码：0=完整成功 / 2=数据不完整 / 1=硬错误。
    #   （历史：L7 曾把缺表判为整体失败返回 False → __main__ 走 exit(1)，
    #   使 C-P2-2 引入的退出码 2 成为不可达死代码；盲审 P2-2 实测坐实后修正。）
    global _JSON_INCOMPLETE
    _JSON_INCOMPLETE = bool(json_failed_tables)
    return True

if __name__ == "__main__":
    try:
        success = main()
        if not success:
            sys.exit(1)
        # C-P2-2：0=完整成功；2=数据不完整（JSON 缺表，报告已按现有数据生成）
        sys.exit(2 if _JSON_INCOMPLETE else 0)
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ 程序出错: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
