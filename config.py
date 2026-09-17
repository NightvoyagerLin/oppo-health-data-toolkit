# SPDX-License-Identifier: MIT
"""
OPPO 健康数据导出与分析工具 - 配置文件
========================================
所有可配置项集中在这里，修改后无需改动主脚本。

使用方法：
    from config import CONFIG
    adb_path = CONFIG['adb_path']

路径约定：
  - 工作目录相关路径一律相对"本文件所在目录"推导（__file__ 自定位），
    整个文件夹移动/复制/改名后零改动可运行；
  - 外部依赖（Frida CLI、雷电模拟器）不再硬编码机器上的绝对路径，
    改为按优先级自动探测：环境变量 > PATH > 当前解释器/常见安装位置，
    探测不到时记 None（相关功能降级并给出手动指引），换机器零改动。
"""

import glob
import os
import shutil

from utils import sanitize_adb_serial

# ==================== 设备序列号白名单（防命令注入，三入口共用）====================
# adb_device 会被拼进 shell 命令（run_cmd shell=True）——若 ANDROID_SERIAL 含
# shell 元字符（& | > 等），可造成命令注入（典型场景：插入伪装序列号的恶意设备）。
# 合法序列号仅含字母/数字/点/冒号/连字符（如 127.0.0.1:5555、emulator-5554）。
# 校验逻辑统一定义在 utils.sanitize_adb_serial。
_raw_serial = sanitize_adb_serial(os.environ.get("ANDROID_SERIAL", "") or "127.0.0.1:5555")

# ==================== 路径配置 ====================
# 自定位：以"本文件所在目录"为工作目录——整个文件夹改名/移动/复制后
# 无需修改任何路径，也不受启动时"当前工作目录"影响。
WORK_DIR = os.path.dirname(os.path.abspath(__file__))


def _resolve_frida_cli():
    """自动定位 frida.exe（相对化：不再硬编码任何机器路径）。
    优先级：环境变量 OPPO_FRIDA_CLI > PATH > 当前解释器 Scripts > 常见 Python 安装位置。
    探测不到返回 None（调用方应跳过 Frida 取密钥、直接用回退密钥）。"""
    env = os.environ.get("OPPO_FRIDA_CLI")
    if env and os.path.isfile(env):
        return env
    found = shutil.which("frida")
    if found:
        return found
    candidates = []
    try:
        import sysconfig
        candidates.append(os.path.join(sysconfig.get_paths()["scripts"], "frida.exe"))
    except Exception:
        pass
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates += glob.glob(os.path.join(
            local, "Programs", "Python", "Python3*", "Scripts", "frida.exe"))
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def _resolve_emulator():
    """自动定位雷电模拟器 dnplayer.exe / ldconsole.exe（环境变量优先，其次探测默认安装位）。

    优先级：环境变量 OPPO_EMULATOR_DIR > 各盘符常见安装根下的 leidian/LDPlayer*。
    常见安装根指盘符根目录本身，以及其下的一层子目录（1、soft、tools、Program Files 等）。
    这些只是"默认安装位"的兜底探测项、并非写死：装在别处时用 OPPO_EMULATOR_DIR 覆盖即可。
    探测不到返回 (None, None)——模拟器启动由手册命令或人工完成，不阻塞导出流程。
    """
    dirs = []
    env_dir = os.environ.get("OPPO_EMULATOR_DIR")
    if env_dir:
        dirs.append(env_dir)
    #   修复：此前探测仅查固定安装根，用户把雷电装在带层级子目录的位置时
    #   会恒为"未探测到"。改为在常见盘符的常见安装根下统一探测（去重且保序）。
    #   装在非标准位置时，用环境变量 OPPO_EMULATOR_DIR 精确指定即可。
    _seen = set(dirs)
    for _drv in ("C:\\", "D:\\", "E:\\", "F:\\"):
        if not os.path.isdir(_drv):
            continue
        for _sub in ("", "1\\", "soft\\", "tools\\", "Program Files\\", "Program Files (x86)\\"):
            _root = os.path.join(_drv, _sub, "leidian")
            for _p in glob.glob(os.path.join(_root, "LDPlayer*")):
                if _p not in _seen:
                    _seen.add(_p)
                    dirs.append(_p)
    for d in dirs:
        dn = os.path.join(d, "dnplayer.exe")
        ld = os.path.join(d, "ldconsole.exe")
        if os.path.isfile(dn):
            return dn, (ld if os.path.isfile(ld) else None)
    return None, None


_EMU_PATH, _EMU_CONSOLE = _resolve_emulator()

CONFIG = {
    # ---- ADB 与模拟器 ----
    "adb_path": os.path.join(WORK_DIR, "platform", "adb.exe"),
    "adb_device": _raw_serial,
    # 相对化改造：模拟器路径自动探测（见 _resolve_emulator），不再是硬编码绝对路径
    "emulator_path": _EMU_PATH,
    "emulator_console": _EMU_CONSOLE,

    # ---- OPPO 健康 ----
    "package_name": "com.heytap.health",
    "launch_activity": "com.heytap.health/.oobe.LaunchActivity",
    # 数据库回退密钥的唯一来源：export_health_data.py 的 DEFAULT_DB_KEY 常量。
    # （此处不提供同名回退密钥键：它与 export 脚本内的常量构成"双份配置"，
    #   且生产代码从不读取，易造成"改了这里不生效"的误导——与本文件下方 flask 部分同类。）

    # ---- 脚本路径 ----
    "work_dir": WORK_DIR,
    "export_script": os.path.join(WORK_DIR, "export_health_data.py"),
    "db_dir": os.path.join(WORK_DIR, "DB"),
    "log_dir": os.path.join(WORK_DIR, "logs"),

    # ---- 等待时间（秒）----
    "wait_emulator_start": 30,      # 模拟器启动等待
    "wait_data_sync": 120,           # 数据同步等待
    "wait_after_export": 2,          # 导出后等待文件写入
    "wait_app_launch": 5,            # App启动后等待
    "wait_adb_reconnect": 5,         # ADB重连等待

    # ---- 日志配置 ----
    "log_to_file": True,              # 是否同时输出到文件
    "log_max_size_mb": 10,            # 单个日志文件最大大小
    "log_backup_count": 5,            # 保留的日志文件数量
    "log_level": "INFO",              # 日志级别：DEBUG, INFO, WARNING, ERROR

    # ---- 进度条配置 ----
    "progress_bar_width": 30,         # 进度条宽度（字符数）
    "progress_show_percent": True,    # 是否显示百分比
    "progress_show_eta": True,        # 是否显示预计剩余时间

    # ---- 数据完整性校验 ----
    # 本开关已接入 export_health_data.py 步骤7.5——
    #   解密成功后自动执行 utils.DataIntegrityChecker（仅打印提示，不阻断导出）；
    #   深度合理性校验仍以独立运行的 data_validation.py 为准。
    "enable_integrity_check": True,
    "min_tables": 30,                  # 最少表数量（低于此值警告）
    "min_total_rows": 100,             # 最少总行数
    "required_tables": [               # 必须存在的核心表
        "DBSleepDataStatTable",
        "DBHeartRateDataStatTable",
        "DBSportDataStat",
    ],
    "max_sleep_hours": 16,             # 单日睡眠最大合理值（小时），超过警告
    "min_heart_rate": 30,              # 心率最小合理值（bpm）
    "max_heart_rate": 220,             # 心率最大合理值（bpm）
    "min_spo2": 70,                    # 血氧最小合理值（%）
    "max_spo2": 100,                   # 血氧最大合理值（%）

    # ---- 分析配置 ----
    "sleep_deep_min_ratio": 15,        # 深睡比例正常下限（%）
    "sleep_deep_max_ratio": 25,        # 深睡比例正常上限（%）
    "late_night_hour": 23,             # 晚睡判定阈值（23点后）
    "late_night_early_hour": 6,        # 凌晨入睡判定上限（6点前）
    "target_daily_steps": 8000,        # 每日目标步数
    "target_weekly_exercise_min": 150, # 每周目标中高强度运动（分钟）

    # ---- Frida CLI（可选）----
    # export_health_data.py 需要 frida.exe 取密钥。
    # 相对化改造：不再硬编码本机用户目录绝对路径——
    #   改为 _resolve_frida_cli() 自动探测（环境变量 OPPO_FRIDA_CLI > PATH >
    #   当前解释器 Scripts > 常见安装位置），探测不到为 None，
    #   导出脚本将跳过 Frida 取密钥、直接用内置回退密钥解密。改行为请设环境变量。
    "frida_cli": _resolve_frida_cli(),

    # ---- Flask 后端（可选）----
    # 说明：上传链路的地址/监听/鉴权 Token 均以
    #   **环境变量**为覆盖来源（OPPO_HEALTH_URL / OPPO_HEALTH_HOST / OPPO_HEALTH_TOKEN，
    #   见 upload_data.py 与 server/upload_server.py；Token 未设置时两端默认
    #   CHANGE_ME_TOKEN 占位值，**使用前请设置环境变量**）。
    #   避免与 upload_data.py 内的默认值形成"双份配置"造成修改不生效的误解。
}


def get_config():
    """获取配置副本（防止意外修改全局配置）"""
    return CONFIG.copy()


def validate_config():
    """校验配置项的合理性，返回 (is_valid, warnings)"""
    warnings = []

    # 检查路径是否存在
    for key in ["adb_path", "export_script"]:
        path = CONFIG.get(key)
        if path and not os.path.exists(path):
            warnings.append(f"路径不存在: {key} = {path}")

    # 检查时间配置
    if CONFIG["wait_data_sync"] < 30:
        warnings.append(f"wait_data_sync={CONFIG['wait_data_sync']}秒，可能不足以完成数据同步")

    # 检查日志目录（盲审复审：makedirs 属副作用且可能因权限抛 OSError 冒泡——
    #   自检应只读；失败降级为警告，不阻断自检流程）
    if CONFIG["log_to_file"]:
        try:
            os.makedirs(CONFIG["log_dir"], exist_ok=True)
        except OSError as e:
            warnings.append(f"日志目录无法创建: {CONFIG['log_dir']}（{e}）")

    is_valid = len(warnings) == 0
    return is_valid, warnings


if __name__ == "__main__":
    # 配置自检
    print("=" * 60)
    print("  OPPO 健康导出工具 - 配置自检")
    print("=" * 60)
    is_valid, warnings = validate_config()
    print(f"\n配置有效: {'✅ 是' if is_valid else '❌ 否'}")
    if warnings:
        print("\n⚠️  警告:")
        for w in warnings:
            print(f"  - {w}")
    print("\n📋 关键配置:")
    print(f"  工作目录: {CONFIG['work_dir']}")
    print(f"  ADB路径: {CONFIG['adb_path']}")
    print(f"  模拟器: {CONFIG['emulator_path'] or '未探测到（可设环境变量 OPPO_EMULATOR_DIR 指定）'}")
    print(f"  Frida CLI: {CONFIG['frida_cli'] or '未探测到（可设环境变量 OPPO_FRIDA_CLI 指定，取密钥将回退已知密钥）'}")
    print(f"  数据同步等待: {CONFIG['wait_data_sync']}秒")
    print(f"  日志输出: {'开启' if CONFIG['log_to_file'] else '关闭'}")
    print(f"  完整性校验: {'开启' if CONFIG['enable_integrity_check'] else '关闭'}")
