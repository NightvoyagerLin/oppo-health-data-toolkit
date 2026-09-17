# SPDX-License-Identifier: MIT
import sqlite3
import hashlib
import hmac
import re
from datetime import datetime, timedelta, timezone
import os
import sys
import logging
from flask import Flask, request, jsonify

# 与全项目统一的时区基准 CN(UTC+8)。
#   _to_epoch_ms 的 YYYYMMDD 日期分支此前用 naive datetime（宿主本地时区），
#   非 UTC+8 机器上 __t 会整体偏移；现统一按 CN 解释（两端与 test 同步修改）。
_CN = timezone(timedelta(hours=8))

# BUG-3 服务端兜底清洗：与 mcp_server.py 同规则。
#   绕过客户端直发 /api/upload 的载荷会把 ssoid/设备号原样写进 sink 库。
#   现服务端入库前按同一清单兜底清洗（置于 row_hash 计算之前，保证双通道哈希一致；
#   对已清洗过的客户端载荷是幂等的）。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from utils import IDENTITY_COLUMNS, warn_suspicious_identity_columns
except ImportError as _e:
    IDENTITY_COLUMNS = ()
    def warn_suspicious_identity_columns(header, source="", sink=None):
        pass
    print("⚠️  未能导入 utils.IDENTITY_COLUMNS，上传入库**不会**兜底清洗身份列：%s" % _e,
          file=sys.stderr)

# 配置日志
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)
# 限制请求体大小（默认无上限），防止超大 POST 耗尽内存。
# 正常载荷为数 MB 量级，64MB 余量充足；超限 Flask 自动返回 413。
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024

# ========== API Token（2026-09-09 三轮复审按用户要求写死：本机个人测试环境，
#             仅此一个密钥；须与 upload_data.py 的 token 完全一致）==========
# 支持环境变量 OPPO_HEALTH_TOKEN 覆盖——
#   默认值仍为 CHANGE_ME_TOKEN（两端默认一致，不破坏测试/本机一致性）；
#   共享或非本机环境请设置该环境变量为自定义强口令。
_DEFAULT_TOKEN = "CHANGE_ME_TOKEN"   # 公开占位值，禁止在对外监听时继续使用（见文件末尾启动检查）
API_TOKEN = os.environ.get("OPPO_HEALTH_TOKEN", _DEFAULT_TOKEN)
# ================================================================================

# ---------- S1修复：SQL 标识符白名单校验（对齐 mcp_server.py 的安全标准） ----------
# 表名/列名来自上传文本，此前直接 f-string 拼进 CREATE/ALTER/INSERT，存在注入面。
_IDENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

def _check_ident(name: str, kind: str = "标识符") -> str:
    """校验并原样返回合法标识符；非法抛 ValueError（拒绝注入）。

    与 server/mcp_server.py 的同名实现**逐字对齐**
    （此处原为无注解版且错误文案用全角括号）。跨通道幂等去重依赖两侧对同一
    标识符的判定与报错完全一致；差异由 test_auto_export.TestServerHelperParity 守护。
    """
    if not isinstance(name, str) or not _IDENT.match(name):
        raise ValueError(f"非法{kind}: {name!r}(仅允许字母/数字/下划线)")
    if name in ('_watermark', 'sink_cursor'):
        # C-P2-10：保留表名——水位/游标表是 sink 库基础设施，同名"数据表"写入
        #   会破坏增量水位与幂等去重。两侧实现保持逐字一致（parity 测试守护），
        #   故用字面量而非 mcp 侧的 _WATERMARK_TABLE 变量。
        raise ValueError(f"保留表名，禁止作为数据表写入: {name!r}")
    return name

def _to_epoch_ms(raw):
    """时间值统一转毫秒时间戳；非法返回 None。
    原 YYYYMMDD 分支用 `int(raw)*86400000`——
    20260908×86400000 ≈ 1.75e15 ms ≈ 公元 5.7 万年，量纲完全错误（正确应约 1.79e12）。
    改用 strptime 解析为当日 0 点的毫秒时间戳。
    当日 0 点统一按 CN(UTC+8) 解释（原 naive datetime
    按宿主本地时区，与全项目 L12 统一时区口径不一致）。
    源库时间列存在大量 0 值（如 DBBreathRateStat
    .modified_timestamp 全表为 0），0 原样入库变成 __t=0（1970 年），污染时间索引
    与增量水位。非正数时间戳一律视为无效返回 None（__t 记 NULL、不参与水位）。"""
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
        if v < int(datetime(2010, 1, 1, tzinfo=_CN).timestamp() * 1000):
            return None
        return v
    except (ValueError, TypeError):
        return None

# 数据库路径：与 mcp_server.py 保持一致
DB_PATH = os.path.join(os.path.dirname(__file__), 'sink', 'health.db')

def get_db():
    """获取数据库连接，自动创建目录，启用 WAL 和 busy_timeout"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute('PRAGMA journal_mode=WAL;')
    conn.execute('PRAGMA busy_timeout=5000;')
    return conn

def init_db():
    """初始化数据库，至少创建 _watermark 表"""
    # 连接关闭移入 finally（此前异常路径泄漏）。
    conn = get_db()
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS _watermark (
            table_name TEXT PRIMARY KEY,
            max_timestamp INTEGER
        )''')
        conn.commit()
    finally:
        conn.close()

def parse_section(text):
    """
    解析分节数据格式：
    ===TABLE:表名|TIMECOL:时间列===
    表头行
    数据行...
    返回 [(表名, 时间列, 表头列表, 数据行字典列表), ...]
    改用 csv.reader 解析行（与 mcp_server 的解析对称），
      支持带引号字段——此前 line.split(',') 遇到第三方客户端直发的含引号/逗号数据
      会错位拆列；upload_data 客户端组包无引号字段，行为完全兼容。
    """
    import csv
    import io as _io
    pattern = re.compile(
        # TIMECOL 值允许为空——upload_data 找不到可信时间列时
        #   发送空 TIMECOL（__t 记 NULL），原 `([^=]+)` 会把整个分节丢弃。
        r'===TABLE:([^|]+)\|TIMECOL:([^=]*)===\n(.*?)(?=\n===TABLE:|\Z)',
        re.DOTALL
    )
    sections = []
    skipped = 0  # 被丢弃的"列数不匹配"行数（局部变量，随返回值传给调用方）
    for match in pattern.finditer(text):
        table = match.group(1).strip()
        timecol = match.group(2).strip()
        body = match.group(3)
        lines = [line.strip() for line in body.strip().split('\n') if line.strip()]
        if len(lines) < 2:
            continue  # 至少需要表头和一行数据
        header = [h.strip() for h in next(csv.reader(_io.StringIO(lines[0])))]
        rows = []
        for line in lines[1:]:
            values = [v.strip() for v in next(csv.reader(_io.StringIO(line)))]
            if len(values) != len(header):
                # 畸形行原文不落日志（行内可能含 ssoid 等身份信息），仅记录列数差异。
                logging.warning(f"跳过列数不匹配的行：预期 {len(header)} 列，实际 {len(values)} 列（原文不落日志）")
                skipped += 1
                continue
            row = dict(zip(header, values))
            rows.append(row)
        sections.append((table, timecol, header, rows))
    # 把被丢弃的"列数不匹配"行数随结果一起返回，
    #   客户端不再只看到 200 ok 却不知部分行已丢失。
    # 原用模块级 `_SKIPPED_ROWS = [0]` 累加——
    #   Flask 开发服务器默认多线程，该全局变量会被并发请求共享/交错重置，
    #   导致响应里的 skipped_rows 串号。现改为函数内局部变量随返回值传递，
    #   每个请求各算各的，彻底消除共享可变状态。
    return sections, skipped

def row_hash(row, header):
    """计算行哈希用于幂等去重。
    M3修复：与 mcp_server.ingest_sections 的哈希算法统一（\\x1f 分隔、按 header 列序），
    同一数据无论走 Flask 通道还是 MCP 通道都得到相同哈希，跨通道幂等去重生效。
    入库前与 mcp_server 同步做"值内逗号→空白"规范化——
    客户端（upload_data.py）组包时已做该替换，服务端再兜底一次（幂等），保证无论客户端
    行为如何，两通道对同一逻辑行的存储内容与行哈希都一致。"""
    vals = [_normalize_cell(row.get(h, '')) for h in header]
    return hashlib.sha256("\x1f".join(vals).encode('utf-8')).hexdigest()


def _normalize_cell(v):
    """L3加固：与 mcp_server.ingest_sections 完全一致的单元格规范化。"""
    return str(v).replace(',', ' ').replace('\n', ' ').replace('\r', ' ')

def insert_section(conn, table, timecol, header, rows):
    """将解析后的一个表的数据插入数据库"""
    # S1修复：表名/列名先过白名单，非法直接拒绝（非法值不再触及 SQL 拼接）
    _check_ident(table, "表名")
    for col in header:
        _check_ident(col, "列名")
    # 可疑身份列提醒：清单之外的新疑似列（如新版 App 引入 imei/mac）告警供人工复核，不阻断。
    warn_suspicious_identity_columns(header, source=f"upload:{table}",
                                     sink=lambda m: logging.warning(m))
    # M1修复：补齐 __t 列（毫秒时间戳，取自 timecol），与 mcp_server.py 的 schema 对齐——
    #   mcp health_query 依赖 __t（ORDER BY / since/until 过滤），导致对 upload
    #   通道入库的全部表直接抛 "no such column: __t"。旧表缺列时自动 ALTER 补齐。
    conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" (__row_hash TEXT UNIQUE, __t INTEGER)')
    existing_cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
    for col in list(header) + ['__t']:
        if col not in existing_cols:
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{col}" '
                         f'{"INTEGER" if col == "__t" else "TEXT"}')
    # 插入数据
    ti = header.index(timecol) if timecol in header else -1
    inserted = 0
    for row in rows:
        # BUG-3 服务端兜底：先清洗身份列再算哈希/入库，
        #   与 mcp_server.ingest_sections 同规则同清单（utils.IDENTITY_COLUMNS）。
        #   对已由客户端清洗过的载荷是幂等的；对绕过客户端的直发载荷堵住身份外泄。
        if IDENTITY_COLUMNS:
            row = {k: ("" if k in IDENTITY_COLUMNS else v) for k, v in row.items()}
        rh = row_hash(row, header)
        ts = _to_epoch_ms(row.get(timecol)) if ti >= 0 else None
        cols = list(header) + ['__row_hash', '__t']
        # L3加固：存储值与哈希输入走同一 _normalize_cell，保证"哈希=存储内容之哈希"，
        #   且与 mcp_server 通道的存储内容逐字节一致（跨通道幂等的前提）。
        vals = [_normalize_cell(row.get(c, '')) for c in header] + [rh, ts]
        placeholders = ','.join(['?'] * len(cols))
        colnames = ','.join(f'"{c}"' for c in cols)
        cur = conn.execute(f'INSERT OR IGNORE INTO "{table}" ({colnames}) VALUES ({placeholders})', vals)
        inserted += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    # 更新水位（M2修复：同样走 _to_epoch_ms，日期字符串不再被当毫秒原样入库）
    #   IGNORE（inserted=0），水位仍会按 payload 内的最大时间前进，造成"水位超前于
    #   sink 实际数据"，增量同步可能据此跳过真实缺失的行。现仅在实际插入 >0 行时更新。
    if inserted > 0 and timecol in header:
        timestamps = [t for t in (_to_epoch_ms(row.get(timecol)) for row in rows) if t is not None]
        if timestamps:
            max_ts = max(timestamps)
            conn.execute('INSERT OR REPLACE INTO _watermark (table_name, max_timestamp) VALUES (?, ?)',
                         (table, max_ts))

@app.before_request
def check_auth_and_log():
    """鉴权 + 日志记录"""
    # 对需要鉴权的路径进行验证
    if request.path in ('/api/cursor', '/api/upload'):
        auth_header = request.headers.get('Authorization', '')
        expected = f'Bearer {API_TOKEN}'
        # 常量时间比较，消除理论上的计时旁路
        if not hmac.compare_digest(auth_header, expected):
            return jsonify({"error": "unauthorized"}), 401

    # 记录请求。S4修复：不记录完整 Authorization header（避免 Token 泄露到日志）。
    # 不再把 POST 请求体（含健康数据片段）写入日志文件，
    #   仅记录字节长度，避免健康数据落盘（日志目录虽已 gitignore，仍以少留痕为宜）。
    if request.method == 'POST':
        data_len = len(request.get_data() or b'')
    else:
        data_len = 0
    auth = request.headers.get('Authorization', '')
    # 仅记录鉴权方案名（如 Bearer），不再截取
    #   Token 前 3 字符；原 auth[:10] 会在自定义强 Token 下泄露前缀。
    auth_safe = (auth.split(' ', 1)[0] + ' …') if auth else ''
    logging.info(f"Request: {request.method} {request.path} | Auth: {auth_safe} | BodyBytes: {data_len}")

@app.route('/api/cursor', methods=['GET'])
def get_cursor():
    """返回各表当前水位"""
    init_db()
    # 连接关闭移入 finally（此前异常路径泄漏）。
    conn = get_db()
    try:
        rows = conn.execute('SELECT table_name, max_timestamp FROM _watermark').fetchall()
    finally:
        conn.close()
    return jsonify({r[0]: r[1] for r in rows})

@app.route('/api/upload', methods=['POST'])
def upload():
    """接收手机上传的分节数据"""
    init_db()
    # 原 errors='ignore' 静默丢弃非法字节，数据被截断入库且行哈希随之变化
    #   （破坏幂等语义），客户端却收到 200。现显式拒绝并说明原因。
    try:
        text = request.data.decode('utf-8')
    except UnicodeDecodeError as _ude:
        return jsonify({"error": "invalid utf-8 payload",
                        "detail": str(_ude)}), 400
    if not text.strip():
        return jsonify({"error": "empty body"}), 400
    sections, skipped_rows = parse_section(text)
    if not sections:
        return jsonify({"error": "no valid sections"}), 400
    conn = get_db()
    try:
        for table, timecol, header, rows in sections:
            insert_section(conn, table, timecol, header, rows)
        conn.commit()
    except ValueError as ve:
        # S1修复：非法表名/列名等校验错误返回 400（客户端问题），与真实数据库错误(500)区分
        conn.rollback()
        logging.warning(f"拒绝非法数据段: {ve}")
        return jsonify({"error": str(ve)}), 400
    except Exception:
        conn.rollback()
        # 原 return jsonify({"error": str(e)}), 500
        #   会把内部异常文本（可能含 SQL 片段、表结构、文件路径）回显给客户端。
        #   改为只回通用文案，细节仅进服务端日志（logging.exception 已带完整堆栈）。
        logging.exception("数据库写入失败")
        return jsonify({"error": "internal error"}), 500
    finally:
        conn.close()
    logging.info(f"成功处理 {len(sections)} 个数据段")
    # 把被丢弃的"列数不匹配"行数回传，客户端不再只看到 200 ok。
    return jsonify({"status": "ok", "sections": len(sections),
                    "skipped_rows": skipped_rows,
                    "partial": skipped_rows > 0})

if __name__ == '__main__':
    # 顺序调整：原先 init_db() 在配置校验**之前**执行——即使因
    #   危险组合被拒绝启动，也已经先在 server/sink/ 下建出了数据库文件（无谓副作用）。
    #   改为先校验配置、通过后再初始化存储。
    # 默认只监听本机回环（127.0.0.1），避免局域网任意设备访问敏感健康数据。
    #   若确需局域网设备上传，把 OPPO_HEALTH_HOST 设为非回环地址（0.0.0.0 或本机局域网 IP），
    #   并务必配合强 Token 与网络隔离。
    host = os.environ.get("OPPO_HEALTH_HOST", "127.0.0.1").strip()
    # 外部监听判定：任何非回环地址都算对外（含 0.0.0.0 与局域网 IP）。
    #   危险组合「对外监听 + 未改默认口令」下，默认口令 CHANGE_ME_TOKEN 就写在
    #   本仓库源码里，同网段任何人都能凭它读写健康数据库——等于完全没有鉴权。
    #   仅靠 warning 不足以拦住，改为**直接拒绝启动**（想对外监听必须同时设强口令）。
    if host not in ("127.0.0.1", "::1", "localhost"):
        if API_TOKEN == _DEFAULT_TOKEN:
            logging.error(
                "拒绝启动：OPPO_HEALTH_HOST=%s（对外监听）但 OPPO_HEALTH_TOKEN "
                "仍是源码中的默认值 %r。默认口令已公开在仓库里，等于无鉴权。\n"
                "        请先设置自定义强口令，例如：\n"
                "          Windows:  set OPPO_HEALTH_TOKEN=<足够长的随机串>\n"
                "          Linux/mac: export OPPO_HEALTH_TOKEN=<足够长的随机串>\n"
                "        客户端 upload_data.py 需设置**同一个**值。", host, _DEFAULT_TOKEN)
            sys.exit(1)
        logging.warning("OPPO_HEALTH_HOST=%s：服务将暴露给局域网/外部，请确认已加强鉴权！", host)
        logging.warning("        建议通过 Nginx/Caddy/traefik 等反向代理提供 HTTPS，避免 Token 与健康数据在局域网中明文传输。")

    # 公开占位 Token 无条件提醒（即使仅回环监听）：能访问本机服务端口的进程皆可读写。
    if API_TOKEN == _DEFAULT_TOKEN:
        logging.warning("正在使用公开占位 Token（%s）：默认回环监听下风险可控，"
                        "但任何能访问本机服务端口的进程都可读写数据；共享环境请设置 OPPO_HEALTH_TOKEN。",
                        _DEFAULT_TOKEN)

    # 配置校验通过，此处才创建/迁移 sink 数据库（见上方顺序调整说明）
    init_db()

    # 明确标注这是 Flask 自带开发服务器——单进程、无 TLS、
    #   无并发保护，仅供本机/受控内网临时接收数据，不可直接用于生产部署。
    #   确有需要时请在前面套一层生产级 WSGI 服务器（gunicorn / waitress）+ 反向代理。
    logging.warning("正在使用 Flask 内置开发服务器（非生产级：单进程、无 TLS、无并发保护）。"
                    "仅供本机/受控内网临时接收数据使用；生产部署请改用 waitress/gunicorn。")
    # 监听端口支持环境变量 OPPO_HEALTH_PORT 覆盖
    #   （此前 5000 写死，手册 D5"换端口"需改源码）。客户端 OPPO_HEALTH_URL 同步改端口。
    _port_raw = os.environ.get("OPPO_HEALTH_PORT", "5000")
    try:
        _port = int(_port_raw)
    except ValueError:
        logging.error("OPPO_HEALTH_PORT 必须是整数，收到: %r", _port_raw)
        sys.exit(1)
    app.run(host=host, port=_port, debug=False)