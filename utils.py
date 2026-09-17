# SPDX-License-Identifier: MIT
"""
OPPO 健康数据导出工具 - 通用工具模块
====================================
包含：日志、进度条、数据完整性校验、安全类型转换等通用功能。

使用方法：
    from utils import setup_logger, ProgressBar, DataIntegrityChecker, safe_int, safe_float
"""

import hashlib
import os
import re
import sys
import logging
import tempfile
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from datetime import datetime


# ==================== ADB 序列号白名单（防命令注入，三入口共用）====================
# adb_device 会被拼进 shell 命令（run_cmd shell=True）——若 ANDROID_SERIAL 含
# shell 元字符（& | > 等），可造成命令注入（典型场景：插入伪装序列号的恶意设备）。
# 合法序列号仅含字母/数字/点/冒号/连字符（如 127.0.0.1:5555、emulator-5554）。
ADB_SERIAL_RE = re.compile(r'^[A-Za-z0-9._:\-]{1,64}$')


def smart_decode(b):
    """子进程输出的智能解码：先 UTF-8，失败或出现替换符时回落 GBK（盲审 U-1）。

    背景：adb/frida 的正常输出是 UTF-8，而中文 Windows 的 cmd.exe 报错
    （如 "'adb' 不是内部或外部命令"）是 GBK 字节——固定任一编码都会让
    另一类变成乱码/问号，新手最常见故障的提示因此不可读。
    顺序保证：UTF-8 文本必然先命中 UTF-8 严格解码；GBK 报错对 UTF-8 严格
    解码几乎必然失败，才轮到 GBK。两者都失败时按旧口径 utf-8+replace 兜底
    （保证永不抛错）。入参 None 返回空串、str 原样返回。
    """
    if b is None:
        return ""
    if isinstance(b, str):
        return b
    for enc in ("utf-8", "gbk"):
        try:
            s = b.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        if "\ufffd" not in s:
            return s
    return b.decode("utf-8", errors="replace")


def sanitize_adb_serial(value, default="127.0.0.1:5555"):
    """校验 ADB 序列号；非法时打印告警并回退默认值。config.py 与两个入口脚本共用。"""
    s = str(value or "").strip()
    if ADB_SERIAL_RE.match(s):
        return s
    print(f"⚠️  ANDROID_SERIAL 含非法字符（仅允许字母/数字/./:/-），已回退默认 {default}")
    return default


def safe_fs_name(name, max_len=80):
    """把表名/日期串等外部来源文本安全化为文件名/目录名（导出脚本与 GUI 共用）。

    C-P2-12 两类风险：①原名含 / \\ : 等字符时会目录穿越或拼出错误子路径；
    ②Windows/macOS 文件系统大小写不敏感，不同原名折叠后互相覆盖。
    处理：仅保留 [A-Za-z0-9_.-]，其余替换为 _；发生改写时追加原串 md5 前 8 位，
    既保留可读性又保证改写后不撞名。未改写的名字原样返回（不追加哈希）。
    """
    s = str(name or "")
    cleaned = re.sub(r"[^A-Za-z0-9_.\-]", "_", s)
    if cleaned != s:
        cleaned = f"{cleaned[:max_len]}_{hashlib.md5(s.encode('utf-8')).hexdigest()[:8]}"
    return cleaned


def ensure_utf8_stdout():
    """强制 stdout/stderr 使用 UTF-8，避免 Windows 控制台或重定向输出乱码。

    在 Python 3.7+ 中，sys.stdout 支持 reconfigure；旧版本或特殊句柄上调用失败时
    静默忽略，不影响主流程。入口脚本应在最顶部调用一次。
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError, ValueError):
        pass
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, OSError, ValueError):
        pass


@contextmanager
def atomic_write(path, encoding="utf-8", newline=None):
    """原子写文件：先写同目录临时文件，全部写完后再 os.replace 覆盖目标。

    用途：避免导出/生成被中断（Ctrl+C、崩溃、设备断连）时留下半截或截断的文件
    ——半截 CSV/JSON/HTML 会被下游（json_to_sqlite、浏览器）当成有效文件解析失败。

    用法：把 `with open(p, "w", encoding=...) as f:` 换成
         `with atomic_write(p, encoding=...) as f:`，块内代码无需改动。

    异常安全性：块内抛异常时删除临时文件并原样抛出，目标文件保持修改前的状态。

    并发安全性：Windows 上 os.replace 覆盖一个正被其他句柄
    （杀毒软件实时扫描、另一线程刚写完尚未释放）持有的目标文件时，会抛
    PermissionError(WinError 5)。实测 16 线程并发写同一目标时有约一半调用报此错。
    此处对 replace 做短重试：既保留原子性（目标文件始终完整），又让并发调用
    不至于把异常抛给上层。
    """
    _dir = os.path.dirname(path) or "."
    _fd, _tmp = tempfile.mkstemp(dir=_dir, suffix=".tmp")
    try:
        with os.fdopen(_fd, "w", encoding=encoding, newline=newline) as f:
            yield f
        _replace_with_retry(_tmp, path)
    except BaseException:
        try:
            os.remove(_tmp)
        except OSError:
            pass
        raise


def _replace_with_retry(src, dst, tries=12, delay=0.05):
    """os.replace 的 Windows 容错封装：目标被短暂占用时重试，而非直接抛异常。

    仅对「目标被占用」这一类可恢复错误重试；其余错误（跨设备、权限不足等）
    立即抛出，避免把真正的故障拖成超时。
    """
    import time as _time
    last = None
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError as e:      # Windows: 目标被杀软/其他句柄短暂占用
            last = e
        except OSError as e:
            # Windows 上占用目标还会表现为 errno 13 / WinError 5 之外的 EACCES
            if getattr(e, "winerror", None) in (5, 32) or getattr(e, "errno", None) == 13:
                last = e
            else:
                raise
        _time.sleep(delay * (i + 1))
    raise last


# 「事件时间列」候选清单的**唯一来源**。
#   GUI（health_export_gui.get_date_columns）此前只认 6 列，
#   而按天拆分（split_db_by_day.EVENT_EPOCH）认 20 列——同一张表在 GUI 里可能
#   找不到时间列，导致用户设定的时间范围被**静默忽略**、直接整表导出。
#   统一放此处由两处共用，避免再次漂移（同类问题已发生过一次：查找最新导出目录）。
#   注意：此处只放「事件时间戳」列，不含 create_time/modified_time 等元数据时间戳。
EVENT_TIME_COLUMNS = [
    "data_created_timestamp", "data_timestamp", "measurement_timestamp",
    "record_start_timestamp", "snore_start_timestamp", "start_timestamp",
    # OPPO 部分表用 *_time_stamp 拼写（如 DBECGRecord.start_time_stamp，
    #   生成器 q_ecg 读的就是它）。原白名单缺此变体 → ECG 表被判为"静态表"整表复制进每个日库。
    "start_time_stamp", "end_time_stamp", "record_start_time_stamp", "snore_start_time_stamp",
    "start_time", "end_timestamp", "end_time", "bedtime", "train_time",
    "rest_in_timestamp", "sleep_in_timestamp", "sleep_out_timestamp",
    "display_startTime", "display_endTime", "last_train_time",
    "deleted_timestamp", "join_time", "bind_time",
]

# ==================== 身份列（隐私清洗）====================
# 实测 sink 库中大多数表含身份列（DBWeightBodyFatTable 一张表就带全 6 种）。
#   这些列对健康分析毫无用处，却把"你是谁 / 用什么设备"一并存了下来——属不必要的收集。
#   本清单与手册第九部分 9.2「投喂前必做：隐私处理」的口径一致，**只此一份**，
#   由上传客户端（upload_data.py）与 MCP 入库端（server/mcp_server.py）共用，
#   保证两条通道对同一行算出**相同**的 row_hash，跨通道幂等去重不被破坏。
IDENTITY_COLUMNS = (
    "ssoid", "open_id", "sub_account", "device_unique_id", "sn",
    "user_tag_id", "old_user_tag_id", "residence", "occupation", "metadata",
)


def scrub_identity_columns(header, row):
    """把一行里的身份列值置空（保留列本身，保持列序与长度不变）。
    保留列、只清值是刻意的：两通道的 row_hash 都按「header 列序 + 值」计算，
    列集合与顺序必须完全一致，否则跨通道幂等会失效。"""
    out = list(row)
    for i, h in enumerate(header):
        if i >= len(out):
            break
        if h in IDENTITY_COLUMNS:
            out[i] = ""
    return out


# 可疑身份列提醒：清单之外若出现疑似身份列（如新版 App 引入 imei/mac/user_id），
# 命中仅告警不阻断，提示人工复核 IDENTITY_COLUMNS 是否需要扩充。
_SUSPICIOUS_ID_RE = re.compile(
    r"(?:imei|oaid|mac|device|user|account|phone|mobile|location|addr|ssoid|\bsn\b)", re.I)


def warn_suspicious_identity_columns(header, source="", sink=None):
    """对表头扫描 IDENTITY_COLUMNS 之外的疑似身份列，命中则打印告警（不阻断）。
    sink 可注入自定义输出（如 MCP/stdio 场景需重定向到 stderr，避免污染协议通道）。"""
    extra = sorted({str(h) for h in header
                    if h and h not in IDENTITY_COLUMNS and _SUSPICIOUS_ID_RE.search(str(h))})
    if extra:
        src = f"[{source}] " if source else ""
        (sink or print)(f"⚠️  [隐私]{src}发现疑似身份列（不在 IDENTITY_COLUMNS 内，请人工复核）：{extra}")


# ==================== 安全类型转换 ====================
def _round_half_away(x):
    """四舍五入（0.5 远离零），避免 Python round() 的银行家舍入带来的意外。"""
    return int(x + 0.5) if x >= 0 else -int(-x + 0.5)


def safe_int(val, default=0):
    """安全的整数转换，处理 None、字符串、浮点数等各种情况。

    L6：
      1) 原实现 `int(float(val))` 对小数**静默截断**（"12.5" → 12）。现改为四舍五入
         ——对睡眠时长这类"451.6 分钟"更准确；对整数值（"451.0"）结果不变。
      2) 原实现对**整数字符串**也先转 float 再转 int，超过 2^53 的整数（如 19 位 ID、
         纳秒时间戳）会**经浮点丢精度**（"12345678901234567890" 会变成 …67168）。
         现先尝试精确的 int()，仅在含小数点/科学计数法时才走浮点路径。
    """
    if val is None:
        return default
    if isinstance(val, bool):          # bool 是 int 子类，先拦下以免语义混乱
        return int(val)
    try:
        if isinstance(val, str):
            s = val.strip()
            if not s:
                return default
            try:
                return int(s)                       # 整数字符串：精确、无精度损失
            except ValueError:
                return _round_half_away(float(s))   # 含小数/科学计数：四舍五入
        if isinstance(val, float):
            return _round_half_away(val)
        return int(val)
    except (ValueError, TypeError, OverflowError):
        return default


def safe_float(val, default=0.0):
    """安全的浮点数转换"""
    if val is None:
        return default
    try:
        if isinstance(val, str):
            val = val.strip()
            if not val:
                return default
        return float(val)
    except (ValueError, TypeError):
        return default


# ==================== 日志模块 ====================
def setup_logger(name="oppo_health_export", log_dir=None, log_level="INFO",
                 max_size_mb=10, backup_count=5, log_to_file=True):
    """
    设置日志记录器，同时输出到控制台和文件。

    Args:
        name: 日志记录器名称
        log_dir: 日志文件目录
        log_level: 日志级别（DEBUG/INFO/WARNING/ERROR）
        max_size_mb: 单个日志文件最大大小（MB）
        backup_count: 保留的日志文件数量
        log_to_file: 是否同时输出到文件

    Returns:
        logging.Logger: 配置好的日志记录器
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # 避免重复添加handler（N5说明：同名 logger 已配置时直接复用现有配置，
    #   此时 log_dir/log_level 等参数不会生效——首次调用决定配置，后续调用仅获取）。
    if logger.handlers:
        return logger

    # 日志格式
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 控制台输出
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 文件输出
    if log_to_file and log_dir:
        try:
            os.makedirs(log_dir, exist_ok=True)
            log_file = os.path.join(log_dir, f"{name}_{datetime.now().strftime('%Y%m%d')}.log")
            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=max_size_mb * 1024 * 1024,
                backupCount=backup_count,
                encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except Exception as e:
            logger.warning(f"无法创建日志文件: {e}")

    return logger


# ==================== 进度条模块 ====================
class ProgressBar:
    """
    轻量级文本进度条，无需额外依赖。

    使用方法：
        pb = ProgressBar(total=100, desc="导出中")
        for i in range(100):
            pb.update(i + 1)
        pb.finish()
    """

    def __init__(self, total, desc="处理中", width=30, show_percent=True, show_eta=True):
        """
        初始化进度条。

        Args:
            total: 总进度
            desc: 进度描述
            width: 进度条宽度（字符数）
            show_percent: 是否显示百分比
            show_eta: 是否显示预计剩余时间
        """
        self.total = max(total, 1)
        self.desc = desc
        self.width = width
        self.show_percent = show_percent
        self.show_eta = show_eta
        self.current = 0
        self.start_time = datetime.now()
        self._last_print_len = 0

    def update(self, current=None, inc=None):
        """
        更新进度。

        Args:
            current: 当前进度（绝对位置）
            inc: 增量（相对于当前进度）
        """
        if inc is not None:
            self.current += inc
        elif current is not None:
            self.current = current

        self.current = min(self.current, self.total)
        self._print()

    def _print(self):
        """打印进度条"""
        progress = self.current / self.total
        filled = int(self.width * progress)
        bar = "█" * filled + "░" * (self.width - filled)

        parts = [f"\r{self.desc}: |{bar}|"]

        if self.show_percent:
            parts.append(f" {progress * 100:.1f}%")

        if self.show_eta and self.current > 0:
            elapsed = (datetime.now() - self.start_time).total_seconds()
            eta = elapsed / self.current * (self.total - self.current)
            if eta < 60:
                parts.append(f" ETA: {eta:.0f}s")
            else:
                parts.append(f" ETA: {eta / 60:.1f}min")

        line = "".join(parts)
        # 清除上一行的残留字符
        if len(line) < self._last_print_len:
            line += " " * (self._last_print_len - len(line))
        self._last_print_len = len(line)
        print(line, end="", flush=True)

    def finish(self, message=None):
        """完成进度条"""
        self.current = self.total
        self._print()
        print()  # 换行
        if message:
            print(f"✅ {message}")


# ==================== 数据完整性校验模块 ====================
class DataIntegrityChecker:
    """
    数据库数据完整性校验器。

    检查项：
    1. 表数量是否合理
    2. 必需表是否存在
    3. 总行数是否合理
    4. 关键字段值是否在合理范围内
    5. 日期范围是否合理
    """

    def __init__(self, db_path, config=None):
        """
        初始化校验器。

        Args:
            db_path: 数据库文件路径
            config: 配置字典（可选，使用默认配置）
        """
        import sqlite3
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.cursor = self.conn.cursor()
        self.config = config or {}
        self.warnings = []
        self.errors = []
        self.info = []

    def check_all(self):
        """执行所有检查，返回 (is_valid, report)"""
        self._check_table_count()
        self._check_required_tables()
        self._check_total_rows()
        self._check_sleep_data()
        self._check_heart_rate_data()
        self._check_spo2_data()
        self._check_date_range()

        is_valid = len(self.errors) == 0
        report = self._generate_report()
        return is_valid, report

    @staticmethod
    def _q_ident(name):
        """把表名/列名安全地包成 SQLite 标识符（`"` 翻倍）。
        与 json_to_sqlite.q_ident / health_export_gui.q_ident / mcp_server._check_ident
        同一标准，避免异常名字破坏语句。"""
        return '"' + str(name).replace('"', '""').replace("\x00", "") + '"'

    def _record_check_failure(self, label, exc):
        """记录校验查询失败的**分类**结果。
        L5：原实现把所有异常一律 append 进 warnings，
        而 `_generate_report` 只在 `len(self.errors) == 0` 时判为有效——于是"库损坏 /
        磁盘 IO 异常 / 查询写错"这类**真实故障**会被降级成一条不起眼的警告，用户看到
        "校验通过（有警告）"就以为数据没问题。现按异常类型区分：
          · 缺表 / 缺列  → 属预期的数据形态差异（不同 App 版本表结构不同），记 warning；
          · 其它异常     → 真实查询故障，记 **error**，使整体校验判为不通过。
        """
        msg = str(exc).lower()
        if "no such table" in msg or "no such column" in msg:
            self.warnings.append(f"{label}: 缺表或缺列（{exc}）")
        else:
            self.errors.append(f"{label}: 查询失败——{exc}")

    def _check_table_count(self):
        """检查表数量"""
        min_tables = self.config.get("min_tables", 30)
        self.cursor.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'")
        count = self.cursor.fetchone()[0]
        self.info.append(f"表数量: {count}")
        if count < min_tables:
            self.warnings.append(f"表数量偏少: {count} < {min_tables}（可能数据不完整）")

    def _check_required_tables(self):
        """检查必需表是否存在"""
        required = self.config.get("required_tables", [])
        self.cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        existing = {row[0] for row in self.cursor.fetchall()}
        # U-1修复：原 info 行用 `if not self.errors` 判断，而 errors 是本函数
        #   后面才 append 的——缺失时 info 仍显示"N/N 全部存在"（展示与实际不符）。
        #   改为先算缺失清单，再按缺失数如实展示。
        missing = [t for t in required if t not in existing]
        for table in missing:
            self.errors.append(f"必需表不存在: {table}")
        if required:
            self.info.append(
                f"必需表检查: {len(required) - len(missing)}/{len(required)} 存在"
                + (f"（缺失 {len(missing)} 个）" if missing else "")
            )
        else:
            self.info.append("必需表检查: 未配置必需表")

    def _check_total_rows(self):
        """检查总行数"""
        min_rows = self.config.get("min_total_rows", 100)
        self.cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in self.cursor.fetchall()]
        total_rows = 0
        failed_tables = []
        for table in tables:
            try:
                # L2 修复：原行是 f"... FROM [{table}]"，
                #   L2 加固时误写成 f"... FROM [{self._q_ident(table)}]"——q_ident 已自带
                #   双引号，再套方括号会拼出 [\"T1\"]，被 SQLite 当作**字面名为 "T1" 的表**，
                #   导致 101 张表全部统计失败、总行数恒为 0（实测复现：no such table: "T1"）。
                #   方括号与双引号二选一即可，此处统一用 q_ident（含转义）。
                self.cursor.execute(f"SELECT COUNT(*) FROM {self._q_ident(table)}")
                total_rows += self.cursor.fetchone()[0]
            except Exception as _e:
                # 原为 except: pass——某张表统计失败时
                #   总行数被静默少算，随后与 min_rows 比较会产出"总行数偏少"的**假告警**。
                failed_tables.append(table)
                self._record_check_failure(f"表 {table} 行数统计", _e)
        # 本处统计的是 sqlite_master 里的**全部表**，
        #   含 sqlite_sequence / room_master_table 这类框架内部表；而 export_health_data.py
        #   的完成汇报统计的是**数据表**（跳过内部表）。两者相差十几行，此前标签都叫
        #   "总行数"，实测被误读成"数据不一致"。现显式标注口径。
        _scope = "全部表（含 sqlite_sequence / room_master_table 等内部表）"
        if failed_tables:
            # 明确标注总数不完整，避免用户把"少算的行数"当成真实数据量
            self.info.append(
                f"总行数: {total_rows}　[{_scope}]（**不完整**："
                f"{len(failed_tables)} 张表统计失败，见下方告警/错误）"
            )
        else:
            self.info.append(f"总行数: {total_rows}　[{_scope}]")
        # 仅在本轮统计完整时才用总行数判断"偏少"，否则会误报
        if not failed_tables and total_rows < min_rows:
            self.warnings.append(f"总行数偏少: {total_rows} < {min_rows}（可能数据不完整）")

    def _check_sleep_data(self):
        """检查睡眠数据合理性"""
        try:
            self.cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='DBSleepDataStatTable'")
            if not self.cursor.fetchone():
                return
            max_hours = self.config.get("max_sleep_hours", 16)
            # 修复：原把配置值 f-string 直插 SQL。
            #   虽然来源是本地 config（非外部输入、不构成实际注入面），但一旦
            #   max_sleep_hours 被填成非数值（如 "16; DROP TABLE x"），轻则语法错误
            #   让整个完整性校验静默失效、重则为后续改动留下注入面。
            #   改为参数绑定，并把配置值强转数值——非数值时回退默认 16 小时。
            try:
                _thr = float(max_hours) * 60
            except (TypeError, ValueError):
                _thr = 16 * 60
                max_hours = 16
            self.cursor.execute(
                "SELECT date, total_sleep_time FROM DBSleepDataStatTable "
                "WHERE CAST(total_sleep_time AS REAL) > ?", (_thr,))
            abnormal = self.cursor.fetchall()
            if abnormal:
                self.warnings.append(f"睡眠时长异常（>{max_hours}小时）: {len(abnormal)} 天，例如 {abnormal[0][0]}: {abnormal[0][1]}分钟")
        except Exception as e:
            self._record_check_failure("睡眠数据检查", e)

    def _check_heart_rate_data(self):
        """检查心率数据合理性"""
        try:
            self.cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='DBHeartRateDataStatTable'")
            if not self.cursor.fetchone():
                return
            min_hr = self.config.get("min_heart_rate", 30)
            max_hr = self.config.get("max_heart_rate", 220)
            # 该 SQL 无占位符，去掉多余的 f 前缀（静态检查噪音，无功能影响）
            self.cursor.execute("SELECT date, min_hr, max_hr, average_hr FROM DBHeartRateDataStatTable")
            rows = self.cursor.fetchall()
            abnormal_count = 0
            for row in rows:
                for val in [row[1], row[2], row[3]]:
                    # 先 safe_int 再判 0——原 `if val and`
                    # 对字符串 "0"（真值）会转成 0 再与阈值比较，产生 <30 假告警
                    _v = safe_int(val)
                    if _v and (_v < min_hr or _v > max_hr):
                        abnormal_count += 1
                        break
            if abnormal_count > 0:
                self.warnings.append(f"心率数据异常（<{min_hr}或>{max_hr}）: {abnormal_count} 天")
        except Exception as e:
            self._record_check_failure("心率数据检查", e)

    def _check_spo2_data(self):
        """检查血氧数据合理性"""
        try:
            self.cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='DBBloodOxygenSaturationDataStat'")
            if not self.cursor.fetchone():
                return
            min_spo2 = self.config.get("min_spo2", 70)
            max_spo2 = self.config.get("max_spo2", 100)
            # 该 SQL 无占位符，去掉多余的 f 前缀（静态检查噪音，无功能影响）
            self.cursor.execute("SELECT date, min_blood_oxygen_saturation, max_blood_oxygen_saturation, average_blood_oxygen_saturation FROM DBBloodOxygenSaturationDataStat")
            rows = self.cursor.fetchall()
            abnormal_count = 0
            for row in rows:
                for val in [row[1], row[2], row[3]]:
                    # 同心率检查——先 safe_int 再判 0，
                    # 排除字符串 "0" 造成的假告警
                    _v = safe_int(val)
                    if _v and (_v < min_spo2 or _v > max_spo2):
                        abnormal_count += 1
                        break
            if abnormal_count > 0:
                self.warnings.append(f"血氧数据异常（<{min_spo2}%或>{max_spo2}%）: {abnormal_count} 天")
        except Exception as e:
            self._record_check_failure("血氧数据检查", e)

    def _check_date_range(self):
        """检查日期范围合理性"""
        try:
            # 检查睡眠表的日期范围
            self.cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='DBSleepDataStatTable'")
            if not self.cursor.fetchone():
                return
            self.cursor.execute("SELECT MIN(date), MAX(date) FROM DBSleepDataStatTable")
            min_date, max_date = self.cursor.fetchone()
            if min_date and max_date:
                self.info.append(f"数据日期范围: {min_date} ~ {max_date}")
                # 检查日期格式是否为 YYYYMMDD
                if len(str(min_date)) != 8 or len(str(max_date)) != 8:
                    self.warnings.append(f"日期格式异常: {min_date} ~ {max_date}（期望 YYYYMMDD 格式）")
        except Exception as e:
            self._record_check_failure("日期范围检查", e)

    def _generate_report(self):
        """生成校验报告"""
        lines = []
        lines.append("=" * 60)
        lines.append("  数据完整性校验报告")
        lines.append("=" * 60)
        lines.append(f"数据库: {self.db_path}")
        lines.append(f"校验时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("")

        lines.append("📊 基本信息:")
        for info in self.info:
            lines.append(f"  - {info}")
        lines.append("")

        if self.errors:
            lines.append(f"❌ 错误 ({len(self.errors)}):")
            for err in self.errors:
                lines.append(f"  - {err}")
            lines.append("")

        if self.warnings:
            lines.append(f"⚠️  警告 ({len(self.warnings)}):")
            for warn in self.warnings:
                lines.append(f"  - {warn}")
            lines.append("")

        if not self.errors and not self.warnings:
            lines.append("✅ 数据完整性校验通过，未发现异常！")
        elif not self.errors:
            lines.append("✅ 校验通过（有警告但无致命错误）")
        else:
            lines.append("❌ 校验未通过，存在致命错误！")

        return "\n".join(lines)

    def close(self):
        """关闭数据库连接"""
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass

    # 支持 with 上下文管理，异常路径也能保证连接关闭
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


# ==================== 模块自检 ====================
if __name__ == "__main__":
    print("=" * 60)
    print("  utils.py 模块自检")
    print("=" * 60)

    # 测试安全类型转换
    print("\n1. 安全类型转换测试:")
    print(f"   safe_int(None) = {safe_int(None)}")
    print(f"   safe_int('480.5') = {safe_int('480.5')}")
    print(f"   safe_int('abc') = {safe_int('abc')}")
    print(f"   safe_float('3.14') = {safe_float('3.14')}")

    # 测试进度条
    print("\n2. 进度条测试:")
    pb = ProgressBar(total=10, desc="测试进度", width=20)
    for i in range(10):
        import time
        time.sleep(0.1)
        pb.update(i + 1)
    pb.finish("进度条测试完成")

    # 测试日志
    print("\n3. 日志测试:")
    logger = setup_logger("test", log_dir="./logs", log_to_file=True)
    logger.info("这是一条测试日志")
    logger.warning("这是一条警告日志")
    print("   日志已写入 ./logs/ 目录")

    print("\n✅ 所有模块自检通过！")
