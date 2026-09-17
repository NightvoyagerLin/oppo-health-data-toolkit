# SPDX-License-Identifier: MIT
#!/usr/bin/env python3
"""
oppo-health MCP Server — OPPO 健康数据接收/查询的标准 MCP 规范接口
================================================================
独立于 Flask 接收端(纯 mcp + sqlite3, 无 flask 依赖), 与手机 APK
通道共用同一 sink 库 (server\\sink\\health.db, 与 upload_server.py 完全一致), 数据天然一致。

安全设计:
  - 所有标识符(表名/列名)白名单校验 ^[A-Za-z_][A-Za-z0-9_]*$, 杜绝 SQL 注入
  - health_query 的 columns 逐列校验必须是该表真实存在的列, 禁止任意 SQL 片段
  - health_ingest 的表/列来自外部文本, 同样校验, 非法即拒绝
  - CSV 用 csv 模块解析(支持带引号字段), 不用脆弱 split(',')
  - 数据写入全参数化(INSERT VALUES 绑参)

注意: 本 server 是本地可信环境(stdio, 供 agent/hook 用)。若暴露到公网,
需在接入层加鉴权(Token/网络隔离), 健康数据属敏感个人信息。
"""
import csv
import hashlib
import io
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

# 与全项目统一的时区基准 CN(UTC+8)。
#   _to_epoch_ms 的 YYYYMMDD 日期分支此前用 naive datetime（宿主本地时区），
#   非 UTC+8 机器上 __t 会整体偏移；现统一按 CN 解释（两端与 test 同步修改）。
_CN = timezone(timedelta(hours=8))

# 从项目根的 utils.py 引入**唯一**的身份列清单。
#   本文件位于 server/ 子目录、以 `python server/mcp_server.py` 运行，sys.path[0] 是
#   server/，故需显式把上一级（项目根）加入搜索路径——与 split_db_by_day.py 引用
#   utils.EVENT_TIME_COLUMNS 的做法同源。清单**只此一份**，绝不内联副本（副本即漂移源）。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from utils import IDENTITY_COLUMNS, warn_suspicious_identity_columns
except ImportError as _e:
    IDENTITY_COLUMNS = ()
    def warn_suspicious_identity_columns(header, source="", sink=None):
        pass
    print("⚠️  未能导入 utils.IDENTITY_COLUMNS，MCP 入库**不会**清洗身份列：%s" % _e,
          file=sys.stderr)

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as _e:
    # mcp 属可选依赖，原顶层 import 直接失败仅留堆栈，用户不知如何处置。
    raise SystemExit(
        "❌ 缺少 MCP 依赖，无法启动本服务。\n"
        "   安装：pip install -r requirements.txt（或单独 pip install mcp）\n"
        "   说明：mcp 为**可选**依赖——只用本地导出与网页报告无需安装它。\n"
        "   原始错误：%s" % _e)

HERE = os.path.dirname(os.path.abspath(__file__))


def _to_epoch_ms(raw):
    """时间值统一转毫秒时间戳；非法返回 None。
    与 upload_server.py 的同名函数保持完全一致——此前 ingest_sections 用
    `int(float(row))` 直转，日期型列（YYYYMMDD，如 20260908）会被当成毫秒
    （≈1970 年）写入 __t，跨通道幂等去重/水位/排序失真。
    YYYYMMDD 日期分支统一按 CN(UTC+8) 解释
    （原 naive datetime 按宿主本地时区，与全项目 L12 统一时区口径不一致）。"""
    raw = str(raw or '').strip()
    try:
        if len(raw) == 8 and raw.isdigit():
            return int(datetime.strptime(raw, "%Y%m%d").replace(tzinfo=_CN).timestamp() * 1000)
        v = int(float(raw))
        if v <= 0:
            return None
        # 10 位秒级时间戳（如 1757300000）此前被原样当毫秒写入 __t，
        #   换算成 1970 年，导致增量水位与 ORDER BY __t 全程错位。现按量级归一：
        #   1e9~1e11 视为秒级 → ×1000；>=1e13 视为微秒 → //1000；13 位毫秒原样通过。
        if 1_000_000_000 <= v < 100_000_000_000:
            v *= 1000
        elif v >= 10_000_000_000_000:
            v //= 1000
        # 小正数（如 "99999"≈1970-01-01 00:01:39）此前直通 return v，
        #   污染 __t 时间索引与增量水位。加 2010 下界（≈1.26e12 ms），早于 2010 视为脏数据返回 None。
        #   与 upload_server.py 的同名函数保持一致（test_auto_export 校验两侧一致）。
        if v < int(datetime(2010, 1, 1, tzinfo=_CN).timestamp() * 1000):
            return None
        return v
    except (ValueError, TypeError):
        return None

#   与 upload_server.py 的 <根>\server\sink\health.db 分裂为两个库，"跨通道共用同一 sink"
#   声明不成立。现与 upload_server 完全一致：脚本所在目录下的 sink\health.db。
SINK_DB = os.path.join(HERE, 'sink', 'health.db')

# L3修复：水位表与 upload_server.py 完全同名同列（_watermark/table_name/max_timestamp），
#   两通道读写同一张水位表，增量游标才真正互通。
_WATERMARK_TABLE = '_watermark'

mcp = FastMCP("oppo-health")

# ---------- 安全校验 ----------
_IDENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
# 允许查询时附加的元数据列(sink 库自动生成, 不在手机 schema 里)
_META_COLS = {"__row_hash", "__t"}


def _check_ident(name: str, kind: str = "标识符") -> str:
    """校验并原样返回合法标识符; 非法抛 ValueError(禁止 SQL 注入/Traversal)。"""
    if not isinstance(name, str) or not _IDENT.match(name):
        raise ValueError(f"非法{kind}: {name!r}(仅允许字母/数字/下划线)")
    if name in ('_watermark', 'sink_cursor'):
        # C-P2-10：保留表名——水位/游标表是 sink 库基础设施，同名"数据表"写入
        #   会破坏增量水位与幂等去重。两侧实现保持逐字一致（parity 测试守护），
        #   故用字面量而非 mcp 侧的 _WATERMARK_TABLE 变量。
        raise ValueError(f"保留表名，禁止作为数据表写入: {name!r}")
    return name


def _existing_tables(c) -> set:
    return {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        f"AND name NOT IN ('{_WATERMARK_TABLE}','sink_cursor')")}


def _table_cols(c, table: str) -> list:
    return [r[1] for r in c.execute(f'PRAGMA table_info("{table}")')]


def _conn():
    # 全新环境未跑过 Flask 接收端时 sink/ 目录不存在，
    #   直接 connect 会 "unable to open database file"，令 MCP 全部工具不可用。
    os.makedirs(os.path.dirname(SINK_DB), exist_ok=True)
    c = sqlite3.connect(SINK_DB, timeout=15)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=15000")
    # L3修复：水位表名/列名与 upload_server.py 完全一致（_watermark.max_timestamp）
    c.execute("CREATE TABLE IF NOT EXISTS _watermark(table_name TEXT PRIMARY KEY, max_timestamp INTEGER)")
    c.commit()
    return c


def _cursor_map():
    c = _conn()
    try:
        return {t: v for t, v in c.execute(
            "SELECT table_name, max_timestamp FROM _watermark").fetchall()}
    finally:
        c.close()


def _parse_section_rows(text):
    """把分节文本转成 [(表, 时间列, [列头], [行])]。行用 csv 解析(支持引号字段)。"""
    sections, cur = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith('===TABLE:'):
            if cur is not None and cur["header"] is not None:
                sections.append((cur["table"], cur["tc"], cur["header"], cur["rows"]))
            m = line[len('===TABLE:'):]
            if m.endswith('==='):
                m = m[:-3]
            parts = [p.strip() for p in m.split('|')]
            if not parts or not parts[0]:
                continue
            table = _check_ident(parts[0], "表名")
            tc = ""
            for p in parts[1:]:
                if p.startswith('TIMECOL:'):
                    tc = p[len('TIMECOL:'):].strip()
            if tc:
                tc = _check_ident(tc, "时间列")
            cur = {"table": table, "tc": tc, "header": None, "rows": []}
        elif cur is not None:
            cells = next(csv.reader(io.StringIO(line))) if line else []
            cells = [x.strip() for x in cells]
            if cur["header"] is None:
                cur["header"] = cells
            elif cells and not (len(cells) == 1 and cells[0] == ''):
                cur["rows"].append(cells)
    if cur is not None and cur["header"] is not None:
        sections.append((cur["table"], cur["tc"], cur["header"], cur["rows"]))
    return sections


def ingest_sections(sections):
    """分节数据校验 + 幂等入库(行哈希 UNIQUE) + 更新水位 -> (表数, 新行, 重复跳过)。"""
    c = _conn()
    total_tables = total_rows = skipped = 0
    try:
        tables = _existing_tables(c)
        for table, timecol, header, rows in sections:
            if not table or not header or not rows:
                continue
            # 列头校验(合法标识符 + 去重)
            seen, clean_head = set(), []
            for h in header:
                h = _check_ident(h, "列名")
                if h in seen:
                    continue
                seen.add(h)
                clean_head.append(h)
            if not clean_head:
                continue
            # 可疑身份列提醒：清单之外的新疑似列告警供人工复核（stderr，不污染 stdio 协议）。
            warn_suspicious_identity_columns(clean_head, source=f"mcp:{table}",
                                             sink=lambda m: print(m, file=sys.stderr))
            col_sql = ", ".join(f'"{x}"' for x in clean_head)
            # 建表(如已存在则无关), 并补齐缺失列(增量导入列集合可能新增)
            if table not in tables:
                c.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ('
                          + col_sql
                          + ', "__row_hash" TEXT UNIQUE, "__t" INTEGER)')
                tables.add(table)
            else:
                have = set(_table_cols(c, table))
                for h in clean_head:
                    if h not in have and h not in _META_COLS:
                        c.execute(f'ALTER TABLE "{table}" ADD COLUMN "{h}" TEXT')
                        have.add(h)
            ti = clean_head.index(timecol) if timecol in clean_head else -1
            placeholders = ",".join(["?"] * (len(clean_head) + 2))
            col_names = ", ".join(f'"{x}"' for x in clean_head) + ', "__row_hash", "__t"'
            insert_sql = f'INSERT OR IGNORE INTO "{table}" ({col_names}) VALUES ({placeholders})'
            n, last_ts = 0, None
            for row in rows:
                # 按 header 长度归一(缺位补空, 超位截断)
                row = (row + [''] * len(clean_head))[:len(clean_head)]
                # 与 upload 通道**同规则**清洗身份列
                #   （ssoid/open_id/device_unique_id/sn/… 值置空、保留列）。
                #   必须在算 row_hash **之前**做，且两通道用同一份清单——否则同一逻辑行
                #   经两通道得到不同哈希，跨通道幂等去重会失效（这也是本处注释反复强调的坑）。
                if IDENTITY_COLUMNS:
                    row = [("" if clean_head[_i] in IDENTITY_COLUMNS else _v)
                           for _i, _v in enumerate(row)]
                # 服务端统一做"值内逗号→空格"规范化——
                #   upload 通道在客户端组包时已做该替换（upload_data.py），MCP 通道此前不做，
                #   同一逻辑行经两通道存储内容不同 → 行哈希必然不同，跨通道幂等去重失效。
                #   规范化放服务端两侧（upload_server 同步加固）后，无论客户端行为如何，
                #   同库同哈希。该替换对已替换过的 upload 载荷是幂等的。
                row = [str(x).replace(',', ' ').replace('\n', ' ').replace('\r', ' ') for x in row]
                h = hashlib.sha256("\x1f".join(row).encode('utf-8')).hexdigest()
                ts = None
                if ti >= 0:
                    # 统一走 _to_epoch_ms（与 upload_server
                    #   同语义）——日期型 YYYYMMDD 列不再被当毫秒原样入库；0/负值与
                    #   非法值返回 None（原 D1 修复的"非正数视为无效"语义保留在内）。
                    ts = _to_epoch_ms(row[ti])
                if ts is not None:
                    last_ts = ts if last_ts is None else max(last_ts, ts)
                cur = c.execute(insert_sql, list(row) + [h, ts])
                if cur.rowcount > 0:
                    n += 1
                else:
                    skipped += 1
            if n:
                total_tables += 1
                total_rows += n
                if last_ts is not None:
                    c.execute("INSERT INTO _watermark(table_name,max_timestamp) VALUES(?,?) "
                              "ON CONFLICT(table_name) DO UPDATE SET "
                              "max_timestamp=max(_watermark.max_timestamp, excluded.max_timestamp)",
                              (table, last_ts))
            # C-P2-9：commit 移出 if n——纯重复批次（n=0）也可能执行过 ALTER TABLE 补列，
            #   不 commit 会在 finally close 时回滚，同一批新列下次导入还得重 ALTER 一次。
            c.commit()
    finally:
        c.close()
    return total_tables, total_rows, skipped


# ---------- MCP 工具 ----------
@mcp.tool()
def health_ingest(sections_text: str) -> dict:
    """接收健康数据增量分节文本(格式: ===TABLE:<表>|TIMECOL:<列>=== + 列头行 + 数据行,
    可多表分节)。幂等入库(行哈希 UNIQUE)并更新水位。
    返回 {tables, new_rows, dup_skipped, watermark}。"""
    try:
        secs = _parse_section_rows(sections_text)
        t, r, s = ingest_sections(secs)
    except ValueError as e:
        return {"error": f"非法标识符: {e}"}
    return {"tables": t, "new_rows": r, "dup_skipped": s, "watermark": _cursor_map()}


@mcp.tool()
def health_watermark() -> dict:
    """查询各表水位(已同步到的最大时间戳, 毫秒)。"""
    return _cursor_map()


@mcp.tool()
def health_list_tables() -> list:
    """列出 sink 库所有已入库表 + 各自行数。"""
    c = _conn()
    try:
        tabs = sorted(_existing_tables(c))
        return [{"table": t, "rows": c.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]}
                for t in tabs]
    finally:
        c.close()


@mcp.tool()
def health_stats() -> dict:
    """各表统计概览 {表: {rows, min_t, max_t}}。"""
    c = _conn()
    try:
        res = {}
        for t in sorted(_existing_tables(c)):
            n = c.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
            try:
                tmin, tmax = c.execute(f'SELECT min(__t), max(__t) FROM "{t}"').fetchone()
            except Exception:
                tmin = tmax = None
            res[t] = {"rows": n, "min_t": tmin, "max_t": tmax}
        return res
    finally:
        c.close()


@mcp.tool()
def health_query(table: str, columns: str = "*", since: int = None, until: int = None,
                 limit: int = 500) -> dict:
    """查询某表已入库数据。table=表名(须已存在); columns=逗号分隔的合法列名(默认* 全部,
    可含 __t/__row_hash); since/until=毫秒时间戳过滤 __t(增量同步用);
    limit=最多行(默认500, 最大5000)。返回 {columns, rows}。"""
    # int(limit) 原本裸写在 try 之外，传入非数字（如 "abc"）
    #   会抛未包装的 ValueError。虽然 FastMCP 框架层会兜底返回错误、不至于崩溃，
    #   但错误信息是内部堆栈、对调用方不友好。此处显式校验并返回可读错误。
    try:
        limit = max(1, min(int(limit), 5000)) if limit else 500  # X6：负值钳到 1，防 LIMIT 负值=无上限
    except (TypeError, ValueError):
        return {"error": f"limit 必须是整数（1~5000），收到: {limit!r}"}
    c = _conn()
    try:
        tables = _existing_tables(c)
        if table not in tables:
            return {"error": f"表不存在: {table!r}，可用表见 health_list_tables"}
        valid = set(_table_cols(c, table)) | _META_COLS
        if not columns or columns.strip() == '*' or columns.strip().lower() == 'all':
            sel = [f'"{x}"' for x in sorted(valid)]  # all: 列名全部引号包裹, 无注入面
        else:
            sel = []
            for col in columns.split(','):
                col = col.strip()
                if not col:
                    continue
                if col not in valid:
                    raise ValueError(f"非法列: {col!r}(该表无此列, 可用列: {sorted(valid)})")
                sel.append(f'"{col}"')
            if not sel:
                raise ValueError("未指定有效列")
        where, params = [], []
        if since is not None:
            where.append("__t >= ?"); params.append(int(since))
        if until is not None:
            where.append("__t <= ?"); params.append(int(until))
        sql = f'SELECT {", ".join(sel)} FROM "{table}"'
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY __t LIMIT ?"
        params.append(limit)
        cur = c.execute(sql, params)
        colnames = [d[0] for d in cur.description]
        rows = [list(r) for r in cur.fetchall()]
        return {"columns": colnames, "rows": rows}
    except (ValueError, sqlite3.OperationalError) as e:
        # C-P3-8：OperationalError 同捕——异常库缺 __t 等列时原会堆栈崩溃而非返回 error
        return {"error": str(e)}
    finally:
        c.close()


@mcp.tool()
def health_schema(table: str) -> dict:
    """查看某表结构(列名)。table 须已存在。"""
    c = _conn()
    try:
        tables = _existing_tables(c)
        if table not in tables:
            return {"error": f"表不存在: {table!r}"}
        return {"table": table, "columns": _table_cols(c, table)}
    finally:
        c.close()


if __name__ == "__main__":
    mcp.run()
