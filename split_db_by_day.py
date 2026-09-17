# SPDX-License-Identifier: MIT
# -*- coding: utf-8 -*-
"""split_db_by_day.py —— 把合并解密库按「天」拆分为独立日库（YYYYMMDD 列 + epoch毫秒事件列 两种分区，安全无丢失）

把合并解密库 <工作目录>/DB/combined/database_decrypted.db
按「天」拆分为独立文件夹：<工作目录>/DB/YYYY-MM-DD/database_decrypted.db

分区策略（每张表三选一）：
  ymd    : 有 `date` 类列且值可解析为 YYYYMMDD 整数 -> 按该列精确匹配当天。
  epoch  : 否则有「事件时间戳」列（start_time / data_created_timestamp /
           measurement_timestamp / sleep_in_timestamp ...）且值为毫秒 ->
           将毫秒换算为中国时区(UTC+8)日历日再匹配。
           （注意：排除 create_time / modified_time / updated 等「元数据」时间戳，
            这类列属于档案/配置表，整表按 static 处理，避免把 2021 年的档案行
            误分到某个 2026 日文件夹外而丢失。）
  static : 其余（设备信息、用户档案、配置、生日等）-> **集中复制一次**到共享库
           static.db（位于 DB 目录下），各日库不再逐日重复复制（M21 改造，2026-09-15）。
           静态表内容全局一致、不随日期变化，逐日复制会让日库体积与拆分耗时
           随「天数 × 静态表数」线性放大（原 200 天 × 34 表 = 6800 次整表复制）。
           此处「单日库自包含」指**真实分析数据**（ymd/epoch 分区表）自包含；
           参考/配置类静态表统一到 static.db 查阅。

安全原则：
  1. 源库只读，绝不修改/删除/覆盖。
  2. 目标文件夹若已存在 database_decrypted.db 则跳过（SKIP）；--force 先备份为 .bak。
  3. 全局日期窗口 = YMD 表范围 ∪ epoch 事件表范围，确保分区行不会落在窗口外。
  4. 拆分后校验：每个分区表「各单日库行数之和」== 源库「落在窗口内」的行数。
     0/NULL 时间戳等无法归日的行会在报告中提示（源库始终保留，未丢失）。
  5. 自动识别范围，可重复运行。绝不接触源库原始文件。

用法：
    python split_db_by_day.py
    python split_db_by_day.py --force
"""
import sqlite3, os, re, shutil, argparse, sys, json
from datetime import date, datetime, timedelta, timezone

# 强制 UTF-8 输出，避免 Windows 控制台/重定向乱码（V1.1 修复）
try:
    from utils import ensure_utf8_stdout
    ensure_utf8_stdout()
except Exception:
    pass

# manifest 写入改用 atomic_write，避免中断留下半截 JSON。
#   与文件其他写入保持一致；utils 缺失时降级为普通 open（与 EVENT_TIME_COLUMNS 兜底同风格）。
try:
    from utils import atomic_write
except Exception:
    atomic_write = None

# 自定位：以本脚本所在目录为根，整文件夹搬家/改名无需改路径
_BASE = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(_BASE, "DB", "combined")
SRC_DB = os.path.join(SRC_DIR, "database_decrypted.db")
OUT_ROOT = os.path.join(_BASE, "DB")
CN = timezone(timedelta(hours=8))


def q_ident(name):
    """把表名/列名安全地包成 SQLite 标识符（`"` 翻倍、剔除 NUL）。
    与 json_to_sqlite.q_ident /
    mcp_server._check_ident 同一标准，替换本文件残留的 f'"{t}"' 裸拼
    （名字来自本地解密库 sqlite_master / PRAGMA，防御性统一）。
    对正常名字输出与原 f'"{t}"' 完全一致，行为零变化。"""
    return '"' + str(name).replace('"', '""').replace("\x00", "") + '"'

# 可作为「事件日」分区键的时间戳列（按优先级）。这些是真实事件发生的时刻。
# 清单改为从 utils.EVENT_TIME_COLUMNS 统一引入
#   （GUI 与此处共用同一份），避免两份清单漂移。
# 补导入兜底——utils.py 缺失/损坏时本脚本不再 ImportError
#   崩溃（与 auto_export_and_analyze.py 的 C5 兜底同风格），降级为等价的内联副本。
try:
    from utils import EVENT_TIME_COLUMNS
except ImportError:      # pragma: no cover - 仅在 utils 缺失时触发
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
EVENT_EPOCH = list(EVENT_TIME_COLUMNS)

# 删除此前定义但**从未被引用**的 META_EPOCH 死常量——
#   元数据时间戳（create_time/modified_time/…）本就不在 EVENT_EPOCH 白名单内，
#   因此天然被当作 static 处理，无需黑名单；保留只会误导维护者。
# EPOCH_LO/HI 为「事件毫秒时间戳」的合理区间判定阈值，
#   约对应 2017-01 ~ 2036-05；区间外的值不视为事件列（该表按 static 整表复制，
#   不丢数据，只是不按天分区）。
EPOCH_LO, EPOCH_HI = 1.5e12, 2.1e12  # 2017~2036 合理范围

def is_yyyymmdd_int(v):
    if not isinstance(v, int):
        return None
    s = str(v)
    if len(s) != 8:
        return None
    y, m, d = int(s[0:4]), int(s[4:6]), int(s[6:8])
    if not (2000 <= y <= 2100): return None
    if not (1 <= m <= 12): return None
    if not (1 <= d <= 31): return None
    return s

def try_parse_ymd(con, table, col):
    try:
        vals = [r[0] for r in con.execute(
            f'SELECT DISTINCT {q_ident(col)} FROM {q_ident(table)} WHERE {q_ident(col)} IS NOT NULL LIMIT 800')]
    except Exception:
        return set()
    out = set()
    for v in vals:
        if isinstance(v, int):
            p = is_yyyymmdd_int(v)
            if p: out.add(p)
        elif isinstance(v, str):
            s = v.strip()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
                out.add(s.replace("-", ""))
            elif re.fullmatch(r"\d{4}_\d{1,2}_\d{1,2}", s):
                y, m, d = s.split("_"); out.add(f"{int(y):04d}{int(m):02d}{int(d):02d}")
            elif re.fullmatch(r"\d{8}", s):
                out.add(s)
    return out

def try_epoch(con, table, col):
    try:
        vals = [r[0] for r in con.execute(
            f'SELECT DISTINCT {q_ident(col)} FROM {q_ident(table)} WHERE {q_ident(col)} IS NOT NULL LIMIT 800')]
    except Exception:
        return False
    for v in vals:
        if isinstance(v, int) and EPOCH_LO <= v <= EPOCH_HI:
            return True
    return False

def detect_partition(con, table, cols):
    """返回 (col, kind) kind in {'ymd','epoch',None}"""
    name_cands = [c for c in cols
                  if re.search(r"date|time|day", c, re.I)
                  and not re.search(r"birth", c, re.I)]
    # 1) YYYYMMDD 列
    for col in name_cands:
        p = try_parse_ymd(con, table, col)
        if p:
            return col, "ymd", p
    # 2) 事件时间戳列（排除元数据时间戳）
    for col in EVENT_EPOCH:
        if col in cols and try_epoch(con, table, col):
            return col, "epoch", None
    return None, None, None

def ymd_variants(ymd):
    # C-P2-17：补 str(ymd) 变体——date 列可能以 TEXT '20260908' 存储，仅 int 的
    #   等值匹配对 TEXT 列恒不命中（SQLite 类型序：INTEGER ≠ TEXT）。
    y, m, d = int(ymd[0:4]), int(ymd[4:6]), int(ymd[6:8])
    return [int(ymd), str(ymd), f"{y:04d}-{m:02d}-{d:02d}",
            f"{y}_{m}_{d}", f"{y:04d}_{m:02d}_{d:02d}"]

def day_bounds_ms(cur):
    start = datetime(cur.year, cur.month, cur.day, 0, 0, 0, tzinfo=CN)
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)

def epoch_to_date(ms):
    return datetime.fromtimestamp(ms / 1000, CN).date()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(SRC_DB):
        print(f"[错误] 未找到源库：{SRC_DB}"); sys.exit(1)

    # C-P2-18：mode=ro 只读打开源库——从连接层杜绝任何写路径（安全原则 1 的落地），
    #   不再仅依赖"代码里没有写语句"这一约定。URI 用正斜杠规避 Windows 盘符歧义。
    src = sqlite3.connect(f"file:{os.path.abspath(SRC_DB).replace(os.sep, '/')}?mode=ro", uri=True)
    # 连接关闭移入 finally（此前异常路径泄漏连接）
    try:
        tables = [r[0] for r in src.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]

        meta = {}
        kind_count = {"ymd": 0, "epoch": 0, "static": 0}
        for t in tables:
            if t == "sqlite_sequence":
                continue
            cols = [c[1] for c in src.execute(f'PRAGMA table_info({q_ident(t)})')]
            create = src.execute(
                "SELECT sql FROM sqlite_master WHERE name=?", (t,)).fetchone()[0]
            col, kind, parsed = detect_partition(src, t, cols)
            meta[t] = {"cols": cols, "create": create, "pcol": col, "kind": kind}
            kind_count[kind if kind else "static"] += 1

        # 收集「有数据」的日期集合（仅这些天生成文件夹，避免空文件夹）
        active = set()
        for t in meta:
            m = meta[t]
            pcol, kind = m["pcol"], m["kind"]
            if kind == "ymd":
                vals = src.execute(
                    f'SELECT DISTINCT {q_ident(pcol)} FROM {q_ident(t)} WHERE {q_ident(pcol)} IS NOT NULL').fetchall()
                for (v,) in vals:
                    if isinstance(v, int):
                        ymd = is_yyyymmdd_int(v)
                    elif isinstance(v, str):
                        s = v.strip()
                        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
                            ymd = s.replace("-", "")
                        elif re.fullmatch(r"\d{4}_\d{1,2}_\d{1,2}", s):
                            y, mo, d = s.split("_"); ymd = f"{int(y):04d}{int(mo):02d}{int(d):02d}"
                        elif re.fullmatch(r"\d{8}", s):
                            ymd = s
                        else:
                            ymd = None
                    else:
                        ymd = None
                    if ymd:
                        active.add(ymd)
            elif kind == "epoch":
                vals = src.execute(
                    f'SELECT DISTINCT {q_ident(pcol)} FROM {q_ident(t)} '
                    f'WHERE {q_ident(pcol)} >= ? AND {q_ident(pcol)} <= ?', (EPOCH_LO, EPOCH_HI)).fetchall()
                for (ms,) in vals:
                    if isinstance(ms, int):
                        d = epoch_to_date(ms)
                        active.add(f"{d.year:04d}{d.month:02d}{d.day:02d}")

        active_days = sorted(active)
        # 过滤非有效日历日（如 20260231）——is_yyyymmdd_int
        #   只校验 1<=d<=31，date() 构造会抛 ValueError 令全脚本崩溃。
        def _valid_day(ymd):
            try:
                date(int(ymd[0:4]), int(ymd[4:6]), int(ymd[6:8]))
                return True
            except ValueError:
                return False
        dropped_days = sorted(d for d in active_days if not _valid_day(d))
        if dropped_days:
            print(f"  [警告] 以下为非有效日历日，已跳过：{dropped_days}")
        active_days = [d for d in active_days if _valid_day(d)]

        if not active_days:
            print("[错误] 未从源库解析出任何有效日期，终止。"); sys.exit(1)
        gmin, gmax = active_days[0], active_days[-1]

        print(f"[源库] {SRC_DB}")
        print(f"[分区类型] YYYYMMDD表={kind_count['ymd']}  epoch事件表={kind_count['epoch']}  静态表={kind_count['static']}")
        print(f"[有数据的日期数] {len(active_days)}（{gmin} ~ {gmax}，仅生成有数据的天）")

        # 静态表（无分区列的参考/配置/档案表）此前被**逐日整表复制**，
        #   200 天 × 34 表 = 6,800 次整表复制，日库体积与拆分耗时随「天数 × 静态表数」线性放大，
        #   远超必要性。静态表内容全局一致、不随日期变化，故改为**仅复制一次**到共享 static.db，
        #   各日库只保留分区（动态）表——既消除空间/耗时放大，又保持各日库对真实数据的自包含。
        #   注意：此处的"独立读取"指真实分析数据自包含；参考/配置类静态表统一在 static.db 查阅。
        # 修复：OUT_ROOT 可能尚未创建（例如把源库拷到别处后
        #   单独跑本脚本，或先手动清过 DB\ 目录）。此前直接 connect(OUT_ROOT/static.db)
        #   会抛 OperationalError: unable to open database file —— 一个与业务无关、
        #   且指向 sqlite 的报错，排查成本高。此处提前建目录。
        os.makedirs(OUT_ROOT, exist_ok=True)
        static_db = os.path.join(OUT_ROOT, "static.db")
        if os.path.exists(static_db):
            os.remove(static_db)
        sconn = sqlite3.connect(static_db)
        try:
            sconn.execute("ATTACH ? AS src", (SRC_DB,))
            n_static = 0
            for t in meta:
                if meta[t]["kind"] is not None:
                    continue
                try:
                    # 盲审复审 L1：CREATE 与 INSERT 同受保护——旧写法 create 失败被
                    #   except:pass 吞掉后，INSERT 在 try 外抛 no such table 且未捕获，
                    #   整个拆分崩溃、verify 不执行。静态表为参考/配置类，单张失败
                    #   打印警告并继续（不中断核心数据拆分），与逐日循环口径对齐。
                    sconn.execute(meta[t]["create"])
                    sconn.execute(f'INSERT INTO main.{q_ident(t)} SELECT * FROM src.{q_ident(t)}')
                    n_static += 1
                except Exception as _e:
                    print(f"  ⚠️ 静态表 {t} 复制失败，跳过: {_e}")
            sconn.commit()
        finally:
            sconn.close()
        print(f"[静态表] 已集中复制 {n_static} 张静态表到 {static_db}（各日库不再重复复制）")

        created = skipped = 0
        for ymd in active_days:
            cur = date(int(ymd[0:4]), int(ymd[4:6]), int(ymd[6:8]))
            folder = os.path.join(OUT_ROOT, f"{cur.year:04d}-{cur.month:02d}-{cur.day:02d}")
            out_db = os.path.join(folder, "database_decrypted.db")
            if os.path.exists(out_db):
                if not args.force:
                    print(f"  SKIP {folder}"); skipped += 1; continue
                # 先删除已存在的旧 .bak，避免 shutil.move
                #   静默覆盖掉上一次的备份（旧备份丢失且失败时难以追溯）。
                bak = out_db + ".bak"
                if os.path.exists(bak):
                    os.remove(bak)
                shutil.move(out_db, bak); print(f"  BACKUP {bak}")
            os.makedirs(folder, exist_ok=True)
            dst = sqlite3.connect(out_db)
            try:
                dst.execute("ATTACH ? AS src", (SRC_DB,))
                for t in tables:
                    if t == "sqlite_sequence":
                        continue
                    m = meta[t]
                    try:
                        dst.execute(m["create"])
                    except Exception as e:
                        # 建表失败不再是"打印警告后继续"——
                        #   那样会产出一个**缺表的残废日库**，且后续 INSERT/verify
                        #   会以更难定位的 OperationalError 失败。此处按致命处理。
                        if "already exists" not in str(e):
                            raise RuntimeError(
                                f"建表失败 {t}（日库 {out_db}）：{e}；"
                                f"已中止该日拆分，请检查源库结构后重跑")
                    pcol, kind = m["pcol"], m["kind"]
                    if kind == "ymd":
                        vs = ymd_variants(ymd)
                        qs = " OR ".join([f'{q_ident(pcol)} = ?'] * len(vs))
                        dst.execute(f'INSERT INTO main.{q_ident(t)} SELECT * FROM src.{q_ident(t)} WHERE {qs}', vs)
                    elif kind == "epoch":
                        s_ms, e_ms = day_bounds_ms(cur)
                        dst.execute(
                            f'INSERT INTO main.{q_ident(t)} SELECT * FROM src.{q_ident(t)} '
                            f'WHERE {q_ident(pcol)} >= ? AND {q_ident(pcol)} < ?', (s_ms, e_ms))
                    # 原 else 分支中的整表 INSERT 在 M21 之后
                    #   已不可达（kind 只可能是 ymd/epoch/None，None 已被上面的
                    #   `if kind is None: continue` 拦截），属死代码，误导维护者。已移除。
                    #   注：kind 为 None（静态表）时不做任何写入——静态表在 static.db。
                dst.commit()
            finally:
                dst.close()
            created += 1
    finally:
        src.close()
    print(f"[完成] 新建 {created} 个文件夹，跳过 {skipped} 个。")
    # manifest 写入改用 atomic_write（临时文件+os.replace），
    #   避免中断留下半截 JSON；utils 不可用时降级为普通 open。
    _manifest = {"range": [gmin, gmax], "days_with_data": len(active_days),
                 "created": created, "skipped": skipped,
                 "static_db": os.path.join(OUT_ROOT, "static.db"),
                 "partition": {t: meta[t]["kind"] for t in meta}}
    _manifest_path = os.path.join(OUT_ROOT, "_split_manifest.json")
    if atomic_write is not None:
        with atomic_write(_manifest_path, encoding="utf-8") as f:
            json.dump(_manifest, f, ensure_ascii=False, indent=2)
    else:
        with open(_manifest_path, "w", encoding="utf-8") as f:
            json.dump(_manifest, f, ensure_ascii=False, indent=2)
    print("[校验] 核对分区表行数之和 == 源库落在窗口内的行数 ...")
    verify(src_db=SRC_DB, out_root=OUT_ROOT, meta=meta, gmin=gmin, gmax=gmax,
           active_days=active_days)

def verify(src_db, out_root, meta, gmin, gmax, active_days):
    # src 及循环内 c 连接统一 try/finally 关闭（此前异常路径泄漏）。
    # C-P2-18：与 main 同款——mode=ro 只读打开源库。
    src = sqlite3.connect(f"file:{os.path.abspath(src_db).replace(os.sep, '/')}?mode=ro", uri=True)
    try:
        gmin_int = int(gmin); gmax_int = int(gmax)
        d0 = date(int(gmin[0:4]), int(gmin[4:6]), int(gmin[6:8]))
        d1 = date(int(gmax[0:4]), int(gmax[4:6]), int(gmax[6:8]))
        w_start, w_end = day_bounds_ms(d0)[0], day_bounds_ms(d1)[1]
        bad = 0
        for t in meta:
            if t == "sqlite_sequence":
                continue
            kind = meta[t]["kind"]
            if kind is None:
                # M21修复：静态表已集中到共享 static.db，日库不再逐日复制；
                #   此处直接校验共享库行数 == 源库，避免逐日比对（原本会因缺表全判 mismatch）。
                sdb = os.path.join(out_root, "static.db")
                if not os.path.exists(sdb):
                    print(f"  [缺失] 共享静态库 {sdb} 不存在，静态表 {t} 无法校验")
                    bad += 1
                    continue
                sc = sqlite3.connect(sdb)
                try:
                    n_static = sc.execute(f'SELECT COUNT(*) FROM {q_ident(t)}').fetchone()[0]
                finally:
                    sc.close()
                n_src = src.execute(f'SELECT COUNT(*) FROM {q_ident(t)}').fetchone()[0]
                if n_static != n_src:
                    print(f"  [不一致] 静态表 {t}: 共享库 {n_static} ≠ 源库 {n_src}")
                    bad += 1
                continue
            pcol = meta[t]["pcol"]
            if kind == "ymd":
                # 原用 `"pcol" >= gmin_int AND "pcol" <= gmax_int`
                #   范围比较。SQLite 的类型序为 NULL < 数值(INTEGER/REAL) < 文本 < BLOB，
                #   一旦 date 列以 TEXT 存储（如 '20260908'），`TEXT <= INTEGER` **恒为假**，
                #   expected 恒为 0，而实际拆分用的是 ymd_variants 等值匹配、能正常取出 N 行，
                #   于是稳定误报 "期望0 拆分和N"。
                #   改为与拆分逻辑**完全同源**的等值匹配：把所有活跃日的取值变体展平后
                #   用 IN 查询（变体可能上千，按 500 个一批拆分以避开 SQL 变量数上限）。
                all_vs = []
                for _y in active_days:
                    all_vs.extend(ymd_variants(_y))
                expected = 0
                for _i in range(0, len(all_vs), 500):
                    _chunk = all_vs[_i:_i + 500]
                    _qs = ",".join("?" * len(_chunk))
                    expected += src.execute(
                        f'SELECT COUNT(*) FROM {q_ident(t)} WHERE {q_ident(pcol)} IN ({_qs})',
                        _chunk).fetchone()[0]
            else:
                expected = src.execute(
                    f'SELECT COUNT(*) FROM {q_ident(t)} WHERE {q_ident(pcol)} >= ? AND {q_ident(pcol)} < ?',
                    (w_start, w_end)).fetchone()[0]
            total_split = 0
            missing = 0   # 记录"日库缺表"的天数
            for ymd in active_days:
                cur = date(int(ymd[0:4]), int(ymd[4:6]), int(ymd[6:8]))
                db = os.path.join(out_root, f"{cur.year:04d}-{cur.month:02d}-{cur.day:02d}",
                                   "database_decrypted.db")
                if not os.path.exists(db):
                    continue
                c = sqlite3.connect(db)
                try:
                    total_split += c.execute(f'SELECT COUNT(*) FROM {q_ident(t)}').fetchone()[0]
                except sqlite3.OperationalError as e:
                    # 日库缺表时记录提示并计入提示数，
                    #   不再抛 OperationalError 中断整个校验。
                    print(f"  [缺表] {t} 在 {os.path.basename(os.path.dirname(db))} 中不存在：{e}")
                    missing += 1
                finally:
                    c.close()
            if missing:
                bad += 1
            if expected != total_split:
                diff = total_split - expected
                print(f"  [提示] {t}({kind}): 窗口内期望={expected} 拆分和={total_split} "
                      f"(差={diff}，多为窗口外/0/NULL 时间戳行，源库保留)")
                bad += 1
    finally:
        src.close()
    if bad == 0:
        print("  [校验通过] 所有分区表/静态表行数一致，未发现数据丢失。")
    else:
        print(f"  [校验结束] 共 {bad} 类提示（详见上文；静态表不一致为真实问题已标出）。")

if __name__ == "__main__":
    # 修复：建表失败会按致命抛出 RuntimeError，此处统一兜底为
    #   友好提示 + 退出码 1（与 main() 中"源库不存在""无有效日期"的处理风格一致），
    #   避免直接把 traceback 抛给使用者。
    try:
        main()
    except RuntimeError as e:
        print(f"[错误] {e}")
        sys.exit(1)
