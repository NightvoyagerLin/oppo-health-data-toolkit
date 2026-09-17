# SPDX-License-Identifier: MIT
"""
OPPO 健康数据自动导出与分析工具（增强版）
============================================
自动化完成：启动模拟器 → 打开OPPO健康 → 同步数据 → 导出数据 → 生成增强版分析报告

增强功能：
- OSA/打鼾分析
- HRV（心率变异性）专章分析
- 综合健康评分（0-100分，7维度加权）
- Top 3 健康问题自动识别
- 7天改善计划
- 就医建议

使用方法：
    python auto_export_and_analyze.py

输出：
    - 导出的数据文件（CSV/JSON/SQLite）
    - 增强版分析报告（Markdown 格式）
"""

import os
import sys

# 强制 UTF-8 输出，避免 Windows 控制台/重定向乱码
from utils import ensure_utf8_stdout, sanitize_adb_serial, smart_decode, atomic_write
ensure_utf8_stdout()

import time
import subprocess
import sqlite3
from datetime import datetime, timedelta, timezone

# 统一时区基准为中国时区 UTC+8。此前数据时间戳用
#   datetime.fromtimestamp()（本地时区）解释，与 GUI/split_db_by_day 的 CN(UTC+8) 口径混用；
#   非 UTC+8 机器上同一份数据会出现日期错位。现全项目统一用 CN 解释数据时间戳与"今天"。
CN = timezone(timedelta(hours=8))


def ts_ms(dt):
    """datetime → 毫秒时间戳；naive（无时区）一律按 CN(UTC+8) 解释。

    naive datetime 的 .timestamp() 会按**宿主机本地时区**换算，
    在非 UTC+8 的机器上，同一个"某日 00:00"会偏移若干小时，导致 7 天窗口边界
    与 CN 口径不一致（少算/多算一天）。此处统一补 CN 后再换算，与全项目口径对齐。
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CN)
    return int(dt.timestamp() * 1000)


# ==================== 医学建议阈值（由散落字面量提取为具名常量）====================
# 说明：以下阈值用于「十一、就医建议」章节的临床预警判定，集中定义便于复核与调参。
#   注意：改动任一阈值后，需同步核对网页报告（generate_html_report.py）与手册中展示的阈值文案，
#   避免"判定变了、提示文案没变"的口径不一致。
MED_THRESHOLDS = {
    "ahi_severe": 15,      # AHI ≥15：中重度睡眠呼吸暂停，建议就诊
    "ahi_mild": 5,         # AHI 5~15：轻度
    "spo2_low": 90,        # 平均血氧 <90% 为预警
    "spo2_normal": 95,     # 血氧缺省参考值（无数据时）
    "rest_hr_high": 100,   # 静息心率 >100 bpm 为预警
    "rest_hr_normal": 70,  # 静息心率缺省参考值
    "rmssd_low": 20,       # HRV RMSSD <20 提示压力相关
    "rmssd_normal": 50,    # RMSSD 缺省参考值
    "score_low": 60,       # 综合评分 <60 时叠加压力相关提示
    "ahi_moderate": 30,    # AHI 中/重度分界（评估文案统一取自此表）
    "rest_hr_low_normal": 60,  # 静息心率正常下限
    "rest_hr_elevated": 90,    # 静息心率“偏高”线（与 Top3 判据共用，消除 60-100 与 >90 的自相矛盾）
    "rmssd_mid": 30,       # RMSSD 中等下限
}

# 导入集中配置（修改 config.py 即可改变所有配置，无需改动脚本）
try:
    from config import CONFIG
except ImportError:
    # 兜底：如果 config.py 不存在，使用默认配置
    CONFIG = {
        "adb_path": os.path.join(os.path.dirname(os.path.abspath(__file__)), "platform", "adb.exe"),
        "adb_device": os.environ.get("ANDROID_SERIAL", "127.0.0.1:5555"),
        "work_dir": os.path.dirname(os.path.abspath(__file__)),
        "export_script": os.path.join(os.path.dirname(os.path.abspath(__file__)), "export_health_data.py"),
        "package_name": "com.heytap.health",
        "launch_activity": "com.heytap.health/.oobe.LaunchActivity",
        "wait_emulator_start": 30,
        "wait_data_sync": 120,
    }
    print("⚠️  config.py 未找到，使用默认配置")

# ==================== 配置区（从 config.py 读取） ====================
# 所有配置集中在 config.py，修改配置请编辑 config.py，无需改动本脚本。
ADB_PATH = CONFIG.get("adb_path", os.path.join(os.path.dirname(os.path.abspath(__file__)), "platform", "adb.exe"))
# 模拟器同时存在 127.0.0.1:5555 与 emulator-5554 两个条目，
# 不指定 -s 时 adb 会报 "more than one device/emulator"，故显式指定设备。
ADB_DEVICE = sanitize_adb_serial(CONFIG.get("adb_device", os.environ.get("ANDROID_SERIAL", "127.0.0.1:5555")))
ADB = f'"{ADB_PATH}" -s {ADB_DEVICE}'
os.environ["ANDROID_SERIAL"] = ADB_DEVICE
WORK_DIR = CONFIG.get("work_dir", os.path.dirname(os.path.abspath(__file__)))
EXPORT_SCRIPT = CONFIG.get("export_script", os.path.join(WORK_DIR, "export_health_data.py"))
PACKAGE_NAME = CONFIG.get("package_name", "com.heytap.health")
# 该 ROM 没有 monkey 命令，必须用 am start 显式启动
LAUNCH_ACTIVITY = CONFIG.get("launch_activity", "com.heytap.health/.oobe.LaunchActivity")

# 等待时间（秒）
WAIT_EMULATOR_START = CONFIG.get("wait_emulator_start", 30)
WAIT_DATA_SYNC = CONFIG.get("wait_data_sync", 120)
# 删除死变量 WAIT_EXPORT_COMPLETE——
#   其读取的 config 键 wait_export_complete 不存在（config 实际是 wait_after_export），
#   恒取默认值 60，且全文无任何使用，只会误导维护者以为改 config 能生效。

# 分析阈值（从config读取，修改config.py即可改变评分标准）
TARGET_DAILY_STEPS = CONFIG.get("target_daily_steps", 8000)
TARGET_WEEKLY_EXERCISE = CONFIG.get("target_weekly_exercise_min", 150)
SLEEP_DEEP_MIN_RATIO = CONFIG.get("sleep_deep_min_ratio", 15)
SLEEP_DEEP_MAX_RATIO = CONFIG.get("sleep_deep_max_ratio", 25)
LATE_NIGHT_HOUR = CONFIG.get("late_night_hour", 23)
LATE_NIGHT_EARLY_HOUR = CONFIG.get("late_night_early_hour", 6)
# 删除本文件内 5 个「读而不用」的死常量
#   （MAX_SLEEP_HOURS / MIN_HEART_RATE / MAX_HEART_RATE / MIN_SPO2 / MAX_SPO2）——
#   这些阈值实际由 utils.DataIntegrityChecker 与 data_validation.py 直接读 CONFIG 消费，
#   本文件从不引用，保留易造成"改了这里不生效"的误导（同类：config 中已清理的死配置）。

# ==================== 工具函数 ====================
def run_cmd(cmd, timeout=60, shell=True):
    """执行命令并返回 (输出, 退出码)。

    盲审复审（外部报告 P1-2）：旧写法 subprocess.run(timeout=) 超时只杀直接子进程，
    持有 stdout 管道的孙进程（如 adb.exe）不被杀 → 收尾 communicate 永久阻塞——
    同款问题在 export 侧 run_cmd 注释中记为"实测整条导出链死锁 6.5 分钟"。
    现同步为 Popen + 超时后 taskkill /F /T 杀整个进程树，任何情况下不悬挂。"""
    try:
        # bytes + smart_decode：cmd.exe 的 GBK 报错（如 "'adb' 不是内部或外部命令"）
        #   不再被固定 UTF-8 解码成问号（见 utils.smart_decode）
        p = subprocess.Popen(
            cmd, shell=shell, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=WORK_DIR
        )
    except Exception as e:
        return f"ERROR: {str(e)}", -1
    try:
        out, err = p.communicate(timeout=timeout)
        return smart_decode(out) + smart_decode(err), p.returncode
    except subprocess.TimeoutExpired:
        # 原无条件调用 Windows 专有 taskkill，非 Windows 上抛 FileNotFoundError；
        # 按平台选择终止方式（本工具主战场为 Windows）
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                               capture_output=True, timeout=10)
            else:
                p.kill()
        except Exception:
            pass
        try:
            out, err = p.communicate(timeout=5)
            return "TIMEOUT" + smart_decode(out) + smart_decode(err), p.returncode
        except Exception:
            return "TIMEOUT", -1
    except Exception as e:
        return f"ERROR: {str(e)}", -1

def log(msg):
    """输出日志"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}")

# safe_int 统一从 utils 导入；utils 缺失时降级为等价的内联实现。
try:
    from utils import safe_int
except ImportError:
    def safe_int(val, default=0):
        if val is None:
            return default
        try:
            if isinstance(val, str):
                val = val.strip()
                if not val:
                    return default
                return int(float(val))
            return int(val)
        except (ValueError, TypeError):
            return default

def get_latest_export_dir():
    """获取最新的导出目录（标准口径：优先 DB\\combined 合并全量库，兼容历史 export_* 目录）"""
    candidates = []

    # 1) DB\\日期 目录（export_health_data.py 实际输出位置）
    db_root = os.path.join(WORK_DIR, "DB")
    if os.path.isdir(db_root):
        for d in os.listdir(db_root):
            # 排除基准快照目录（test_ 前缀）——
            #   与 upload_data/data_validation 的 N1/N2 修复保持一致。
            #   主流程有"本次运行新产出解密库"校验保护，此处为防御性对齐。
            if d.startswith("test_"):
                continue
            full = os.path.join(db_root, d)
            if os.path.isdir(full) and os.path.exists(os.path.join(full, "database_decrypted.db")):
                candidates.append(full)

    # 2) 兼容旧版 export_* 目录
    for d in os.listdir(WORK_DIR):
        if d.startswith('export_') and os.path.isdir(os.path.join(WORK_DIR, d)):
            full = os.path.join(WORK_DIR, d)
            if os.path.exists(os.path.join(full, "database_decrypted.db")):
                candidates.append(full)

    if not candidates:
        return None
    # 标准口径——DB\combined 合并全量库恒为最新且完整，
    #   显式优先返回，避免 split_db_by_day.py 产出的日库（mtime=拆分时间，可能晚于
    #   combined）被按 mtime 误选，导致报告只分析单日数据。
    #   与 data_validation.py / upload_data.py 的同类逻辑保持一致。
    combined = os.path.join(db_root, "combined")
    if os.path.isfile(os.path.join(combined, "database_decrypted.db")):
        return combined
    # 安全排序：忽略文件不存在或无法访问的目录
    def safe_mtime(p):
        try:
            return os.path.getmtime(os.path.join(p, "database_decrypted.db"))
        except OSError:
            return 0
    candidates.sort(key=safe_mtime, reverse=True)
    return candidates[0]

# ==================== 主流程 ====================
# C-P2-2：导出"数据不完整"标志（JSON 缺表，导出脚本退出码 2）——报告照常生成，
#   __main__ 据此以退出码 2 收尾，供定时任务/CI 区分完整成功(0)与硬错误(1)。
_EXPORT_INCOMPLETE = False

def main():
    # 盲审 U-2：主入口接入配置自检——轻量、无交互，配置错误早停（同 export 侧口径）
    from config import validate_config
    _ok, _warns = validate_config()
    if not _ok:
        print("❌ 配置自检未通过，中止。先解决以下问题（或运行 python config.py 查看完整自检）：")
        for _w in _warns:
            print(f"  - {_w}")
        return False
    print("=" * 70)
    print("  OPPO 健康数据自动导出与分析工具（增强版）")
    print(f"  开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    # ========== 步骤1: 检查 ADB 连接 ==========
    log("步骤1: 检查 ADB 连接...")
    output, code = run_cmd(f'{ADB} devices')
    print(output)

    # 检测设备：排除第一行 "List of devices attached"，检查是否有实际设备行
    device_lines = [l for l in output.split("\n") if l.strip() and "List of devices" not in l]

    # E4修复：adb devices 每行格式为 "<serial>\t<state>"，
    #   只有状态为 device 才算真正可用；offline/unauthorized 等此前被误判为连接正常，
    #   错误会延后到导出环节才暴露。
    def _has_ready_device(lines):
        for l in lines:
            parts = l.strip().split()
            if len(parts) >= 2 and parts[-1] == "device":
                return True
        return False

    has_device = _has_ready_device(device_lines)
    if not has_device:
        log("未检测到设备，尝试连接 127.0.0.1:5555...")
        # 原本丢弃 connect 的原始输出，连接失败时用户
        #   只能看到"未检测到设备"，无法判断是端口不对、模拟器没开 Root/ADB、
        #   还是 adb 被杀软拦截。export_health_data.py（L5 修复）已回显，此处对齐。
        _out, _code = run_cmd(f'"{ADB_PATH}" connect {ADB_DEVICE}')
        _out = (_out or "").strip()
        if _out:
            log(f"adb connect 返回（exit={_code}）：{_out}")
        time.sleep(5)
        output, code = run_cmd(f'{ADB} devices')
        device_lines = [l for l in output.split("\n") if l.strip() and "List of devices" not in l]
        has_device = _has_ready_device(device_lines)
        if not has_device:
            log("❌ 无法连接模拟器，请确保模拟器已启动")
            return False
    log("✅ ADB 连接正常")

    # ========== 步骤2: 启动 OPPO 健康 ==========
    log("步骤2: 启动 OPPO 健康...")
    run_cmd(f'{ADB} shell "am start -n {LAUNCH_ACTIVITY}"')
    time.sleep(5)

    # 检查是否启动成功（pidof 可能不存在，用 ps 备用）
    output, code = run_cmd(f'{ADB} shell "pidof {PACKAGE_NAME} 2>/dev/null || ps -A | grep {PACKAGE_NAME} | grep -v grep"')
    if output.strip():
        pid = output.strip().split()[0] if output.strip().split() else "unknown"
        log(f"✅ OPPO 健康启动成功 (PID: {pid})")
    else:
        log("⚠️  OPPO 健康可能未启动（可能是Zygisk问题），继续执行导出（有密钥回退）...")

    # ========== 步骤3: 等待数据同步 ==========
    log(f"步骤3: 等待数据同步（{WAIT_DATA_SYNC} 秒）...")
    for i in range(0, WAIT_DATA_SYNC, 10):
        time.sleep(10)
        log(f"  已等待 {i + 10} 秒...")
    log("✅ 数据同步完成")

    # ========== 步骤4: 运行导出脚本 ==========
    log("步骤4: 运行一键导出脚本...")
    log(f"  执行: python {EXPORT_SCRIPT}")

    # 记录本次运行起始时间，用于判断"今日解密库"
    #   是否由本次运行产生——防止"今天目录存在但内容是早前残骸"被误当新数据。
    _run_started_ts = time.time()

    # 运行导出脚本
    # 子进程 stdout=PIPE 时，Python 按 locale 编码（GBK/CP936）
    #   写出——子脚本里的 emoji（✅/❌/⚠️）无法 GBK 编码，print 直接抛
    #   UnicodeEncodeError 令导出崩溃（实测返回码 1）；且 GBK 字节被本脚本按
    #   utf-8 读回会变成乱码。注入 PYTHONUTF8=1 强制子进程以 UTF-8 写出。
    _child_env = {**os.environ, "PYTHONUTF8": "1"}
    proc = subprocess.Popen(
        [sys.executable, EXPORT_SCRIPT],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding='utf-8', errors='replace',
        cwd=WORK_DIR, env=_child_env
    )

    # 实时输出。改为「泵线程 + 队列 + 总超时」模式——
    #   原 readline() 在子进程静默卡死时会永久阻塞，任何超时检查都没有执行机会；
    #   现与 export_health_data.py 的 B1 修复同构：总超时 15 分钟，超时强杀并按
    #   失败处理（走既有"拒绝用旧数据出报告"逻辑）。
    import queue as _queue
    import threading as _threading
    _q = _queue.Queue()

    def _pump_out():
        try:
            for _line in proc.stdout:
                _q.put(_line)
        except Exception:
            # 此处静默是有意的（L4 复核结论 2026-09-15）：这是**取输出的后台线程**，
            #   子进程被强杀时 read 会抛异常，这正是它该退出的信号；此处若打日志
            #   会与主线程的退出日志重复刷屏，且异常往往已在主流程被记录。非判定分支。
            pass
        finally:
            _q.put(None)  # EOF 哨兵

    _threading.Thread(target=_pump_out, daemon=True).start()

    _EXPORT_TIMEOUT_S = 900  # 15 分钟总超时（导出正常 3-5 分钟，余量充足）
    _timed_out = False
    last_progress = time.time()
    # 将「泵循环 + wait」整体包进 try/finally——
    #   finally 兜底强杀（幂等：已退出时 poll() 非 None，不再重复处理）。
    try:
        while True:
            try:
                line = _q.get(timeout=1.0)
            except _queue.Empty:
                if time.time() - _run_started_ts > _EXPORT_TIMEOUT_S:
                    _timed_out = True
                    log("  ❌ 导出子进程超过 15 分钟未完成，强制终止")
                    try:
                        proc.kill()
                    except Exception:
                        # 尽力而为：进程可能已自行退出，kill 失败无需处理（L4 复核结论）。
                        pass
                    break
                # 每60秒输出一次进度提示
                if time.time() - last_progress > 60:
                    log("  导出进行中，请稍候...")
                    last_progress = time.time()
                continue
            if line is None:  # 子进程输出流已结束
                break
            print(f"  {line.strip()}")
            last_progress = time.time()

        # wait 加超时——子进程若已关闭 stdout（读到 EOF→break）
        #   却迟迟不退出，原 proc.wait() 会永久阻塞，绕过整个超时保护。
        try:
            proc.wait(timeout=30)
        except Exception:
            try:
                proc.kill()
            except Exception:
                # 兜底强杀：wait 超时后尽力回收；kill 失败说明进程已不在（L4 复核结论）。
                pass
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
    if _timed_out:
        log("❌ 导出子进程超时被强制终止，中止——拒绝用旧数据出报告")
        return False
    if proc.returncode != 0:
        log(f"⚠️  导出脚本返回码: {proc.returncode}（非0）")
        if proc.returncode == 2:
            # C-P2-2：导出脚本以 2 报告"JSON 缺表但已按现有数据继续"
            global _EXPORT_INCOMPLETE
            _EXPORT_INCOMPLETE = True
        #   get_latest_export_dir() 会拿昨天的旧目录照样出报告并退出码 0——
        #   违反"0=成功"契约，CI/定时任务无法感知失败。
        #   现校验：今天目录未生成 → 判定导出失败并中止；已生成 → 视为警告继续。
        # 首版只查"今天目录是否存在"，但导出脚本是
        #   先 makedirs(OUTPUT_DIR) 再 pull/解密——中途失败时目录已存在却没有任何
        #   解密库，get_latest_export_dir() 仍会回退到昨日旧库出报告（探针实测
        #   返回值 True）。故须同时要求：今日目录里确有本次运行新产出的
        #   database_decrypted.db（mtime >= 本次运行起始）。
        # 标准口径：导出统一写入 DB\combined\（合并全量库），此处直接校验 combined
        _today_dir = os.path.join(WORK_DIR, "DB", "combined")
        _today_db = os.path.join(_today_dir, "database_decrypted.db")
        _fresh = os.path.isfile(_today_db) and os.path.getmtime(_today_db) >= _run_started_ts - 5
        if not _fresh:
            log(f"❌ 导出失败且今天目录（{_today_dir}）中没有本次运行新产出的解密库，中止——拒绝用旧数据出报告")
            return False
        if proc.returncode == 2:
            log("❌ 导出数据不完整（JSON 缺表）：已按现有数据继续生成报告，但本次运行退出码将为 2")
        else:
            log("⚠️  导出脚本返回码非0，但今天的导出目录已有本次新生成的解密库，继续（疑似次要警告）")
    else:
        log(f"✅ 导出脚本执行完成 (返回码: {proc.returncode})")

    # ========== 步骤5: 找到最新的导出数据 ==========
    log("步骤5: 查找导出的数据...")
    time.sleep(2)  # 等待文件写入完成

    export_dir = get_latest_export_dir()
    if not export_dir:
        log("❌ 未找到导出目录")
        return False

    log(f"✅ 导出目录: {export_dir}")

    # 检查关键文件
    db_path = os.path.join(export_dir, "database_decrypted.db")
    json_path = os.path.join(export_dir, "health_data.json")

    if os.path.exists(db_path):
        log(f"✅ 解密数据库: {db_path} ({os.path.getsize(db_path)/1024/1024:.2f} MB)")
    else:
        log("❌ 未找到解密数据库")
        return False

    if os.path.exists(json_path):
        log(f"✅ JSON 数据: {json_path} ({os.path.getsize(json_path)/1024/1024:.2f} MB)")
    else:
        log("⚠️  未找到 JSON 数据")

    # ========== 步骤6: 生成分析报告 ==========
    log("步骤6: 生成增强版分析报告...")
    report_path = generate_analysis_report(db_path, export_dir)

    if report_path:
        log(f"✅ 分析报告已生成: {report_path}")
    else:
        log("❌ 分析报告生成失败")

    # ========== 完成 ==========
    # E1修复：报告生成失败时不再宣称"完成"，返回值如实反映结果供自动化调用方判断
    print("\n" + "=" * 70)
    print("  🎉 自动导出与分析完成！" if report_path else "  ⚠️ 数据已导出，但分析报告生成失败！")
    print("=" * 70)
    print(f"\n导出目录: {export_dir}")
    print(f"解密数据库: {db_path}")
    if report_path:
        print(f"分析报告: {report_path}")
    print(f"\n完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    return report_path is not None


def _pct_display(active_weights):
    """归一化权重 → 合计恒为 100 的整数百分比（最大余数法）。
    原 {w*100:.0f} 对各维度独立四舍五入，合计可能 99%~101%（实测 28+17+17+17+11+11=101%）。
    规则：先全部向下取整，再把差额按小数余数从大到小逐个 +1；并列时按维度顺序稳定分配。
    加权计算仍使用未舍入的真实权重，此处仅影响显示。空权重返回空 dict。"""
    dims = list(active_weights.keys())
    if not dims:
        return {}
    raw = [active_weights[d] * 100.0 for d in dims]
    ints = [int(x) for x in raw]
    short = 100 - sum(ints)                # 需补回的百分点数（数学上 0~len(dims)）
    short = max(0, min(short, len(dims)))  # 浮点误差防御
    order = sorted(range(len(dims)), key=lambda i: raw[i] - ints[i], reverse=True)
    for i in order[:short]:
        ints[i] += 1
    return dict(zip(dims, ints))


def generate_analysis_report(db_path, export_dir):
    """生成增强版分析报告（含OSA/HRV/综合评分/Top3问题/7天计划/就医建议）"""
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        report = []
        report.append("# OPPO 健康数据分析报告（增强版）")
        report.append(f"\n生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        report.append(f"数据库: {os.path.basename(db_path)}")

        # 获取日期范围
        # 用 CN(UTC+8) 的"今天"，与 L12 统一时区基准一致。
        #   原 datetime.now() 取宿主机本地时区，非 UTC+8 机器上"今天/昨天"会整体偏移一天，
        #   进而影响最新完整日探测与所有"近7天"窗口。
        today = datetime.now(CN)
        yesterday = today - timedelta(days=1)

        # B8优化：动态探测最新完整日，替代固定yesterday
        # 完整日判断标准：自适应阈值（最近10天不含今天的最大值×50%，最低50条）
        #   不同设备/佩戴习惯的数据量差异大，硬编码500会导致低数据量设备永远探测不到
        # 探测逻辑：从最近10天中倒序查找，找到第一个满足自适应阈值的日期
        # 回退方案：找不到满足阈值的完整日时，取最近一个有数据(>0条)且非今天的日期
        #   （不能回退到yesterday，因为滞后场景下yesterday恰恰就是没数据的那天）
        latest_complete_day = yesterday
        complete_day_reason = "回退默认（未找到任何有数据的日期）"
        try:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='DBHeartRate'")
            if cursor.fetchone():
                # 查询最近10天每天的心率数据条数
                probe_start = (today - timedelta(days=10)).strftime("%Y%m%d")
                probe_end = today.strftime("%Y%m%d")
                cursor.execute("""
                    SELECT strftime('%Y%m%d', data_created_timestamp/1000, 'unixepoch', '+8 hours') as dt, COUNT(*) as cnt
                    FROM DBHeartRate
                    WHERE strftime('%Y%m%d', data_created_timestamp/1000, 'unixepoch', '+8 hours') >= ?
                      AND strftime('%Y%m%d', data_created_timestamp/1000, 'unixepoch', '+8 hours') <= ?
                    GROUP BY dt
                    ORDER BY dt DESC
                """, (probe_start, probe_end))
                daily_counts = cursor.fetchall()
                today_str = today.strftime("%Y%m%d")

                # B8修复①：自适应阈值——以最近10天(不含今天)的最大值为基准，取其50%，最低50条
                non_today_counts = [cnt for dt, cnt in daily_counts if dt != today_str]
                if non_today_counts:
                    adaptive_threshold = max(50, int(max(non_today_counts) * 0.5))
                else:
                    adaptive_threshold = 50  # 无历史数据时用最低阈值

                # 倒序查找第一个满足自适应阈值的日期（排除今天）
                found_complete = False
                for row in daily_counts:
                    dt_str, cnt = row[0], row[1]
                    if dt_str == today_str:
                        continue  # 跳过今天
                    if cnt >= adaptive_threshold:
                        latest_complete_day = datetime.strptime(dt_str, "%Y%m%d")
                        complete_day_reason = f"动态探测（当天心率数据{cnt}条，≥自适应阈值{adaptive_threshold}条）"
                        found_complete = True
                        break

                # B8修复②：找不到满足阈值的完整日时，回退到"最近一个有数据(>0条)且非今天的日期"
                #   不能回退到yesterday，因为滞后场景下yesterday恰恰就是没数据的那天
                if not found_complete:
                    for row in daily_counts:
                        dt_str, cnt = row[0], row[1]
                        if dt_str == today_str:
                            continue
                        if cnt > 0:
                            latest_complete_day = datetime.strptime(dt_str, "%Y%m%d")
                            complete_day_reason = f"回退（取最近有数据日，{cnt}条，未达自适应阈值{adaptive_threshold}条）"
                            break
        except Exception as e:
            complete_day_reason = f"探测异常，回退默认（{str(e)[:50]}）"

        # D1进阶修复：7天窗口改为「最近7个完整日」(latest_complete_day-6 ~ latest_complete_day)
        #   今日数据通常只同步至当前时刻，若纳入会与 6 个完整日混合平均，
        #   拉低/抬高步数、睡眠等均值（原 D1 缺陷）。「昨晚」单日睡眠仍用 date=today，与 7 天窗口刻意分离。
        last7_start = latest_complete_day - timedelta(days=6)
        last7_end = latest_complete_day  # 动态探测的最新完整日
        last7_start_str = last7_start.strftime("%Y%m%d")
        last7_end_str = last7_end.strftime("%Y%m%d")

        report.append(f"\n## 时间范围")
        report.append(f"- 报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        # L6修复：旧文案写「昨晚: 昨天」，但睡眠表 date 语义是起床日，
        #   实际查的是 date=今天。两个"昨晚"定义并存会自相矛盾，此处写清真实口径。
        # （L7修复：原「最近3天」是死标签，全文无任何3天分析，已删除）
        report.append(f"- 昨晚睡眠: 取 `date={today.strftime('%Y%m%d')}` 的记录"
                      f"（该表 date 语义为**起床日**，即 {yesterday.strftime('%Y-%m-%d')} 夜间入睡、"
                      f"{today.strftime('%Y-%m-%d')} 起床的那一觉）")
        # B8优化：显示动态探测的完整日信息
        report.append(f"- 最近7天(完整日): {last7_start.strftime('%Y-%m-%d')} ~ {last7_end.strftime('%Y-%m-%d')}"
                      f"（最新完整日: {latest_complete_day.strftime('%Y-%m-%d')}，{complete_day_reason}）")

        # L1修复：数据时效校验（修正表名和列名，不再吞掉异常）
        # 实际表名是 DBHeartRate（不是 DBHeartRateTable），列名是 data_created_timestamp（不是 timestamp）
        try:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='DBHeartRate'")
            if cursor.fetchone():
                cursor.execute("SELECT MAX(CAST(data_created_timestamp AS INTEGER)) FROM DBHeartRate")
                latest_hr = cursor.fetchone()
                if latest_hr and latest_hr[0]:
                    latest_ts = latest_hr[0] / 1000
                    # 数据时间戳统一用 CN(UTC+8) 解释，与 GUI/split 口径一致；
                    #   now_dt 也用 CN，保证两者同为感知型 datetime，相减不会因时区混用抛 TypeError。
                    latest_dt = datetime.fromtimestamp(latest_ts, tz=CN)
                    now_dt = datetime.now(CN)
                    time_diff_hours = (now_dt - latest_dt).total_seconds() / 3600
                    if time_diff_hours > 2:
                        report.append(
                            f"\n> ⚠️ **数据时效提示**: 最新心率数据时间为 {latest_dt.strftime('%Y-%m-%d %H:%M')}，"
                            f"距报告生成时间已过去 {time_diff_hours:.1f} 小时（超过2小时阈值）。"
                            f"可能是设备侧未同步最新数据，建议在OPPO健康中手动下拉刷新后重新导出。"
                        )
                    else:
                        report.append(
                            f"\n> ✅ **数据时效**: 最新心率数据时间为 {latest_dt.strftime('%Y-%m-%d %H:%M')}，"
                            f"距报告生成时间 {time_diff_hours:.1f} 小时，数据新鲜。"
                        )
            else:
                report.append("\n> ⚠️ **数据时效提示**: 未找到 DBHeartRate 表，无法校验数据时效。")
        except Exception as e:
            report.append(f"\n> ⚠️ **数据时效校验失败**: {str(e)}")

        # 用于存储各维度评分和数据，供后续综合评分使用
        scores = {}
        health_data = {}

        # ==================== 1. 睡眠分析 ====================
        report.append(f"\n## 😴 一、睡眠分析")

        try:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='DBSleepDataStatTable'")
            if cursor.fetchone():
                # 昨晚睡眠
                # ⚠️ 关键修正：DBSleepDataStatTable.date 的语义是"起床日"，不是入睡日。
                #   例如 09-07 晚上入睡、09-08 凌晨/下午起床的一觉，date=20260908。
                #   所以查"昨晚"应该用 date=today，而不是 date=yesterday。
                last_night_str = today.strftime("%Y%m%d")

                # 残留修复②：昨晚睡眠彻底动态化——date=today 无记录且设备滞后时，
                #   回退展示「最近一次有记录的睡眠」明细（而非只给提示），避免用户
                #   误以为"昨晚真没睡"；回退日期明确标注，评分亦取该真实记录。
                def _fetch_night(dstr):
                    """按起床日取一条睡眠记录：主睡表优先，日汇总表兜底。返回 (main_row, row, cols)。"""
                    mr = None
                    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='DBSleepMainStat'")
                    if cursor.fetchone():
                        cursor.execute("SELECT * FROM DBSleepMainStat WHERE date = ?", (dstr,))
                        _mr = cursor.fetchone()
                        if _mr:
                            mr = dict(zip([d[0] for d in cursor.description], _mr))
                    cursor.execute("SELECT * FROM DBSleepDataStatTable WHERE date = ?", (dstr,))
                    _cols = [desc[0] for desc in cursor.description]
                    _row = cursor.fetchone()
                    return mr, _row, _cols

                night_fallback = None  # 回退时记录实际使用的起床日(YYYYMMDD)
                main_row, row, cols = _fetch_night(last_night_str)
                if not (row or main_row):
                    # 边界修正：回退查询只排除今晚（date < today），不再以心率同步进度为上限——
                    #   否则"昨晚真睡了但心率未同步"（睡眠比心率新）时会错误展示更老的记录。
                    #   今晚无记录可能是"没同步"也可能是"真没睡/未监测"，展示最近一次记录
                    #   并明确标注日期，比干巴巴的"暂无数据"信息量更大。
                    try:
                        cursor.execute(
                            "SELECT MAX(date) FROM DBSleepDataStatTable WHERE date < ?",
                            (last_night_str,))
                        _ld = cursor.fetchone()
                        if _ld and _ld[0]:
                            # ⚠️ date 列在库中是 INTEGER，MAX() 返回 int；
                            #   night_fallback 后续要切片格式化，必须统一转 str。
                            main_row, row, cols = _fetch_night(str(_ld[0]))
                            if row or main_row:
                                night_fallback = str(_ld[0])
                    except Exception as _e:
                        # 原为 except: pass——静默吞掉异常会让
                        #   "主睡回退查询"失败时无声无息，报告口径悄悄降级而无人知晓。
                        log(f"  ⚠️ [降级] 睡眠主睡回退查询失败，已跳过该回退：{_e}")

                # ⚠️ 边界修复：yesterday = today-1天 带当前时刻（如 09-08 10:5x），
                #   而 latest_complete_day 是当日 00:00；直接比较 latest < yesterday
                #   在 latest==昨天 时也恒为 True（00:00 < 10:5x），"设备滞后"判断失效。
                #   统一按日期（忽略时间）比较。
                device_lagged = latest_complete_day.date() < yesterday.date()
                if night_fallback:
                    _fd = f"{night_fallback[:4]}-{night_fallback[4:6]}-{night_fallback[6:]}"
                    if device_lagged:
                        report.append(
                            f"\n### 1.1 最近一次睡眠 ({_fd} 起床；设备数据截至 "
                            f"{latest_complete_day.strftime('%Y-%m-%d')}，今晚记录尚未同步)"
                        )
                    else:
                        report.append(
                            f"\n### 1.1 最近一次睡眠 ({_fd} 起床；今晚无睡眠记录)"
                        )
                else:
                    report.append(f"\n### 1.1 昨晚睡眠 ({today.strftime('%Y-%m-%d')} 起床)")
                if row or main_row:
                    if night_fallback:
                        report.append(f"- ℹ️ 以下为**最近一次有记录的睡眠**（date={night_fallback}），"
                                      f"非今晚数据（今晚记录尚未同步）；评分亦取该真实记录。")
                    row_dict = dict(zip(cols, row)) if row else {}
                    total_sleep = safe_int(row_dict.get('total_sleep_time'))
                    deep_sleep = safe_int(row_dict.get('total_deep_sleep_time'))
                    light_sleep = safe_int(row_dict.get('total_lightly_sleep_time'))
                    rem_sleep = safe_int(row_dict.get('total_rem_time'))
                    wake_time = safe_int(row_dict.get('total_wake_up_time'))
                    sleep_score = safe_int(row_dict.get('sleep_score'))
                    fall_asleep = safe_int(row_dict.get('fall_asleep'))
                    # 0 是"前一天 00:00 入睡"的合法值，不能与"无记录"混为一谈。
                    #   此处保留原始列的存在性，供下方入睡时间分支区分二者。
                    _fa_present = row_dict.get('fall_asleep') is not None
                    sleep_out = safe_int(row_dict.get('sleep_out'))

                    if main_row:
                        # 主睡口径覆盖：时长/分期/入睡/起床
                        total_sleep = safe_int(main_row.get('total_sleep_time'))
                        deep_sleep = safe_int(main_row.get('total_deep_sleep_time'))
                        light_sleep = safe_int(main_row.get('total_lightly_sleep_time'))
                        rem_sleep = safe_int(main_row.get('total_rem_time'))
                        wake_time = safe_int(main_row.get('total_wake_up_time'))
                        _fa_main = main_row.get('sleep_in_minute')
                        if _fa_main is not None:
                            fall_asleep = safe_int(_fa_main)
                            _fa_present = True
                        sleep_out = safe_int(main_row.get('sleep_out_minute')) or sleep_out

                    report.append(f"- 睡眠评分: **{sleep_score}** / 100")
                    report.append(f"- 总睡眠时长: {total_sleep} 分钟 ({total_sleep/60:.1f} 小时)")
                    if total_sleep:
                        report.append(f"- 深睡: {deep_sleep} 分钟 ({deep_sleep/total_sleep*100:.1f}%)")
                        report.append(f"- 浅睡: {light_sleep} 分钟 ({light_sleep/total_sleep*100:.1f}%)")
                        report.append(f"- REM: {rem_sleep} 分钟 ({rem_sleep/total_sleep*100:.1f}%)")
                    report.append(f"- 清醒: {wake_time} 分钟")
                    if _fa_present:
                        fa = fall_asleep % 1440
                        # ⚠️ L3修复：添加 fa >= 23*60（23:00-23:59入睡也算熬夜），
                        #   与 is_late_night() 函数保持一致。之前只判断跨午夜和0-6点，23点入睡漏判。
                        # 原判据含 23:00 之后却统一写"凌晨入睡"，
                        #   23:15 会被误标为凌晨。现按实际时段分档描述。
                        if fall_asleep >= 1440 or fa < LATE_NIGHT_EARLY_HOUR * 60:
                            late_flag = "（⚠️ 凌晨入睡，属熬夜）"
                        elif fa >= LATE_NIGHT_HOUR * 60:
                            late_flag = "（⚠️ 入睡偏晚，属熬夜）"
                        else:
                            late_flag = ""
                        report.append(f"- 入睡时间: {fa//60:02d}:{fa%60:02d}{late_flag}")
                    else:
                        report.append("- 入睡时间: ⚠️ 数据缺失（无 fall_asleep 记录）")
                    if sleep_out:
                        so = sleep_out % 1440
                        report.append(f"- 起床时间: {so//60:02d}:{so%60:02d}")
                    else:
                        report.append("- 起床时间: ⚠️ 数据缺失（sleep_out=0）")

                    # L3修复：明确区分夜间主睡与白天小睡，避免把午睡结束时间当成起床时间
                    if main_row:
                        day_total = safe_int(row_dict.get('total_sleep_time'))
                        if day_total > 0:
                            nap = max(day_total - total_sleep, 0)
                            report.append(
                                f"- 睡眠构成: 夜间主睡 **{total_sleep}** 分钟 + 白天小睡 **{nap}** 分钟 "
                                f"= 当日合计 {day_total} 分钟"
                            )
                            report.append(
                                f"\n> ℹ️ **口径说明**: 以上时长/分期/起床时间均为**夜间主睡**"
                                f"（取自 `DBSleepMainStat`）。当日合计 {day_total} 分钟含白天小睡 {nap} 分钟；"
                                f"深睡比例按主睡 {total_sleep} 分钟计算，避免小睡稀释分母。"
                            )
                        else:
                            # 主睡表有记录、睡眠日汇总表当日无记录时，
                            report.append(
                                f"- 睡眠构成: 夜间主睡 **{total_sleep}** 分钟"
                                f"（睡眠日汇总表当日无记录，无法拆分白天小睡）"
                            )

                    # sleep_score 为 0/NULL 时此前直接 `scores['sleep']=0`
                    #   ——0 分×25% 权重拖垮总分（探针实测：总分大幅下降、等级跌至"需关注"）。
                    #   "有明细但无评分"（固件未算分/新用户）≠"睡得极差"，应视为无效：
                    #   不赋分 → 走缺失排除并归一化；7 天均值仍会在 1.2 节照常展示。
                    if sleep_score > 0:
                        scores['sleep'] = sleep_score
                    else:
                        report.append(
                            "- ⚠️ 当晚未生成睡眠评分（score=0/NULL），睡眠维度将从综合评分中排除；"
                            "时长/分期等明细仅供参考。"
                        )
                    health_data['sleep_last_night'] = {
                        'total_sleep': total_sleep, 'deep_sleep': deep_sleep,
                        'light_sleep': light_sleep, 'rem_sleep': rem_sleep,
                        'sleep_score': sleep_score, 'fall_asleep': fall_asleep
                    }
                else:
                    # 走到这里说明：今晚与回退日期均无任何睡眠记录。
                    #   设备滞后 → 连历史睡眠都缺失，大概率未同步；设备正常 → 当晚确实无监测。
                    #   （同一处日期比较边界：用 .date() 忽略时间部分，见上方 device_lagged）
                    if latest_complete_day.date() < yesterday.date():
                        report.append(
                            f"- ⚠️ 设备数据仅同步至 **{latest_complete_day.strftime('%Y-%m-%d')}**（B8 动态探测），"
                            f"且此前亦无睡眠记录。今晚睡眠大概率尚未同步，不代表未佩戴或未睡眠。"
                        )
                    else:
                        report.append("- 暂无数据（该维度将从综合评分中排除）")

                # 最近7天睡眠
                report.append(f"\n### 1.2 最近7天睡眠统计")
                # ⚠️ L3/L4修复：LEFT JOIN 主睡表，列口径改为「主睡 / 小睡」分离，
                #   入睡时间优先取日汇总表，缺失时用主睡表 sleep_in_minute 回填（D2）。
                #   返回列：0日期 1日汇总总睡眠 2深睡 3浅睡 4REM 5评分 6入睡分钟 7主睡总时长
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='DBSleepMainStat'")
                has_main_7d = cursor.fetchone() is not None
                if has_main_7d:
                    cursor.execute("""
                        SELECT d.date,
                               CAST(d.total_sleep_time AS INTEGER),
                               CAST(d.total_deep_sleep_time AS INTEGER),
                               CAST(d.total_lightly_sleep_time AS INTEGER),
                               CAST(d.total_rem_time AS INTEGER),
                               CAST(d.sleep_score AS INTEGER),
                               COALESCE(NULLIF(CAST(d.fall_asleep AS INTEGER), 0),
                                        CAST(m.sleep_in_minute AS INTEGER), 0),
                               COALESCE(CAST(m.total_sleep_time AS INTEGER),
                                        CAST(d.total_sleep_time AS INTEGER))
                        FROM DBSleepDataStatTable d
                        LEFT JOIN DBSleepMainStat m ON m.date = d.date
                        WHERE d.date >= ? AND d.date <= ?
                        ORDER BY d.date DESC
                    """, (last7_start_str, last7_end_str))
                else:
                    cursor.execute("""
                        SELECT date,
                               CAST(total_sleep_time AS INTEGER),
                               CAST(total_deep_sleep_time AS INTEGER),
                               CAST(total_lightly_sleep_time AS INTEGER),
                               CAST(total_rem_time AS INTEGER),
                               CAST(sleep_score AS INTEGER),
                               CAST(fall_asleep AS INTEGER),
                               CAST(total_sleep_time AS INTEGER)
                        FROM DBSleepDataStatTable
                        WHERE date >= ? AND date <= ?
                        ORDER BY date DESC
                    """, (last7_start_str, last7_end_str))
                rows = cursor.fetchall()

                if rows:
                    # 缺天提示——7 天窗口以日汇总表为驱动表，
                    #   日汇总缺失的日期（主睡表可能独有）会整行消失且无提示；
                    #   运动维度有"仅 N/7"告警（B3），睡眠补上对称保护。
                    _days = len({str(r[0]) for r in rows})
                    if _days < 7:
                        report.append(
                            f"\n> ⚠️ 本窗口仅 **{_days}/7** 天有睡眠日汇总记录，均值为 {_days} 天均值；"
                            f"若主睡表在该缺失日有记录，也不会计入本表（以日汇总表为准）。"
                        )
                    report.append("\n| 日期 | 主睡(分) | 小睡(分) | 深睡(分) | 浅睡(分) | REM(分) | 评分 | 入睡时间 |")
                    report.append("|------|---------|---------|---------|---------|---------|------|---------|")
                    for r in rows:
                        fa_raw = safe_int(r[6])
                        fa = fa_raw % 1440 if fa_raw else 0
                        fa_str = f"{fa//60:02d}:{fa%60:02d}" if fa_raw else "-"
                        main_t = safe_int(r[7]) or (r[1] or 0)
                        nap_t = max((r[1] or 0) - main_t, 0)
                        report.append(f"| {r[0]} | {main_t} | {nap_t} | {r[2] or 0} | {r[3] or 0} | {r[4] or 0} | {r[5] or 0} | {fa_str} |")

                    # 行内口径标注——浅睡列取自睡眠日汇总表，
                    #   设备把白天小睡计入该列的"浅睡"（实测样本：日汇总浅睡
                    #   − 主睡浅睡 = 小睡分钟），导致同一行内 深+浅+REM 之和 = 主睡+小睡
                    #   而对不上主睡列，读者易误判为数据错误。显式说明口径。
                    report.append(
                        "\n> ℹ️ **口径说明**: 深睡/浅睡/REM 列取自睡眠日汇总表（**浅睡含白天小睡**），"
                        "主睡/小睡列由主睡表拆分。同一行内 深+浅+REM 之和 = 主睡+小睡（当日合计），"
                        "不等于主睡列——这不是数据错误。深睡比例按主睡时长计算（见下方评估）。"
                    )

                    # 主睡时长序列（L4：深睡比例/平均时长一律用主睡口径）
                    total_sleeps = [(safe_int(r[7]) or (r[1] or 0)) for r in rows]
                    total_sleeps = [v for v in total_sleeps if v > 0]
                    sleep_scores = [r[5] for r in rows if r[5]]
                    if total_sleeps:
                        avg_sleep = sum(total_sleeps)/len(total_sleeps)
                        report.append(f"\n- 平均夜间主睡时长: **{avg_sleep:.0f} 分钟 ({avg_sleep/60:.1f} 小时)**")
                        report.append(f"- 主睡时长范围: {min(total_sleeps)} ~ {max(total_sleeps)} 分钟")
                        # 修复同类隐患：sleep_7d_avg_hours 原先只在有睡眠评分时才写入，
                        # 评分全缺时 Top3「睡眠时长不足」会因默认值8而永不触发。
                        health_data['sleep_7d_avg_hours'] = avg_sleep/60
                    if sleep_scores:
                        report.append(f"- 平均睡眠评分: **{sum(sleep_scores)/len(sleep_scores):.1f}**")
                        health_data['sleep_7d_avg_score'] = sum(sleep_scores)/len(sleep_scores)

                    # ⚠️ 熬夜统计（必须先于深睡比例评估计算：后者的提示逻辑会引用 valid_fa / late_nights）
                    # ⚠️ 关键修正：fall_asleep 是从“前一天 0 点”起算的分钟数，
                    #   原始值 >= 1440 表示跨午夜（即次日凌晨才入睡）；
                    #   取模后落在 00:00-05:59 同样属于熬夜。
                    def is_late_night(raw):
                        raw_int = safe_int(raw)
                        if not raw_int:
                            return False
                        m = raw_int % 1440
                        return raw_int >= 1440 or m >= 23 * 60 or m < 6 * 60

                    valid_fa = [safe_int(r[6]) for r in rows if safe_int(r[6]) > 0]
                    late_nights = sum(1 for v in valid_fa if is_late_night(v))
                    health_data['late_nights'] = late_nights
                    _late_vals = sorted(v % 1440 for v in valid_fa if is_late_night(v))
                    if _late_vals:
                        _lo, _hi = _late_vals[0], _late_vals[-1]
                        health_data['late_night_range'] = f"{_lo//60:02d}:{_lo%60:02d}~{_hi//60:02d}:{_hi%60:02d}"

                    # ⚠️ 深睡比例评估：正常范围 15-25%，偏低可能与入睡太晚有关
                    deep_ratios = []
                    for r in rows:
                        # L4修复：分母用主睡时长 r[7]（不含小睡），无主睡表时退回日汇总 r[1]
                        total = safe_int(r[7]) or (r[1] or 0)
                        deep = r[2] or 0   # deep_sleep
                        if total > 0 and deep > 0:
                            deep_ratios.append(deep / total * 100)
                    if deep_ratios:
                        avg_deep_ratio = sum(deep_ratios) / len(deep_ratios)
                        report.append(f"- 平均深睡比例: **{avg_deep_ratio:.1f}%**（正常范围 {SLEEP_DEEP_MIN_RATIO}-{SLEEP_DEEP_MAX_RATIO}%）")
                        health_data['avg_deep_ratio'] = avg_deep_ratio
                        if avg_deep_ratio < SLEEP_DEEP_MIN_RATIO:
                            report.append(
                                f"- 深睡评估: 🔴 偏低（{avg_deep_ratio:.1f}% < {SLEEP_DEEP_MIN_RATIO}%）"
                            )
                            # 如果同时熬夜频繁，提示可能相关（需确保有入睡数据，避免空列表时 0>=0 误判）
                            if len(valid_fa) > 0 and health_data.get('late_nights', 0) >= len(valid_fa) * 0.5:
                                report.append(
                                    "> 💡 深睡比例偏低可能与入睡太晚有关。熬夜会推迟深睡周期，"
                                    "导致深睡占比下降。建议提前入睡时间，可能改善深睡质量。"
                                )
                        elif avg_deep_ratio <= SLEEP_DEEP_MAX_RATIO:
                            report.append(f"- 深睡评估: ✅ 正常（{avg_deep_ratio:.1f}%）")
                        else:
                            report.append(f"- 深睡评估: ⚠️ 偏高（{avg_deep_ratio:.1f}% > {SLEEP_DEEP_MAX_RATIO}%，可能数据异常）")

                    # 输出熬夜统计（valid_fa / late_nights 已在深睡比例评估之前计算完成）
                    # 盲审复审 L6：valid_fa 全空（窗口内入睡时间全部缺失）时旧写法
                    #   显示 "0 / 0 天"，与下方"N 天缺失"提示口径打架——统一为缺失文案。
                    if not valid_fa:
                        report.append(
                            f"- 熬夜天数: 无法统计（{len(rows)} 天入睡时间全部缺失，"
                            f"两个睡眠表均无记录）"
                        )
                    else:
                        report.append(f"- 熬夜天数(23点后或凌晨入睡): **{late_nights} / {len(valid_fa)} 天**")
                        # L9修复：有效天数少于记录天数时显式说明，避免读者误以为分母就是7天
                        if len(valid_fa) < len(rows):
                            missing = len(rows) - len(valid_fa)
                            report.append(
                                f"  - ⚠️ 其中 {missing} 天入睡时间缺失（两个睡眠表均无记录），"
                                f"未计入熬夜统计，分母为 {len(valid_fa)} 而非 {len(rows)} 天"
                            )
                else:
                    report.append("- 暂无数据")
            else:
                report.append("\n- 睡眠数据表不存在（该维度将从综合评分中排除）")
        except Exception as e:
            report.append(f"\n- 睡眠分析出错: {str(e)}")
            # 兜底隐患修复：不再静默赋分，缺失维度将在综合评分段被显式排除并标红

        # ==================== 2. OSA/打鼾分析（新增）====================
        report.append(f"\n## 😴 二、睡眠呼吸健康（OSA/打鼾）")

        try:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='DBOsaResult'")
            if cursor.fetchone():
                report.append(f"\n### 2.1 最近7天睡眠呼吸暂停分析")
                cursor.execute("""
                    SELECT date,
                           CAST(osa_level AS INTEGER),
                           CAST(ahi AS REAL),
                           CAST(snore_ratio AS REAL),
                           CAST(silenced_ratio AS REAL),
                           CAST(silenced_time AS INTEGER)
                    FROM DBOsaResult
                    WHERE date >= ? AND date <= ?
                    ORDER BY date DESC
                """, (last7_start_str, last7_end_str))
                rows = cursor.fetchall()

                if rows:
                    report.append("\n| 日期 | OSA等级 | AHI指数 | 打鼾比例(%) | 静音比例(%) | 静音时长(秒) |")
                    report.append("|------|---------|---------|-----------|-----------|------------|")
                    for r in rows:
                        # r[1] 为 NULL 时原 else str(r[1]) 会渲染成 "None"，
                        #   改为显示 "—"（与同表其余列的兜底风格一致）。
                        osa_level_str = (["正常", "轻度", "中度", "重度"][r[1]]
                                         if r[1] is not None and 0 <= r[1] <= 3
                                         else ("—" if r[1] is None else str(r[1])))
                        report.append(f"| {r[0]} | {osa_level_str} | {r[2] or 0:.1f} | {r[3] or 0:.1f} | {r[4] or 0:.1f} | {r[5] or 0} |")

                    # ⚠️ L4修复：用 is not None 而不是 if r[2]，避免把 ahi=0 的记录过滤掉。
                    ahis = [r[2] for r in rows if r[2] is not None]
                    osa_levels = [r[1] for r in rows if r[1] is not None]
                    if ahis:
                        avg_ahi = sum(ahis)/len(ahis)
                        report.append(f"\n- 平均AHI指数: **{avg_ahi:.1f}** 次/小时")
                        if avg_ahi < MED_THRESHOLDS["ahi_mild"]:
                            report.append(f"- AHI评估: ✅ 正常（<{MED_THRESHOLDS['ahi_mild']}）")
                        elif avg_ahi < MED_THRESHOLDS["ahi_severe"]:
                            report.append(f"- AHI评估: ⚠️ 轻度睡眠呼吸暂停（{MED_THRESHOLDS['ahi_mild']}-{MED_THRESHOLDS['ahi_severe']}）")
                        elif avg_ahi < MED_THRESHOLDS["ahi_moderate"]:
                            report.append(f"- AHI评估: 🔴 中度睡眠呼吸暂停（{MED_THRESHOLDS['ahi_severe']}-{MED_THRESHOLDS['ahi_moderate']}），建议就医")
                        else:
                            report.append(f"- AHI评估: 🔴 重度睡眠呼吸暂停（>{MED_THRESHOLDS['ahi_moderate']}），强烈建议就医")
                        health_data['avg_ahi'] = avg_ahi
                        scores['osa'] = max(0, 100 - avg_ahi * 5)
                        # 数据可信度校验：AHI 逐日恒定 + 打鼾记录全为0 => 疑似默认估算值
                        # L5修复：原实现未查询任何 DBSnore* 表，却硬编码断言"打鼾明细表为空"，
                        #   实际 DBSnoreResult 有记录（只是值全为0）。此处真实统计后再生成文案。
                        #   浮点全同——真实设备估算值往往多天全同、仅个别天存在微小偏差，
                        #   1 天噪声即击穿判定。
                        #   改为众数占比判定：≥80% 的天数为同一值（且 ≥5 天）即视为疑似默认估算。
                        if len(ahis) >= 5:
                            from collections import Counter
                            _cnt = Counter(round(a, 4) for a in ahis if a is not None)
                            _mode_val, _mode_cnt = (_cnt.most_common(1)[0] if _cnt else (None, 0))
                            _mode_ratio = (_mode_cnt / len(ahis)) if _cnt else 0.0
                        else:
                            _mode_val, _mode_cnt, _mode_ratio = None, 0, 0.0
                        if _mode_ratio >= 0.8:
                            health_data['osa_unreliable'] = True
                            snore_desc = "打鼾明细表（DBSnore*）不存在或无法查询"
                            try:
                                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='DBSnoreResult'")
                                if cursor.fetchone():
                                    cursor.execute("SELECT COUNT(*), SUM(CASE WHEN CAST(snore_sum_time AS INTEGER) > 0 THEN 1 ELSE 0 END) FROM DBSnoreResult")
                                    srow = cursor.fetchone()
                                    snore_total = srow[0] or 0
                                    snore_nonzero = srow[1] or 0
                                    if snore_total == 0:
                                        snore_desc = "打鼾明细表 DBSnoreResult 为**空表（0 行）**"
                                    elif snore_nonzero == 0:
                                        snore_desc = (f"打鼾明细表 DBSnoreResult 有 **{snore_total} 行记录但打鼾时长全部为 0**"
                                                      f"（非空表，只是没有采集到打鼾事件）")
                                    else:
                                        snore_desc = (f"打鼾明细表 DBSnoreResult 有 {snore_total} 行，"
                                                      f"其中 {snore_nonzero} 行打鼾时长 > 0")
                            except Exception as _e:
                                # 同上，异常不再静默吞掉。
                                log(f"  ⚠️ [降级] 打鼾明细统计失败，已跳过该段描述：{_e}")
                            report.append(
                                # 修复：原文案"最近 N 天 AHI 全部为同一个值
                                #   {ahis[0]}"与新的众数占比判定不符——6/7 同值 + 1 天噪声时，
                                #   既说"全部"又引用了恰好是离群值的最新一天。改为如实描述众数占比。
                                f"\n> ⚠️ **数据可信度提示**: 最近 {len(ahis)} 天中有 {_mode_cnt} 天 AHI "
                                f"为同一值 {_mode_val:.2f}（众数占比 {_mode_ratio*100:.0f}%）；{snore_desc}。"
                                f"二者结合说明很可能未开启打鼾/呼吸监测，AHI 为默认估算值。"
                                f"OSA 评分仅供参考，不能作为睡眠呼吸暂停的判断依据。"
                                f"\n> 📊 **综合评分调整**: 由于OSA数据不可靠，该维度（权重10%）已从综合评分中排除，其余维度权重已重新归一化。"
                            )
                    else:
                        # 同类隐患修复：有 OSA 记录但 AHI 全为空时，原实现不赋值 → 兜底70。
                        #   此处显式标记不可靠并从综合评分中排除，避免"无数据"被当成"中等健康"。
                        report.append("\n- AHI: ⚠️ 最近7天无有效 AHI 数值，OSA 维度标记为不可靠")
                        health_data['osa_unreliable'] = True
                    if osa_levels:
                        abnormal_osa = sum(1 for l in osa_levels if l >= 1)
                        # 分母改用「有效等级天数」len(osa_levels)
                        #   （原用含层级 NULL 的 len(rows)，NULL 天无法判异常却计入分母 → 比例被低报）；
                        #   若有层级缺失，另行注明。
                        _lv_null = len(rows) - len(osa_levels)
                        _null_note = f"（另有 {_lv_null} 天等级缺失未计入）" if _lv_null > 0 else ""
                        report.append(f"- OSA异常天数: **{abnormal_osa} / {len(osa_levels)} 天**{_null_note}")
                else:
                    report.append("- 暂无数据（该维度将从综合评分中排除）")
            else:
                report.append("\n- OSA数据表不存在（该维度将从综合评分中排除）")

            # 打鼾分析
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='DBSnoreResult'")
            if cursor.fetchone():
                report.append(f"\n### 2.2 最近7天打鼾分析")
                cursor.execute("""
                    SELECT date,
                           CAST(snore_mean_db AS REAL),
                           CAST(snore_max_db AS REAL),
                           CAST(snore_sum_time AS INTEGER),
                           CAST(snore_sum_num AS INTEGER)
                    FROM DBSnoreResult
                    WHERE date >= ? AND date <= ?
                    ORDER BY date DESC
                """, (last7_start_str, last7_end_str))
                rows = cursor.fetchall()

                if rows:
                    report.append("\n| 日期 | 平均分贝 | 最大分贝 | 打鼾总时长(秒) | 打鼾次数 |")
                    report.append("|------|---------|---------|--------------|---------|")
                    for r in rows:
                        report.append(f"| {r[0]} | {r[1] or 0:.1f} | {r[2] or 0:.1f} | {r[3] or 0} | {r[4] or 0} |")

                    mean_dbs = [r[1] for r in rows if r[1]]
                    if mean_dbs:
                        report.append(f"\n- 平均打鼾分贝: **{sum(mean_dbs)/len(mean_dbs):.1f} dB**")
                else:
                    report.append("- 暂无数据")
        except Exception as e:
            report.append(f"\n- OSA/打鼾分析出错: {str(e)}")
            # 兜底隐患修复：不再静默赋分，缺失维度将在综合评分段被显式排除并标红

        # ==================== 3. 心率分析 ====================
        report.append(f"\n## ❤️ 三、心率分析")

        try:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='DBHeartRateDataStatTable'")
            if cursor.fetchone():
                report.append(f"\n### 3.1 最近7天心率统计")
                cursor.execute("""
                    SELECT date,
                           CAST(max_hr AS INTEGER),
                           CAST(min_hr AS INTEGER),
                           CAST(average_hr AS INTEGER),
                           CAST(rest_hr AS INTEGER),
                           CAST(walk_avg_hr AS INTEGER),
                           CAST(sleep_base_hr AS INTEGER)
                    FROM DBHeartRateDataStatTable
                    WHERE date >= ? AND date <= ?
                    ORDER BY date DESC
                """, (last7_start_str, last7_end_str))
                rows = cursor.fetchall()

                if rows:
                    report.append("\n| 日期 | 最大 | 最小 | 平均 | 静息 | 步行平均 | 睡眠基础 |")
                    report.append("|------|------|------|------|------|---------|---------|")
                    for r in rows:
                        report.append(f"| {r[0]} | {r[1] or '-'} | {r[2] or '-'} | {r[3] or '-'} | {r[4] or '-'} | {r[5] or '-'} | {r[6] or '-'} |")

                    #   全零/全空时列表为空 → scores 从不赋值 → 综合评分兜底 70，
                    #   既掩盖了数据缺失，也让"没数据"和"数据一般"无法区分。
                    avg_hrs = [safe_int(r[3]) for r in rows if r[3] is not None and safe_int(r[3]) > 0]
                    rest_hrs = [safe_int(r[4]) for r in rows if r[4] is not None and safe_int(r[4]) > 0]
                    if not rest_hrs:
                        # 有记录但静息心率全为0/无效：视为无效数据，显式给中性分并说明，阻断后续评分覆盖
                        report.append("\n- 静息心率: ⚠️ 最近7天静息心率均为 0（无效数据），心率维度按中性分 70 计")
                        scores['heart_rate'] = 70
                        health_data['avg_rest_hr'] = 0
                    else:
                        if avg_hrs:
                            report.append(f"\n- 平均心率: **{sum(avg_hrs)/len(avg_hrs):.0f}** bpm")
                            health_data['avg_hr'] = sum(avg_hrs)/len(avg_hrs)
                        avg_rest = sum(rest_hrs)/len(rest_hrs)
                        report.append(f"- 平均静息心率: **{avg_rest:.0f}** bpm")
                        if MED_THRESHOLDS["rest_hr_low_normal"] <= avg_rest <= MED_THRESHOLDS["rest_hr_elevated"]:
                            report.append(f"- 静息心率评估: ✅ 正常范围（{MED_THRESHOLDS['rest_hr_low_normal']}-{MED_THRESHOLDS['rest_hr_elevated']}）")
                        elif avg_rest < MED_THRESHOLDS["rest_hr_low_normal"]:
                            report.append("- 静息心率评估: ✅ 偏低（可能是心肺功能好，运动员常见）")
                        else:
                            report.append("- 静息心率评估: ⚠️ 偏高，建议关注")
                        health_data['avg_rest_hr'] = avg_rest
                        if 60 <= avg_rest <= 80:
                            scores['heart_rate'] = 100
                        elif 50 <= avg_rest < 60 or 80 < avg_rest <= 90:
                            scores['heart_rate'] = 80
                        else:
                            scores['heart_rate'] = 60
                else:
                    report.append("- 暂无数据（该维度将从综合评分中排除）")
            else:
                report.append("\n- 心率数据表不存在（该维度将从综合评分中排除）")
        except Exception as e:
            report.append(f"\n- 心率分析出错: {str(e)}")
            # 兜底隐患修复：不再静默赋分，缺失维度将在综合评分段被显式排除并标红

        # ==================== 4. HRV 专章分析（新增）====================
        report.append(f"\n## 💓 四、心率变异性（HRV）分析")

        try:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='DBStressTable'")
            if cursor.fetchone():
                report.append(f"\n### 4.1 最近7天 HRV 统计")
                # ⚠️ L2修复：用自然日开始时间（00:00:00），与其他维度一致，
                #   而不是滚动144小时窗口（last7_start.timestamp()会少当天0点到当前时刻的数据）
                last7_start_midnight = last7_start.replace(hour=0, minute=0, second=0, microsecond=0)
                seven_days_ago_ts = ts_ms(last7_start_midnight)
                # D1进阶：HRV 同样限定为「7个完整日」，上限截到昨日 23:59:59.999，排除今日不完整数据
                last7_end_eod = last7_end.replace(hour=23, minute=59, second=59, microsecond=999000)
                seven_days_end_ts = ts_ms(last7_end_eod)
                # ⚠️ L1修复：WHERE不过滤sdnn>0，用子查询分别计算总数和有效数。
                #   真实存在一定比例无效记录被掩盖（总数应大于有效数）。
                cursor.execute("""
                    SELECT
                        AVG(CASE WHEN CAST(sdnn AS INTEGER) > 0 THEN CAST(sdnn AS INTEGER) END) as avg_sdnn,
                        AVG(CASE WHEN CAST(sdnn AS INTEGER) > 0 THEN CAST(rmssd AS INTEGER) END) as avg_rmssd,
                        MIN(CASE WHEN CAST(sdnn AS INTEGER) > 0 THEN CAST(sdnn AS INTEGER) END) as min_sdnn,
                        MAX(CASE WHEN CAST(sdnn AS INTEGER) > 0 THEN CAST(sdnn AS INTEGER) END) as max_sdnn,
                        COUNT(*) as total_records,
                        SUM(CASE WHEN CAST(sdnn AS INTEGER) > 0 THEN 1 ELSE 0 END) as valid_records
                    FROM DBStressTable
                    WHERE CAST(data_created_timestamp AS INTEGER) >= ? AND CAST(data_created_timestamp AS INTEGER) < ?
                """, (seven_days_ago_ts, seven_days_end_ts))
                row = cursor.fetchone()

                # 窗口内无记录时 SQL SUM() 返回 NULL（None），
                #   原判据 None > 0 抛 TypeError → 被 except 捕获后显示"HRV分析出错"，
                #   下方"暂无 HRV 数据"优雅分支（1031行）永远不可达。safe_int(None)=0 修复，
                #   使无数据场景正确落入 else 分支（实测复现见 logs/audit_round2_boundary.py）。
                if row and safe_int(row[5]) > 0:
                    avg_sdnn = row[0] or 0
                    # 盲审复审 L-5：AVG 在"有有效 sdnn 记录但 rmssd 全空"时返回 NULL，
                    #   旧 `or 0` 会折叠成 0.0 并输出"平均 RMSSD 0.0ms"的虚假过低告警
                    #   且扣 30 分，违反本文件"缺失→排除"原则——改为保留 None 分支。
                    avg_rmssd = row[1]
                    valid = row[5] or 0
                    total = row[4] or 0

                    report.append(f"- 有效 HRV 记录数: **{valid} / {total}** 条")
                    if avg_sdnn is not None:
                        report.append(f"- 平均 SDNN: **{avg_sdnn:.1f} ms**")
                    if avg_rmssd is not None:
                        report.append(f"- 平均 RMSSD: **{avg_rmssd:.1f} ms**")
                    if row[2]:
                        report.append(f"- SDNN 范围: {row[2]} ~ {row[3]} ms")

                    report.append(f"\n### 4.2 HRV 评估")
                    if avg_rmssd is None:
                        report.append("- RMSSD 评估: —（本窗口无有效 RMSSD 值，该维度不计分）")
                    elif avg_rmssd >= MED_THRESHOLDS["rmssd_normal"]:
                        report.append(f"- RMSSD 评估: ✅ 良好（≥{MED_THRESHOLDS['rmssd_normal']}ms，副交感神经活跃，恢复能力好）")
                        scores['hrv'] = 90
                    elif avg_rmssd >= MED_THRESHOLDS["rmssd_mid"]:
                        report.append(f"- RMSSD 评估: ⚠️ 中等（{MED_THRESHOLDS['rmssd_mid']}-{MED_THRESHOLDS['rmssd_normal']}ms，恢复能力一般）")
                        scores['hrv'] = 70
                    elif avg_rmssd >= MED_THRESHOLDS["rmssd_low"]:
                        report.append(f"- RMSSD 评估: ⚠️ 偏低（{MED_THRESHOLDS['rmssd_low']}-{MED_THRESHOLDS['rmssd_mid']}ms，可能压力较大或恢复不足）")
                        scores['hrv'] = 50
                    else:
                        report.append(f"- RMSSD 评估: 🔴 过低（<{MED_THRESHOLDS['rmssd_low']}ms，建议关注压力和恢复）")
                        scores['hrv'] = 30

                    report.append(f"\n> 💡 HRV 说明：SDNN 反映整体心率变异性，RMSSD 反映副交感神经活性（与压力和恢复密切相关）。RMSSD 越高通常表示身体恢复能力越好、压力越小。")

                    # D6修复：检查SDNN<RMSSD的记录比例
                    try:
                        cursor.execute("""
                            SELECT
                                SUM(CASE WHEN CAST(sdnn AS INTEGER) > 0 AND CAST(rmssd AS INTEGER) > 0
                                          AND CAST(rmssd AS INTEGER) > CAST(sdnn AS INTEGER) THEN 1 ELSE 0 END) as inverted,
                                SUM(CASE WHEN CAST(sdnn AS INTEGER) > 0 AND CAST(rmssd AS INTEGER) > 0 THEN 1 ELSE 0 END) as total
                            FROM DBStressTable
                            WHERE CAST(data_created_timestamp AS INTEGER) >= ? AND CAST(data_created_timestamp AS INTEGER) < ?
                        """, (seven_days_ago_ts, seven_days_end_ts))
                        hrv_row = cursor.fetchone()
                        # 同 P7——SUM 空集返回 None，
                        #   原判据 TypeError 后被 except: pass 静默吞掉，倒置检查悄然失效。
                        if hrv_row and safe_int(hrv_row[1]) > 0:
                            inverted_ratio = hrv_row[0] / hrv_row[1] * 100
                            if inverted_ratio > 50:
                                report.append(
                                    f"\n> ⚠️ **HRV字段口径提示**: 最近7天有 {inverted_ratio:.1f}% 的记录 "
                                    f"RMSSD > SDNN（{hrv_row[0]}/{hrv_row[1]}条）。生理上SDNN通常>=RMSSD，"
                                    f"持续倒置说明两字段可能来自不同算法或时间窗口，HRV评分仅供参考。"
                                )
                    except Exception as _e:
                        # 原为 except: pass。本处作者注释早已承认
                        #   "源判据 TypeError 后被 except: pass 静默吞掉，倒置检查悄然失效"——
                        #   正是静默吞异常的典型危害：数据口径异常却无人察觉。现显式记录。
                        log(f"  ⚠️ [降级] HRV 倒置检查失败，已跳过该提示"
                            f"（HRV 评分仍按原口径给出）：{_e}")

                    health_data['avg_sdnn'] = avg_sdnn
                    health_data['avg_rmssd'] = avg_rmssd
                else:
                    # L4修复：无 HRV 数据不再赋中性分 70（与 B9"缺失→排除→归一化"原则统一）
                    report.append("- 暂无 HRV 数据（该维度将从综合评分中排除）")
            else:
                # L4修复：DBStressTable 不存在时此前给 hrv=70 计入——与缺失排除原则矛盾
                report.append("\n- 压力数据表不存在（HRV/压力维度将从综合评分中排除）")
        except Exception as e:
            report.append(f"\n- HRV 分析出错: {str(e)}")
            # 兜底隐患修复：不再静默赋分，缺失维度将在综合评分段被显式排除并标红

        # ==================== 5. 运动分析 ====================
        report.append(f"\n## 🏃 五、运动分析")

        try:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='DBSportDataStat'")
            if cursor.fetchone():
                report.append(f"\n### 5.1 最近7天运动统计（sport_mode=-2）")
                # 口径说明（排查报告）：实测 mode 0/1/2/6 的时长之和"约等于"
                #   -2 的时长——细分关系成立，但个别天存在 1 分钟级舍入差）；
                #   -2/-3/-4 步数也基本同源（可有极小差异）。
                #   若把各 mode 相加会重复计算，故只取 -2 即全天总量。
                report.append("\n> ℹ️ **口径说明**: 步数取 `sport_mode=-2`（全天汇总口径）。"
                              "该表其余 mode（0/1/2/6 等）的时长为 -2 的**近似**细分（个别天存在 1 分钟级舍入差）、"
                              "-2/-3/-4 步数基本同源，相加会重复计算，故只取 -2。")
                cursor.execute("""
                    SELECT date,
                           CAST(total_steps AS INTEGER),
                           CAST(total_distance AS INTEGER),
                           CAST(total_calories AS INTEGER),
                           CAST(total_duration AS INTEGER),
                           CAST(total_workout_minutes AS INTEGER)
                    FROM DBSportDataStat
                    WHERE date >= ? AND date <= ? AND sport_mode = -2
                    ORDER BY date DESC
                """, (last7_start_str, last7_end_str))
                rows = cursor.fetchall()

                if rows:
                    # B3修复：检查7天窗口覆盖度，缺天时显式警告
                    if len(rows) < 7:
                        report.append(f"\n> ⚠️ 本窗口仅 **{len(rows)}/7** 天有运动数据，均值为 {len(rows)} 天均值，总量可能被低估")
                    report.append("\n| 日期 | 步数 | 距离(公里) | 活动消耗(千卡)| 活动时长(分) | 中高强度(分) |")
                    report.append("|------|------|---------|------------|------------|------------|")
                    for r in rows:
                        calories = (r[3] or 0) / 1000
                        duration = (r[4] or 0) / 60000
                        report.append(f"| {r[0]} | {r[1] or 0} | {(r[2] or 0)/1000:.2f} | {calories:.1f} | {duration:.1f} | {r[5] or 0} |")

                    # L2修复：不过滤0，全零场景也正确计算评分（与压力bug #21 同类修复）
                    steps = [safe_int(r[1]) for r in rows]
                    workout_mins = [safe_int(r[5]) for r in rows if r[5] is not None]
                    avg_steps = sum(steps)/len(steps) if steps else 0
                    health_data['avg_steps'] = avg_steps  # L2修复：恒写入，避免get默认值8000导致不报警
                    report.append(f"\n- 平均步数: **{avg_steps:.0f}** 步")
                    if steps:
                        report.append(f"- 步数范围: {min(steps)} ~ {max(steps)} 步")
                    if avg_steps >= TARGET_DAILY_STEPS:
                        report.append(f"- 步数评估: ✅ 达标（≥{TARGET_DAILY_STEPS}步）")
                        scores['exercise'] = 90
                    elif avg_steps >= 5000:
                        report.append("- 步数评估: ⚠️ 基本达标（5000-8000步）")
                        scores['exercise'] = 70
                    elif avg_steps > 0:
                        report.append("- 步数评估: 🔴 不足（<5000步，建议增加活动量）")
                        scores['exercise'] = 40
                    else:
                        report.append("- 步数评估: 🔴 严重不足（完全没有步数记录）")
                        scores['exercise'] = 20  # L2修复：全零场景给最低分，不是兜底70
                    calories_list = [(r[3] or 0) / 1000 for r in rows if r[3]]
                    if calories_list:
                        avg_calories = sum(calories_list) / len(calories_list)
                        report.append(f"- 平均活动消耗: **{avg_calories:.1f}** 千卡/天（设备原始值单位为毫千卡，已÷1000）")
                    if workout_mins:
                        total_workout = sum(workout_mins)
                        report.append(f"- 7天中高强度运动总时长: **{total_workout}** 分钟（推荐每周≥{TARGET_WEEKLY_EXERCISE}分钟）")
                        if total_workout >= TARGET_WEEKLY_EXERCISE:
                            report.append("- 中高强度运动评估: ✅ 达标")
                        elif total_workout > 0:
                            report.append(f"- 中高强度运动评估: ⚠️ 不足（还差 {TARGET_WEEKLY_EXERCISE-total_workout} 分钟）")
                        else:
                            # L2回归修复：workout_mins 不再过滤0后，全零场景会走进本分支，
                            #   若不加此判断会退化显示成"⚠️ 不足（还差150分钟）"，掩盖"完全没有运动"。
                            report.append("- 中高强度运动评估: 🔴 严重不足（完全没有中高强度运动）")
                        health_data['total_workout_7d'] = total_workout
                    else:
                        # ⚠️ L5修复+P1对齐：有行但 workout 列全空 → 写入 0
                        #   （这是数据层面的真实"无运动记录"，非数据缺失）；Top3 判据为
                        #   "键存在才判定"，0<150 照常报警，不再依赖 get 默认值语义。
                        total_workout = 0
                        health_data['total_workout_7d'] = 0
                        report.append(f"- 7天中高强度运动总时长: **0** 分钟（推荐每周≥{TARGET_WEEKLY_EXERCISE}分钟）")
                        report.append("- 中高强度运动评估: 🔴 严重不足（完全没有中高强度运动）")
                else:
                    report.append("- 暂无数据（该维度将从综合评分中排除）")
                    # 窗口无数据 ≠ 运动 0 分钟——
                    #   改为不写入（键缺失），Top3 判据同步改为"键存在才判定"。
            else:
                report.append("\n- 运动数据表不存在（该维度将从综合评分中排除）")
        except Exception as e:
            report.append(f"\n- 运动分析出错: {str(e)}")
            # 兜底隐患修复：不再静默赋分，缺失维度将在综合评分段被显式排除并标红

        # ==================== 6. 压力分析 ====================
        report.append(f"\n## 😰 六、压力分析")

        try:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='DBStressDataStatTable'")
            if cursor.fetchone():
                report.append("\n### 6.1 最近7天压力统计")
                report.append("\n> ⚠️ 注意：")
                report.append("> 1. 压力时长字段是采样点个数，每个采样点约2分钟，实际时长=个数×2分钟")
                report.append('> 2. "压力监测心率"是压力监测期间的心率（通常在静止/睡眠时测量，偏低是正常的），不是全天平均心率')
                cursor.execute("""
                    SELECT date,
                           CAST(average_hr AS INTEGER),
                           CAST(relax_stress_total_time AS INTEGER),
                           CAST(normal_stress_total_time AS INTEGER),
                           CAST(middle_stress_total_time AS INTEGER),
                           CAST(high_stress_total_time AS INTEGER)
                    FROM DBStressDataStatTable
                    WHERE date >= ? AND date <= ?
                    ORDER BY date DESC
                """, (last7_start_str, last7_end_str))
                rows = cursor.fetchall()

                if rows:
                    report.append("\n| 日期 | 压力监测心率 | 放松(点) | 正常(点) | 中等(点) | 高压(点) | 总时长(小时) |")
                    report.append("|------|---------|---------|---------|---------|---------|------------|")
                    for r in rows:
                        total_samples = (r[2] or 0) + (r[3] or 0) + (r[4] or 0) + (r[5] or 0)
                        total_hours = total_samples * 2 / 60
                        report.append(f"| {r[0]} | {r[1] or '-'} | {r[2] or 0} | {r[3] or 0} | {r[4] or 0} | {r[5] or 0} | {total_hours:.1f} |")

                    # 字段可信度校验：人体静息心率不可能低至 30 上下，说明该列并非真实心率
                    hr_col = [r[1] for r in rows if r[1]]
                    if hr_col and max(hr_col) < 45:
                        report.append(
                            f"\n> ⚠️ **字段存疑**: 该列取值 {min(hr_col)}~{max(hr_col)}，远低于人体静息心率下限"
                            f"（约 40 bpm），并非真实心率，字段实际含义未知，请勿按心率解读。"
                        )

                    # ⚠️ 关键修正：不过滤0，否则连续7天零高压时 high_stress 为空，
                    #   下面的 if high_stress: 不进入，scores['stress'] 从未赋值（兜底70）。
                    #   结果是：最健康的零高压反而只有70分。
                    high_stress_all = [safe_int(r[5]) for r in rows]
                    avg_high = sum(high_stress_all)/len(high_stress_all) if high_stress_all else 0
                    report.append(f"\n- 平均高压采样点: {avg_high:.1f} 个/天")
                    if avg_high == 0:
                        report.append("- 压力评估: ✅ 无高压状态（最健康）")
                        scores['stress'] = 100
                    elif avg_high < 5:
                        report.append("- 压力评估: ✅ 良好（高压时间少）")
                        scores['stress'] = 85
                    elif avg_high < 20:
                        report.append("- 压力评估: ⚠️ 中等（有一定高压时间）")
                        scores['stress'] = 65
                    else:
                        report.append("- 压力评估: 🔴 偏高（高压时间较多，建议放松）")
                        scores['stress'] = 40
                else:
                    report.append("- 暂无数据（该维度将从综合评分中排除）")
            else:
                # L4修复：压力表不存在时此前给 stress=70 计入（文案却说会排除）——统一为排除
                report.append("\n- 压力数据表不存在（该维度将从综合评分中排除）")
        except Exception as e:
            report.append(f"\n- 压力分析出错: {str(e)}")
            # 兜底隐患修复：不再静默赋分，缺失维度将在综合评分段被显式排除并标红

        # ==================== 7. 血氧分析 ====================
        report.append(f"\n## 🩸 七、血氧分析")

        try:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='DBBloodOxygenSaturationDataStat'")
            if cursor.fetchone():
                report.append(f"\n### 7.1 最近7天血氧统计")
                cursor.execute("""
                    SELECT date,
                           CAST(max_blood_oxygen_saturation AS INTEGER),
                           CAST(min_blood_oxygen_saturation AS INTEGER),
                           CAST(average_blood_oxygen_saturation AS INTEGER),
                           CAST(low_blood_oxygen_saturation_total_time AS INTEGER)
                    FROM DBBloodOxygenSaturationDataStat
                    WHERE date >= ? AND date <= ?
                    ORDER BY date DESC
                """, (last7_start_str, last7_end_str))
                rows = cursor.fetchall()

                if rows:
                    report.append("\n| 日期 | 最高 | 最低 | 平均 | 低血氧时长(秒) |")
                    report.append("|------|------|------|------|---------------|")
                    for r in rows:
                        report.append(f"| {r[0]} | {r[1] or '-'} | {r[2] or '-'} | {r[3] or '-'} | {r[4] or 0} |")

                    avg_spo2 = [r[3] for r in rows if r[3]]
                    if avg_spo2:
                        avg = sum(avg_spo2)/len(avg_spo2)
                        report.append(f"\n- 平均血氧: **{avg:.1f}%**")
                        if avg >= 95:
                            report.append(f"- 血氧评估: ✅ 正常（≥{MED_THRESHOLDS['spo2_normal']}%）")
                            scores['spo2'] = 100
                        elif avg >= 90:
                            report.append(f"- 血氧评估: ⚠️ 偏低（{MED_THRESHOLDS['spo2_low']}-{MED_THRESHOLDS['spo2_normal']}%，建议关注）")
                            scores['spo2'] = 70
                        else:
                            report.append(f"- 血氧评估: 🔴 过低（<{MED_THRESHOLDS['spo2_low']}%，建议就医）")
                            scores['spo2'] = 40
                        health_data['avg_spo2'] = avg

                        # ⚠️ 异常值检测：单次低血氧但低血氧时长为0 => 疑似手表松动误测
                        # 正常低血氧应该持续一段时间（时长>0），单次极低值+时长0通常是测量误差
                        suspicious_days = []
                        for r in rows:
                            min_val = r[2]
                            low_time = r[4] or 0
                            if min_val and min_val < 90 and low_time == 0:
                                suspicious_days.append((r[0], min_val))
                        if suspicious_days:
                            report.append(
                                f"\n> ⚠️ **疑似单次误测**: {len(suspicious_days)} 天出现最低血氧 < 90% "
                                f"但低血氧时长为 0 秒（{', '.join(f'{d}({v}%)' for d, v in suspicious_days)}）。"
                                f"正常低血氧应持续一段时间，单次极低值+时长0通常是手表松动/佩戴不当导致的测量误差，"
                                f"不代表真实持续低血氧。"
                            )
                            # 如果只有1天异常且其他天都正常，更可能是误测，不影响整体评分
                            if len(suspicious_days) == 1 and len(rows) > 3:
                                normal_days = [r for r in rows if r[2] and r[2] >= 90]
                                if len(normal_days) >= len(rows) - 1:
                                    report.append(
                                        "> ℹ️ 仅1天异常且其余天数均正常，判定为单次误测，不影响血氧整体评估。"
                                    )
                            # D3增强：全表口径提示，避免把偶发误测与系统性噪声混淆
                            if len(suspicious_days) > 3:
                                report.append(
                                    f"> 📌 注意：异常天数已达 {len(suspicious_days)} 天，属于**系统性测量噪声**"
                                    f"（多为佩戴松动），而非偶发单次误测；建议检查佩戴方式，"
                                    f"且本维度评分主要依据平均值，个别极低值不会直接拉低评分。"
                                )
                    else:
                        # 同类隐患修复：有记录但平均血氧全为0/空 → 显式给分并说明，
                        # 避免 scores['spo2'] 不赋值后被综合评分兜底成 70（与"数据一般"无法区分）
                        report.append("\n- 平均血氧: ⚠️ 最近7天平均血氧均为 0 或缺失（无效数据），血氧维度按中性分 70 计")
                        scores['spo2'] = 70
                else:
                    report.append("- 暂无数据（该维度将从综合评分中排除）")
            else:
                report.append("\n- 血氧数据表不存在（该维度将从综合评分中排除）")
        except Exception as e:
            report.append(f"\n- 血氧分析出错: {str(e)}")
            # 兜底隐患修复：不再静默赋分，缺失维度将在综合评分段被显式排除并标红

        # ==================== 8. 综合评分（新增）====================
        report.append(f"\n## 📊 八、综合健康评分")

        weights = {
            'sleep': 0.25,
            'heart_rate': 0.15,
            'hrv': 0.15,
            'exercise': 0.15,
            'stress': 0.10,
            'spo2': 0.10,
            'osa': 0.10,
        }

        total_score = 0
        total_weight = 0
        excluded_dims = []
        if health_data.get('osa_unreliable', False):
            excluded_dims.append('osa')

        # 兜底隐患修复：显式识别「未计算出分数」的维度（分析整段抛异常或表不存在/无数据分支未赋值），
        #   不再用 scores.get(dim, 70) 静默取 70（会让"无数据"与"数据一般"无法区分）。
        #   缺失维度加入排除列表，由其余维度重新归一化，并在报告中标红提示。
        missing_dims = [d for d in weights if d not in scores]
        for d in missing_dims:
            if d not in excluded_dims:
                excluded_dims.append(d)

        active_weights = {k: v for k, v in weights.items() if k not in excluded_dims}
        weight_sum = sum(active_weights.values())
        active_weights = {k: v / weight_sum for k, v in active_weights.items()} if weight_sum > 0 else {}

        # P4修复：整数百分比显示用最大余数法（合计恒=100），加权计算仍用未舍入权重
        pct_disp = _pct_display(active_weights)

        dim_names_disp = {
            'sleep': '😴 睡眠', 'heart_rate': '❤️ 心率', 'hrv': '💓 HRV',
            'exercise': '🏃 运动', 'stress': '😰 压力', 'spo2': '🩸 血氧', 'osa': '😴 OSA'
        }
        report.append(f"\n### 8.1 各维度评分")
        if excluded_dims:
            excluded_disp = [dim_names_disp.get(d, d) for d in excluded_dims]
            report.append(f"\n> ⚠️ 以下维度因数据不可靠/缺失已从综合评分中排除: {', '.join(excluded_disp)}")
            report.append(f"> 其余维度权重已重新归一化（总和=100%）")
        report.append("\n| 维度 | 评分 | 权重 | 加权得分 |")
        report.append("|------|------|------|---------|")
        # 兜底隐患修复：仅遍历「确有分数」的维度，缺失维度已在上文排除；
        #   此处用 scores[dim] 而非 get(dim,70)，彻底消除静默兜底。
        for dim, weight in active_weights.items():
            score = scores.get(dim)  # dim 必在 scores 中（已排除缺失项），get 仅作防御
            if score is None:
                continue
            weighted = score * weight
            total_score += weighted
            total_weight += weight
            # 维度名统一复用 dim_names_disp，避免双份漂移。
            report.append(f"| {dim_names_disp.get(dim, dim)} | {score:.0f} | {pct_disp.get(dim, int(weight*100))}% | {weighted:.1f} |")

        if pct_disp:
            report.append("\n> ℹ️ 权重为整数显示（最大余数法，合计恒为 100%）；加权得分按未舍入的真实权重计算。")

        # B9修复：数据不足保护——当有效维度原始权重合计<50%时，不给虚假评分
        #   weight_sum 是有效维度的原始权重合计（未归一化），<0.5表示超过一半维度无数据
        data_sufficient = weight_sum >= 0.5
        if data_sufficient:
            final_score = total_score / total_weight if total_weight > 0 else 0
            report.append(f"\n### 8.2 综合评分")
            report.append(f"\n# 综合健康评分: **{final_score:.1f} / 100**")

            if final_score >= 85:
                report.append("\n**评估：优秀** 🌟 整体健康状况良好，继续保持！")
            elif final_score >= 70:
                report.append("\n**评估：良好** ✅ 整体健康状况尚可，有提升空间。")
            elif final_score >= 60:
                report.append("\n**评估：一般** ⚠️ 存在一些健康问题，建议关注并改善。")
            else:
                report.append("\n**评估：需关注** 🔴 存在较多健康问题，建议积极改善并考虑就医。")
        else:
            final_score = None
            report.append(f"\n### 8.2 综合评分")
            report.append(f"\n# ⚠️ 有效数据不足，综合评分不可用")
            report.append(f"\n> 有效维度原始权重合计仅 **{weight_sum*100:.0f}%**（<50%阈值），"
                          f"超过一半维度无数据或数据不可靠。")
            report.append(f"> 请确保设备已同步最新数据，或检查数据库完整性后重新导出。")
            report.append(f"> 以下各维度分析仅供参考，不代表整体健康状况。")

        health_data['final_score'] = final_score

        # ==================== 9. Top3 健康问题（新增）====================
        report.append(f"\n## ⚠️ 九、Top 3 健康问题")

        problems = []

        # 显式区分「无数据」与「有数据」——原 `.get(..., 8)` 在
        #   7 天睡眠整体无数据时会用默认 8h 掩盖缺失（使该 Top3 告警永不触发、且语义含糊）。
        #   现仅在确有数据时判定（无数据时由睡眠章节自身的"暂无数据"提示覆盖）。
        _sleep7h = health_data.get('sleep_7d_avg_hours')
        if _sleep7h is not None and _sleep7h < 7:
            problems.append({
                'problem': '睡眠时长不足',
                'detail': f"最近7天平均睡眠仅 {_sleep7h:.1f} 小时，低于推荐的 7-9 小时",
                'impact': '长期睡眠不足会影响免疫力、记忆力、代谢和情绪',
                'severity': 'high'
            })
        if health_data.get('late_nights', 0) >= 5:
            problems.append({
                'problem': '熬夜频繁',
                'detail': (f"最近7天有 {health_data.get('late_nights', 0)} 天在23点后或凌晨入睡"
                           + (f"（实测入睡时间 {health_data['late_night_range']}）" if health_data.get('late_night_range') else "")
                           + "，作息不规律"),
                'impact': '熬夜会打乱生物钟，影响睡眠质量和内分泌',
                'severity': 'medium'
            })
        # 改"键存在才判定"——运动窗口无数据时 avg_steps
        #   不写入（无数据≠0步），不再用 get 默认值制造虚假告警；与睡眠（默认8不触发）、
        #   静息心率（默认70不触发）的防误报风格统一。有数据时 avg_steps 恒写入（L2 修复），判定不变。
        if 'avg_steps' in health_data and health_data['avg_steps'] < 5000:
            problems.append({
                'problem': '活动量严重不足',
                'detail': f"最近7天平均步数仅 {health_data['avg_steps']:.0f} 步，远低于推荐的 8000 步",
                'impact': '久坐不动会增加心血管疾病、肥胖、糖尿病风险',
                'severity': 'high'
            })
        # 同 avg_steps——键存在才判定，窗口无数据不再误报"仅 0 分钟"
        if 'total_workout_7d' in health_data and health_data['total_workout_7d'] < 150:
            problems.append({
                'problem': '中高强度运动不足',
                'detail': f"最近7天中高强度运动仅 {health_data['total_workout_7d']} 分钟，推荐每周≥{TARGET_WEEKLY_EXERCISE}分钟",
                'impact': '缺乏中高强度运动影响心肺功能和代谢健康',
                'severity': 'medium'
            })
        if health_data.get('avg_ahi', 0) >= 5 and not health_data.get('osa_unreliable'):
            problems.append({
                'problem': '睡眠呼吸暂停风险',
                'detail': f"平均AHI指数 {health_data.get('avg_ahi', 0):.1f}，超过正常范围（<5）",
                'impact': '睡眠呼吸暂停会导致夜间缺氧，增加心血管疾病风险，白天嗜睡',
                'severity': 'high'
            })
        if health_data.get('avg_rmssd', 50) < 30:
            problems.append({
                'problem': 'HRV偏低，压力大/恢复差',
                'detail': f"平均RMSSD仅 {health_data.get('avg_rmssd', 0):.1f}ms，副交感神经活性低",
                'impact': 'HRV低通常表示压力大、身体恢复能力差，长期会影响免疫力',
                'severity': 'medium'
            })
        if health_data.get('avg_rest_hr', MED_THRESHOLDS["rest_hr_normal"]) > MED_THRESHOLDS["rest_hr_elevated"]:
            problems.append({
                'problem': '静息心率偏高',
                'detail': f"平均静息心率 {health_data.get('avg_rest_hr', 0):.0f} bpm，偏高",
                'impact': '静息心率高可能与压力、缺乏运动、甲状腺问题等有关',
                'severity': 'medium'
            })

        # B10修复：有效数据不足（final_score is None）时，Top3 各判据多依赖 health_data 默认值
        #   （avg_steps 未写入时默认 0、total_workout_7d 无数据时写 0），
        #   会把「无数据」误判为「指标为 0」而输出虚假告警（如"平均步数仅 0 步"），
        #   与上文"有效数据不足，综合评分不可用"自相矛盾。此处统一丢弃并说明。
        if final_score is None:
            report.append("\n> ⚠️ 有效数据不足，暂不输出健康问题清单"
                          "（各判据依赖默认值，会把「无数据」误判为「指标为 0」）")
            problems = []

        severity_order = {'high': 0, 'medium': 1, 'low': 2}
        problems.sort(key=lambda x: severity_order.get(x['severity'], 2))
        top3 = problems[:3]

        for i, p in enumerate(top3, 1):
            report.append(f"\n### {i}. {p['problem']}")
            report.append(f"- **具体表现**: {p['detail']}")
            report.append(f"- **对健康的影响**: {p['impact']}")
            report.append(f"- **严重程度**: {'🔴 高' if p['severity']=='high' else '⚠️ 中' if p['severity']=='medium' else '🟢 低'}")

        health_data['top3_problems'] = top3

        # ==================== 10. 7天改善计划（动态生成）====================
        report.append(f"\n## 📅 十、7天改善计划")

        # B7修复：根据Top3问题动态生成重点关注项
        if top3:
            focus_items = "、".join([p['problem'] for p in top3])
            report.append(f"\n> 🎯 **根据您的Top3健康问题，本周重点关注：{focus_items}**")

        # 步数目标根据当前平均步数动态调整
        current_steps = health_data.get('avg_steps', 0)
        if current_steps < 3000:
            step_phase1, step_phase2, step_phase3 = 3000, 4000, 5000
        elif current_steps < 5000:
            step_phase1, step_phase2, step_phase3 = 4000, 5000, 6000
        elif current_steps < 8000:
            step_phase1, step_phase2, step_phase3 = 6000, 7000, 8000
        else:
            step_phase1 = step_phase2 = step_phase3 = 8000

        report.append(f"\n### 第1-2天：基础调整")
        report.append("- [ ] 固定起床时间（每天同一时间起床，包括周末）")
        report.append("- [ ] 睡前1小时不看手机，改用看书或听轻音乐")
        if current_steps < 8000:
            # avg_steps 键缺失（运动无数据）时不再显示"当前平均 0 步"
            _avg_note = (f"（当前平均 {current_steps:.0f} 步，可以分次完成）"
                         if 'avg_steps' in health_data else "（暂无近7天步数数据，先从基础活动量开始）")
            report.append(f"- [ ] 每天步行至少 {step_phase1} 步{_avg_note}")
        else:
            report.append("- [ ] 保持每天 8000 步以上的活动量")
        report.append("- [ ] 记录每天的入睡时间和起床时间")

        report.append(f"\n### 第3-4天：增加活动")
        if current_steps < 8000:
            report.append(f"- [ ] 每天步行至少 {step_phase2} 步")
        report.append("- [ ] 增加一次 20 分钟的中等强度运动（快走、慢跑、骑车等）")
        report.append("- [ ] 每坐 1 小时起身活动 5 分钟")
        report.append("- [ ] 睡前做 5 分钟深呼吸或冥想放松")

        report.append(f"\n### 第5-6天：巩固习惯")
        if current_steps < 8000:
            report.append(f"- [ ] 每天步行至少 {step_phase3} 步")
        # 如果有熬夜问题，强调早睡
        if health_data.get('late_nights', 0) >= 3:
            report.append("- [ ] 保持 00:00 前上床（您最近经常凌晨入睡，逐步提前）")
        else:
            report.append("- [ ] 保持 23:30 前上床")
        report.append("- [ ] 增加第二次 20 分钟运动")
        report.append("- [ ] 记录运动后的感受（精力、心情）")

        report.append(f"\n### 第7天：复盘总结")
        report.append("- [ ] 回顾本周睡眠、运动、压力数据")
        report.append("- [ ] 对比第1天，看看哪些指标有改善")
        report.append("- [ ] 制定下周的目标（步数、睡眠时长、运动次数）")
        report.append("- [ ] 奖励自己（看电影、吃顿好的等，非食物奖励更好）")

        report.append(f"\n> 💡 提示：不要一开始就追求完美，循序渐进最重要。哪怕每天只进步一点点，长期坚持也会有大变化。")

        # ==================== 11. 就医建议（新增）====================
        report.append(f"\n## 🏥 十一、就医建议")

        need_doctor = False
        doctor_advice = []

        _ahi = health_data.get('avg_ahi', 0)
        if _ahi >= MED_THRESHOLDS["ahi_severe"] and not health_data.get('osa_unreliable'):
            need_doctor = True
            doctor_advice.append(f"- **睡眠呼吸暂停**: AHI≥{MED_THRESHOLDS['ahi_severe']}，建议挂**呼吸内科**或**睡眠医学科**，做睡眠监测（PSG）")
        elif _ahi >= MED_THRESHOLDS["ahi_mild"] and not health_data.get('osa_unreliable'):
            doctor_advice.append(f"- **睡眠呼吸暂停**: AHI {MED_THRESHOLDS['ahi_mild']}-{MED_THRESHOLDS['ahi_severe']}，轻度，建议观察，如白天嗜睡明显建议就医")

        if health_data.get('avg_spo2', MED_THRESHOLDS["spo2_normal"]) < MED_THRESHOLDS["spo2_low"]:
            need_doctor = True
            doctor_advice.append(f"- **血氧过低**: 平均血氧<{MED_THRESHOLDS['spo2_low']}%，建议挂**呼吸内科**检查")

        if health_data.get('avg_rest_hr', MED_THRESHOLDS["rest_hr_normal"]) > MED_THRESHOLDS["rest_hr_high"]:
            need_doctor = True
            doctor_advice.append(f"- **静息心率过高**: >{MED_THRESHOLDS['rest_hr_high']} bpm，建议挂**心内科**检查心电图和甲状腺功能")

        # B11修复：final_score 为 None（有效数据不足）时，
        #   health_data.get('final_score', 70) 返回的是 None 而非默认 70（键存在、值为 None），
        #   直接比较会抛 TypeError 导致整个报告生成失败。此处显式判空。
        _fs = health_data.get('final_score')
        if health_data.get('avg_rmssd', MED_THRESHOLDS["rmssd_normal"]) < MED_THRESHOLDS["rmssd_low"] and _fs is not None and _fs < MED_THRESHOLDS["score_low"]:
            doctor_advice.append("- **压力相关**: HRV持续偏低且综合评分低，如伴有焦虑、失眠等症状，建议挂**心理科**或**精神科**咨询")

        if doctor_advice:
            if need_doctor:
                report.append(f"\n### ⚠️ 需要尽快就医（以下指标达到临床预警值）")
                report.append(f"> 血氧<{MED_THRESHOLDS['spo2_low']}%、静息心率>{MED_THRESHOLDS['rest_hr_high']} bpm、"
                              f"AHI≥{MED_THRESHOLDS['ahi_severe']} 属于需**尽快**就诊的预警信号，"
                              "建议优先挂号相应科室，不要拖延；本提示不能替代急诊判断。")
            else:
                report.append(f"\n### 需要关注的就医建议")
            for advice in doctor_advice:
                report.append(advice)
        else:
            report.append(f"\n### 暂无紧急就医需求")
            report.append("- 各项指标基本在正常范围内，继续保持健康生活方式即可")
            report.append("- 建议每年做一次常规体检（血常规、生化、心电图等）")

        report.append(f"\n> ⚠️ **免责声明**: 以上分析基于可穿戴设备数据，仅供参考，不能替代专业医生诊断。如有身体不适，请及时就医。")

        # ==================== 12. 一句话总结 ====================
        report.append(f"\n## 📝 十二、一句话总结")

        if final_score is None:
            summary = "有效数据不足，无法给出整体健康评估。请确保设备已同步最新数据后重新导出。"
        else:
            # 本节原用 80 / 65 两档，与第八章「综合健康评分」的
            #   85 / 70 / 60 分档不一致——同一分数会出现"第八章=良好、本章=尚可"的自相矛盾
            #   （如 75 分、68 分）。现统一为 85 / 70 / 60，与手册 3.2 节口径一致。
            # 保留：高分段总结不再回避 Top1（84 分 + Top1"活动量严重不足"
            #   时，原"继续保持"与 Top3 章节自相矛盾）。
            _top1 = top3[0]['problem'] if top3 else ""
            _suffix = f"；当前最需关注：{_top1}" if _top1 else ""
            _problem = top3[0]['problem'] if top3 else '活动量不足'
            if final_score >= 85:
                summary = f"整体健康状况优秀（{final_score:.0f}分），继续保持规律作息和适量运动{_suffix}。"
            elif final_score >= 70:
                summary = f"整体健康状况良好（{final_score:.0f}分），继续保持规律作息和适量运动{_suffix}。"
            elif final_score >= 60:
                summary = f"整体健康一般（{final_score:.0f}分），主要问题是{_problem}，建议重点改善。"
            else:
                summary = f"健康状况需要关注（{final_score:.0f}分），Top问题是{_problem}，建议积极改善并考虑就医。"

        report.append(f"\n> **{summary}**")

        report.append(f"\n### 三个最关键的行动项")
        report.append(f"1. **{top3[0]['problem']}**: {top3[0]['detail'][:50]}..." if len(top3) > 0 else "1. 保持规律作息")
        report.append(f"2. **{top3[1]['problem']}**: {top3[1]['detail'][:50]}..." if len(top3) > 1 else "2. 增加日常活动量")
        report.append(f"3. **{top3[2]['problem']}**: {top3[2]['detail'][:50]}..." if len(top3) > 2 else "3. 关注压力和恢复")

        # ==================== 报告说明 ====================
        report.append(f"\n---")
        report.append(f"\n## 📊 报告说明")
        report.append(f"- 报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        report.append(f"- 数据来源: {os.path.basename(db_path)}")
        report.append(f"- 分析时间范围: 最近7个完整日（已排除今日不完整数据）")
        report.append(f"- 分析维度: 睡眠、OSA/打鼾、心率、HRV、运动、压力、血氧")
        # L8修复：权重说明改为动态生成，与实际参与计算的 active_weights 完全一致，
        #   避免 OSA 被排除后仍硬编码显示其 10% 权重（此前与 8.1 节表格冲突）。
        try:
            # 与 8.1 节共用同一份最大余数法整数百分比（pct_disp）——
            #   两处显示不仅口径一致，且各自合计恒为 100%（原 {:.0f} 独立舍入会合计 101%）。
            weight_str = "、".join(f"{dim_names_disp.get(d, d)}{pct_disp.get(d, int(w*100))}%" for d, w in active_weights.items())
        except Exception:
            weight_str = "睡眠25%、心率15%、HRV15%、运动15%、压力10%、血氧10%、OSA10%"
        report.append(f"- 综合评分权重（已归一化，与 8.1 节一致）: {weight_str}")


        # 保存报告（盲审复审：统一走 utils.atomic_write——自带临时文件、os.replace
        #   与杀软/句柄竞争时的 replace 重试，旧手写版缺重试、竞争下直接抛 PermissionError）
        # 原文件名仅用秒级时间戳，同一秒内两次运行会互相覆盖。
        #   现追加微秒后缀，保证即便同秒运行文件名仍唯一。
        _ts = datetime.now()
        report_path = os.path.join(export_dir, f"analysis_report_enhanced_{_ts:%Y%m%d_%H%M%S}_{_ts.microsecond:06d}.md")
        with atomic_write(report_path, encoding="utf-8") as f:
            f.write("\n".join(report))

        return report_path

    except Exception as e:
        print(f"  生成报告失败: {str(e)}")
        import traceback
        traceback.print_exc()
        return None
    finally:
        # 确保数据库连接被关闭，即使发生异常
        if conn:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    try:
        success = main()
        if success and _EXPORT_INCOMPLETE:
            # C-P2-2：报告已生成但导出数据不完整（JSON 缺表）——退出码 2
            sys.exit(2)
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ 程序出错: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
