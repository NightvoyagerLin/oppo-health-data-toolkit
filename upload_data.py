# SPDX-License-Identifier: MIT
import sqlite3
import requests
import os
import sys
from datetime import datetime, timedelta, timezone

# 强制 UTF-8 输出，避免 Windows 控制台/重定向乱码（V1.1 修复）
try:
    from utils import ensure_utf8_stdout
    ensure_utf8_stdout()
except Exception:
    pass

# 上传前对身份列做清洗。
#   为什么在**客户端**做：这样身份信息根本不会离开本机（HTTP 通道即便是
#   OPPO_HEALTH_HOST=0.0.0.0 的局域网场景，也不会把 ssoid/设备号送出去）。
#   为什么用 utils 里的唯一清单：MCP 入库端（server/mcp_server.py）用的是同一份清单，
#   两条通道对同一行算出的 row_hash 才一致，跨通道幂等去重不会被破坏。
try:
    from utils import IDENTITY_COLUMNS, scrub_identity_columns, warn_suspicious_identity_columns
except ImportError:  # utils 缺失时降级：不清洗，但明确告警而不是静默
    IDENTITY_COLUMNS = ()
    def scrub_identity_columns(header, row):
        return row
    def warn_suspicious_identity_columns(header, source="", sink=None):
        pass
    print("⚠️  未能导入 utils 的身份列清单，本次上传**不会**清洗 ssoid/device_unique_id 等身份列。")


def q_ident(name):
    """把表名/列名安全地包成 SQLite 标识符（`"` 翻倍、剔除 NUL）。
    与 json_to_sqlite.q_ident /
    mcp_server._check_ident 同一标准，替换本文件残留的 f'"{t}"' 裸拼
    （表名/列名来自本地解密库 sqlite_master，防御性统一）。"""
    return '"' + str(name).replace('"', '""').replace("\x00", "") + '"'

# 修复：统一以中国时区(UTC+8)判定"今天"。
#   原 datetime.now() 取宿主机本地时区，非 UTC+8 机器上跨越午夜时会错一天，
#   导致"识别最新导出目录"认错日期（进而上传了不是当天的数据）。
CN = timezone(timedelta(hours=8))

# ==================== 配置区 ====================
# DOC-1+Token 写死：
#   - U1b 修复后本脚本直接用标准 sqlite3 打开"已解密"的 database_decrypted.db，
#     全程无需任何 DB 密钥——原注释宣称的"db_key.txt / OPPO_HEALTH_DBKEY"已过时删除。
#   - 鉴权 Token 默认写死（本机个人测试环境），须与 server/upload_server.py 的
#     API_TOKEN 保持一致；两端均支持 OPPO_HEALTH_TOKEN
#     环境变量覆盖，默认值仍为 CHANGE_ME_TOKEN（保持两端默认一致）。
#   - 上传地址仍支持环境变量 OPPO_HEALTH_URL 覆盖（本机测试需指向 127.0.0.1）。
upload_url = os.environ.get("OPPO_HEALTH_URL", "http://127.0.0.1:5000/api/upload")
token = os.environ.get("OPPO_HEALTH_TOKEN", "CHANGE_ME_TOKEN")

# 每表最多上传的行数（"分节快照"口径，与手册 3.5 节 / 附录D 一致）。
# 原 500 为散落字面量，提取为具名常量便于统一调整与口径核对。
MAX_ROWS_PER_TABLE = 500

# U1修复：原 strftime("%Y_%-m_%-d") 中的 %-m 是 Linux 专有格式，
#   Windows 上直接抛 ValueError: Invalid format string，脚本一运行就崩溃（与 D7 同源）。
#   改为平台无关写法；标准口径：导出统一写入 DB\combined\（合并全量库），
#   优先取 combined，找不到时回退到最新含解密库的日期目录（YYYY-MM-DD）。
def _is_today_dir(dirname):
    """DB\\combined（合并全量库）恒为当前有效目录；日期目录按标准 YYYY-MM-DD 解析。"""
    if dirname == "combined":
        return True
    parts = dirname.split('-')
    if len(parts) != 3:
        return False
    try:
        _today = datetime.now(CN)
        return tuple(int(p) for p in parts) == (_today.year, _today.month, _today.day)
    except ValueError:
        return False

def _find_latest_export_dir():
    # 自定位：DB 根目录 = 本脚本所在目录下的 DB 子目录，随文件夹整体移动
    db_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "DB")
    candidates = []
    if os.path.isdir(db_root):
        for d in os.listdir(db_root):
            # 排除基准快照目录（test_ 前缀）——
            #   全新环境尚无导出数据时，直接跑上传会把基准快照当导出数据上传，
            #   污染 sink 库并误报成功。快照只服务于附录H 出错后自检，永不参与上传。
            if d.startswith("test_"):
                continue
            full = os.path.join(db_root, d)
            if os.path.isfile(os.path.join(full, "database_decrypted.db")):
                candidates.append(full)
    if not candidates:
        print("❌ 未找到任何可上传的导出目录（已自动排除基准快照 test_*），请先运行 auto_export_and_analyze.py。")
        sys.exit(1)
    # 标准口径：合并全量库 DB\combined 恒为最新且完整，优先上传它。
    combined = os.path.join(db_root, "combined")
    if os.path.isfile(os.path.join(combined, "database_decrypted.db")):
        return combined
    candidates.sort(key=lambda p: os.path.getmtime(os.path.join(p, "database_decrypted.db")), reverse=True)
    if not _is_today_dir(os.path.basename(candidates[0])):
        print(f"⚠️  未找到今天的导出目录，回退使用最新: {candidates[0]}")
    return candidates[0]

if __name__ == "__main__":
    export_dir = _find_latest_export_dir()
    db_path = os.path.join(export_dir, "database_decrypted.db")

    # database_decrypted.db 是已解密的普通 SQLite，
    #   原脚本用 sqlcipher3 + PRAGMA key 打开会报 "file is not a database"
    #   （此前因 U1 的 %-m 崩溃从未跑到这里，该问题一直被掩盖）。
    #   直接用标准 sqlite3 打开即可，无需任何密钥——U2 的"密钥不落源码"要求就此彻底满足。

    print("=" * 60)
    print("OPPO Health Data Upload (correct format)")
    print("=" * 60)

    # Step 1: Open database
    print("\n[1/3] Opening database...")
    conn = sqlite3.connect(db_path)
    # 线性脚本无 try/finally 包裹（整体缩进改动大、风险高），
    #   改用 atexit 注册兜底关闭——正常结束时的显式 conn.close()（见文件末尾）先执行，
    #   中途异常/提前退出时由 atexit 兜底释放句柄（close 幂等，重复调用安全）。
    import atexit as _atexit
    _atexit.register(conn.close)

    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cursor.fetchall()]
    print(f"  Found {len(tables)} tables")

    # Step 2: Convert to section format
    print("\n[2/3] Converting to section format...")

    # Map table names to time columns
    timecol_map = {
        "DBSportDataDetail": "modified_time",
        "DBSportDataStat": "modified_time",
        "DBOneTimeSport": "modified_time",
        "DBHeartRate": "modified_timestamp",
        "DBHeartRateDataStatTable": "modified_timestamp",
        "DBSleepTable": "modified_timestamp",
        "DBSleepDataStatTable": "modified_timestamp",
        "DBECGRecord": "modified_time_stamp",
        "DBBloodOxygenSaturation": "modified_timestamp",
        "DBBloodOxygenSaturationDataStat": "modified_timestamp",
        "DBStressTable": "modified_timestamp",
        "DBStressDataStatTable": "modified_timestamp",
        "DBWeightBodyFatTable": "modified_timestamp",
        "DBOsaResult": "modified_timestamp",
        "DBSnoreResult": "date",
        "DBSensorOsa": "modified_timestamp",
        "DBSleepIndex": "modified_timestamp",
        "DBSpo2Warning": "modified_timestamp",
        "DBAssessmentRecord": "modified_timestamp",
        "DBSleepDayStat": "modified_timestamp",
        "DBSleepMainStat": "sleep_in_timestamp",
        "DBSleepDayFrgStat": "sleep_in_timestamp",
        "DBSleepPiece": "start_timestamp",
        "DBWristTemperatureStat": "modified_timestamp",
        "DBSportMetadata": "start_timestamp",
        "DBSedentary": "modified_timestamp",
        "DBSleepHeartRateStat": "modified_timestamp",
        "DBBreathRateStat": "modified_timestamp",
        "DBPhysicalMentalStat": "modified_timestamp",
        "DBHealthArchiveRecord": "modified_timestamp",
        "DBHealthIndicatorDetail": "modified_timestamp",
        "DBHealthIndicatorStat": "modified_timestamp",
        "DBTrackMetadata": "start_timestamp",
        "DBRelax": "modified_timestamp",
        "DBRelaxStat": "modified_timestamp",
        "DBOneTimeSportStat": "modified_time",
        "DBUserGoalInfo": "modified_time",
    }

    sections_text = ""
    total_tables = 0
    total_rows = 0
    _seen_headers = set()

    # Tables to skip (SQLite internal tables)
    skip_tables = {"sqlite_sequence", "room_master_table"}

    for table in tables:
        if table in skip_tables:
            print(f"  {table}: SKIPPED (internal table)")
            continue
    
        try:
            # Get row count
            cursor = conn.execute(f'SELECT COUNT(*) FROM {q_ident(table)}')
            count = cursor.fetchone()[0]
        
            if count == 0:
                continue
        
            # Determine time column
            timecol = timecol_map.get(table, "modified_time")
        
            # Get columns
            cursor = conn.execute(f'SELECT * FROM {q_ident(table)} LIMIT 1')
            columns = [desc[0] for desc in cursor.description]

            # 可疑身份列提醒：清单之外的新疑似列（如新版 App 引入 imei/mac）告警供人工复核，不阻断。
            _new_cols = [c for c in columns if c not in _seen_headers]
            _seen_headers.update(columns)
            if _new_cols:
                warn_suspicious_identity_columns(_new_cols, source=f"upload:{table}")
        
            # Check if timecol exists
            if timecol not in columns:
                # U5修复：兜底只认 *timestamp* 子串或精确的 date 列。
                #   原兜底匹配 "time"/"date" 子串，会误中 timezone（含 time 但非时间）、
                #   snore_sum_time（时长秒数）等伪时间列，导致 __t/排序依据错误。
                # 兜底优先级：timestamp > date/_date > time/_time（排除 timezone 陷阱）。
                #   时间列由客户端此处单方选定后随 payload 的 TIMECOL 下发，
                #   两端 server 均只透传、不做独立判定（upload_server 甚至允许空
                #   TIMECOL：__t 记 NULL、不写水位）——保证双通道 __t 语义一致；
                #   找不到可信时间列时置空 TIMECOL（服务端 __t 记 NULL、不写水位）——
                #   不再用首列（如 _id）冒充时间（那会把 __t 写成 1 → 1970 年的垃圾值，
                #   实测 DBSpaceInfo 即因此 __t=1，与 README "NULL 属预期"的描述矛盾）。
                def _ts_score(col):
                    n = col.lower()
                    if n == "timezone":
                        return -1
                    if "timestamp" in n:
                        return 3
                    if n == "date" or n.endswith("_date"):
                        return 2
                    if n == "time" or n.endswith("_time"):
                        return 1
                    return -1
                _best_col, _best_s = None, 0
                for col in columns:
                    sc = _ts_score(col)
                    if sc > _best_s:
                        _best_col, _best_s = col, sc
                timecol = _best_col if _best_col is not None else ""
        
            # U3修复：原 `LIMIT 500` 无 ORDER BY，取的是物理存储顺序（最旧的行），
            #   导致最新数据反而没被上传；且静默截断无任何提示。
            #   现按时间列倒序取「最新」500 行，超出上限时显式警告。
            # timecol 为空（无可信时间列）时 order_clause 为空，
            #   LIMIT 500 退化为 rowid 序（≈最旧优先）——对"行数>500 且无时间列"的表，
            #   上传的恰是手册承诺的反面（最旧数据）且无告警。现兜底 `rowid DESC`
            #   （物理插入序倒序≈最新优先），保证任何情况下取的都是最新 500 行。
            if timecol in columns:
                order_clause = f' ORDER BY {q_ident(timecol)} DESC'
            else:
                order_clause = ' ORDER BY rowid DESC'
            if count > MAX_ROWS_PER_TABLE:
                print(f"  ⚠️ {table}: 共 {count} 行，仅上传最新 {MAX_ROWS_PER_TABLE} 行（截断 {count - MAX_ROWS_PER_TABLE} 行）")
            cursor = conn.execute(f'SELECT * FROM {q_ident(table)}{order_clause} LIMIT {MAX_ROWS_PER_TABLE}')
            rows = cursor.fetchall()
        
            if not rows:
                continue
        
            # Build section
            sections_text += f"===TABLE:{table}|TIMECOL:{timecol}===\n"
            sections_text += ",".join(columns) + "\n"
        
            for row in rows:
                # BUG-3 修复：身份列（ssoid/open_id/device_unique_id/sn/…）值置空后再上传。
                #   只清值、保留列，保证列序与长度不变（row_hash 依赖列序，两通道必须一致）。
                values = []
                for val in scrub_identity_columns(columns, row):
                    if val is None:
                        values.append("")
                    elif isinstance(val, bytes):
                        values.append(val.hex())
                    else:
                        values.append(str(val).replace(",", " ").replace("\n", " "))
                sections_text += ",".join(values) + "\n"
        
            sections_text += "\n"
            total_tables += 1
            total_rows += len(rows)
            print(f"  {table}: {len(rows)} rows (timecol={timecol})")
        
        except Exception as e:
            print(f"  {table}: ERROR - {e}")

    print(f"\n  Total: {total_tables} tables, {total_rows} rows")
    print(f"  Payload size: {len(sections_text) / 1024:.2f} KB")

    # 上传仅经由 HTTPS/HTTP 发送，不在本地落盘任何明文载荷（隐私最小化）。

    # Step 3: Upload
    print("\n[3/3] Uploading to Flask server...")
    try:
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "text/plain"
        }
    
        response = requests.post(
            upload_url,
            data=sections_text.encode("utf-8"),
            headers=headers,
            timeout=60
        )
    
        print(f"  Response status: {response.status_code}")
        print(f"  Response body: {response.text[:500]}")
    
        if response.status_code == 200:
            print("\n" + "=" * 60)
            print("SUCCESS! Data uploaded successfully!")
            print("=" * 60)
        else:
            print(f"\nWARNING: Upload returned status {response.status_code}")
            # 上传失败此前仍以退出码 0 结束，定时任务/CI 无法感知失败，
            #   会误判"今日已同步"。现以非零退出码明示失败。
            sys.exit(1)

    except requests.exceptions.ConnectionError:
        print("  ERROR: Cannot connect to Flask server.")
        sys.exit(1)
    except Exception as e:
        print(f"  ERROR: {e}")
        sys.exit(1)

    conn.close()
    print("\nDone.")
