# SPDX-License-Identifier: MIT
"""
将 OPPO 健康 JSON 数据导入到 SQLite 数据库（增强版：自动类型推断）

改进：
- 自动推断字段类型（INTEGER / REAL / TEXT）
- 数值字段保持原始类型，不再统一转成字符串
- 支持空值处理

用法：
    单独运行：python json_to_sqlite.py
    作为模块：from json_to_sqlite import convert; convert(<导出目录>)，
              例：convert(os.path.join(os.path.dirname(os.path.abspath(__file__)), "DB", "combined"))
"""
import json
import sqlite3
import os
import re
import sys

# 强制 UTF-8 输出，避免 Windows 控制台/重定向乱码（V1.1 修复）
from utils import ensure_utf8_stdout
ensure_utf8_stdout()

DEFAULT_WORK_DIR = os.path.dirname(os.path.abspath(__file__))


def q_ident(name):
    """把表名/列名安全地包成 SQLite 标识符。
    L2：原实现直接 f'"{name}"' 拼接，名字里若含双引号
    会提前闭合标识符（如 `a" , b TEXT --`），造成语法错误或改变语句结构。
    按 SQL 规范把 `"` 翻倍即可安全嵌入；这里同时剔除 NUL 等控制字符。
    注：Python sqlite3 的 execute() 拒绝多语句，故不构成"堆叠注入"，但转义后可
    彻底消除语法破坏面，与 mcp_server.py 的 _check_ident 标准保持一致。"""
    return '"' + str(name).replace('"', '""').replace("\x00", "") + '"'


def convert(export_dir):
    """将 export_dir 下的 health_data.json 转为带类型推断的 oppo_health_full.db。
    返回生成的 db 路径；失败返回 None。
    """
    json_path = os.path.join(export_dir, "health_data.json")
    if not os.path.exists(json_path):
        print(f"❌ 未找到 JSON 文件: {json_path}")
        return None

    db_path = os.path.join(export_dir, "oppo_health_full.db")
    print(f"\n[json_to_sqlite] 读取 JSON: {json_path}")
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as _e:
        # 损坏/截断的 health_data.json（导出中断、磁盘写满、手工编辑出错）
        # 在独立运行时会直接崩栈——与项目"友好提示 + 退出码 1"的风格不符。
        # 经导出主流程调用时上层本就有 try/except，此兜底主要为独立运行服务。
        print(f"⚠️  JSON 文件无法解析（可能已损坏/截断）：{_e}")
        print(f"    建议重新运行 export_health_data.py 生成 health_data.json 后重试。")
        return None
    print(f"  共 {len(data)} 张表")

    if os.path.exists(db_path):
        os.remove(db_path)
        print(f"  已删除旧数据库")

    print(f"[json_to_sqlite] 创建 SQLite 数据库: {db_path}")
    conn = sqlite3.connect(db_path)
    # 连接关闭移入 finally（此前异常路径泄漏连接）
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")

        def infer_column_type(values):
            # 数字形字符串的语义边界——
            #   前导零串（'0030'，如时间/编号，int() 会吞掉前导零）与 'inf'/'nan'
            #   按 TEXT 保留原样，避免类型化库中数据失真；'1e5' 科学记数法
            #   float 转换无损（=100000.0），按 REAL 处理。
            #   （P10修正 2026-09-09 深夜复审：原注释称 '1e5' 按 TEXT，与实际行为不符）
            def _is_pure_int(s):
                if isinstance(s, str):
                    stripped = s.strip()
                    # lstrip('-') 可能为空串（值形如 "--"），
                    #   先判空防 IndexError
                    _ls = stripped.lstrip('-')
                    if len(stripped) > 1 and _ls and _ls[0] == '0' and _ls.isdigit():
                        return False  # 前导零整数（'0030'）：语义是文本
                    if stripped.lstrip('-').lower() in ('inf', 'infinity', 'nan'):
                        return False
                    # C-P3-9：Python int() 接受下划线分隔符（int("1_0")==10）与
                    #   Unicode 数字（int("１２３")==123），会把编号类文本误判为整数。
                    #   用 ASCII 纯整数正则收紧：仅 [+-]?[0-9]+ 放行。
                    if not re.fullmatch(r"[+-]?[0-9]+", stripped):
                        return False
                try:
                    int(s)
                    return True
                except (ValueError, TypeError):
                    return False

            def _is_pure_float(s):
                if isinstance(s, str):
                    stripped = s.strip()
                    # J1补充+前导零排除仅针对"纯整数形"前导零串
                    #   （'0030' 会 float 成 30.0 丢前导零，须按 TEXT 保留）；原实现把 '0.5' 去点后
                    #   变 '05' 也判为前导零，导致 -1~1 的合法小数字符串整列被误判 TEXT。
                    #   现只在不含小数点时才做前导零排除。
                    # 同 _is_pure_int，先判空防 IndexError
                    _ls = stripped.lstrip('-')
                    if ('.' not in stripped and len(stripped) > 1
                            and _ls and _ls[0] == '0' and _ls.isdigit()):
                        return False
                    if stripped.lstrip('-').lower() in ('inf', 'infinity', 'nan'):
                        return False
                    # C-P3-9 同类：float() 同样接受下划线分隔符（float("1_0")==10.0）
                    #   与 Unicode 数字。数据文本里带下划线的"数字"一定是编号而非数值。
                    if "_" in stripped or not stripped.isascii():
                        return False
                try:
                    float(s)
                    return True
                except (ValueError, TypeError):
                    return False

            has_int = has_real = has_text = has_value = False
            for v in values:
                if v is None or v == "":
                    continue
                has_value = True
                if isinstance(v, str):
                    if _is_pure_int(v):
                        has_int = True
                    elif _is_pure_float(v):
                        has_real = True
                    else:
                        has_text = True
                elif isinstance(v, bool):
                    has_int = True
                elif isinstance(v, int):
                    has_int = True
                elif isinstance(v, float):
                    if v != v or v in (float('inf'), float('-inf')):
                        has_text = True  # nan/inf 值本身按文本保留
                    else:
                        has_real = True
                else:
                    has_text = True
            if not has_value:
                return 'TEXT'
            if has_text:
                return 'TEXT'
            if has_real:
                return 'REAL'
            if has_int:
                return 'INTEGER'
            return 'TEXT'

        def convert_value(value, col_type):
            if value is None or value == "":
                return None
            if col_type == 'INTEGER':
                try:
                    return int(value)
                except (ValueError, TypeError):
                    return None
            elif col_type == 'REAL':
                try:
                    return float(value)
                except (ValueError, TypeError):
                    return None
            return str(value)

        table_schemas = {}
        for table_name, table_data in data.items():
            columns = table_data.get("columns", [])
            rows = table_data.get("rows", [])
            # 盲审复审 L5（注释勘误）：空表（有列定义、无行）也建表——全 TEXT 零行，
            #   保证按"表存在性"判断的下游不漏表。注：本项目导出侧 health_data.json
            #   **不含**空表（M20：与 CSV 步骤口径一致，0 行表直接跳过），此分支
            #   仅对第三方工具产出的 JSON 生效。
            if not columns:
                continue
            if not rows:
                col_types = {col: "TEXT" for col in columns}
                table_schemas[table_name] = {'columns': columns, 'col_types': col_types, 'rows': rows}
                continue
            col_types = {}
            for col in columns:
                col_types[col] = infer_column_type([row.get(col) for row in rows])
            table_schemas[table_name] = {'columns': columns, 'col_types': col_types, 'rows': rows}

        total_rows = 0
        for table_name, schema in table_schemas.items():
            columns = schema['columns']
            col_types = schema['col_types']
            rows = schema['rows']
            col_defs = ", ".join([f'{q_ident(col)} {col_types[col]}' for col in columns])
            create_sql = f'CREATE TABLE IF NOT EXISTS {q_ident(table_name)} ({col_defs})'
            try:
                cursor.execute(create_sql)
            except Exception as e:
                print(f"  ❌ {table_name}: 创建表失败 - {e}")
                continue
            placeholders = ", ".join(["?" for _ in columns])
            col_names = ", ".join([q_ident(col) for col in columns])
            insert_sql = f'INSERT INTO {q_ident(table_name)} ({col_names}) VALUES ({placeholders})'
            # 逐行 execute 改 executemany 批量插入（同事务内），
            #   35K 行级别下显著减少 Python 层循环与 SQL 编译开销。
            all_values = [[convert_value(row.get(col), col_types[col]) for col in columns]
                          for row in rows]
            inserted = 0
            try:
                cursor.executemany(insert_sql, all_values)
                inserted = len(all_values)
            except Exception as e:
                print(f"  ⚠️  {table_name}: 批量插入失败（回退逐行，跳过坏行）- {e}")
                # executemany 中途失败时，已成功的前缀行仍留在事务中；
                try:
                    conn.rollback()
                except Exception:
                    pass
                _skipped = 0
                for values in all_values:
                    try:
                        cursor.execute(insert_sql, values)
                        inserted += 1
                    except Exception:
                        _skipped += 1
                        continue
                if _skipped:
                    # 盲审复审 L4：坏行不再静默——typed 库行数与源 JSON 的差异必须可见
                    print(f"  ⚠️  {table_name}: 逐行回退中 {_skipped} 行坏数据被跳过"
                          f"（导入 {inserted} 行，源 JSON {len(all_values)} 行）")
            conn.commit()
            total_rows += inserted
            print(f"  ✅ {table_name}: {inserted} 行")
            # 该表已入库，及时释放行数据引用，
            #   降低多表大库的峰值内存（schema['rows'] 与 data 中为同一对象，置 None 即释放；
            #   data/table_schemas 此刻均不在被遍历状态，原地置 None 安全）。
            data[table_name] = None
            schema['rows'] = None

        print(f"[json_to_sqlite] 导入完成：{len(table_schemas)} 张表, {total_rows} 行")
    finally:
        # 盲审 P3-3：关闭前主动 checkpoint——WAL 模式下导出刚完成时主 .db 偏小
        #   （实测 3.8MB）而 -wal 里还有约一半数据（4.1MB），用户此刻只拷走 .db
        #   会丢数据。TRUNCATE 把 wal 合并回主文件并清空。失败不回滚导入成果。
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        conn.close()
    print(f"数据库已保存到: {db_path}")
    return db_path


if __name__ == "__main__":
    # ⚠️ D7修复：原脚本用 strftime("%Y_%-m_%-d")，其中 %-m/%-d 在 Windows 上不被支持，
    #   会原样输出 "2026_%-m_%-d" 导致目录拼错。改为 f-string 拼日期（与 export_health_data.py 一致）。
    # 标准口径：导出统一写入 DB\combined\（合并全量库），
    #   故默认导出目录直接指向 combined；找不到时回退到 DB 下最新含 health_data.json 的目录。
    work_dir = DEFAULT_WORK_DIR
    export_dir = os.path.join(work_dir, "DB", "combined")
    if not os.path.isdir(export_dir):
        # 退化：用最新存在的 DB 目录
        db_root = os.path.join(work_dir, "DB")
        if os.path.isdir(db_root):
            # 目录名按标准 YYYY-MM-DD（如 2026-09-09）格式，
            #   解析为 (年,月,日) 数字元组排序，解析失败的目录排最后。
            def _dir_key(name):
                try:
                    y, m, d = (int(x) for x in name.split('-'))
                    return (y, m, d)
                except (ValueError, AttributeError):
                    return (-1, -1, -1)
            candidates = sorted(
                (d for d in os.listdir(db_root)
                 if os.path.isdir(os.path.join(db_root, d)) and os.path.exists(os.path.join(db_root, d, "health_data.json"))),
                key=_dir_key,
                reverse=True,
            )
            if candidates:
                export_dir = os.path.join(db_root, candidates[0])
    print("=" * 60)
    print(" OPPO 健康 JSON → SQLite 导入工具（增强版）")
    print("=" * 60)
    print(f"导出目录: {export_dir}")
    # 未找到 JSON 时 convert() 返回 None，脚本此前仍以退出码 0 结束，
    #   自动化无法感知失败（导出脚本的"可选增强"因此静默无效）。现按结果给出退出码。
    _converted = convert(export_dir)
    sys.exit(0 if _converted else 1)
