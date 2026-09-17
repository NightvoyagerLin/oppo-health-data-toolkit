# SPDX-License-Identifier: MIT
# -*- coding: utf-8 -*-
"""
OPPO 健康数据网页版分析报告生成器
=================================
产出单文件离线 HTML：左侧导航 + 顶部总览 + 昨天/近3天/近7天/近30天四个时间窗口，
每窗口六模块（健康建议/睡眠/心率/运动与活动/压力与恢复/体重变化），图表可交互。
- 四窗口基于数据截止日 DATA_END 倒推；数据截止日默认本机当天，可用命令行首参
 或环境变量 OPPO_DATA_DATE（YYYY-MM-DD / YYYYMMDD）锚定历史日；DB 缺失时显式报错退出。
- 医学睡眠评分：AASM/NSF 六维 0~100 分（时长30/深睡20/REM15/节律15/连续10/效率10）。
- 双写输出：主报告 <工作目录>\\OPPO健康数据分析报告.html + 数据源目录副本。
口径：睡眠=夜间主睡（DBSleepMainStat）；运动=sport_mode=-2；压力值 0~100=采样均值；
 HRV=sdnn/rmssd>0；体重=克/1000（TEXT 列需 float 转换）；腕温=值/100（℃）。
"""
import sqlite3
import os
import html
import sys

# 强制 UTF-8 输出，避免 Windows 控制台/重定向乱码（V1.1 修复）
from utils import ensure_utf8_stdout, atomic_write
ensure_utf8_stdout()
import re
import tempfile
from datetime import datetime, timedelta, timezone

# L12修复：统一时区基准为 CN(UTC+8)。数据时间戳与"今天"此前用
#   datetime.fromtimestamp()/datetime.now() 的本地时区，与 GUI/split 的 CN 口径混用；
#   非 UTC+8 机器上同一份数据会出现日期错位。现全项目统一用 CN。
CN = timezone(timedelta(hours=8))

BASE = os.path.dirname(os.path.abspath(__file__))

# 盲审 P1-1：--single-day 显式开关——强制使用锚点日单日库（生成单日深度报告）。
#   缺省时永远优先 DB\combined 全量库：四个统计窗口（锚点日前 N 天，不含锚点日
#   当天）的语义天然需要跨多日数据，单日库只含锚点日一天，多日窗口必然全空；
#   旧版"日库优先"使「跑过 split_db_by_day.py → 生成报告」这条文档路径必然
#   静默降级为全空报告（205KB → 45.7KB，退出码仍 0）。
_SINGLE_DAY = any(a.strip() == "--single-day" for a in sys.argv[1:])

def _resolve_data_date():
    """解析「数据截止日」（返回 datetime 对象）。

    优先级：命令行第 1 个位置参数  >  环境变量 OPPO_DATA_DATE  >  本机当天日期。
    支持格式：YYYY-MM-DD 或 YYYYMMDD（也兼容 2026/09/09、2026_09_09）。
    可选开关 --single-day（强制单日库）可放在任意参数位置，不参与日期解析。
    """
    import sys
    raw = ""
    # V1.0 修复：仅当本文件作为脚本直接运行时才读取命令行第 1 个参数。
    #   的任意参数（如 unittest 的测试名）会被误当日期，直接 SystemExit 崩溃。
    #   盲审 P1-1：跳过 -- 开头的开关参数（如 --single-day），取首个位置参数。
    _pos_args = ([a for a in sys.argv[1:] if not a.strip().startswith("--")]
                 if __name__ == "__main__" and len(sys.argv) > 1 else [])
    if _pos_args and _pos_args[0].strip():
        raw = _pos_args[0].strip()
    else:
        raw = os.environ.get("OPPO_DATA_DATE", "").strip()
    if raw:
        raw = raw.replace("/", "-").replace("_", "-")
        if re.fullmatch(r"\d{8}", raw):
            d = datetime.strptime(raw, "%Y%m%d")
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            d = datetime.strptime(raw, "%Y-%m-%d")
        else:
            raise SystemExit(
                f"[配置错误] 数据日期格式无效：{raw!r}\n"
                f"请使用 YYYY-MM-DD 或 YYYYMMDD，例如 2026-09-09 或 20260909。")
    else:
        # L12修复：默认数据日期（"今天"）用 CN(UTC+8)，与全项目时区口径一致
        d = datetime.now(CN)
    return d

def _dated_folder_candidates(d):
    """该日期对应的候选数据文件夹名（仅标准 YYYY-MM-DD）。

    按天拆分由 split_db_by_day.py 产出，例如 2026-09-09。
    若指定日期无对应日库，_resolve_db_path 会回退到稳定的合并库 DB/combined/。
    """
    y, m, day = d.year, d.month, d.day
    return [
        f"{y:04d}-{m:02d}-{day:02d}",   # 标准约定 YYYY-MM-DD
    ]

def _resolve_db_path(d):
    # 盲审 P1-1：全量合并库**优先**——四窗口（锚点日前 N 天、不含锚点日当天）
    #   需要跨多日数据，单日库只含锚点日一天。日库仅在两种情况使用：
    #   ① 显式 --single-day（单日深度报告）；② combined 尚不存在（未跑过导出）。
    combined = os.path.join(BASE, "DB", "combined", "database_decrypted.db")
    if not _SINGLE_DAY and os.path.exists(combined):
        return "combined", combined
    for f in _dated_folder_candidates(d):
        p = os.path.join(BASE, "DB", f, "database_decrypted.db")
        if os.path.exists(p):
            return f, p
    # 日库未命中时仍回退合并库（--single-day 但该日无日库等场景）
    if os.path.exists(combined):
        # 盲审复审 L7：--single-day 意图落空（该日无日库）时必须让用户知道——
        #   静默回退会让"单日报告"变成全量报告，意图未达成却无任何痕迹。
        if __name__ == "__main__" and _SINGLE_DAY:
            print(f"⚠️  --single-day 指定的 {d:%Y-%m-%d} 没有对应日库，"
                  f"已回退使用 DB\\combined 全量库（产出为多日报告）。")
        return "combined", combined
    # 都不存在：默认用 YYYY-MM-DD 作为报错提示路径
    f = f"{d.year:04d}-{d.month:02d}-{d.day:02d}"
    return f, os.path.join(BASE, "DB", f, "database_decrypted.db")

_DATA_END_DT = _resolve_data_date()
_DATED_FOLDER, DB_PATH = _resolve_db_path(_DATA_END_DT)
# 记录本次实际用的是哪个数据源，供报告页头与空窗口提示区分措辞。
#   "combined" = 全量合并库；其它值 = DB/<日期>/ 单日库。
_SRC_IS_COMBINED = (_DATED_FOLDER == "combined")
if __name__ == "__main__" and not _SRC_IS_COMBINED:
    # 盲审 P1-1：单日库模式必须在终端层同样可感知（CI/定时任务只看 stdout 与
    #   退出码，HTML 页头标注不够）。--single-day 为用户显式启用，做醒目提示
    #   而非拒绝；HTML 报告页头同步有标注。
    print(f"\n⚠️  单日库模式：数据源为 DB\\{_DATED_FOLDER}\\（只含该日数据）。")
    print("    四个统计窗口均落在锚点日之前、将为空；如需完整多日报告，")
    print("    去掉 --single-day 参数重新运行（默认使用 DB\\combined 全量库）。")
OUT_MAIN = os.path.join(BASE, "OPPO健康数据分析报告.html")
DATA_END = _DATA_END_DT.strftime("%Y%m%d")
DATA_END_FMT = _DATA_END_DT.strftime("%Y-%m-%d")

# ---------------- 基础工具 ----------------

def esc(x):
    return html.escape(str(x), quote=True)

def d0(m):
    m = int(m) % 1440
    return f"{m//60:02d}:{m%60:02d}"

def hmin(v):
    v = int(v)
    return f"{v//60}h{v%60:02d}m" if v >= 60 else f"{v}分钟"

def ts_date(ms):
    # P1-1修复：原无 try/except；ms 为 None/负数/超大值（如 1e18）时
    #   datetime.fromtimestamp 抛 OSError/ValueError/OverflowError，穿透 main() 致整页报告 0 输出
    #   （与 S2/S3 同类的"整页中止"模式）。现归一为 None，下游 ts_dstr/ts_ymd 兜底。
    try:
        # L12修复：数据时间戳用 CN(UTC+8) 解释，与 GUI/split_db_by_day 一致
        return datetime.fromtimestamp(ms / 1000.0, tz=CN)
    except (OSError, ValueError, TypeError, OverflowError):
        return None

def ts_dstr(ms):
    d = ts_date(ms)
    return d.strftime("%Y-%m-%d") if d is not None else "—"

def ts_ymd(ms):
    """P1-1修复：返回 YYYYMMDD 字符串；非法时间戳返回空串（不抛异常）。"""
    d = ts_date(ms)
    return d.strftime("%Y%m%d") if d is not None else ""

def avg(lst):
    # 对元素统一 _num 归一——DB 数值列含非数字占位串（如 "--"）时
    #   sum() 会抛 TypeError 中断整份报告；对合法数值结果不变（_num(70)==70.0）。
    #   盲审复审 L3：占位串经 _num 会变 0.0 计入均值，与 avg_scaled"剔除缺失"
    #   的口径相反，幻影 0 会系统性拉低均值——改为"无法转数值即剔除"
    #   （_num(x, None) 仅在 x 可转数值时返回非 None；真实 0 仍正常计入），
    #   与 avg_scaled 同源。
    lst = [_num(x) for x in lst if x is not None and _num(x, None) is not None]
    return sum(lst)/len(lst) if lst else None

def fmt(x, nd=1, dash="—"):
    return dash if x is None else f"{x:.{nd}f}"


def avg_scaled(lst, k=1.0):
    """缩放求均：先剔除 None / 非数值，再统一除以 k。

    修复前 avg 未做数值过滤——None 转成 0.0 后 `if x is not None` 过滤失效，
    缺失值被当作真实 0 计入，系统性拉低均值（如某天没记录久坐=0 分钟，而非
    "未采集"）。健康指标中 NULL 的语义是「缺失」而非「零」，故本函数先剔除
    再缩放。
    """
    vs = []
    for x in lst:
        if x is None:
            continue
        try:
            vs.append(float(x) / k)
        except (TypeError, ValueError):
            continue          # 非数字占位串（如 "--"）同样按缺失处理
    return sum(vs) / len(vs) if vs else None


def dur_min_avg(vals):
    """时长列求「分钟」均值：按数量级自适应判定原始单位（毫秒 / 分钟）。

    DBSportDataStat.sedentary_total_duration 的单位未登记在
    手册 3.3 单位表中，同表 total_duration 已确认为毫秒，但不同 App 版本对该列
    存在「毫秒 / 分钟」两种口径。原代码直接按分钟渲染，若库里存的是毫秒，
    「日均久坐」会虚高约 6 万倍，且 >=300 分钟的阈值判断完全失真。

    判定依据：单日任何时长都不可能超过 1440 分钟（24 小时）。当观测最大值超过
    该上限时判定原始单位为毫秒并统一 /60000，否则按分钟原样使用。
    两种口径下结果都落在同一「分钟」单位上，报告与阈值判断均成立。
    """
    vs = []
    for x in vals:
        if x is None:
            continue
        try:
            vs.append(float(x))
        except (TypeError, ValueError):
            continue
    if not vs:
        return None
    if max(vs) > 1440:
        vs = [v / 60000.0 for v in vs]
    return sum(vs) / len(vs)

def _num(x, d=0.0):
    """安全数值转换：DB 列可能含非数字占位串（如 "--"），非数字/None 统一回退默认值。"""
    try:
        return float(x)
    except (TypeError, ValueError):
        return d

def _mmdd(s):
    """把 YYYYMMDD 日期串格式化为 MM/DD；
    非 8 位纯数字（如带连字符的 TEXT 日期）原样返回——直接切片会拼出乱码标签。"""
    s = str(s)
    return f"{s[4:6]}/{s[6:]}" if len(s) == 8 and s.isdigit() else s

def date_range_str(dates):
    ds = sorted(dates)
    f = lambda d: f"{d[:4]}-{d[4:6]}-{d[6:]}"
    return f"{f(ds[0])} ~ {f(ds[-1])}" if len(ds) > 1 else f(ds[0])

def late_min(mh):
    """B10 修复：把跨午夜取模后的入睡分钟数归一化为"相对前一天 18:00 的连续时间轴"。
    mh∈[0,12:00) 表示凌晨（前夜入睡后的次日凌晨）→ +1440 映射到 24~29h；
    mh∈[12:00,24:00) 保持原值。使 23:30=1410 < 00:30=1470 < 01:30=1530 可正确比较大小。"""
    mh = int(mh) % 1440
    return mh + 1440 if mh < 12*60 else mh

# ---------------- SVG 图表 v2（带交互数据点） ----------------

C_GRID, C_TXT, C_MUT = "#2D3B55", "#CBD5E1", "#94A3B8"
_DUID = [0]

def _nice_bounds(vals, lo_pad=0.1):
    vals = [v for v in vals if v is not None]
    if not vals:
        return 0, 1
    lo, hi = min(vals), max(vals)
    if hi == lo:
        hi = lo + 1
    pad = (hi - lo) * lo_pad
    return lo - pad, hi + pad

def line_chart(points, color="#38BDF8", refs=(), h=260):
    W, L, R, T, B = 680, 48, 680-14, 18, h-28
    if not points:
        return f'<svg viewBox="0 0 {W} {h}"><text x="{W/2}" y="{h/2}" fill="{C_MUT}" font-size="13" text-anchor="middle">暂无数据</text></svg>'
    points = [(lb, _pt(v)) for lb, v in points]
    vals = [v for _, v in points if v is not None]
    lo, hi = _nice_bounds(vals + [r[0] for r in refs])
    span = (hi - lo) or 1
    n = len(points)
    def X(i): return L + (R-L) * (i/(n-1) if n > 1 else 0.5)
    def Y(v): return T + (B-T) * (1 - (v-lo)/span)
    g = [f'<svg viewBox="0 0 {W} {h}" xmlns="http://www.w3.org/2000/svg" font-family="system-ui,sans-serif">']
    for k in range(5):
        yv = lo + span*k/4
        y = Y(yv)
        g.append(f'<line x1="{L}" y1="{y:.1f}" x2="{R}" y2="{y:.1f}" stroke="{C_GRID}"/>')
        g.append(f'<text x="{L-7}" y="{y+4:.1f}" fill="{C_MUT}" font-size="11" text-anchor="end">{yv:.0f}</text>')
    for v, txt, c in refs:
        y = Y(v)
        g.append(f'<line x1="{L}" y1="{y:.1f}" x2="{R}" y2="{y:.1f}" stroke="{c}" stroke-width="1.2" stroke-dasharray="5 4"/>')
        g.append(f'<text x="{R-2}" y="{y-5:.1f}" fill="{c}" font-size="10.5" text-anchor="end">{esc(txt)}</text>')
    step = max(1, n // 8)
    for i, (lb, v) in enumerate(points):
        if v is None:
            continue
        if n <= 10 or i % step == 0 or i == n-1:
            g.append(f'<text x="{X(i):.1f}" y="{B+15}" fill="{C_MUT}" font-size="10.5" text-anchor="middle" transform="rotate({-32 if n>10 else 0} {X(i):.1f} {B+15})">{esc(lb)}</text>')
    coords = [(X(i), Y(v)) for i, (lb, v) in enumerate(points) if v is not None]
    if len(coords) >= 2:
        d = " ".join(("M" if k == 0 else "L") + f"{x:.1f},{y:.1f}" for k, (x, y) in enumerate(coords))
        g.append(f'<path d="{d} L{coords[-1][0]:.1f},{B} L{coords[0][0]:.1f},{B} Z" fill="{color}" opacity="0.10"/>')
        g.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2.2"/>')
    for (x, y), (lb, v) in zip(coords, [p for p in points if p[1] is not None]):
        g.append(f'<circle class="jpt" cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}" data-l="{esc(lb)}" data-v="{v:.1f}" data-x="{x:.1f}" data-y="{y:.1f}" data-r0="3"/>')
    g.append("</svg>")
    return "".join(g)

def bar_chart(items, color="#38BDF8", refs=(), h=260, label_nd=0):
    W, L, R, T, B = 680, 48, 680-14, 18, h-28
    if not items:
        return f'<svg viewBox="0 0 {W} {h}"><text x="{W/2}" y="{h/2}" fill="{C_MUT}" font-size="13" text-anchor="middle">暂无数据</text></svg>'
    items = [(lb, _pt(v)) for lb, v in items]
    vals = [v for _, v in items if v is not None]
    vmax = max(vals + [r[0] for r in refs] + [1]) * 1.14
    n = len(items)
    slot = (R-L)/n
    bw = min(46, slot*0.62)
    def Y(v): return T + (B-T)*(1 - v/vmax)
    g = [f'<svg viewBox="0 0 {W} {h}" xmlns="http://www.w3.org/2000/svg" font-family="system-ui,sans-serif">']
    for k in range(5):
        yv = vmax*k/4
        y = Y(yv)
        g.append(f'<line x1="{L}" y1="{y:.1f}" x2="{R}" y2="{y:.1f}" stroke="{C_GRID}"/>')
        g.append(f'<text x="{L-7}" y="{y+4:.1f}" fill="{C_MUT}" font-size="11" text-anchor="end">{yv:.0f}</text>')
    for v, txt, c in refs:
        y = Y(v)
        g.append(f'<line x1="{L}" y1="{y:.1f}" x2="{R}" y2="{y:.1f}" stroke="{c}" stroke-width="1.2" stroke-dasharray="5 4"/>')
        g.append(f'<text x="{R-2}" y="{y-5:.1f}" fill="{c}" font-size="10.5" text-anchor="end">{esc(txt)}</text>')
    step = max(1, n // 10)
    for i, (lb, v) in enumerate(items):
        cx = L + slot*(i+0.5)
        y = Y(v or 0)
        g.append(f'<rect x="{cx-bw/2:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{max(1, B-y):.1f}" fill="{color}" rx="3"/>')
        if n <= 12 or i % step == 0 or i == n-1:
            g.append(f'<text x="{cx:.1f}" y="{y-5:.1f}" fill="{C_TXT}" font-size="10.5" text-anchor="middle">{(v or 0):.{label_nd}f}</text>')
            g.append(f'<text x="{cx:.1f}" y="{B+15}" fill="{C_MUT}" font-size="10.5" text-anchor="middle" transform="rotate({-32 if n>10 else 0} {cx:.1f} {B+15})">{esc(lb)}</text>')
        g.append(f'<circle class="jpt" cx="{cx:.1f}" cy="{max(T+4, y-2):.1f}" r="3.5" fill="transparent" data-l="{esc(lb)}" data-v="{(v or 0):.{label_nd}f}" data-x="{cx:.1f}" data-y="{(y+B)/2:.1f}" data-r0="3.5"/>')
    g.append("</svg>")
    return "".join(g)

def donut(segs, center_top, center_bot, legend=True):
    """segs: [(name, pct, color, extra_text)]；hover/点击扇区 → 加粗 + 中心文字切换（JS 联动）"""
    _DUID[0] += 1
    uid = _DUID[0]
    W, H, cx, cy, r, sw = 220, 200, 110, 90, 56, 24
    import math
    ang = -90.0
    g = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" font-family="system-ui,sans-serif">']
    for name, pct, col, extra in segs:
        if pct <= 0:
            continue
        a2 = ang + 360*pct/100
        large = 1 if (a2-ang) > 180 else 0
        x1, y1 = cx + r*math.cos(math.radians(ang)), cy + r*math.sin(math.radians(ang))
        x2, y2 = cx + r*math.cos(math.radians(a2)), cy + r*math.sin(math.radians(a2))
        g.append(f'<path class="jseg" data-c1="dc1_{uid}" data-c2="dc2_{uid}" data-name="{esc(name)}" data-pct="{pct:.1f}" data-extra="{esc(extra)}" data-def1="{esc(center_top)}" data-def2="{esc(center_bot)}" '
                 f'd="M{x1:.2f},{y1:.2f} A{r},{r} 0 {large} 1 {x2:.2f},{y2:.2f}" fill="none" stroke="{col}" stroke-width="{sw}" style="cursor:pointer;transition:stroke-width .15s"/>')
        ang = a2
    g.append(f'<text id="dc1_{uid}" x="{cx}" y="{cy-2}" fill="#F1F5F9" font-size="16" text-anchor="middle" font-weight="600" style="pointer-events:none">{esc(center_top)}</text>')
    g.append(f'<text id="dc2_{uid}" x="{cx}" y="{cy+16}" fill="{C_MUT}" font-size="9.5" text-anchor="middle" style="pointer-events:none">{esc(center_bot)}</text>')
    if legend:
        lx, ly = 8, 166
        g.append('<g font-size="10">')
        for name, pct, col, _ in segs:
            if pct <= 0:
                continue
            if lx > 110:  # 每行两项，避免第三行越出 viewBox 被裁
                lx = 8
                ly += 14
            g.append(f'<rect x="{lx}" y="{ly}" width="8" height="8" fill="{col}" rx="2"/><text x="{lx+11}" y="{ly+8}" fill="#CBD5E1">{esc(name)} {pct:.0f}%</text>')
            lx += 22 + (len(name) + 5) * 10
        g.append("</g>")
    g.append("</svg>")
    return "".join(g)

# ---------------- 数据访问 ----------------

def in_clause(dates):
    """生成 IN (...) 的占位符串（V1.0 加固：日期值改为参数绑定，不再拼进 SQL 文本——
    此前 f-string 直插 DB 内容，存在自我注入面；参数化后行为不变）。"""
    return ",".join("?" for _ in dates)

def in_params(dates):
    """与 in_clause 配套的参数列表（保留原值类型，避免 TEXT/INTEGER 亲和性差异）。"""
    return list(dates)

def _has_table(cur, name):
    """判断表是否存在。

    收尾加固：不同设备型号 / App 版本的表集合并不完全一致
    （如仅手表才有的 DBWristTemperatureStat、按机型可选的 DBECGRecord、采样明细表
    DBHeartRate / DBStressTable 等）。此前所有 q_* 均直接查询，缺表时抛
    OperationalError 并因 main() 无保护而中断整份报告（0 输出）。
    现统一在查询前判存在，缺表按"无数据"处理。
    """
    try:
        return cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,)).fetchone() is not None
    except Exception:
        return False

def _pt(v):
    """图表取值净化（非数字/None 一律视为“缺口”（返回 None），
    既避免把脏串当 0 画出误导性下探，也避免 min()/max() 混类型抛 TypeError。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

_COL_CACHE = {}

def _cols_ok(cur, table, *cols):
    """列存在性守卫：查询前校验所需列，缺列按"无数据"降级（与缺表同等处理），
    避免 App 版本列名漂移时抛 no such column 中断整份报告。"""
    key = (id(cur.connection), table, cols)  # 加连接标识，避免多库场景守卫静默失效
    if key in _COL_CACHE:
        return _COL_CACHE[key]
    try:
        have = {r[1] for r in cur.execute(f'PRAGMA table_info("{table}")')}
    except Exception:
        have = set()
    ok = all(c in have for c in cols)
    _COL_CACHE[key] = ok
    return ok

def q_sport(cur, dates):
    # 收尾加固：表缺失时按"无数据"处理，不中断整份报告
    if not _has_table(cur, "DBSportDataStat"): return []
    if not _cols_ok(cur, "DBSportDataStat", "date","total_steps","total_calories","total_duration","total_workout_minutes","sedentary_total_duration","sedentary_counts","current_day_steps_goal","steps_goal_complete","total_static_cal"): return []
    return [(str(r[0]),)+r[1:] for r in cur.execute(f"SELECT date,total_steps,total_calories,total_duration,total_workout_minutes,sedentary_total_duration,sedentary_counts,current_day_steps_goal,steps_goal_complete,total_static_cal FROM DBSportDataStat WHERE sport_mode=-2 AND date IN ({in_clause(dates)}) ORDER BY date", in_params(dates)).fetchall()]

def q_sleep(cur, dates):
    # 收尾加固：表缺失时按"无数据"处理，不中断整份报告
    if not _has_table(cur, "DBSleepMainStat"): return []
    if not _cols_ok(cur, "DBSleepMainStat", "date","sleep_in_minute","sleep_out_minute","total_sleep_time","total_deep_sleep_time","total_lightly_sleep_time","total_rem_time","total_wake_up_time","wake_count"): return []
    return [(str(r[0]),)+r[1:] for r in cur.execute(f"SELECT date,sleep_in_minute,sleep_out_minute,total_sleep_time,total_deep_sleep_time,total_lightly_sleep_time,total_rem_time,total_wake_up_time,wake_count FROM DBSleepMainStat WHERE date IN ({in_clause(dates)}) ORDER BY date", in_params(dates)).fetchall()]

def q_osa(cur, dates):
    # 收尾加固：缺表时按"无数据"处理（返回空列表对），不中断整份报告
    ahi = ([(str(r[0]),)+r[1:] for r in cur.execute(
        f"SELECT date,ahi FROM DBOsaResult WHERE date IN ({in_clause(dates)}) ORDER BY date", in_params(dates)).fetchall()]
        if (_has_table(cur, "DBOsaResult") and _cols_ok(cur, "DBOsaResult", "date", "ahi")) else [])
    snore = ([(str(r[0]),)+r[1:] for r in cur.execute(
        f"SELECT date,snore_sum_time,snore_mean_db,snore_max_db FROM DBSnoreResult WHERE date IN ({in_clause(dates)})", in_params(dates)).fetchall()]
        if (_has_table(cur, "DBSnoreResult") and _cols_ok(cur, "DBSnoreResult", "date", "snore_sum_time", "snore_mean_db", "snore_max_db")) else [])
    return ahi, snore

def q_hr_stat(cur, dates):
    # 收尾加固：表缺失时按"无数据"处理，不中断整份报告
    if not _has_table(cur, "DBHeartRateDataStatTable"): return []
    if not _cols_ok(cur, "DBHeartRateDataStatTable", "date","average_hr","rest_hr","max_hr","min_hr","sleep_base_hr","walk_avg_hr"): return []
    return [(str(r[0]),)+r[1:] for r in cur.execute(f"SELECT date,average_hr,rest_hr,max_hr,min_hr,sleep_base_hr,walk_avg_hr FROM DBHeartRateDataStatTable WHERE date IN ({in_clause(dates)}) ORDER BY date", in_params(dates)).fetchall()]

def q_spo2_stat(cur, dates):
    # 收尾加固：表缺失时按"无数据"处理，不中断整份报告
    if not _has_table(cur, "DBBloodOxygenSaturationDataStat"): return []
    if not _cols_ok(cur, "DBBloodOxygenSaturationDataStat", "date","average_blood_oxygen_saturation","min_blood_oxygen_saturation","low_blood_oxygen_saturation_total_time"): return []
    return [(str(r[0]),)+r[1:] for r in cur.execute(f"SELECT date,average_blood_oxygen_saturation,min_blood_oxygen_saturation,low_blood_oxygen_saturation_total_time FROM DBBloodOxygenSaturationDataStat WHERE date IN ({in_clause(dates)}) ORDER BY date", in_params(dates)).fetchall()]

def q_stress_stat(cur, dates):
    # 收尾加固：表缺失时按"无数据"处理，不中断整份报告
    if not _has_table(cur, "DBStressDataStatTable"): return []
    if not _cols_ok(cur, "DBStressDataStatTable", "date","relax_stress_total_time","normal_stress_total_time","middle_stress_total_time","high_stress_total_time"): return []
    return [(str(r[0]),)+r[1:] for r in cur.execute(f"SELECT date,relax_stress_total_time,normal_stress_total_time,middle_stress_total_time,high_stress_total_time FROM DBStressDataStatTable WHERE date IN ({in_clause(dates)}) ORDER BY date", in_params(dates)).fetchall()]

def q_stress_detail(cur, floor_ms=None):
    # 收尾加固：表缺失时按"无数据"处理，不中断整份报告
    # 明细表此前全量载入内存，大库（数十万行分钟级
    #   采样）下报告生成内存/耗时随全库历史线性增长。明细仅用于窗口内聚合
    #   （窗口最大 30 天），故支持传入下界 floor_ms（锚点日-35 天 0 点）裁剪；
    #   floor_ms=None 时保持原全量行为（向后兼容）。
    if not _has_table(cur, "DBStressTable"): return []
    if not _cols_ok(cur, "DBStressTable", "data_created_timestamp","stress_value","sdnn","rmssd"): return []
    if floor_ms is None:
        return cur.execute("SELECT data_created_timestamp,stress_value,sdnn,rmssd FROM DBStressTable").fetchall()
    return cur.execute(
        "SELECT data_created_timestamp,stress_value,sdnn,rmssd FROM DBStressTable "
        "WHERE CAST(data_created_timestamp AS INTEGER) >= ?", (floor_ms,)).fetchall()

def q_hr_detail(cur, floor_ms=None):
    # 收尾加固：表缺失时按"无数据"处理，不中断整份报告
    # 同 q_stress_detail——加窗口下界裁剪，防大库全量载入。
    if not _has_table(cur, "DBHeartRate"): return []
    if not _cols_ok(cur, "DBHeartRate", "data_created_timestamp","heart_rate_value"): return []
    if floor_ms is None:
        return cur.execute("SELECT data_created_timestamp,heart_rate_value FROM DBHeartRate WHERE heart_rate_value>0").fetchall()
    return cur.execute(
        "SELECT data_created_timestamp,heart_rate_value FROM DBHeartRate "
        "WHERE heart_rate_value>0 AND CAST(data_created_timestamp AS INTEGER) >= ?", (floor_ms,)).fetchall()

def q_sedentary(cur, floor_ms=None):
    # 收尾加固：表缺失时按"无数据"处理，不中断整份报告
    # 同 q_stress_detail——加窗口下界裁剪，防大库全量载入。
    if not _has_table(cur, "DBSedentary"): return []
    if not _cols_ok(cur, "DBSedentary", "start_timestamp","value"): return []
    if floor_ms is None:
        return cur.execute("SELECT start_timestamp,value FROM DBSedentary").fetchall()
    return cur.execute(
        "SELECT start_timestamp,value FROM DBSedentary "
        "WHERE CAST(start_timestamp AS INTEGER) >= ?", (floor_ms,)).fetchall()

def q_weight(cur):
    # 收尾加固：表缺失时按"无数据"处理，不中断整份报告
    if not _has_table(cur, "DBWeightBodyFatTable"): return []
    if not _cols_ok(cur, "DBWeightBodyFatTable", "measurement_timestamp","weight","bmi"): return []
    return cur.execute("SELECT measurement_timestamp,weight,bmi FROM DBWeightBodyFatTable ORDER BY measurement_timestamp").fetchall()

def q_one_time_sport(cur):
    # 收尾加固：表缺失时按"无数据"处理，不中断整份报告
    if not _has_table(cur, "DBOneTimeSportStat"): return []
    if not _cols_ok(cur, "DBOneTimeSportStat", "date","sport_mode","total_steps","total_distance","total_calories","total_duration","start_time"): return []
    return [(str(r[0]),)+r[1:] for r in cur.execute("SELECT date,sport_mode,total_steps,total_distance,total_calories,total_duration,start_time FROM DBOneTimeSportStat ORDER BY start_time").fetchall()]

def q_ecg(cur):
    # 收尾加固：表缺失时按"无数据"处理，不中断整份报告
    if not _has_table(cur, "DBECGRecord"): return []
    if not _cols_ok(cur, "DBECGRecord", "start_time_stamp","ecg_result_name"): return []
    return cur.execute("SELECT start_time_stamp,ecg_result_name FROM DBECGRecord WHERE ecg_result_name IS NOT NULL ORDER BY start_time_stamp").fetchall()

def q_breath(cur):
    # 收尾加固：表缺失时按"无数据"处理，不中断整份报告
    if not _has_table(cur, "DBBreathRate"): return []
    if not _cols_ok(cur, "DBBreathRate", "data_created_timestamp","value"): return []
    return cur.execute("SELECT data_created_timestamp,value FROM DBBreathRate WHERE value>0").fetchall()

def q_wrist_temp(cur, dates):
    # 收尾加固：表缺失时按"无数据"处理，不中断整份报告
    if not _has_table(cur, "DBWristTemperatureStat"): return []
    if not _cols_ok(cur, "DBWristTemperatureStat", "date","day_baseline_value","value","min_value","max_value"): return []
    return [(str(r[0]),)+r[1:] for r in cur.execute(f"SELECT date,day_baseline_value,value,min_value,max_value FROM DBWristTemperatureStat WHERE date IN ({in_clause(dates)}) ORDER BY date", in_params(dates)).fetchall()]

def q_sleep_index(cur):
    # 收尾加固：表缺失时按"无数据"处理，不中断整份报告
    if not _has_table(cur, "DBSleepIndex"): return []
    if not _cols_ok(cur, "DBSleepIndex", "data_created_timestamp","avg_sleep_spo2","avg_sleep_heart_rate","basal_breathe","sleep_recovery_rate"): return []
    return cur.execute("SELECT data_created_timestamp,avg_sleep_spo2,avg_sleep_heart_rate,basal_breathe,sleep_recovery_rate FROM DBSleepIndex").fetchall()

# ---------------- 医学睡眠评分（AASM/NSF 六维，禁用设备评分） ----------------

# S4修复：六维满分权重由此前的散落字面量提取为具名常量，便于复核与调参。
#   注意：改动任一权重后，需同步核对报告内展示的"时长/深睡/REM/节律/连续性/效率"权重说明文案，
#   避免"算法变了、展示文案没变"的口径不一致。
SLEEP_SCORE_WEIGHTS = {
    "duration": 30,     # 睡眠时长
    "deep": 20,         # 深睡占比
    "rem": 15,          # REM 占比
    "rhythm": 15,       # 入睡节律
    "continuity": 10,   # 睡眠连续性
    "efficiency": 10,   # 睡眠效率
}


def medical_sleep_score(dur_min, deep_pct, rem_pct, in_minute, wake_cnt, eff_pct):
    """医学睡眠评分（AASM/NSF 六维 0~100，覆盖低分到高分）。
    阈值收紧后：很差 < 35 / 欠佳 35~55 / 尚可 55~70 / 良好 70~85 / 优秀 ≥85。"""
    items = []
    # 1. 睡眠时长（满分 30）——更严格，理想区间收窄
    h = (dur_min or 0) / 60
    if 7.5 <= h <= 8.5:
        s1, t1 = 30, f"{h:.1f}h，处 7.5~8.5h 理想区间"
    elif 7 <= h < 7.5 or 8.5 < h <= 9:
        s1, t1 = 24, f"{h:.1f}h，略偏 7.5~8.5h 理想区间"
    elif 9 < h <= 10:
        # C-P2-14：9~10h 补一档 15 分——原来直接落入"过长"2 分，与短侧 6~7h 档的 15 分不对称
        s1, t1 = 15, f"{h:.1f}h，超过 9h 理想上限，偏长"
    elif 6 <= h < 7:
        s1, t1 = 15, f"{h:.1f}h，低于 7h 下限"
    elif 5 <= h < 6:
        s1, t1 = 8, f"{h:.1f}h，明显偏短"
    elif 4 <= h < 5:
        s1, t1 = 4, f"{h:.1f}h，严重不足"
    else:
        s1, t1 = (1, f"{h:.1f}h，极端偏短") if h < 4 else (2, f"{h:.1f}h，过长")
    items.append(("睡眠时长", s1, SLEEP_SCORE_WEIGHTS["duration"], t1))
    # 2. 深睡占比（满分 20）——理想区间收窄
    dp = deep_pct or 0
    if 15 <= dp <= 20:
        s2, t2 = 20, f"{dp:.1f}%，处 15~20% 理想区间"
    elif 13 <= dp < 15 or 20 < dp <= 23:
        s2, t2 = 15, f"{dp:.1f}%，略偏理想区间"
    elif 10 <= dp < 13 or 23 < dp <= 27:
        s2, t2 = 8, f"{dp:.1f}%，明显偏离"
    elif 7 <= dp < 10 or 27 < dp <= 30:
        s2, t2 = 4, f"{dp:.1f}%，严重偏离"
    else:
        s2, t2 = 1, f"{dp:.1f}%，极端异常"
    items.append(("深睡占比", s2, SLEEP_SCORE_WEIGHTS["deep"], t2))
    # 3. REM 占比（满分 15）
    rp = rem_pct or 0
    if 20 <= rp <= 25:
        s3, t3 = 15, f"{rp:.1f}%，处参考区间（20~25%）"
    elif 17 <= rp < 20 or 25 < rp <= 28:
        s3, t3 = 11, f"{rp:.1f}%，略偏"
    elif 14 <= rp < 17 or 28 < rp <= 32:
        s3, t3 = 6, f"{rp:.1f}%，明显偏离"
    else:
        s3, t3 = 2, f"{rp:.1f}%，异常"
    items.append(("REM 占比", s3, SLEEP_SCORE_WEIGHTS["rem"], t3))
    # 4. 入睡节律（满分 15）——0 点前加分，0 点后减分明显
    if in_minute is None:
        # C-P2-16：缺失入睡时间按"该维度无数据"处理——维度分传 None 而非冒充
        #   0 分/满分，聚合处 total 会跳过 None 维度，缺失不再被当作真实成绩计入。
        items.append(("入睡节律", None, SLEEP_SCORE_WEIGHTS["rhythm"], "无入睡时间记录"))
    else:
        mh = late_min(in_minute)
        # 白天入睡属数据异常（非夜间主睡），单独判 0 分。两条窗口（C-P2-15）：
        #   ① 当日 12:00–18:00 入睡 → late_min 归一化后落在 [720,1080)；
        #   ② 上午 06:00–12:00 补觉 → 原值 [360,720) 被 late_min +1440 → [1800,2160)。
        #   注意 late_min 只对 <12:00 的值 +1440、对 ≥12:00 的值恒等，故窗口②必须
        #   以 [1800,2160) 表达——照搬钟点写 [360,720) 将永远匹配不上（无效修复）。
        #   口径与 data_validation.py "白天入睡(06:00–18:00)视为异常"一致；
        #   18:00–23:00 的较早就寝维持原分级不动。
        if (12 * 60 <= mh < 18 * 60) or (30 * 60 <= mh < 36 * 60):
            s4, t4 = 0, f"{d0(in_minute)} 入睡（白天时段），疑数据异常"
        elif mh <= 23 * 60:
            s4, t4 = 15, f"{d0(in_minute)} 入睡，节律健康"
        elif mh <= 23 * 60 + 30:
            s4, t4 = 12, f"{d0(in_minute)} 入睡，节律较好"
        elif mh <= 24 * 60:
            s4, t4 = 7, f"{d0(in_minute)} 入睡，略晚"
        elif mh <= 24 * 60 + 30:
            s4, t4 = 4, f"{d0(in_minute)} 入睡，明显偏晚"
        elif mh <= 25 * 60:
            s4, t4 = 2, f"{d0(in_minute)} 入睡，偏晚较多"
        else:
            s4, t4 = 1, f"{d0(in_minute)} 入睡，严重晚睡"
        items.append(("入睡节律", s4, SLEEP_SCORE_WEIGHTS["rhythm"], t4))
    # 5. 睡眠连续性（满分 10）——按次数严格分级
    wc = wake_cnt or 0
    if wc == 0:
        s5 = 10
    elif wc <= 1:
        s5 = 8
    elif wc <= 2:
        s5 = 5
    elif wc <= 3:
        s5 = 3
    elif wc <= 5:
        s5 = 1
    else:
        s5 = 0
    items.append(("睡眠连续性", s5, SLEEP_SCORE_WEIGHTS["continuity"], f"夜醒 {wc:.1f} 次/晚"))
    # 6. 睡眠效率（满分 10）
    ep = eff_pct if eff_pct is not None else 0
    if ep >= 90:
        s6 = 10
    elif ep >= 85:
        s6 = 8
    elif ep >= 80:
        s6 = 5
    elif ep >= 70:
        s6 = 3
    else:
        s6 = 1
    items.append(("睡眠效率", s6, SLEEP_SCORE_WEIGHTS["efficiency"], f"{ep:.0f}%"))
    # C-P2-16：维度分可能为 None（如"入睡节律"缺数据），聚合时跳过而非报错/当 0。
    # N-2：跳维后满分 <100，若仍按 85/70/55/35 固定阈值评级，缺记录的日子会被
    #   系统性压分（实测同参数 98"优秀" vs 83"良好"）——改为按可用满分归一化回
    #   百分制：全勤时可用满分=100 退化为原值；缺节律(满分15)时 83→98，与有记录日同尺。
    _avail = sum(it[2] for it in items if it[1] is not None)
    total = round(100 * sum(it[1] for it in items if it[1] is not None) / _avail) if _avail else 0
    if total >= 85:
        grade, color = "优秀", "#047857"
    elif total >= 70:
        grade, color = "良好", "#0E7490"
    elif total >= 55:
        grade, color = "尚可", "#B45309"
    elif total >= 35:
        grade, color = "欠佳", "#B91C1C"
    else:
        grade, color = "很差", "#7F1D1D"
    return total, grade, color, items

# ---------------- 窗口数据聚合 ----------------

def build_stats(cur, dates, hr_detail, stress_detail, sed_detail):
    S = {"dates": dates, "range": date_range_str(dates), "n_days": len(dates)}
    dset = set(dates)
    sl = q_sleep(cur, dates)
    # 睡眠数值列统一 _num 安全转换——原 `r[n] or 0` 对非数字串
    #   （如 "--"）无效，后续 `d["total"] > 0` / `sum()` 会抛 TypeError 中断整份报告；
    #   in/out 保留原值以区分 None（缺失）与 0。
    S["sleep_days"] = [{"date": r[0], "in": r[1], "out": r[2], "total": _num(r[3]), "deep": _num(r[4]),
                        "light": _num(r[5]), "rem": _num(r[6]), "wake": _num(r[7]), "wake_cnt": _num(r[8])} for r in sl]
    slv = [d for d in S["sleep_days"] if d["total"] > 0]
    S["sleep_valid"] = slv
    S["sleep_avg_total"] = avg([d["total"] for d in slv])
    # C-P2-1：先 late_min 归一化再求均——直接对取模分钟求均会把跨午夜入睡
    #   （如 23:50=1430 与 00:10=10）平均成中午 720；归一化后 1430/1450 → 1440=00:00。
    #   late_min 对已归一化值幂等（<720 的值 +1440 后 %1440 复原），下游二次调用不受影响。
    S["sleep_avg_in"] = avg([late_min(_num(d["in"])) for d in slv if d["in"] is not None])
    # N-1：out（sleep_out_minute）是连续轴值（已含次日偏移，全库实测 1618~2257），
    #   不可套 late_min——其对 ≥2160（次日 12:00 后起床）会折叠 -1440（如 2257→817），
    #   实测曾致 6~8 月 11 天起床均值失真 4 小时。直接平均后由 d0 显示层 %1440 转钟点。
    S["sleep_avg_out"] = avg([_num(d["out"]) for d in slv if d["out"] is not None])
    S["sleep_avg_wakecnt"] = avg([d["wake_cnt"] for d in slv])
    # 用日均分钟数计算占比，确保表格中"分钟"与"占比"严格一致（解决 0分钟 0.2% 矛盾）
    # 同时输出窗口总占比（深睡均值）用于环形图（兼容旧版）
    S["deep_avg_min"] = avg([d["deep"] for d in slv]) or 0
    S["light_avg_min"] = avg([d["light"] for d in slv]) or 0
    S["rem_avg_min"] = avg([d["rem"] for d in slv]) or 0
    S["wake_avg_min"] = avg([d["wake"] for d in slv]) or 0
    tot = sum(d["total"] for d in slv) or None
    n_sl = len(slv)
    avg_total_min = (tot / n_sl) if tot else 0
    S["deep_pct"] = round(100 * S["deep_avg_min"] / avg_total_min, 1) if avg_total_min else None
    S["light_pct"] = round(100 * S["light_avg_min"] / avg_total_min, 1) if avg_total_min else None
    S["rem_pct"] = round(100 * S["rem_avg_min"] / avg_total_min, 1) if avg_total_min else None
    S["wake_pct"] = round(100 * S["wake_avg_min"] / avg_total_min, 1) if avg_total_min else None
    # 阶段显示归零条件：占比 <0.5% **且** 日均分钟 <0.5（盲审 P3-1 修正——
    #   原注释写"或"但只实现了占比侧，导致"2 分钟 / 0.0%"的自相矛盾：
    #   清醒 2.04 分钟≥0.5 不该归零，真实占比 0.435% 应正常显示；
    #   而"0 分钟 0.2%"（分钟 0.2<0.5 且占比<0.5）才是本抑制的原始目标。
    #   两个显示值取与归零，保证分钟与占比始终同源）
    if S["wake_pct"] is not None and S["wake_pct"] < 0.5 and (S["wake_avg_min"] or 0) < 0.5:
        S["wake_pct"] = 0
    if S["rem_pct"] is not None and S["rem_pct"] < 0.5 and (S["rem_avg_min"] or 0) < 0.5:
        S["rem_pct"] = 0
    effs = []
    for d in slv:
        if d["in"] is not None and d["out"] is not None:
            # 分钟列改 _num 安全转换（脏值不再抛错）
            span = (int(_num(d["out"])) - int(_num(d["in"]))) % 1440
            if span > 0:
                effs.append(100.0 * _num(d["total"]) / span)
    S["sleep_eff"] = avg(effs)
    ahi, snore = q_osa(cur, dates)
    S["ahi_list"] = [(r[0], _num(r[1])) for r in ahi if r[1] is not None]
    S["ahi_avg"] = avg([v for _, v in S["ahi_list"]])
    S["ahi_max"] = max([v for _, v in S["ahi_list"]], default=None)
    S["ahi_ge5_days"] = sum(1 for _, v in S["ahi_list"] if v >= 5)
    S["snore_days"] = [r for r in snore if _num(r[1]) > 0]
    si = q_sleep_index(cur)
    s_spo2, s_rec = [], []
    for ts, spo2, shr, br, rec in si:
        if ts_dstr(ts).replace("-", "") in dset:
            if spo2:
                s_spo2.append(spo2)
            if rec is not None:
                s_rec.append(rec)
    S["si_spo2"] = avg(s_spo2)
    S["si_rec"] = avg(s_rec)
    hr = q_hr_stat(cur, dates)
    S["hr_days"] = hr
    S["hr_rest"] = avg([r[2] for r in hr if r[2]]); S["hr_avg"] = avg([r[1] for r in hr if r[1]])
    S["hr_max"] = max([_num(r[3]) for r in hr if r[3] is not None], default=None); S["hr_min"] = min([_num(r[4]) for r in hr if r[4] is not None], default=None)
    S["hr_sleep_base"] = avg([r[5] for r in hr if r[5]]); S["hr_walk"] = avg([r[6] for r in hr if r[6]])
    sp = q_spo2_stat(cur, dates)
    S["spo2_avg"] = avg([r[1] for r in sp if r[1]]); S["spo2_min"] = min([_num(r[2]) for r in sp if r[2] is not None], default=None)
    S["spo2_low_days"] = sum(1 for r in sp if _num(r[3]) > 0)
    sp2 = q_sport(cur, dates)
    S["sport_days"] = sp2
    S["steps_avg"] = avg([r[1] for r in sp2 if r[1] is not None])
    # 原 `int(avg(...) or 8000)` 会把合法目标值 0 当作 falsy
    #   误判为缺失并回退 8000；改为仅在"无任何有效值"时才回退。
    _goal = avg([r[7] for r in sp2 if r[7] is not None])
    S["steps_goal"] = int(_goal) if _goal is not None else 8000
    S["steps_goal_hit"] = sum(1 for r in sp2 if _num(r[1]) >= S["steps_goal"])
    # 统一走「先剔除缺失、再换算单位」的口径。
    #   cal/dur/static_cal 的换算系数与原先一致（÷1000 / ÷60000），
    #   sed（久坐）改用 dur_min_avg 自适应毫秒/分钟，详见该函数说明。
    S["cal_avg"] = avg_scaled([r[2] for r in sp2], 1000)
    S["dur_avg"] = avg_scaled([r[3] for r in sp2], 60000)
    S["wk_avg"] = avg_scaled([r[4] for r in sp2])
    S["sed_avg"] = dur_min_avg([r[5] for r in sp2])
    S["sed_cnt_avg"] = avg_scaled([r[6] for r in sp2])
    S["static_cal_avg"] = avg_scaled([r[9] for r in sp2], 1000)
    ss = q_stress_stat(cur, dates)
    S["stress_stat_n"] = len(ss)   # 盲审 P3-5：日汇总口径的窗口内天数，渲染时标注分母
    S["stress_stage_min"] = {
        "放松": 2*avg([_num(r[1]) for r in ss]) if ss else None,
        "正常": 2*avg([_num(r[2]) for r in ss]) if ss else None,
        "中等": 2*avg([_num(r[3]) for r in ss]) if ss else None,
        "偏高": 2*avg([_num(r[4]) for r in ss]) if ss else None}
    sdw = {}
    for ts, v, sdnn, rmssd in stress_detail:
        dstr = ts_ymd(ts)
        if dstr in dset:
            sdw.setdefault(dstr, []).append((_pt(v), _pt(sdnn), _pt(rmssd)))
    S["stress_days"] = sdw
    S["stress_avg"] = avg([avg([x[0] for x in v]) for v in sdw.values() if v])
    S["stress_max"] = max([x[0] for v in sdw.values() for x in v if x[0] is not None], default=None)
    hrv_rows = [(x[1], x[2]) for v in sdw.values() for x in v if x[1] and x[2]]
    S["sdnn"] = avg([a for a, _ in hrv_rows]); S["rmssd"] = avg([b for _, b in hrv_rows])
    S["hrv_inv_pct"] = (100*sum(1 for a, b in hrv_rows if b > a)/len(hrv_rows)) if hrv_rows else None
    S["hrv_n"] = len(hrv_rows)
    if len(dates) == 1:
        hh = {}
        for ts, v, _, _ in stress_detail:
            if ts_ymd(ts) == dates[0]:
                hh.setdefault(ts_date(ts).hour, []).append(v)
        S["stress_24h"] = [(f"{h:02d}:00", avg(v)) for h, v in sorted(hh.items())]
        hh = {}
        for ts, v in hr_detail:
            if ts_ymd(ts) == dates[0]:
                hh.setdefault(ts_date(ts).hour, []).append(v)
        S["hr_24h"] = [(f"{h:02d}:00", avg(v)) for h, v in sorted(hh.items())]
    else:
        S["stress_24h"] = []
        S["hr_24h"] = []
    hours = {h: 0 for h in range(24)}
    for ts, v in sed_detail:
        if v and v >= 1 and ts_ymd(ts) in dset:
            hours[ts_date(ts).hour] += 1
    S["sed_hours"] = hours
    wt = q_weight(cur)
    # weight/bmi 列可能含非数字占位串（如 "--"），
    # 盲审 P3-4：过滤晚于锚点日的测量——历史报告是"截至锚点日的回看"，
    #   否则锚定 2026-07-01 的报告会显示"最近一次 2026-09-08"的穿越数据。
    S["weight_all"] = [(ts_dstr(r[0]), _num(r[1])/1000, _num(r[2])) for r in wt
                       if ts_dstr(r[0]).replace("-", "") <= DATA_END]
    S["weight_win"] = [x for x in S["weight_all"] if x[0].replace("-", "") in dset]
    ots = q_one_time_sport(cur)
    S["one_time"] = [r for r in ots if r[0] in dset]
    S["one_time_last"] = ots[-1] if ots else None
    ecg = q_ecg(cur)
    S["ecg_win"] = [r for r in ecg if ts_dstr(r[0]).replace("-", "") in dset]
    S["ecg_last"] = ecg[-1] if ecg else None
    br = q_breath(cur)
    # 呼吸率：DBBreathRate.value 单位为 0.1 次/分，须 ÷10（手册 3.3）
    S["breath"] = avg([v / 10.0 for ts, v in br if ts_ymd(ts) in dset])
    S["wrist"] = q_wrist_temp(cur, dates)
    return S

# ---------------- 通用组件 ----------------

def kvi(k, v, n=""):
    return f'<div class="kvi"><div class="k">{esc(k)}</div><div class="v">{v}</div><div class="n">{esc(n)}</div></div>'

_LEVEL = {
    "red":   ("#B91C1C", "需关注", "220,38,38"),
    "amber": ("#B45309", "待改善", "217,119,6"),
    "green": ("#047857", "良好", "5,150,105"),
    "info":  ("#0E7490", "说明", "10,161,192"),
}

# P3修复：网页报告"提醒级"阈值集中管理（与 auto_export 的 MED_THRESHOLDS 就医级
#   为有意分层的两套，互不复用；手册 L387 已说明。改此处即同步网页所有提醒级判据。
WEB_ALERT = {"rest_hr_bpm": 85, "spo2_min_pct": 93}  # 静息心率关注线 / 低血氧阈值（提醒级）

def flag(level, title, detail, adv=None):
    # 转义契约（X3收尾 2026-09-11）：title/adv 由本函数统一 esc()；
    #   detail 属「可信 HTML」——全部由 render_advice() 用代码内数值拼接
    #   （含刻意保留的 <b> 标签），不含任何 DB 原文，故整体 esc() 会破坏
    #   排版。今后若需在 detail 中嵌入 DB 原文，调用方必须先 esc()。
    c, tag, rgba = _LEVEL[level]
    a = f'<br><span style="color:#94A3B8">💡 {esc(adv)}</span>' if adv else ""
    return (f'<div class="flag" style="border-left-color:{c};background:rgba({rgba},.10)">'
            f'<span class="tag" style="color:{c};border-color:{c}">{tag}</span>'
            f'<div><b>{esc(title)}</b>：{detail}{a}</div></div>')

# ---------------- 一、健康建议（14 条规则，阈值收紧——不报喜不报忧） ----------------

def render_advice(S):
    flags = []
    if S["steps_avg"] is not None:
        g, sa = S["steps_goal"], S["steps_avg"]
        if sa < 3000:
            flags.append(("red", "活动量严重不足", f"日均步数仅 <b>{sa:.0f}</b> 步（目标 {g}）。",
                          "从餐后 15 分钟散步开始每天递增 500 步；把「连续坐 1 小时」设为提醒，起身活动 3 分钟。"))
        elif sa < 6000:
            flags.append(("amber", "活动量偏少", f"日均步数 {sa:.0f} 步，距目标 {g} 还差 {g-sa:.0f} 步。",
                          "通勤提前一站下车、午休快走 10 分钟，先稳住 6000 再冲 8000。"))
        elif sa < g:
            flags.append(("amber", "步数未达标", f"日均步数 {sa:.0f} 步，距目标 {g} 差 {g-sa:.0f} 步（达成 {S['steps_goal_hit']}/{len(S['sport_days'])} 天）。",
                          "把缺口拆成早晚两个 15 分钟快走段，比一次走完更容易坚持。"))
        else:
            flags.append(("green", "活动量达标", f"日均步数 {sa:.0f} 步，达成 {S['steps_goal_hit']}/{len(S['sport_days'])} 天目标。", None))
    if S["wk_avg"] is not None:
        if S["wk_avg"] < 10:
            flags.append(("red", "几乎没有中高强度运动", f"日均中高强度仅 {S['wk_avg']:.0f} 分钟（WHO 建议折合日均 ≥21 分钟）。",
                          "安排 3 次 20 分钟快走/慢跑/跳绳；用「能说话但不能唱歌」判断强度到位。"))
        elif S["wk_avg"] < 30:
            flags.append(("amber", "中高强度运动不足", f"日均中高强度 {S['wk_avg']:.0f} 分钟（WHO 建议折合日均 ≥21~30 分钟）。",
                          "每周 3 次 20 分钟中等强度有氧；步频提到 120~130 步/分即可达标。"))
    if S["sed_avg"] is not None and S["sed_avg"] >= 300:
        # C-P2-13：sed_cnt_avg 可能缺失（None），f"{None:.1f}" 会 TypeError 中断 flag 生成
        _sed_cnt_part = (f"，久坐提醒 {S['sed_cnt_avg']:.1f} 次/天" if S["sed_cnt_avg"] is not None else "")
        flags.append(("amber", "久坐时间偏长", f"日均久坐 {S['sed_avg']:.0f} 分钟（≈{S['sed_avg']/60:.1f} 小时）{_sed_cnt_part}。",
                      "每坐 45~60 分钟起身 3 分钟；久坐最集中的时段（见下方分布图）设为「活动改造区」。"))
    if S["dur_avg"] is not None and S["dur_avg"] < 120:
        flags.append(("amber", "身体活动时间偏少", f"日均活动时长 {S['dur_avg']:.0f} 分钟，全天大部分时间处于静态。", None))
    if S["sleep_avg_total"] is not None:
        h = S["sleep_avg_total"]/60
        if h < 6.5:
            flags.append(("red", "睡眠时长不足", f"日均主睡 {h:.1f} 小时（<6.5h）。",
                          "固定起床时间反推就寝点，先把睡眠补到 7 小时以上。"))
        elif h < 7.5:
            # 盲审 P3-2：h=7.4896 显示"7.5"却判"未到 7.5h"——显示四舍五入与判定
            #   精度不同所致。显示值舍入后恰为 7.5 时改用"逼近"措辞，消除自相矛盾。
            _tail = "，逼近 7.5h 理想线。" if round(h, 1) == 7.5 else "，未到 7.5h 理想线。"
            flags.append(("amber", "睡眠略偏短", f"日均主睡 {h:.1f} 小时{_tail}",
                          "入睡提前 20~30 分钟，起床时间保持固定。"))
        else:
            flags.append(("green", "睡眠时长充足", f"日均主睡 {h:.1f} 小时。", None))
    if S["sleep_avg_in"] is not None:
        lm = late_min(S["sleep_avg_in"])  # B10：归一化比较，修复凌晨入睡（00:00~05:00）提示永不触发的 bug
        if lm >= 25*60+30:  # 01:30 之后
            flags.append(("red", "入睡严重偏晚", f"平均 {d0(S['sleep_avg_in'])} 才入睡，昼夜节律明显后移。",
                          "以每 3 天提前 15 分钟的节奏逐步调整；起床后立刻接触日光 10 分钟重置生物钟。"))
        elif lm >= 25*60:  # 01:00~01:30
            flags.append(("red", "入睡偏晚", f"平均 {d0(S['sleep_avg_in'])} 入睡，已过凌晨 1 点。",
                          "睡前 1 小时调暗灯光、手机勿扰，咖啡因午后不碰。"))
        elif lm >= 24*60:  # 00:00~01:00
            flags.append(("amber", "入睡偏晚", f"平均 {d0(S['sleep_avg_in'])} 入睡，已过午夜。",
                          "把上床时间提前 20~30 分钟；晚睡会压缩深睡与 REM 总量（医学评分节律维度同步失分）。"))
        elif lm >= 23*60+30:  # 23:30~00:00
            flags.append(("amber", "入睡略晚", f"平均 {d0(S['sleep_avg_in'])} 入睡，跨过 23:30 建议窗口。",
                          "把上床时间提前 20 分钟，医学评分的节律分就能明显回升。"))
    if S["deep_pct"] is not None:
        if S["deep_pct"] < 13:
            flags.append(("amber", "深睡占比偏低", f"深睡 {S['deep_pct']:.1f}%（参考 13~23%）。",
                          "睡前避免饮酒饱食；卧室 20~23℃ 全黑；白天加一次中高强度运动可提升当晚深睡。"))
        elif S["deep_pct"] < 18:
            flags.append(("amber", "深睡处于区间下段", f"深睡 {S['deep_pct']:.1f}%（参考 13~23%），虽在范围内但贴近下沿，有提升空间。", None))
    if S["sleep_avg_wakecnt"] is not None and S["sleep_avg_wakecnt"] >= 2:
        flags.append(("amber", "夜醒偏多", f"夜醒 {S['sleep_avg_wakecnt']:.1f} 次/晚（<2 为佳）。",
                      "排查卧室温湿度、噪音与睡前饮水；睡前 90 分钟温水澡有助减少夜醒。"))
    if S["ahi_avg"] is not None:
        if S["ahi_avg"] >= 5:
            flags.append(("red", "睡眠呼吸暂停风险", f"AHI 均值 {S['ahi_avg']:.1f}（≥5 为异常），共 {S['ahi_ge5_days']} 天超标。",
                          "建议呼吸内科/睡眠门诊做多导睡眠监测（PSG）；侧卧睡、控制晚间饮酒。"))
        elif S["ahi_avg"] >= 4:
            flags.append(("amber", "AHI 逼近临界值", f"AHI 均值 {S['ahi_avg']:.1f}，已接近 5 的异常线（窗口内 {S['ahi_ge5_days']} 天 ≥5）。",
                          "关注晨起口干/头痛与白天嗜睡；控制体重与晚间酒精摄入可显著降低 AHI。"))
    if S["snore_days"]:
        flags.append(("amber", "存在打鼾记录", f"窗口内 {len(S['snore_days'])} 天监测到鼾声。",
                      "侧卧睡可减轻舌后坠；若鼾声不规律伴呼吸停顿，建议做睡眠监测。"))
    if S["stress_avg"] is not None:
        if S["stress_avg"] >= 65:
            flags.append(("red", "压力水平偏高", f"日均压力值 {S['stress_avg']:.0f}/100（≥65 偏高）。",
                          "每天 10 分钟腹式呼吸或正念；把最耗神的任务安排在上午。"))
        elif S["stress_avg"] >= 50:
            flags.append(("amber", "压力处于中上水平", f"日均压力值 {S['stress_avg']:.0f}/100（50~65 为中上段）。",
                          "午后 20 分钟散步已被证实能显著降低当日压力峰值；减少睡前刺激性内容。"))
    if S["stress_max"] is not None and S["stress_max"] >= 70:
        flags.append(("amber", "压力峰值较高", f"单点压力最高 {S['stress_max']:.0f}/100。",
                      "回忆峰值出现时段（24 小时压力曲线可见），识别并预处理对应压力源。"))
    if S["rmssd"] is not None:
        if S["rmssd"] < 30:
            flags.append(("red", "HRV 明显偏低", f"RMSSD 均值 {S['rmssd']:.1f}ms（<30 提示恢复能力弱）。",
                          "本周以睡眠和低强度活动为主，暂停高强度训练。"))
        elif S["rmssd"] < 40:
            flags.append(("amber", "HRV 恢复余地较大", f"RMSSD 均值 {S['rmssd']:.1f}ms（40 以上更理想）。",
                          "保证睡眠时长优先于训练量；酒精与晚睡是 HRV 的两大杀手。"))
    if S["hr_rest"] is not None:
        if S["hr_rest"] > WEB_ALERT["rest_hr_bpm"]:
            flags.append(("red", "静息心率偏高", f"静息心率均值 {S['hr_rest']:.0f} bpm（>85 需排查）。",
                          "连续多日偏高伴不适请就医；先排查睡眠、咖啡因与压力。"))
        elif S["hr_rest"] >= 75:
            flags.append(("amber", "静息心率偏高", f"静息心率均值 {S['hr_rest']:.0f} bpm（长期基线约 65，理想 55~70）。",
                          "静息心率升高常见于疲劳、缺睡或压力恢复期——本周把恢复放在训练之前。"))
        else:
            flags.append(("green", "静息心率优秀", f"静息心率均值 {S['hr_rest']:.0f} bpm（理想 55~70）。", None))
    if S["spo2_min"] is not None and S["spo2_min"] <= WEB_ALERT["spo2_min_pct"]:
        flags.append(("amber", "出现低血氧记录", f"窗口内最低血氧 {S['spo2_min']:.0f}%（{S['spo2_low_days']} 天有低血氧时段）。",
                      "单次短暂下降多为佩戴松动；若反复出现请结合 AHI 排查夜间呼吸问题。"))
    order = {"red": 0, "amber": 1, "green": 2}
    flags.sort(key=lambda f: order[f[0]])
    n_red = sum(1 for f in flags if f[0] == "red")
    n_amber = sum(1 for f in flags if f[0] == "amber")
    head = (f'<p class="sub">按优先级排序（需关注 {n_red} 项 · 待改善 {n_amber} 项）——'
            f'综合睡眠/心率/运动/压力/体重多维度评估，良好项仅保留对照参考</p>')
    if not flags:
        # L11修复：全空库或各指标均在参考范围内时 flags 为空，原仅渲染标题
        #   与副标题，第一个模块看起来像渲染故障。补占位说明，与另外五个模块的"本窗口内无…"一致。
        note = ('<p class="note">本窗口内未触发任何健康建议规则（各指标均在参考范围内或数据不足），'
                '保持当前作息与运动习惯即可；数据更新后重新生成报告将重新评估。</p>')
        return '<h3>一、健康建议</h3>' + head + note
    return '<h3>一、健康建议</h3>' + head + "".join(flag(*f) for f in flags)

# ---------------- 二、睡眠 ----------------

def expand_summary(label, count):
    """生成醒目、明确可点击的折叠触发条：强调色描边 + 渐变底 + 三角箭头 + 明细项数 + 点击提示。
    与普通正文形成强对比，用户可一眼识别这是可展开的点击区域。"""
    return (
        '<summary>'
        '<span class="ex-ico" aria-hidden="true"></span>'
        f'<span class="ex-txt">{label}</span>'
        '<span class="ex-meta">'
        f'<span class="ex-count">{count} 项明细</span>'
        '<span class="ex-act"><span class="ex-closed">点击展开 ▾</span><span class="ex-opened">点击收起 ▴</span></span>'
        '</span>'
        '</summary>'
    )


def render_sleep(S):
    out = ['<h3>二、睡眠分析</h3>']
    slv = S["sleep_valid"]
    if not slv:
        return out[0] + '<p class="note">本窗口内无睡眠记录，模块按规则不展开。</p>'
    n_nights = len(slv)
    total_min = S["sleep_avg_total"] or 0
    total_h = total_min / 60 if total_min else 0
    # ============= 第一层：核心摘要（始终可见） =============
    summary_lines = []
    summary_color = "#047857"  # 默认绿
    if total_h >= 7:
        summary_lines.append(f"日均主睡 <b>{total_h:.1f}h</b>，时长达标")
    else:
        summary_lines.append(f"日均主睡 <b>{total_h:.1f}h</b>，时长不足")
        summary_color = "#B91C1C"
    if S["deep_pct"] is not None:
        _dp = S['deep_pct']
        summary_lines.append(f"深睡 <b>{_dp:.0f}%</b>" + ("（达标）" if 13 <= _dp <= 23 else "（偏离）"))
    if S["ahi_avg"] is not None and S["ahi_avg"] >= 5:
        summary_color = "#B91C1C"
    summary_p = '<p class="note" style="margin:8px 0 14px;border-left-color:' + summary_color + '">' + " · ".join(summary_lines) + "</p>"
    core_kv = [
        kvi("日均睡眠时长", f"{total_h:.1f} h" if total_min else "—", f"{n_nights} 晚有效数据"),
        kvi("平均入睡时间", d0(S["sleep_avg_in"]) if S["sleep_avg_in"] is not None else "—", "建议 ≤23:30"),
        kvi("平均起床时间", d0(S["sleep_avg_out"]) if S["sleep_avg_out"] is not None else "—", "起床宜规律"),
        kvi("平均夜醒次数", f"{S['sleep_avg_wakecnt']:.1f} 次/晚" if S["sleep_avg_wakecnt"] is not None else "—", "<2 为佳"),
    ]
    out.append(f'<div class="kv">{"".join(core_kv)}</div>')
    out.append(summary_p)
    # 环形图：默认显示总睡眠时间（中心文字 + 下方说明）
    segs = []
    if S["deep_pct"] is not None:
        def seg(name, pct, col, extra):
            return (name, pct, col, extra) if pct >= 1 else None  # <1% 不入环（防视觉噪音）
        segs = [x for x in [
            ("深睡", S["deep_pct"], "#0E7490", f"日均 {hmin(S['deep_avg_min'])}"),
            ("浅睡", S["light_pct"] or 0, "#38BDF8", f"日均 {hmin(S['light_avg_min'])}"),
            ("REM", S["rem_pct"] or 0, "#06B6D4", f"日均 {hmin(S['rem_avg_min'])}"),
            ("夜间清醒", S["wake_pct"] or 0, "#CBD5E1", f"日均 {hmin(S['wake_avg_min'])}"),
        ] if x is not None]
    default_top = f"{total_h:.1f}h" if total_min else "—"
    default_bot = "总睡眠时间"
    dn = donut(segs, default_top, default_bot)
    out.append(f'<div class="chart donut-box"><h4>睡眠结构占比（悬停查看各阶段）</h4>{dn}</div>')
    # ============= 第二层：详细分析（折叠） =============
    detail_parts = []
    # 表格（用日均分钟 + 对应百分比，保证严格一致）
    def srow(name, mins, pct, ref):
        if mins < 0.5:
            return ""  # < 0.5 分钟不显示该行（防"0分钟 X%"矛盾）
        ok = "正常" if ref[0] <= pct <= ref[1] else "偏离"
        cls = "ok" if ok == "正常" else "bad"
        return (f'<tr class="jrow" data-name="{esc(name)}"><td>{name}</td><td>{hmin(mins)}</td>'
                f'<td>{pct:.1f}%</td><td>{ref[0]}~{ref[1]}%</td><td class="{cls}">{ok}</td></tr>')
    table_rows = ""
    if S["deep_pct"] is not None:
        table_rows = (
            srow("深睡", S["deep_avg_min"], S["deep_pct"], (13, 23))
            + srow("浅睡", S["light_avg_min"], S["light_pct"] or 0, (45, 60))
            + srow("REM", S["rem_avg_min"], S["rem_pct"] or 0, (20, 25))
            + srow("夜间清醒", S["wake_avg_min"], S["wake_pct"] or 0, (0, 5))
        )
    table_html = (
        '<div class="chart"><h4>睡眠阶段明细（成人参考区间）</h4>'
        '<table style="margin:8px 0"><thead><tr><th scope="col">阶段</th><th scope="col">日均时长</th>'
        '<th scope="col">占比</th><th scope="col">参考</th><th scope="col">评价</th></tr></thead>'
        f'<tbody>{table_rows}</tbody></table></div>'
    )
    detail_parts.append(table_html)
    # 每晚主睡时长折线图
    if len(slv) >= 2:
        pts = [(_mmdd(d["date"]), d["total"] / 60) for d in slv]
        detail_parts.append(
            f'<div class="chart" data-unit=" h"><h4>每晚主睡时长（小时，悬停查看）</h4>'
            f'{line_chart(pts, "#38BDF8", refs=[(7, "7h 下限", "#059669"), (9, "9h 上限", "#D97706")])}</div>'
        )
    # 医学评分（收紧阈值版）
    total, grade, color, items = medical_sleep_score(
        S["sleep_avg_total"] or 0, S["deep_pct"], S["rem_pct"],
        S["sleep_avg_in"], S["sleep_avg_wakecnt"] or 0, S["sleep_eff"])
    detail_rows = "".join(
        f'<tr><td>{n}</td><td><b>{s if s is not None else "—"}</b> / {m}</td><td style="color:#94A3B8">{t}</td></tr>'
        for n, s, m, t in items)
    scorebox = (
        f'<div class="scorebox" style="border-left:4px solid {color}">'
        f'<div><span class="sv" style="color:{color}">{total}</span><span class="su">/ 100</span>'
        f'<span class="sg" style="color:{color};border-color:{color}">{grade}</span></div>'
        f'<div class="sd">评分模型：AASM / NSF 成人六维加权 —— '
        f'时长 {SLEEP_SCORE_WEIGHTS["duration"]} · 深睡 {SLEEP_SCORE_WEIGHTS["deep"]} · REM {SLEEP_SCORE_WEIGHTS["rem"]} · 节律 {SLEEP_SCORE_WEIGHTS["rhythm"]} · 连续性 {SLEEP_SCORE_WEIGHTS["continuity"]} · 效率 {SLEEP_SCORE_WEIGHTS["efficiency"]}</div></div>'
        f'<table><thead><tr><th scope="col">维度</th><th scope="col">得分</th><th scope="col">依据</th></tr></thead>'
        f'<tbody>{detail_rows}</tbody></table>'
        '<div class="warnbox">⚠️ <b>特别标注</b>：本评分基于睡眠结构与节律的医学标准算法，与设备端「睡眠评分」完全无关。'
        '设备 sleep_score 存在系统性虚高（与深睡比例、夜醒次数等真实指标弱相关），已在本报告中彻底禁用。</div>'
        '<p class="note">ℹ️ <b>深睡 / REM 占比口径</b>：本报告以<b>日均总睡眠时长</b>为分母'
        '（日汇总表 total_sleep_time，含白天小睡），故深睡参考区间取 13~23%。'
        'Markdown 分析报告则以<b>主睡时长</b>为分母（不含小睡，避免小睡稀释），参考区间相应为 15~25%。'
        '两者分母不同、占比不可直接对照，均非数据错误。</p>'
    )
    detail_parts.append(scorebox)
    # OSA
    osa_kv = "".join([
        kvi("监测晚数", f"{len(S['ahi_list'])}", f"AHI≥5 的天数 {S['ahi_ge5_days']}"),
        kvi("AHI 均值", f"{S['ahi_avg']:.1f}" if S["ahi_avg"] is not None else "—", "正常 <5"),
        kvi("AHI 最高", f"{S['ahi_max']:.1f}" if S["ahi_max"] is not None else "—", "单晚最差记录"),
        kvi("打鼾天数", f"{len(S['snore_days'])}", "窗口内有鼾声记录的天数"),
        kvi("低血氧时段", f"{S['spo2_low_days']} 天", "时长>0 的天数"),
    ])
    osa_note = ""
    if S["ahi_avg"] is not None and S["ahi_avg"] < 5 and S["ahi_ge5_days"] == 0:
        osa_note = '<p class="note"><b>结论：</b>未发现睡眠呼吸暂停风险信号（AHI&lt;5）。此为<b>消费级筛查结论</b>，伴日间嗜睡等症状仍建议 PSG。</p>'
    elif S["ahi_avg"] is not None and S["ahi_avg"] >= 5:
        osa_note = '<p class="note"><b>结论：</b>AHI 出现 ≥5 记录，伴日间嗜睡请就诊呼吸内科 / 睡眠门诊做 PSG 确诊。</p>'
    detail_parts.append(
        '<h4>睡眠呼吸暂停（OSA）风险</h4>'
        f'<div class="kv">{osa_kv}</div>{osa_note}'
    )
    # 腕温
    if S["wrist"]:
        wt = S["wrist"]
        # A1/G-B2修复：腕温 值/基线 改 _num 安全转换（与 M1/M2 同类，
        #   非数字占位串不再抛 ValueError 中断报告）；日期标签经 _mmdd 校验 8 位防乱码。
        wpts = [(_mmdd(r[0]), _num(r[2]) / 100) for r in wt]
        base = avg([_num(r[1]) / 100 for r in wt])
        refs = [(base, f"基线 {base:.2f}℃", "#7C3AED")] if base else []
        dev = [(r[0], _num(r[2]) / 100 - _num(r[1]) / 100) for r in wt]
        hot = [x for x in dev if x[1] > 0.5]
        note = "皮肤温度反映外周血管状态，比核心体温低 2~3℃ 属正常。"
        if hot:
            note = (f"有 {len(hot)} 天实测高于基线 0.5℃ 以上（"
                    + "、".join(esc(f"{d} +{v:.2f}℃") for d, v in hot) + "），若伴乏力/酸痛需留意休息。" + note)
        detail_parts.append(
            f'<h4>手腕皮肤温度（℃，悬停查看）</h4>'
            f'<div class="chart" data-unit=" ℃">{line_chart(wpts, "#FB923C", refs=refs)}</div>'
            f'<p class="note">🌙 腕温解读：{note}</p>'
        )
    # 包装为 details
    details_html = (
        '<details class="expander">' + expand_summary('睡眠阶段表 · 趋势图 · 医学评分 · OSA · 腕温', len(detail_parts))
        + "".join(detail_parts) + '</details>'
    )
    out.append(details_html)
    return "".join(out)

# ---------------- 三、心率 ----------------

def render_heart(S):
    out = ['<h3>三、心率分析</h3>']
    if not S["hr_days"]:
        return out[0] + '<p class="note">本窗口内无心率日统计。</p>'
    rest = S["hr_rest"]
    # C-P3-1：原局部变量名 `avg` 遮蔽全局求均函数 avg()——本函数内此后再调 avg()
    #   会 TypeError（float 不可调用）。改名消除遮蔽。
    hr_avg_val = S["hr_avg"]
    # 第一层：核心摘要
    summary = ""
    color = "#047857"
    if rest is not None and rest > WEB_ALERT["rest_hr_bpm"]:
        summary = f"静息心率 <b>{rest:.0f} bpm</b>，偏高，需关注"
        color = "#B91C1C"
    elif rest is not None and rest >= 75:
        summary = f"静息心率 <b>{rest:.0f} bpm</b>，偏高"
        color = "#B45309"
    elif rest is not None:
        summary = f"静息心率 <b>{rest:.0f} bpm</b>，理想区间"
    core_kv = [
        kvi("静息心率", f"{rest:.0f} bpm" if rest else "—", "理想 55~70"),
        kvi("日均心率", f"{hr_avg_val:.0f} bpm" if hr_avg_val else "—", ""),
        kvi("最低心率", f"{S['hr_min']:.0f} bpm" if S["hr_min"] else "—", "睡眠期常见"),
    ]
    out.append(f'<div class="kv">{"".join(core_kv)}</div>')
    out.append(f'<p class="note" style="margin:8px 0 14px;border-left-color:{color}">{summary}</p>')
    # 折叠详情
    detail_parts = []
    detail_parts.append(
        '<div class="kv">' + "".join([
            kvi("日最高心率", f"{S['hr_max']:.0f} bpm" if S["hr_max"] else "—", "运动峰值可达 220-年龄"),
            kvi("睡眠基础心率", f"{S['hr_sleep_base']:.0f} bpm" if S["hr_sleep_base"] else "—", "夜间平稳为佳"),
            kvi("步行平均心率", f"{S['hr_walk']:.0f} bpm" if S["hr_walk"] else "—", ""),
        ]) + '</div>'
    )
    if len(S["hr_days"]) >= 2:
        pts = [(_mmdd(r[0]), r[2]) for r in S["hr_days"] if r[2]]
        detail_parts.append(
            f'<div class="chart" data-unit=" bpm"><h4>每日静息心率（bpm）</h4>'
            f'{line_chart(pts, "#DC2626", refs=[(60, "60 理想", "#059669"), (WEB_ALERT["rest_hr_bpm"], "85 关注线", "#D97706")])}</div>'
        )
    elif S["hr_24h"]:
        detail_parts.append(
            f'<div class="chart" data-unit=" bpm"><h4>当日心率走势（小时均值）</h4>'
            f'{line_chart(S["hr_24h"], "#38BDF8")}</div>'
        )
    if S["spo2_avg"]:
        low = f"，低血氧（时长>0）天数 {S['spo2_low_days']} 天" if S["spo2_low_days"] else "，未见低血氧时段记录"
        # spo2_avg 与 spo2_min 在 build_stats 中各自独立过滤，
        #   spo2_min 可能为 None（min 列 NULL）→ 原 {:.0f} 抛 TypeError 会中断整份报告。
        #   改用 fmt()，None 显示为 "—"。
        detail_parts.append(
            f'<p class="note">🩸 血氧：窗口平均 {S["spo2_avg"]:.1f}%，最低 {fmt(S["spo2_min"], 0)}%{low}。</p>'
        )
    if S["si_spo2"] is not None:
        detail_parts.append(
            f'<p class="note">😴 睡眠血氧：窗口平均 {S["si_spo2"]:.1f}%（来自睡眠指数表）。</p>'
        )
    # 呼吸率已在 build_stats 中 ÷10（DBBreathRate.value 单位 0.1 次/分）
    if S["breath"] is not None:
        detail_parts.append(
            f'<p class="note">🌬️ 呼吸率：窗口平均 {S["breath"]:.1f} 次/分（成人静息参考 12~20 次/分）。</p>'
        )
    else:
        detail_parts.append('<p class="note">🌬️ 呼吸率：窗口内无测量记录。</p>')
    if S["ecg_win"]:
        names = {}
        for _, nm in S["ecg_win"]:
            names[nm] = names.get(nm, 0) + 1
        desc = "、".join(f"{esc(k)}×{v}" for k, v in names.items())
        detail_parts.append(f'<p class="note">🫀 心电图：窗口内 {len(S["ecg_win"])} 次记录（{desc}）。</p>')
    elif S["ecg_last"]:
        last_d = ts_dstr(S["ecg_last"][0])
        detail_parts.append(f'<p class="note">🫀 心电图：本窗口内无记录（全库最近一次 {last_d}）。</p>')
    out.append('<details class="expander">' + expand_summary('完整心率明细（每日静息 / 最高 / 最低 · 血氧 · 心电图）', len(detail_parts)) + "".join(detail_parts) + '</details>')
    return "".join(out)

# ---------------- 四、运动与活动 ----------------

def render_activity(S):
    out = ['<h3>四、运动与活动</h3>']
    if not S["sport_days"]:
        return out[0] + '<p class="note">本窗口内无运动日汇总记录。</p>'
    steps = S["steps_avg"]
    goal = S["steps_goal"]
    # 第一层：核心摘要
    color = "#047857"
    if steps is not None and steps >= goal:
        summary = f"日均 <b>{steps:.0f} 步</b>，达成目标（{goal}）"
    elif steps is not None and steps >= goal * 0.75:
        summary = f"日均 <b>{steps:.0f} 步</b>，接近目标（{goal}）"
        color = "#B45309"
    elif steps is not None:
        summary = f"日均 <b>{steps:.0f} 步</b>，远低于目标（{goal}）"
        color = "#B91C1C"
    else:
        summary = "暂无数据"
    core_kv = [
        kvi("日均步数", f"{steps:.0f}" if steps is not None else "—", f"目标 {goal}"),
        kvi("日均中高强度", f"{S['wk_avg']:.0f} 分钟" if S["wk_avg"] is not None else "—", "WHO 建议 ≥21"),
        kvi("日均久坐", f"{S['sed_avg']:.0f} 分钟" if S["sed_avg"] is not None else "—", "越小越好"),
    ]
    out.append(f'<div class="kv">{"".join(core_kv)}</div>')
    out.append(f'<p class="note" style="margin:8px 0 14px;border-left-color:{color}">{summary}</p>')
    # 折叠详情
    detail_parts = []
    detail_parts.append(
        '<div class="kv">' + "".join([
            kvi("日均活动消耗", f"{S['cal_avg']:.0f} 千卡" if S["cal_avg"] is not None else "—", "不含基础代谢"),
            kvi("日均基础代谢", f"{S['static_cal_avg']:.0f} 千卡" if S["static_cal_avg"] is not None else "—", "静息消耗"),
            kvi("日均活动时长", f"{S['dur_avg']:.0f} 分钟" if S["dur_avg"] is not None else "—", ""),
        ]) + '</div>'
    )
    spd = [(_mmdd(r[0]), _num(r[1])) for r in S["sport_days"]]
    t = "每日步数" if len(spd) >= 2 else "当日步数"
    detail_parts.append(
        f'<div class="chart" data-unit=" 步"><h4>{t}</h4>'
        f'{bar_chart(spd, "#38BDF8", refs=[(goal, f"{goal} 步目标", "#D97706")])}</div>'
    )
    hours = S["sed_hours"]
    if sum(hours.values()) > 0:
        items = [(f"{h:02d}时", hours[h]) for h in range(24) if hours[h] > 0]
        peak_h, peak_v = max(hours.items(), key=lambda x: x[1])
        detail_parts.append(
            f'<div class="chart" data-unit=" 分钟"><h4>久坐时段分布（窗口合计分钟）</h4>'
            f'{bar_chart(items, "#64748B")}</div>'
            f'<p class="note">久坐最集中时段：<b>{peak_h:02d}:00~{peak_h+1:02d}:00</b>（约 {peak_v} 分钟）。'
            f'把该时段设为「活动改造区」。</p>'
        )
    if S["one_time"]:
        items_html = ""
        for r in S["one_time"]:
            # 距离/卡路里/时长列可能为非数字 TEXT，
            #   原 (r[n] or 0)/k 会抛 TypeError 中断报告；改用 _num 安全转换。
            #   同时 date/mode 等 DB 原文统一 esc()（与全文件转义约定保持一致）。
            dur = _num(r[5]) / 60000
            items_html += (
                f'<div class="flag" style="border-left-color:#0E7490"><span class="tag" '
                f'style="color:#0E7490;border-color:#0E7490">运动</span>'
                f'<div><b>{esc(r[0])}</b>：模式 {esc(r[1])}，{_num(r[2]):.0f} 步 / {_num(r[3])/1000:.2f} km / '
                f'{_num(r[4])/1000:.0f} 千卡 / {dur:.0f} 分钟。</div></div>'
            )
        detail_parts.append('<h4>单独运动记录</h4>' + items_html)
    else:
        last = S["one_time_last"]
        # C-P1-1：last[0]/last[1] 为数据库原文（运动类型/模式），必须过 esc() 防 HTML 注入
        note = (f'最近一次结构化运动为 <b>{esc(last[0])}</b>（模式 {esc(last[1])}）。' if last else "全库暂无单独运动记录。")
        detail_parts.append(f'<p class="note">🏃 本窗口内没有跑步、跳绳等单独运动记录。{note}——建议重启每周 2~3 次结构化运动。</p>')
    out.append('<details class="expander">' + expand_summary('活动明细（消耗 / 时长 / 久坐分布 / 单独运动）', len(detail_parts)) + "".join(detail_parts) + '</details>')
    return "".join(out)

# ---------------- 五、压力与恢复 ----------------

def render_stress(S):
    out = ['<h3>五、压力与恢复</h3>']
    # 盲审 P2-1：早退条件原只看明细表 stress_avg——日汇总表 stress_stage_min 可能
    #   仍有完整数据（实测明细仅覆盖近 13 天、日汇总覆盖全量 196 天，历史窗口明明
    #   有 30 天分期数据却整模块消失）。改为「明细与日汇总都无数据」才跳过；
    #   仅有日汇总时渲染档位分布，分钟级采样指标（压力值/HRV）显示「—」。
    _has_stat = any(v is not None for v in (S.get("stress_stage_min") or {}).values())
    if S["stress_avg"] is None and not _has_stat:
        return out[0] + '<p class="note">本窗口内无压力采样数据。</p>'
    if S["stress_avg"] is None:
        out.append('<p class="note" style="border-left-color:#B45309">本窗口无分钟级压力采样'
                   '（明细表未覆盖该区间），以下档位分布为<b>日汇总口径</b>；'
                   '压力均值与 HRV 显示为「—」。</p>')
    sa = S["stress_avg"]
    # 第一层：核心摘要（sa 可能为 None——仅日汇总口径时）
    if sa is None:
        summary = "本窗口无分钟级采样，无法计算日均压力值（档位分布见下方日汇总口径）"
        color = "#B45309"
    else:
        color = "#047857"
        if sa >= 65:
            summary = f"日均压力 <b>{sa:.0f}</b>/100，偏高，需关注"
            color = "#B91C1C"
        elif sa >= 50:
            summary = f"日均压力 <b>{sa:.0f}</b>/100，中上水平"
            color = "#B45309"
        else:
            summary = f"日均压力 <b>{sa:.0f}</b>/100，正常"
    rmssd = S["rmssd"]
    core_kv = [
        kvi("日均压力值", f"{sa:.0f} / 100" if sa is not None else "—", "正常 <50"),
        kvi("HRV RMSSD", f"{rmssd:.1f} ms" if rmssd else "—", "≥40 恢复良好"),
        kvi("最高压力值", f"{S['stress_max']:.0f}" if S["stress_max"] else "—", "单点峰值"),
    ]
    out.append(f'<div class="kv">{"".join(core_kv)}</div>')
    out.append(f'<p class="note" style="margin:8px 0 14px;border-left-color:{color}">{summary}</p>')
    # 折叠详情
    detail_parts = []
    detail_parts.append(
        '<div class="kv">' + "".join([
            kvi("放松时长", f"{S['stress_stage_min']['放松']:.0f} 分钟/天" if S["stress_stage_min"]["放松"] is not None else "—", f"档位日均 · 日汇总口径（{S.get('stress_stat_n') or 0} 天）"),
            kvi("HRV SDNN", f"{S['sdnn']:.1f} ms" if S["sdnn"] else "—", f"有效样本 {S['hrv_n']}"),
            kvi("睡眠恢复率", f"{S['si_rec']:.0f}" if S["si_rec"] else "—", "设备字段"),
        ]) + '</div>'
    )
    st = S["stress_stage_min"]
    tot = sum(v or 0 for v in st.values())
    segs = [(k, 100*(v or 0)/tot, c, f"均 {v:.0f} 分钟")
            for (k, v), c in zip(st.items(),
                ["#059669", "#38BDF8", "#D97706", "#DC2626"]) if (v or 0) > 0]
    if S["stress_24h"]:
        curve_title = "当日 24 小时压力曲线"
        curve = line_chart(S["stress_24h"], "#D97706",
                           refs=[(60, "60 偏高线", "#DC2626"), (40, "40 轻松线", "#059669")])
    else:
        curve_title = "逐日压力均值"
        dpts = [(k[4:6] + "/" + k[6:], avg([x[0] for x in v])) for k, v in sorted(S["stress_days"].items())]
        curve = (line_chart(dpts, "#D97706",
                           refs=[(60, "60 偏高线", "#DC2626"), (40, "40 轻松线", "#059669")])
                 if dpts else '<p class="note">无逐日采样数据</p>')
    detail_parts.append(
        f'<div class="grid2"><div class="chart donut-box"><h4>压力档位分布</h4>'
        f'{donut(segs, f"{sa:.0f}" if sa is not None else "—", "日均压力")}</div>'
        f'<div class="chart" data-unit=" 分"><h4>{curve_title}</h4>{curve}</div></div>'
    )
    grade = ("恢复良好" if (rmssd or 0) >= 40 and sa < 55 else (
        "恢复一般" if sa < 65 else "恢复承压")) if sa is not None else "—（无分钟级采样）"
    inv = ""
    if S["hrv_inv_pct"] is not None and S["hrv_inv_pct"] > 50:
        inv = f'（注意：{S["hrv_inv_pct"]:.0f}% 采样 RMSSD>SDNN，字段口径可能来自不同算法，趋势比绝对值更有参考意义）'
    detail_parts.append(
        f'<p class="note"><b>身体恢复评估：{grade}。</b>HRV 是自主神经恢复能力的常用窗口指标，'
        f'当前 RMSSD {fmt(rmssd)}ms / SDNN {fmt(S["sdnn"])}ms{inv}。'
        f'提升恢复的三个杠杆：睡眠时长（最优先）、酒精/晚餐时间控制、高强度活动与恢复日交替安排。</p>'
    )
    out.append('<details class="expander">' + expand_summary('压力分布 / 曲线 / HRV 详情', len(detail_parts)) + "".join(detail_parts) + '</details>')
    return "".join(out)

# ---------------- 六、体重 ----------------

def render_weight(S):
    out = ['<h3>六、体重变化</h3>']
    # BMI 缺失（_num 兜底 0.0）时显示"—"且不参与评级，避免误标"偏瘦"
    def _bmi_disp(v):
        return f"{v:.2f}" if v and v > 0 else "—"

    if not S["weight_win"]:
        last = S["weight_all"][-1] if S["weight_all"] else None
        if last:
            out.append(f'<p class="note">本窗口内没有体重测量记录。最近一次：{esc(last[0])}，{last[1]:.2f} kg（BMI {_bmi_disp(last[2])}）。建议每周固定条件（晨起空腹）称重一次。</p>')
        else:
            out.append('<p class="note">全库暂无体重记录。</p>')
        return "".join(out)
    w1 = S["weight_win"][-1][1]
    bmi = S["weight_win"][-1][2]
    # 第一层：核心摘要
    color = "#047857"
    if not (bmi and bmi > 0):
        summary = f"最新体重 <b>{w1:.1f} kg</b>，BMI <b>—</b>（库内无有效 BMI 值，不做评级）"
    elif 18.5 <= bmi <= 23.9:
        summary = f"最新体重 <b>{w1:.1f} kg</b>，BMI <b>{bmi:.2f}</b>（正常范围）"
    elif bmi < 18.5:
        summary = f"最新体重 <b>{w1:.1f} kg</b>，BMI <b>{bmi:.2f}</b>（偏瘦）"
        color = "#B45309"
    else:
        summary = f"最新体重 <b>{w1:.1f} kg</b>，BMI <b>{bmi:.2f}</b>（超重）"
        color = "#B91C1C"
    out.append(f'<p class="note" style="margin:8px 0 14px;border-left-color:{color}">{summary}</p>')
    # 折叠详情
    detail_parts = []
    rows = "".join(
        f'<tr><td>{esc(d)}</td><td>{w:.2f} kg</td><td>{_bmi_disp(bmi_)}</td></tr>'
        for d, w, bmi_ in S["weight_win"])
    detail_parts.append(
        '<table><thead><tr><th scope="col">测量日期</th><th scope="col">体重</th><th scope="col">BMI</th></tr></thead>'
        f'<tbody>{rows}</tbody></table>'
    )
    if len(S["weight_win"]) >= 2:
        pts = [(d[5:], w) for d, w, _ in S["weight_win"]]
        detail_parts.append(
            f'<div class="chart" data-unit=" kg"><h4>窗口内体重趋势</h4>'
            f'{line_chart(pts, "#059669")}</div>'
        )
    d0w, w0 = S["weight_win"][0][0], S["weight_win"][0][1]
    prev = [x for x in S["weight_all"] if x[0] < d0w]
    cmp_txt = ""
    if prev:
        pw = prev[-1][1]
        cmp_txt = f'较上次测量（{prev[-1][0]}，{pw:.1f}kg）<b>{w1-pw:+.1f} kg</b>。'
    detail_parts.append(
        f'<p class="note">窗口内体重 {w0:.1f} → {w1:.1f} kg，{cmp_txt}'
        + (f'BMI {bmi:.2f}（18.5~23.9 为中国成人正常范围）。' if bmi and bmi > 0
           else 'BMI：库内无有效值，不做解读（18.5~23.9 为中国成人正常范围）。')
        + f'短期 0.5~1kg 波动多来自水分与肠道内容物，看周趋势更有意义。</p>'
    )
    out.append('<details class="expander">' + expand_summary('体重明细表与历史对比', len(detail_parts)) + "".join(detail_parts) + '</details>')
    return "".join(out)

# ---------------- JS 交互（B5：每张图表） ----------------

JS = """/* =============================================================
   报告单页面应用（纯原生 JS，无框架 / 无外部依赖 / 离线可用）
   - 单页面：四窗口 × 六模块 全部内联渲染，目录锚点平滑跳转
   - 侧栏：窄屏抽屉（汉堡按钮展开/收起 + 遮罩/ESC/点击链接关闭）
   - 滚动高亮（scrollspy）：当前窗口 / 模块联动侧栏
   - 图表交互：tooltip + donut 扇区联动（含键盘可访问）
   ============================================================= */
(function(){
  'use strict';
  var reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ---------- 滚动渐显 ---------- */
  var ioReveal = ('IntersectionObserver' in window) ? new IntersectionObserver(function(entries){
    entries.forEach(function(en){ if (en.isIntersecting){ en.target.classList.add('is-in'); ioReveal.unobserve(en.target); } });
  }, { rootMargin: '0px 0px -6% 0px', threshold: 0.04 }) : null;
  if (!ioReveal) { document.querySelectorAll('.reveal').forEach(function(el){ el.classList.add('is-in'); }); }

  /* ---------- 侧栏抽屉 ---------- */
  var toggle = document.getElementById('navtoggle');
  var scrim = document.getElementById('scrim');
  var sidenav = document.getElementById('sidenav');
  function syncAria(){
    if (!toggle) return;
    var expanded = (window.innerWidth <= 1024) ? document.body.classList.contains('nav-open')
                                              : !document.body.classList.contains('nav-collapsed');
    toggle.setAttribute('aria-expanded', expanded ? 'true' : 'false');
  }
  function openNav(){ document.body.classList.add('nav-open'); syncAria(); }
  function closeNav(){ document.body.classList.remove('nav-open'); syncAria(); }
  function toggleNav(){
    if (window.innerWidth <= 1024) document.body.classList.toggle('nav-open');
    else document.body.classList.toggle('nav-collapsed');
    syncAria();
  }
  if (toggle) toggle.addEventListener('click', function(e){ e.stopPropagation(); toggleNav(); });
  if (scrim) scrim.addEventListener('click', closeNav);
  document.addEventListener('keydown', function(e){ if (e.key === 'Escape') closeNav(); });
  if (sidenav) sidenav.addEventListener('click', function(e){
    var a = e.target.closest('a'); if (!a) return;
    if (window.innerWidth <= 1024) closeNav();
  });

  /* ---------- 滚动高亮 scrollspy ---------- */
  var winLinks = {}, modLinks = {};
  document.querySelectorAll('.nav-t[data-w]').forEach(function(a){ if (a.dataset.w) winLinks[a.dataset.w] = a; });
  document.querySelectorAll('.nav-sub a[data-target]').forEach(function(a){ modLinks[a.dataset.target] = a; });
  var activeWin = null, activeMod = null;
  function setWin(id){ if (activeWin === id) return; activeWin = id;
    Object.keys(winLinks).forEach(function(k){ winLinks[k].classList.toggle('is-active', k === id); }); }
  function setMod(id){ if (activeMod === id) return; activeMod = id;
    Object.keys(modLinks).forEach(function(k){ var on = (k === id);
      modLinks[k].classList.toggle('is-active', on);
      if (on) modLinks[k].setAttribute('aria-current','true'); else modLinks[k].removeAttribute('aria-current'); }); }

  if ('IntersectionObserver' in window) {
    var secIO = new IntersectionObserver(function(entries){
      entries.forEach(function(en){ if (en.isIntersecting && /^w\\d+$/.test(en.target.id)) setWin(en.target.id); });
    }, { rootMargin: '-18% 0px -72% 0px', threshold: 0 });
    document.querySelectorAll('section.window').forEach(function(s){ secIO.observe(s); });

    var modVisible = {};
    var modIO = new IntersectionObserver(function(entries){
      entries.forEach(function(en){ modVisible[en.target.id] = en.isIntersecting ? en.intersectionRatio : 0; });
      var best = null, br = 0;
      document.querySelectorAll('h3[id^="w"]').forEach(function(h){ var r = modVisible[h.id] || 0; if (r > br){ br = r; best = h.id; } });
      if (best) setMod(best);
    }, { rootMargin: '-12% 0px -60% 0px', threshold: [0.01, 0.2, 0.5, 0.8] });
    document.querySelectorAll('h3[id^="w"]').forEach(function(h){ modIO.observe(h); });
  }

  /* ---------- 图表 tooltip + donut 联动 ---------- */
  var tip = null;
  function ensureTip(){
    if (tip) return tip;
    tip = document.createElement('div');
    tip.setAttribute('role','status'); tip.setAttribute('aria-live','polite');
    tip.style.cssText = 'position:fixed;display:none;pointer-events:none;background:rgba(11,18,32,.94);border:1px solid rgba(56,189,248,.4);border-radius:12px;padding:9px 13px;font-size:12.5px;color:#F8FAFC;z-index:var(--z-tip);box-shadow:0 10px 30px -6px rgba(0,0,0,.7);white-space:nowrap;font-family:var(--sans);';
    document.body.appendChild(tip);
    return tip;
  }
  // dataset 值源自 data-l="{esc(lb)}"——浏览器解析属性时会
  //   把实体解码还原，写入时的 esc() 在此路径被抵消；tooltip 经 innerHTML 插入，
  //   故回填时必须再转义一次，避免潜在 XSS。
  function escHtml(s){
    return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
    });
  }
  function showTip(html, x, y){
    var t = ensureTip(); t.innerHTML = html; t.style.display = 'block';
    var w = t.offsetWidth, h = t.offsetHeight;
    t.style.left = Math.min(x + 14, window.innerWidth - w - 8) + 'px';
    t.style.top = ((y - h - 10 < 8) ? (y + 16) : (y - h - 10)) + 'px';
  }
  function hideTip(){ if (tip) tip.style.display = 'none'; }
  function toSvg(svg, e){
    var r = svg.getBoundingClientRect(), vb = svg.viewBox && svg.viewBox.baseVal;
    var vw = (vb && vb.width) || r.width, vh = (vb && vb.height) || r.height;
    return [(e.clientX - r.left) * vw / r.width, (e.clientY - r.top) * vh / r.height];
  }
  function nearest(pts, mx, my){
    var best = null, bd = 1e12;
    pts.forEach(function(p){ var dx = (+p.dataset.x) - mx, dy = (+p.dataset.y) - my, d = dx*dx + dy*dy*4; if (d < bd){ bd = d; best = p; } });
    return bd < 1e5 ? best : null;
  }
  function bindInteractions(){
    document.querySelectorAll('.chart').forEach(function(box){
      if (box.dataset.bound) return; box.dataset.bound = '1';
      var svg = box.querySelector('svg'); if (!svg) return;
      var pts = [].slice.call(svg.querySelectorAll('.jpt')); if (!pts.length) return;
      var hl = null;
      function reset(){ if (hl){ hl.setAttribute('r', hl.dataset.r0); if (hl.dataset.fill) hl.setAttribute('fill', hl.dataset.fill); hl = null; } }
      svg.addEventListener('mousemove', function(e){
        var m = toSvg(svg, e), best = nearest(pts, m[0], m[1]);
        if (best){
          if (hl && hl !== best) reset();
          if (!hl || hl !== best){
            hl = best;
            if (!hl.dataset.fill) hl.dataset.fill = hl.getAttribute('fill');
            hl.setAttribute('r', (+hl.dataset.r0) + 2.5);
            if (hl.dataset.fill === 'transparent') hl.setAttribute('fill', '#38BDF8');
          }
          showTip('<b style="color:#F8FAFC">' + escHtml(best.dataset.l) + '</b><br>' + escHtml(best.dataset.v) + escHtml(box.dataset.unit || ''), e.clientX, e.clientY);
        } else { reset(); hideTip(); }
      });
      svg.addEventListener('mouseleave', function(){ reset(); hideTip(); });
      svg.addEventListener('click', function(e){
        var m = toSvg(svg, e), best = nearest(pts, m[0], m[1]);
        if (best){
          showTip('<b style="color:#F8FAFC">' + escHtml(best.dataset.l) + '</b>（已锁定）<br>' + escHtml(best.dataset.v) + escHtml(box.dataset.unit || ''), e.clientX, e.clientY);
          setTimeout(hideTip, 2000);
        }
      });
    });
    document.querySelectorAll('.jseg').forEach(function(p){
      if (p.dataset.bound) return; p.dataset.bound = '1';
      var c1 = document.getElementById(p.dataset.c1), c2 = document.getElementById(p.dataset.c2);
      // 关键修复：将联动范围限定到所属时间窗口，避免「3/7/30天」悬停时错误高亮「昨天」的表格
      var scope = p.closest('section.window') || document;
      function on(){
        p.setAttribute('stroke-width', 30);
        p.style.filter = 'drop-shadow(0 0 6px rgba(56,189,248,.55))';
        if (c1) c1.textContent = p.dataset.name;
        if (c2) c2.textContent = p.dataset.pct + '% · ' + p.dataset.extra;
        var row = scope.querySelector('.jrow[data-name="' + p.dataset.name + '"]');
        if (row) row.style.background = 'rgba(56,189,248,.12)';
      }
      function off(){
        p.setAttribute('stroke-width', 24);
        p.style.filter = '';
        if (c1) c1.textContent = p.dataset.def1;
        if (c2) c2.textContent = p.dataset.def2;
        var row = scope.querySelector('.jrow[data-name="' + p.dataset.name + '"]');
        if (row) row.style.background = '';
      }
      p.addEventListener('mouseenter', on);
      p.addEventListener('mouseleave', off);
      p.addEventListener('focus', on);
      p.addEventListener('blur', off);
      p.addEventListener('click', function(){ on(); setTimeout(off, 1800); });
      p.addEventListener('keydown', function(e){ if (e.key==='Enter' || e.key===' '){ e.preventDefault(); on(); setTimeout(off, 1800); } });
    });
    document.querySelectorAll('.jrow').forEach(function(row){
      // 同样限定到所属窗口，确保表格行↔环形图扇区双向联动互不串扰
      var scope = row.closest('section.window') || document;
      var seg = scope.querySelector('.jseg[data-name="' + row.dataset.name + '"]');
      if (!seg) return;
      row.addEventListener('mouseenter', function(){ seg.dispatchEvent(new Event('mouseenter')); });
      row.addEventListener('mouseleave', function(){ seg.dispatchEvent(new Event('mouseleave')); });
    });
  }

  /* ---------- 展开全部 / 收起全部（窗口级 + 全局）---------- */
  function setAllDetails(scope, open){
    scope.querySelectorAll('details.expander').forEach(function(d){ d.open = !!open; });
  }
  function refreshEa(btn){
    if (!btn) return;
    var win = (btn.getAttribute('data-win') === 'all') ? document : btn.closest('.window');
    if (!win) return;
    var ds = win.querySelectorAll('details.expander');
    var closed = win.querySelectorAll('details.expander:not([open])').length;
    var allOpen = (ds.length > 0 && closed === 0);
    btn.classList.toggle('is-open', allOpen);
    btn.setAttribute('aria-pressed', allOpen ? 'true' : 'false');
    var txt = btn.querySelector('.ea-txt');
    if (txt) txt.textContent = allOpen ? '收起全部明细' : '展开全部明细';
    var cnt = btn.querySelector('[data-ea-count]');
    if (cnt) cnt.textContent = ds.length + ' 个模块可展开';
  }
  function wireExpandAll(){
    document.querySelectorAll('.expand-all').forEach(function(btn){
      refreshEa(btn);
      btn.addEventListener('click', function(){
        var win = (btn.getAttribute('data-win') === 'all') ? document : btn.closest('.window');
        if (!win) return;
        var hasClosed = win.querySelector('details.expander:not([open])');
        setAllDetails(win, !!hasClosed);
        refreshEa(btn);
      });
    });
    // 单个折叠被切换时，同步刷新所属窗口与全局按钮状态
    document.querySelectorAll('details.expander').forEach(function(d){
      d.addEventListener('toggle', function(){
        var win = d.closest('.window');
        if (win) refreshEa(win.querySelector('.expand-all'));
        refreshEa(document.querySelector('.expand-all[data-win="all"]'));
      });
    });
  }

  /* ---------- 启动 ---------- */
  function init(){
    if (ioReveal) document.querySelectorAll('.reveal').forEach(function(el){ ioReveal.observe(el); });
    bindInteractions();
    wireExpandAll();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
"""

# ---------------- 页面骨架 ----------------

CSS = """/* =============================================================
   暗色立体磨砂玻璃主题（Apple 极简 + 莱茵生命科研风）
   变量 · 排版 · 卡片 · 磨砂玻璃 · 图表 · 响应式 · 降级
   ============================================================= */
:root{
  --bg:#0B1220; --bg-deep:#060B16;
  --surface:rgba(22,33,58,.62); --surface-2:rgba(15,23,42,.72); --surface-3:rgba(28,40,68,.55);
  --line:rgba(148,163,184,.14); --line-2:rgba(148,163,184,.22); --line-3:rgba(148,163,184,.32);
  --text:#F8FAFC; --text-2:#E2E8F0; --text-3:#CBD5E1; --muted:#94A3B8; --muted-2:#7B8794;
  --accent:#38BDF8; --accent-ink:#7DD3FC; --accent-deep:#0EA5E9; --accent-soft:rgba(56,189,248,.14);
  --red:#F87171; --red-soft:rgba(248,113,113,.14);
  --amber:#FBBF24; --amber-soft:rgba(251,191,36,.14);
  --green:#34D399; --green-soft:rgba(52,211,153,.14);
  --violet:#A78BFA;
  --r:18px; --r-sm:12px; --r-lg:24px;
  --shadow-sm:0 1px 0 rgba(255,255,255,.04) inset,0 2px 8px -2px rgba(0,0,0,.4);
  --shadow:0 1px 0 rgba(255,255,255,.05) inset,0 20px 50px -20px rgba(0,0,0,.7),0 0 0 1px rgba(255,255,255,.02) inset;
  --shadow-lg:0 1px 0 rgba(255,255,255,.06) inset,0 30px 80px -24px rgba(0,0,0,.85),0 0 0 1px rgba(255,255,255,.03) inset;
  --ease:cubic-bezier(.4,0,.2,1); --dur:240ms; --dur-lg:340ms;
  --sans:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI Variable Text","Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  --mono:ui-monospace,SFMono-Regular,"SF Mono","JetBrains Mono",Menlo,Consolas,"Liberation Mono",monospace;
  --nav-w:272px;
  --z-skip:100; --z-toggle:60; --z-sidenav:50; --z-scrim:40; --z-tip:999;
}
*,*::before,*::after{box-sizing:border-box}
html{scroll-behavior:smooth;scroll-padding-top:20px;-webkit-text-size-adjust:100%;background-color:var(--bg-deep);background:
    radial-gradient(1200px 800px at 8% -8%,rgba(56,189,248,.10),transparent 60%),
    radial-gradient(1100px 700px at 100% 100%,rgba(139,92,246,.08),transparent 55%),
    radial-gradient(700px 500px at 50% 50%,rgba(20,184,166,.04),transparent 70%),
    linear-gradient(180deg,var(--bg-deep) 0%,var(--bg) 60%,#0E1729 100%);
  background-attachment:fixed}
body{margin:0;color:var(--text-2);font-family:var(--sans);font-size:15px;line-height:1.78;letter-spacing:.01em;min-height:100vh;background:transparent}
body::before{content:"";position:fixed;inset:0;z-index:0;pointer-events:none;
  background-image:linear-gradient(to right,rgba(148,163,184,.025) 1px,transparent 1px),linear-gradient(to bottom,rgba(148,163,184,.025) 1px,transparent 1px);
  background-size:32px 32px}
.mono{font-family:var(--mono);font-variant-numeric:tabular-nums}
.skip{position:absolute;left:-9999px;top:0;z-index:var(--z-skip);background:var(--bg);color:var(--accent-ink);border:1px solid var(--line-2);border-radius:8px;padding:10px 16px;font-size:13px}
.skip:focus{left:14px;top:14px}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:6px}

.layout{position:relative;display:grid;grid-template-columns:var(--nav-w) minmax(0,1fr);max-width:1440px;margin:0 auto;min-height:100vh}

.sidenav{position:sticky;top:0;align-self:start;height:100vh;overflow-y:auto;padding:22px 14px 36px;border-right:1px solid var(--line);
  background:linear-gradient(180deg,#0B1220 0%,#0A1018 100%)}
.brand{padding:6px 12px 16px;border-bottom:1px solid var(--line);margin-bottom:14px}
.brand .bt{font:650 14px/1.4 var(--sans);color:var(--text);letter-spacing:.02em}
.brand .bs{font:500 11px/1.5 var(--mono);color:var(--muted);margin-top:4px}
.nav-t{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:9px 12px;border-radius:10px;color:var(--text-3);text-decoration:none;font-size:13.5px;font-weight:600;transition:background var(--dur) var(--ease),color var(--dur) var(--ease)}
.nav-t:hover{background:var(--accent-soft);color:var(--text)}
.nav-t.is-active{background:var(--accent-soft);color:var(--accent-ink);box-shadow:0 0 0 1px var(--line-2) inset}
.nav-t .d{font:500 10.5px/1 var(--mono);color:var(--muted-2)}
.nav-sub{list-style:none;margin:2px 0 8px 14px;padding:0 0 0 10px;border-left:1px solid var(--line-2)}
.nav-sub a{display:flex;align-items:center;min-height:30px;padding:3px 10px;border-radius:8px;color:var(--muted);text-decoration:none;font-size:12.5px;transition:background var(--dur) var(--ease),color var(--dur) var(--ease)}
.nav-sub a:hover{background:var(--accent-soft);color:var(--accent-ink)}
.nav-sub a.is-active{background:var(--accent-soft);color:var(--accent-ink);font-weight:600}
.nav-foot{padding:14px 12px 0;border-top:1px solid var(--line);color:var(--muted-2);font-size:11.5px;margin-top:10px;line-height:1.6}

.navtoggle{display:none;position:fixed;left:14px;top:14px;z-index:var(--z-toggle);width:44px;height:44px;align-items:center;justify-content:center;border:1px solid var(--line-2);border-radius:12px;background:rgba(11,18,32,.78);backdrop-filter:blur(14px);cursor:pointer;box-shadow:var(--shadow-sm)}
.scrim{position:fixed;inset:0;z-index:var(--z-scrim);background:rgba(6,11,22,.55);backdrop-filter:blur(2px);opacity:0;pointer-events:none;transition:opacity var(--dur) var(--ease)}
body.nav-open .scrim{opacity:1;pointer-events:auto}

.main{min-width:0;padding:26px 36px 100px;position:relative;z-index:1}

.hero{position:relative;overflow:hidden;background:linear-gradient(180deg,#0F172A 0%,#0B1220 100%);border:1px solid var(--line);border-radius:var(--r-lg);box-shadow:var(--shadow-lg);padding:30px 32px;margin-bottom:24px}
.hero::before{content:"";position:absolute;left:0;right:0;top:0;height:1px;background:linear-gradient(90deg,transparent,rgba(56,189,248,.4),transparent);pointer-events:none}
.hero::after{content:"";position:absolute;right:-80px;top:-80px;width:280px;height:280px;background:radial-gradient(circle,rgba(56,189,248,.18),transparent 62%);pointer-events:none}
.eyebrow{font:600 11px/1 var(--mono);letter-spacing:.16em;text-transform:uppercase;color:var(--accent-ink);margin-bottom:10px}
h1{font-size:28px;line-height:1.3;letter-spacing:-.01em;color:var(--text);margin:0 0 10px;font-weight:650;text-shadow:0 1px 24px rgba(56,189,248,.18)}
.meta{color:var(--muted);font-size:12.5px}

.window{position:relative;background:linear-gradient(180deg,#0F172A 0%,#0B1220 100%);border:1px solid var(--line);border-radius:var(--r-lg);box-shadow:var(--shadow);padding:28px 30px;margin:0 0 22px;scroll-margin-top:20px;transition:box-shadow var(--dur-lg) var(--ease),border-color var(--dur) var(--ease),transform var(--dur-lg) var(--ease)}
.window::before{content:"";position:absolute;left:0;right:0;top:0;height:1px;background:linear-gradient(90deg,transparent,rgba(56,189,248,.3),transparent);pointer-events:none}
.window:hover{box-shadow:0 1px 0 rgba(255,255,255,.05) inset,0 30px 70px -24px rgba(0,0,0,.85);border-color:var(--line-2)}
h2{display:flex;align-items:center;gap:10px;font-size:20px;color:var(--text);margin:0 0 6px;font-weight:650}
h2 .idx{font:600 12px/1 var(--mono);color:var(--accent-ink);background:var(--accent-soft);border:1px solid var(--line-2);border-radius:7px;padding:5px 9px;letter-spacing:.04em}
h3{font-size:15.5px;color:var(--text);margin:30px 0 10px;padding-top:18px;border-top:1px solid var(--line);scroll-margin-top:20px;font-weight:650}
h3:first-of-type{border-top:0;padding-top:0;margin-top:22px}
h4{font-size:12.5px;color:var(--muted);margin:0 0 8px;font-weight:600;letter-spacing:.02em}
.sub{color:var(--muted);font-size:13px;margin:0 0 16px;padding-left:14px;border-left:2px solid var(--line-2)}

.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:16px 0}
.kvi{background:var(--surface-2);backdrop-filter:blur(8px) saturate(140%);-webkit-backdrop-filter:blur(8px) saturate(140%);border:1px solid var(--line);border-radius:var(--r-sm);padding:13px 16px;transition:transform var(--dur) var(--ease),border-color var(--dur) var(--ease),box-shadow var(--dur) var(--ease),background var(--dur) var(--ease)}
.kvi:hover{transform:translateY(-4px);border-color:var(--accent);background:linear-gradient(180deg,rgba(56,189,248,.12),var(--surface-2));box-shadow:0 18px 42px -16px rgba(56,189,248,.5),0 1px 0 rgba(255,255,255,.07) inset;backdrop-filter:blur(16px) saturate(170%);-webkit-backdrop-filter:blur(16px) saturate(170%)}
.kvi:hover .v{text-shadow:0 0 18px rgba(56,189,248,.45)}
.k{color:var(--muted);font-size:12px;letter-spacing:.02em}
.kvi .v{font:600 19px/1.3 var(--mono);font-variant-numeric:tabular-nums;color:var(--text);margin:4px 0 2px;text-shadow:0 0 12px rgba(56,189,248,.15)}
.kvi .n{color:var(--muted-2);font-size:11.5px}
.grid2{display:grid;grid-template-columns:1fr 1.15fr;gap:16px;margin:16px 0;align-items:start}

.chart{background:var(--surface-2);backdrop-filter:blur(10px) saturate(140%);-webkit-backdrop-filter:blur(10px) saturate(140%);border:1px solid var(--line);border-radius:var(--r-sm);padding:14px;margin:14px 0;transition:border-color var(--dur) var(--ease),box-shadow var(--dur) var(--ease),transform var(--dur) var(--ease)}
.chart:hover{border-color:var(--line-2);box-shadow:var(--shadow);transform:translateY(-2px)}
.chart svg{width:100%;height:auto;display:block;min-height:160px}
.donut-box svg{max-width:280px;margin:0 auto;display:block}
.jseg{transition:stroke-width .18s var(--ease),filter .18s var(--ease);cursor:pointer}
.jseg:focus,.jseg:focus-visible{outline:none;stroke-opacity:1;filter:drop-shadow(0 0 8px rgba(56,189,248,.55))}

table{width:100%;border-collapse:collapse;margin:12px 0;font-size:13.5px}
th{background:var(--surface-2);color:var(--muted);text-align:left;padding:10px 12px;font-weight:600;font-size:12px;letter-spacing:.03em;border-bottom:1px solid var(--line-2);font-variant-numeric:tabular-nums}
td{padding:10px 12px;border-bottom:1px solid var(--line);color:var(--text-3);font-variant-numeric:tabular-nums}
tbody tr{transition:background var(--dur) var(--ease)}
tbody tr:hover{background:rgba(56,189,248,.06)}
.jrow{cursor:pointer;transition:background var(--dur) var(--ease)}
.jrow:hover{background:var(--accent-soft)!important}
.ok{color:var(--green);font-weight:600}
.bad{color:var(--red);font-weight:600}

.flag{display:flex;gap:12px;align-items:flex-start;padding:14px 16px;margin:10px 0;border-left:3px solid;border-right:1px solid var(--line);border-top:1px solid var(--line);border-bottom:1px solid var(--line);border-radius:0 var(--r-sm) var(--r-sm) 0;font-size:14px;background:var(--surface-2);transition:transform var(--dur) var(--ease),box-shadow var(--dur) var(--ease)}
.flag:hover{transform:translateX(4px);box-shadow:0 10px 26px -12px rgba(56,189,248,.4)}
.flag .tag{flex:0 0 auto;font:600 11px/1 var(--sans);border:1px solid;border-radius:999px;padding:5px 11px;margin-top:2px;background:rgba(11,18,32,.4)}
.note{color:var(--muted);font-size:13.5px;background:var(--surface-2);backdrop-filter:blur(8px) saturate(140%);-webkit-backdrop-filter:blur(8px) saturate(140%);border:1px solid var(--line);border-left:3px solid var(--line-2);border-radius:var(--r-sm);padding:12px 15px;margin:14px 0;transition:transform var(--dur) var(--ease),border-color var(--dur) var(--ease),box-shadow var(--dur) var(--ease),background var(--dur) var(--ease)}
.note:hover{transform:translateY(-2px);border-color:var(--line-2);background:linear-gradient(180deg,rgba(56,189,248,.08),var(--surface-2));box-shadow:0 12px 30px -14px rgba(56,189,248,.4)}
.warnbox{background:rgba(251,191,36,.10);border:1px solid rgba(251,191,36,.32);color:#FDE68A;border-radius:var(--r-sm);padding:15px 18px;margin:14px 0;font-size:13.5px;line-height:1.7;transition:transform var(--dur) var(--ease),box-shadow var(--dur) var(--ease),border-color var(--dur) var(--ease)}
.warnbox:hover{transform:translateY(-2px);border-color:rgba(251,191,36,.55);box-shadow:0 12px 30px -14px rgba(251,191,36,.35)}
.scorebox{display:flex;flex-direction:column;gap:6px;background:var(--surface-2);backdrop-filter:blur(10px) saturate(140%);-webkit-backdrop-filter:blur(10px) saturate(140%);border:1px solid var(--line);border-radius:var(--r);padding:20px 22px;margin:14px 0;box-shadow:var(--shadow-sm);transition:transform var(--dur) var(--ease),box-shadow var(--dur) var(--ease),border-color var(--dur) var(--ease)}
.scorebox:hover{transform:translateY(-3px);border-color:var(--line-2);box-shadow:0 18px 44px -18px rgba(56,189,248,.4)}
.scorebox .sv{font:700 44px/1 var(--mono);font-variant-numeric:tabular-nums;text-shadow:0 0 16px currentColor}
.scorebox .su{color:var(--muted);font:500 15px/1 var(--mono);margin-left:6px}
.scorebox .sg{font-size:12.5px;border:1px solid;border-radius:999px;padding:4px 12px;margin-left:12px;background:rgba(11,18,32,.4)}
.scorebox .sd{color:var(--muted);font-size:12.5px;margin-top:6px}
code{background:var(--surface-2);border:1px solid var(--line);padding:1.5px 6px;border-radius:5px;font:500 12px/1.6 var(--mono);color:var(--accent-ink)}

footer{margin-top:36px;padding:22px 0 4px;border-top:1px solid var(--line);color:var(--muted);font-size:12.5px}
.disclaimer{color:var(--amber);font-size:12.5px;margin:10px 0 0}

.reveal{opacity:0;transform:translateY(14px);transition:opacity var(--dur-lg) var(--ease),transform var(--dur-lg) var(--ease)}
.reveal.is-in{opacity:1;transform:none}


.module-btns{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 22px;padding:14px;background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-sm);backdrop-filter:blur(8px)}
.module-btns a{padding:7px 13px;border:1px solid var(--line);border-radius:999px;color:var(--text-3);text-decoration:none;font-size:12.5px;transition:all var(--dur) var(--ease)}
.module-btns a:hover{background:var(--accent-soft);color:var(--accent-ink);border-color:var(--accent);box-shadow:0 8px 22px -10px rgba(56,189,248,.45)}
.module-btns a.is-active{background:var(--accent-soft);color:var(--accent-ink);border-color:var(--accent);box-shadow:0 0 0 1px var(--accent) inset,0 0 16px rgba(56,189,248,.25)}
/* ---------- 折叠层（details/summary）· 显眼可点击触发条 ---------- */
details.expander{position:relative;background:var(--surface-2);backdrop-filter:blur(10px);border:1px solid var(--line);border-radius:var(--r-sm);padding:14px 18px;margin:16px 0;transition:border-color var(--dur) var(--ease),box-shadow var(--dur) var(--ease)}
details.expander:hover{border-color:var(--line-2)}
/* 触发条：满宽、强调色描边 + 渐变底，与普通文本形成强对比，明确可点击 */
details.expander>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:12px;
  margin:-14px -18px 0;padding:15px 18px;
  background:linear-gradient(180deg,var(--accent-soft),rgba(56,189,248,.06));
  border:1px solid var(--accent);border-radius:var(--r-sm);
  color:var(--accent-ink);font:600 14px/1.4 var(--sans);
  user-select:none;outline:none;
  transition:background var(--dur) var(--ease),box-shadow var(--dur) var(--ease),transform var(--dur) var(--ease),border-color var(--dur) var(--ease)}
details.expander>summary::-webkit-details-marker{display:none}
details.expander>summary:hover{background:linear-gradient(180deg,rgba(56,189,248,.24),rgba(56,189,248,.12));border-color:var(--accent);box-shadow:0 8px 26px -10px rgba(56,189,248,.55);transform:translateY(-1px)}
details.expander>summary:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
details.expander[open]>summary{margin:-14px -18px 14px;background:var(--accent-soft);border-color:var(--line-2);box-shadow:none;transform:none}
/* 左侧三角箭头（未展开指向右 ▶，展开指向下 ▼） */
.ex-ico{width:0;height:0;border-left:7px solid var(--accent-ink);border-top:5px solid transparent;border-bottom:5px solid transparent;flex:0 0 auto;transition:transform var(--dur) var(--ease)}
details.expander[open]>summary .ex-ico{transform:rotate(90deg)}
.ex-txt{flex:1 1 auto;min-width:0;letter-spacing:.01em}
.ex-meta{display:flex;align-items:center;gap:10px;flex:0 0 auto}
.ex-count{font:600 11.5px/1 var(--sans);color:#042033;background:var(--accent);padding:4px 9px;border-radius:999px;white-space:nowrap}
.ex-act{font:600 12px/1 var(--sans);color:var(--accent-ink);white-space:nowrap;opacity:.92}
.ex-opened{display:none}
details.expander[open]>summary .ex-opened{display:inline}
details.expander[open]>summary .ex-closed{display:none}
details.expander>.chart:first-child,details.expander>.kv:first-child{margin-top:4px}

/* ---------- 主展开/收起全部（窗口级 + 全局）---------- */
.expand-all{display:inline-flex;align-items:center;gap:10px;margin:16px 0 2px;padding:12px 18px;border-radius:var(--r-sm);cursor:pointer;
  font:600 14px/1 var(--sans);color:var(--accent-ink);background:var(--accent-soft);
  border:1px dashed var(--accent);
  transition:background var(--dur) var(--ease),border-color var(--dur) var(--ease),box-shadow var(--dur) var(--ease),transform var(--dur) var(--ease);outline:none}
.expand-all:hover{background:rgba(56,189,248,.2);border-color:var(--accent);border-style:solid;box-shadow:0 8px 26px -10px rgba(56,189,248,.5);transform:translateY(-1px)}
.expand-all:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
.expand-all .ea-ico{width:0;height:0;border-left:7px solid var(--accent-ink);border-top:5px solid transparent;border-bottom:5px solid transparent;transition:transform var(--dur) var(--ease)}
.expand-all.is-open .ea-ico{transform:rotate(90deg)}
.expand-all .ea-hint{margin-left:auto;font:500 11.5px/1 var(--mono);color:var(--muted);white-space:nowrap}
@media(max-width:560px){.expand-all{width:100%;flex-wrap:wrap}.expand-all .ea-hint{margin-left:0;width:100%;margin-top:2px}}

@media(max-width:1024px){
  .layout{grid-template-columns:1fr}
  .sidenav{position:fixed;left:0;top:0;width:284px;height:100vh;z-index:var(--z-sidenav);transform:translateX(-100%);transition:transform var(--dur) var(--ease);box-shadow:0 24px 60px -12px rgba(0,0,0,.8)}
  body.nav-open .sidenav{transform:none}
  .navtoggle{display:flex}
  .main{padding:66px 16px 88px}
  .hero{padding:24px 22px}
  .window{padding:22px 18px}
  .grid2{grid-template-columns:1fr}
  .nav-sub a{min-height:40px;font-size:13px}
  h1{font-size:23px}
}
@media(max-width:560px){
  .kv{grid-template-columns:1fr 1fr;gap:10px}
  .kvi .v{font-size:17px}
  table{font-size:12.5px}
  th,td{padding:8px 9px}
  .module-btns{padding:10px}
}

@media (prefers-reduced-motion: reduce){
  html{scroll-behavior:auto}
  *,*::before,*::after{animation-duration:.001ms!important;animation-iteration-count:1!important;transition-duration:.001ms!important}
  .reveal{opacity:1!important;transform:none!important}
  .flag:hover,.kvi:hover{transform:none}
}
@media print{
  .sidenav,.navtoggle,.scrim{display:none!important}
  .layout{grid-template-columns:1fr}
  /* L1 修复（2026-09-15）：原只把 body 背景改白，未同步文字/边框色——暗色主题的
     浅色文字在白底上几乎不可见（页脚、正文级说明尤甚）。此处统一改为深色文字。 */
  body{background:#fff;color:#111827}
  .window,.hero{box-shadow:none;break-inside:avoid}
  footer,.note,.sd,.sub,h4,th,td,.meta,.k,.brand .bs,.nn,.expand-all .ea-hint{color:#374151!important}
  .kv,.kvi,.tile,.scorebox,.flag,.note,.module-btns,details.expander{background:#fff!important;border-color:#d1d5db!important;box-shadow:none!important}
  th{background:#f3f4f6!important;color:#111827!important}
}
"""

_MODULES = ['一、健康建议', '二、睡眠分析', '三、心率分析', '四、运动与活动', '五、压力与恢复', '六、体重变化']

def _summary_for_module(idx, mid, S, part_html):
    """从六模块 HTML 中提取每个模块的红色/黄色/医药评分/提示"""
    # 原按裸色值 part_html.count("border-left-color:#B91C1C") 计数，
    #   会把各模块「核心摘要条」（<p class="note" style="…border-left-color:#B91C1C">）一并计入，
    #   使首页 tile 的「X 需关注 / Y 待改善」虚高 1。改为只统计 flag() 产出的旗子——
    #   flag() 渲染为 <div class="flag" style="border-left-color:#B91C1C;background:…">，
    #   故以「class="flag" + 对应色值」为前缀精确匹配；不含 info 蓝(#0E7490)/绿(#047857)/摘要条。
    #   （注：勿仅统计 class="flag"——那会把 info 级蓝色旗子也算进去。）
    red = part_html.count('class="flag" style="border-left-color:#B91C1C')
    amber = part_html.count('class="flag" style="border-left-color:#B45309')
    score = None; hint = ""
    if mid == 2:
        m = re.search(r'class="sv"[^>]*>(\d+)</span>', part_html)
        if m: score = int(m.group(1))
        h_total = S.get("sleep_avg_total") or 0
        hint = f"日均 {h_total/60:.1f}h · 医学评分 {score}/100" if score else (f"日均 {h_total/60:.1f}h" if h_total else "医学评分")
    elif mid == 1:
        hint = f"{red} 需关注 / {amber} 待改善"
    elif mid == 3:
        v = S.get("hr_rest") or 0
        hint = f"静息 {v:.0f} bpm" if v else "心率概览"
    elif mid == 4:
        v = S.get("steps_avg") or 0
        hint = f"日均 {v:.0f} 步" if v else "活动概览"
    elif mid == 5:
        v = S.get("stress_avg") or 0
        hint = f"日均压力 {v:.0f}" if v else "压力概览"
    elif mid == 6:
        wt = S.get("weight_win") or []
        hint = (f"窗口内 {len(wt)} 次测量" if wt else "本窗口无测量")
    return {"mid": mid, "name": _MODULES[mid-1].split("、",1)[1], "red": red, "amber": amber, "score": score, "hint": hint}

def render_window(idx, title, dates, S):
    """拆分为 head + body + 6 个模块段，便于按模块拼接与独立渲染。"""
    advice = render_advice(S)
    sleep = render_sleep(S)
    heart = render_heart(S)
    activity = render_activity(S)
    stress = render_stress(S)
    weight = render_weight(S)
    parts = [advice, sleep, heart, activity, stress, weight]
    head = (f'<header class="hero reveal"><div class="eyebrow">窗口 · W{idx}</div>'
            f'<h2><span class="idx mono">W{idx}</span>{esc(title)}</h2>'
            f'<p class="sub">📅 分析日期区间：<b><span class="mono">{esc(S["range"])}</span></b>'
            f'（共 {len(dates)} 天；锚点日 {DATA_END_FMT}）</p>'
            f'<button type="button" class="expand-all" data-win="w{idx}" aria-pressed="false">'
            f'<span class="ea-ico" aria-hidden="true"></span>'
            f'<span class="ea-txt">展开全部明细</span>'
            f'<span class="ea-hint mono" data-ea-count></span></button></header>')
    # 无数据窗口提示条（口径 a：DATA_END 锚定不变，仅补显式提示）
    win_has_data = bool(S["sport_days"] or S["sleep_days"] or S["hr_days"]) or S["spo2_avg"] is not None
    if not win_has_data:
        ldd = S.get("latest_data_day")
        # date 列未必是 8 位 YYYYMMDD（可能为带连字符 TEXT），
        #   切片前校验避免拼出乱码日期；不合规时原样展示。
        if ldd and len(ldd) == 8 and ldd.isdigit():
            ldd_fmt = f"{ldd[:4]}-{ldd[4:6]}-{ldd[6:]}"
        else:
            ldd_fmt = str(ldd) if ldd else ""
        # ldd_fmt 来自 DB 的 MAX(date)，上面 8 位数字
        #   分支已保证安全；但 `str(ldd)` 兜底分支会把原始值直接插进 HTML。此处统一过
        #   esc()，与文件内其它所有 DB 文本值的处理保持一致。
        ldd_txt = f"（全库最新数据日：<b>{esc(ldd_fmt)}</b>）" if ldd_fmt else ""
        # 端到端回归修复 BUG-2：原提示一律写"没有导出数据…导出新数据后
        #   重新生成报告即可"，但最常见的情形其实是**数据源选错了**——跑过
        #   split_db_by_day.py 后 DB/<锚点日>/ 存在，生成器会优先用它（只含那一天），
        #   而四个窗口都在该日之前，于是一份 200KB 的完整报告会缩成 45KB 全空报告，
        #   用户按提示去"重新导出"纯属白折腾（实测有 234 天数据仍如此提示）。
        #   现按数据源区分：用单日库时给出**指向真正原因**的提示。
        if _SRC_IS_COMBINED:
            _hint = "各模块显示「—」属正常现象；导出新数据后重新生成报告即可。"
        else:
            # 盲审 P1-1：单日库现仅在显式 --single-day 模式（或 combined 缺失的
            #   罕见兜底）下使用；旧措辞"锚定到没有日库的日期"在拆分后不可行
            #   （没有日库的日期即没有数据的日期），已删除。
            _hint = (
                f'本次报告使用<b>--single-day 单日库模式</b>（数据源 {esc(_DATED_FOLDER)}，只含该日数据），'
                f'而四个时间窗口都落在该日之前，因此为空——<b>这不是数据缺失</b>。'
                f'需要完整的四窗口报告时，去掉 <span class="mono">--single-day</span> 参数'
                f'重新运行即可（默认使用 <span class="mono">DB\\combined\\</span> 全量库）。'
            )
        head += (f'<p class="note" style="margin:8px 0 14px;border-left-color:#F59E0B;'
                 f'background:rgba(245,158,11,.08)">⚠️ <b>该窗口日期范围内没有数据</b>{ldd_txt}。'
                 f'{_hint}</p>')
    # 给每个模块第一个 h3 加 id
    tagged = []
    for i, p in enumerate(parts, 1):
        p = re.sub(r'<h3>', f'<h3 id="w{idx}-m{i}">', p, count=1)
        tagged.append(p)
    body = "\n".join(tagged)
    modules = [_summary_for_module(idx, i+1, S, tagged[i]) for i in range(6)]
    modules_full = []
    for i, p in enumerate(tagged, 1):
        h = re.search(rf'<h3 id="w{idx}-m{i}">.*?</h3>', p, re.S)
        h_html = h.group(0) if h else f'<h3 id="w{idx}-m{i}">{_MODULES[i-1]}</h3>'
        body_part = p.replace(h_html, "", 1) if h else p
        sm = modules[i-1]
        modules_full.append({"mid": i, "name": _MODULES[i-1].split("、",1)[1], "red": sm["red"], "amber": sm["amber"], "score": sm["score"], "hint": sm["hint"], "html": h_html + body_part})
    return head, body, modules, modules_full

def main():
    if not os.path.exists(DB_PATH):
        raise SystemExit(
            f"[数据缺失] 未找到解密数据库：{DB_PATH}\n"
            f"请先将当天的解密数据库 database_decrypted.db 放入该目录\n"
            f"（参考手册 5.4 节「数据源优先级」：把新导出的 .db 复制到 {os.path.join(BASE, 'DB', 'combined')}），\n"
            f"再运行本生成器。\n"
            f"若需指定历史日期，可用：OPPO_DATA_DATE=YYYY-MM-DD python generate_html_report.py\n"
            f"或：python generate_html_report.py YYYY-MM-DD")

    # R1修复：库文件存在但「0 张表」时提前拦下并给出醒目警告。
    #   背景：解密失败 / 导出中断会留下 0 字节或空壳库，此时生成器仍会正常跑完，
    #   产出一份四窗口全是"该窗口日期范围内没有导出数据"的"空报告"——
    #   用户极易误判成"最近确实没数据"，而真实原因是上游解密挂了。
    #   此处把「空库」与「真的没数据」区分开，并直接给出上游排查方向。
    try:
        _probe = sqlite3.connect(DB_PATH)
        try:
            _ntab = _probe.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        finally:
            _probe.close()
    except sqlite3.DatabaseError as e:
        raise SystemExit(
            f"[数据库损坏] 无法读取：{DB_PATH}\n"
            f"  错误：{e}\n"
            f"  该文件不是有效的 SQLite 数据库，通常是导出中断留下的半截文件。\n"
            f"  请删除后重跑：python export_health_data.py")
    if _ntab == 0:
        _sz = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else -1
        print("=" * 70)
        print(f"⚠️  警告：数据库存在但没有任何表（{_ntab} 张），报告将全部为空！")
        print(f"    文件：{DB_PATH}")
        print(f"    大小：{_sz} 字节")
        print("    这通常是【解密失败或导出中断】留下的空壳，而不是「最近没数据」。")
        print("    请按序排查：")
        print("      1) 检查 DB\\combined\\db_key.txt —— 密钥不能是零值占位（32 个零+db_key 后缀共 38 字符；经实测无法解密正常登录账号的库）")
        print("      2) 重跑 python export_health_data.py 重新导出")
        print("      3) 确认 database_decrypted.db 大小不为 0（正常应为数 MB）")
        print("=" * 70)

    conn = sqlite3.connect(DB_PATH)
    # 连接关闭移入 finally（此前异常路径不关闭）
    try:
        cur = conn.cursor()
        # 明细表查询加窗口下界（锚点日-35 天 0 点，
        #   覆盖最大 30 天窗口 + 锚点日 + 余量），避免大库明细全量载入内存。
        #   naive 锚点日 0 点统一补 CN(UTC+8) 后换算毫秒，与全项目时区口径一致。
        _end_dt = datetime.strptime(DATA_END, "%Y%m%d").replace(tzinfo=CN)
        _floor_ms = int((_end_dt - timedelta(days=35)).timestamp() * 1000)
        hr_detail = q_hr_detail(cur, _floor_ms)
        stress_detail = q_stress_detail(cur, _floor_ms)
        sed_detail = q_sedentary(cur, _floor_ms)

        end = datetime.strptime(DATA_END, "%Y%m%d")
        # 窗口整体前移一天——原 rng(1) 返回 end 当天却被标为
        #   "昨天"，且当天数据通常未同步完整。现四窗口均不含 end 当天：
        #   "昨天"=end-1，"最近N天"=end-N ~ end-1。
        def rng(n):
            return [(end - timedelta(days=i + 1)).strftime("%Y%m%d") for i in range(n)][::-1]
        windows = [("昨天", rng(1)), ("最近3天", rng(3)), ("最近7天", rng(7)), ("最近30天", rng(30))]

        # 全库最新数据日（用于无数据窗口的提示条；取核心日汇总表 MAX(date) 的并集）
        latest_data_day = None
        try:
            _row = cur.execute(
                "SELECT MAX(m) FROM ("
                " SELECT MAX(date) AS m FROM DBSportDataStat WHERE sport_mode=-2"
                " UNION ALL SELECT MAX(date) AS m FROM DBSleepMainStat"
                " UNION ALL SELECT MAX(date) AS m FROM DBHeartRateDataStatTable"
                " UNION ALL SELECT MAX(date) AS m FROM DBBloodOxygenSaturationDataStat)"
            ).fetchone()
            if _row and _row[0]:
                latest_data_day = str(_row[0])
        except Exception:
            latest_data_day = None

        win_payloads = []
        for i, (title, dates) in enumerate(windows, 1):
            S = build_stats(cur, dates, hr_detail, stress_detail, sed_detail)
            S["latest_data_day"] = latest_data_day
            head, body, mods_summary, mods_full = render_window(i, title, dates, S)
            # 简要摘要：用于首页 tile
            brief = {
                "wid": i, "name": title, "range": S["range"], "n_days": len(dates),
                "modules": [{"mid": m["mid"], "name": m["name"], "red": m["red"], "amber": m["amber"],
                             "score": m["score"], "hint": m["hint"]} for m in mods_summary]
            }
            win_payloads.append({
                **brief,
                "head": head,
                "body": body,
                "modules": mods_full
            })
            n_r = sum(m["red"] for m in mods_summary); n_a = sum(m["amber"] for m in mods_summary)
            print(f"  ✅ {title}（{S['range']}）建议：{n_r} 需关注 / {n_a} 待改善")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    idxName = ['一','二','三','四','五','六']
    _esc = lambda s: s.replace("</script", "<\\/script")
    # 侧边栏导航（锚点跳转，单页面）
    nav_parts = ['<a class="nav-t" href="#overview" data-w="overview">总览</a>']
    for i, (title, dates) in enumerate(windows, 1):
        payload = win_payloads[i-1]
        nav_parts.append(
            '<div class="nav-group">'
            f'<a class="nav-t" href="#w{i}" data-w="w{i}">{esc(title)}'
            f'<span class="d mono">{esc(payload["range"])}</span></a>'
            '<ul class="nav-sub">')
        for mid in range(1, 7):
            mname = _MODULES[mid-1].split('、',1)[1]
            nav_parts.append(f'<li><a href="#w{i}-m{mid}" data-target="w{i}-m{mid}">{mname}</a></li>')
        nav_parts.append('</ul></div>')
    nav_html = "\n".join(nav_parts)
    # 数据最新日——V1.0 修复：原 meta 写死"数据截止 {DATA_END}"（锚点日），
    #   用旧快照补生成报告时会误导（声称有锚点日数据，实际数据止于更早）。
    #   现复用上方 latest_data_day（核心日汇总表 MAX(date) 并集）——诚实标注
    #   "数据最新日"与"报告锚点日"两个不同概念，解释"昨天为空"的原因。
    if latest_data_day and len(str(latest_data_day)) == 8 and str(latest_data_day).isdigit():
        _dl = str(latest_data_day)
        data_last_fmt = f"{_dl[:4]}-{_dl[4:6]}-{_dl[6:]}"
    elif latest_data_day:
        data_last_fmt = str(latest_data_day)
    else:
        data_last_fmt = "—"
    # 总览（精简：移除 4 个冗余 KVI，仅保留标题+meta+一句话简介）
    overview = (
        '<header class="hero reveal" id="overview"><div class="eyebrow">OPPO Health Report</div>'
        '<h1>OPPO 健康数据分析报告</h1>'
        # 页头显式标注**实际使用的数据源**。
        #   而两者产出的报告差异极大（200KB 全窗口 vs 45KB 全空），必须让人一眼可辨。
        f'<p class="meta">数据最新日 <b>{data_last_fmt}</b>　·　'
        f'数据源 <b class="mono">DB\\{esc(_DATED_FOLDER)}\\</b>'
        f'（{"全量合并库" if _SRC_IS_COMBINED else "⚠️ 单日库 --single-day"}）　·　'
        f'报告锚点日 {DATA_END_FMT}　·　生成 {datetime.now().strftime("%Y-%m-%d %H:%M")}　·　四时间窗口 × 六大模块</p>'
        '<p class="sub">左侧目录点击任意章节 / 模块即可平滑跳转；窄屏下点击左上角按钮展开目录。各模块按"核心摘要 + 折叠详情"分层展示，避免信息过载。医学睡眠评分依据 AASM / NSF 六维标准，设备端 sleep_score 因虚高不计入。</p>'
        '<button type="button" class="expand-all" data-win="all" aria-pressed="false">'
        '<span class="ea-ico" aria-hidden="true"></span>'
        '<span class="ea-txt">展开全部明细</span>'
        '<span class="ea-hint mono" data-ea-count></span></button>'
        '</header>')
    # 四窗口完整内容
    wins_html = []
    for i, (title, dates) in enumerate(windows, 1):
        payload = win_payloads[i-1]
        btns = "".join(
            f'<a href="#w{i}-m{mid}"><span class="mono">{idxName[mid-1]}</span> · {_MODULES[mid-1].split("、",1)[1]}</a>'
            for mid in range(1,7))
        wins_html.append(
            f'<section class="window reveal" id="w{i}">'
            + payload["head"]
            + f'<div class="module-btns">{btns}</div>'
            + payload["body"]
            + '</section>')
    main_html = overview + "\n".join(wins_html)
    footer_html = ('<footer><div>数据来源：OPPO 健康 App 本地数据库（解密导出）　·　分析口径见脚注</div>'
        '<div class="disclaimer">免责声明：本报告由个人健康数据自动生成，仅供自我管理与趋势参考，不构成任何医疗诊断或治疗建议。如有持续不适请咨询专业医师。</div></footer>')
    toggle_svg = ('<svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true" focusable="false">'
        '<path d="M3 5.5h14M3 10h14M3 14.5h14" stroke="#CBD5E1" stroke-width="1.6" stroke-linecap="round"/></svg>')
    doc = (
        '<!DOCTYPE html>\n<html lang="zh-CN"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        '<meta name="color-scheme" content="dark">\n'
        '<meta name="description" content="OPPO 健康数据网页版分析报告（单页面）：昨天/近3天/近7天/近30天 四窗口 × 健康建议·睡眠·心率·运动·压力·体重 六模块，含医学标准睡眠质量评分。暗色立体磨砂玻璃主题，纯静态无外部依赖。">\n'
        '<title>OPPO 健康数据分析报告（网页版）</title>\n<style>' + CSS + '</style></head><body>\n'
        '<a class="skip" href="#main">跳到主要内容</a>\n'
        f'<button class="navtoggle" id="navtoggle" type="button" aria-expanded="false" aria-controls="sidenav" aria-label="打开章节导航">{toggle_svg}</button>\n'
        '<div class="scrim" id="scrim" aria-hidden="true"></div>\n'
        '<div class="layout">\n'
        '<aside class="sidenav" id="sidenav" aria-label="报告章节导航">\n'
        '<div class="brand"><div class="bt">OPPO 健康数据分析</div>'
        f'<div class="bs">锚点日 {DATA_END_FMT}</div></div>\n'
        + nav_html + '\n'
        '<div class="nav-foot">OPPO Health Report · V1.1<br>评分依据 AASM / NSF 医学标准</div>\n'
        '</aside>\n'
        '<main class="main" id="main">\n' + main_html + '\n' + footer_html + '\n</main>\n'
        '</div>\n'
        + '<script>\n' + _esc(JS) + '\n</script>\n'
        '</body></html>')
    # 盲审 P1-1：副本固定写 DB\combined\（目录存在时），不再写 dirname(DB_PATH)——
    #   旧逻辑在单日库模式下会把报告副本写进按天归档目录，污染拆分结构。
    _copy_paths = [OUT_MAIN]
    _combined_dir = os.path.join(BASE, "DB", "combined")
    if os.path.isdir(_combined_dir):
        _copy_paths.append(os.path.join(_combined_dir, "OPPO健康数据分析报告.html"))
    for path in _copy_paths:
        # 盲审复审：统一走 utils.atomic_write——自带临时文件、os.replace 与
        #   杀软/句柄竞争时的 replace 重试（旧手写版缺重试，竞争下直接抛错）。
        with atomic_write(path, encoding="utf-8") as f:
            f.write(doc)
        print(f"✅ 已写入: {path}（{os.path.getsize(path)/1024:.1f} KB）")



if __name__ == "__main__":
    main()
