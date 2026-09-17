# SPDX-License-Identifier: MIT
"""
OPPO 健康数据导出工具 - 单元测试
==================================
测试工具函数、配置、数据校验等模块。

运行方法：
    python test_auto_export.py
    python -m pytest test_auto_export.py -v
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

# 与全项目统一的时区基准
CN = timezone(timedelta(hours=8))

# 确保能导入本地模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 强制 UTF-8 输出，避免 Windows 控制台/重定向乱码（V1.1 修复）
from utils import ensure_utf8_stdout
ensure_utf8_stdout()

from utils import safe_int, safe_float, ProgressBar, DataIntegrityChecker, setup_logger
from config import CONFIG, validate_config, get_config


def _mktemp_db():
    """废弃 API tempfile.mktemp 有竞态条件（官方建议停用），
    改 mkstemp（原子创建）并返回路径；调用方用 addCleanup 注册删除，断言失败也不留残留。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


class TestSafeInt(unittest.TestCase):
    """测试安全整数转换"""

    def test_none(self):
        self.assertEqual(safe_int(None), 0)

    def test_none_with_default(self):
        self.assertEqual(safe_int(None, -1), -1)

    def test_integer(self):
        self.assertEqual(safe_int(42), 42)

    def test_float(self):
        # 小数由"静默截断"改为"四舍五入"。
        #   原断言 safe_int(3.9) == 3 会把 3.9 悄悄变成 3，对时长类字段不准确。
        self.assertEqual(safe_int(3.9), 4)
        self.assertEqual(safe_int(3.4), 3)
        self.assertEqual(safe_int(-3.9), -4)

    def test_string_integer(self):
        self.assertEqual(safe_int("123"), 123)

    def test_string_float(self):
        # 小数字符串同样改为四舍五入；整数字符串仍保持精确。
        self.assertEqual(safe_int("456.7"), 457)
        self.assertEqual(safe_int("456.2"), 456)
        # L6 补充：超过 2^53 的整数字符串**不得**经 float 丢精度
        #   （原实现 int(float(...)) 会把 12345678901234567890 变成 …67168）
        self.assertEqual(safe_int("12345678901234567890"), 12345678901234567890)

    def test_empty_string(self):
        self.assertEqual(safe_int(""), 0)

    def test_whitespace_string(self):
        self.assertEqual(safe_int("   "), 0)

    def test_invalid_string(self):
        self.assertEqual(safe_int("abc"), 0)

    def test_negative(self):
        self.assertEqual(safe_int(-100), -100)

    def test_zero(self):
        self.assertEqual(safe_int(0), 0)

    def test_boolean(self):
        self.assertEqual(safe_int(True), 1)
        self.assertEqual(safe_int(False), 0)


class TestSafeFloat(unittest.TestCase):
    """测试安全浮点数转换"""

    def test_none(self):
        self.assertEqual(safe_float(None), 0.0)

    def test_integer(self):
        self.assertEqual(safe_float(42), 42.0)

    def test_float(self):
        self.assertAlmostEqual(safe_float(3.14), 3.14)

    def test_string_float(self):
        self.assertAlmostEqual(safe_float("3.14"), 3.14)

    def test_empty_string(self):
        self.assertEqual(safe_float(""), 0.0)

    def test_invalid_string(self):
        self.assertEqual(safe_float("abc"), 0.0)


class TestConfig(unittest.TestCase):
    """测试配置模块"""

    def test_config_has_required_keys(self):
        required_keys = [
            "adb_path", "adb_device", "package_name",
            "work_dir", "export_script",
            # db_key_fallback 已从 config 删除
            #   （与 export_health_data.py 的 DEFAULT_DB_KEY 重复且生产代码从不读取），
            #   故同步移出必填键断言。
            # test_wait_times_positive 直接下标访问
            #   CONFIG["wait_data_sync"] 与 CONFIG["wait_emulator_start"]，但后者原先
            #   不在本清单里——若 config 漏掉该键，测试会抛 KeyError 而不是给出清晰断言。
            "wait_data_sync", "wait_emulator_start", "enable_integrity_check",
            # 覆盖路径相对化改造后的探测键——
            #   探测不到时值为 None，属合法状态（功能降级），只验证键存在。
            "frida_cli", "emulator_path", "emulator_console",
        ]
        for key in required_keys:
            self.assertIn(key, CONFIG, f"配置缺少必需项: {key}")

    def test_get_config_returns_copy(self):
        cfg1 = get_config()
        cfg2 = get_config()
        self.assertEqual(cfg1, cfg2)
        # 修改副本不影响原配置
        cfg1["test_key"] = "test_value"
        self.assertNotIn("test_key", get_config())

    def test_validate_config(self):
        is_valid, warnings = validate_config()
        self.assertIsInstance(is_valid, bool)
        self.assertIsInstance(warnings, list)

    def test_wait_times_positive(self):
        self.assertGreater(CONFIG["wait_data_sync"], 0)
        self.assertGreater(CONFIG["wait_emulator_start"], 0)

    def test_heart_rate_range_valid(self):
        self.assertLess(CONFIG["min_heart_rate"], CONFIG["max_heart_rate"])

    def test_spo2_range_valid(self):
        self.assertLess(CONFIG["min_spo2"], CONFIG["max_spo2"])
        self.assertLessEqual(CONFIG["max_spo2"], 100)


class TestProgressBar(unittest.TestCase):
    """测试进度条"""

    def test_initialization(self):
        pb = ProgressBar(total=100, desc="测试")
        self.assertEqual(pb.total, 100)
        self.assertEqual(pb.current, 0)

    def test_update_absolute(self):
        pb = ProgressBar(total=100)
        pb.update(50)
        self.assertEqual(pb.current, 50)

    def test_update_increment(self):
        pb = ProgressBar(total=100)
        pb.update(inc=10)
        pb.update(inc=20)
        self.assertEqual(pb.current, 30)

    def test_update_not_exceed_total(self):
        pb = ProgressBar(total=100)
        pb.update(150)
        self.assertEqual(pb.current, 100)

    def test_zero_total(self):
        pb = ProgressBar(total=0)
        self.assertEqual(pb.total, 1)  # 应该被修正为1

    def test_finish(self):
        pb = ProgressBar(total=10)
        for i in range(10):
            pb.update(i + 1)
        pb.finish("完成")
        self.assertEqual(pb.current, 10)


class TestLogger(unittest.TestCase):
    """测试日志模块"""

    def test_setup_logger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = setup_logger(
                "test_logger_cleanup",
                log_dir=tmpdir,
                log_to_file=True,
                log_level="DEBUG"
            )
            self.assertIsNotNone(logger)
            self.assertEqual(len(logger.handlers), 2)  # 控制台 + 文件
            # 关闭所有handler，释放文件锁（Windows上需要）
            # ⚠️ L6修复：用 logger.handlers[:] 复制列表后遍历，
            #   否则边遍历边删会跳过file handler，导致PermissionError
            for handler in logger.handlers[:]:
                handler.close()
                logger.removeHandler(handler)

    def test_logger_no_file(self):
        logger = setup_logger("test_no_file", log_to_file=False)
        self.assertEqual(len(logger.handlers), 1)  # 只有控制台

    def test_logger_reuse(self):
        logger1 = setup_logger("test_reuse")
        logger2 = setup_logger("test_reuse")
        self.assertIs(logger1, logger2)
        # 不应该重复添加handler
        self.assertEqual(len(logger1.handlers), 1)


class TestDataIntegrityChecker(unittest.TestCase):
    """测试数据完整性校验器"""

    def _create_test_db(self, tables_data):
        """创建测试数据库"""
        import sqlite3
        tmpdb = _mktemp_db()
        self.addCleanup(self._safe_unlink, tmpdb)
        conn = sqlite3.connect(tmpdb)
        # 连接关闭移入 finally（建表/插入异常时不再泄漏）。
        try:
            cursor = conn.cursor()
            for table_name, columns, rows in tables_data:
                col_defs = ", ".join([f"{col} TEXT" for col in columns])
                cursor.execute(f"CREATE TABLE {table_name} ({col_defs})")
                for row in rows:
                    placeholders = ", ".join(["?"] * len(row))
                    cursor.execute(f"INSERT INTO {table_name} VALUES ({placeholders})", row)
            conn.commit()
        finally:
            conn.close()
        return tmpdb

    @staticmethod
    def _safe_unlink(path):
        try:
            os.unlink(path)
        except OSError:
            pass

    def test_empty_database(self):
        """测试空数据库"""
        import sqlite3
        tmpdb = _mktemp_db()
        self.addCleanup(self._safe_unlink, tmpdb)
        conn = sqlite3.connect(tmpdb)
        conn.close()

        checker = DataIntegrityChecker(tmpdb, config={"min_tables": 1})
        is_valid, report = checker.check_all()
        checker.close()
        # 空数据库应该有警告（表数量少）
        self.assertIn("表数量", report)

    def test_valid_sleep_data(self):
        """测试正常的睡眠数据"""
        tables_data = [
            ("DBSleepDataStatTable",
             ["date", "total_sleep_time", "sleep_score"],
             [("20260901", "480", "80"), ("20260902", "500", "85")]),
        ]
        tmpdb = self._create_test_db(tables_data)
        checker = DataIntegrityChecker(tmpdb, config={"max_sleep_hours": 16})
        is_valid, report = checker.check_all()
        checker.close()
        # 正常数据不应该有睡眠异常警告
        self.assertNotIn("睡眠时长异常", report)

    def test_abnormal_sleep_data(self):
        """测试异常的睡眠数据（超过16小时）"""
        tables_data = [
            ("DBSleepDataStatTable",
             ["date", "total_sleep_time", "sleep_score"],
             [("20260901", "1200", "80")]),  # 20小时，异常
        ]
        tmpdb = self._create_test_db(tables_data)
        checker = DataIntegrityChecker(tmpdb, config={"max_sleep_hours": 16})
        is_valid, report = checker.check_all()
        checker.close()
        # 应该检测到睡眠异常
        self.assertIn("睡眠时长异常", report)

    def test_missing_required_table(self):
        """测试缺少必需表"""
        tables_data = [
            ("SomeOtherTable", ["col1"], [("val1",)]),
        ]
        tmpdb = self._create_test_db(tables_data)
        checker = DataIntegrityChecker(tmpdb, config={
            "required_tables": ["DBSleepDataStatTable"],
            "min_tables": 1,
        })
        is_valid, report = checker.check_all()
        checker.close()
        # 应该检测到缺少必需表
        self.assertIn("必需表不存在", report)
        self.assertFalse(is_valid)

    def test_date_format_check(self):
        """测试日期格式检查"""
        tables_data = [
            ("DBSleepDataStatTable",
             ["date", "total_sleep_time"],
             [("2026-09-01", "480")]),  # 格式不对
        ]
        tmpdb = self._create_test_db(tables_data)
        checker = DataIntegrityChecker(tmpdb, config={"min_tables": 1})
        is_valid, report = checker.check_all()
        checker.close()
        # 应该检测到日期格式异常
        self.assertIn("日期格式异常", report)


class TestPctDisplay(unittest.TestCase):
    """P9补充：P4 修复函数 _pct_display（最大余数法）的常驻回归防线。
    5 个单测会照常绿灯。本类合计恒=100 断言即防线。"""

    @classmethod
    def _fn(cls):
        from auto_export_and_analyze import _pct_display
        return _pct_display

    def test_real_case_sum_100(self):
        """实测报告场景：OSA 排除后 6 维归一化权重（修复前四舍五入合计 101%）"""
        pct = self._fn()
        w = {'sleep': 0.25/0.9, 'heart_rate': 0.15/0.9, 'hrv': 0.15/0.9,
             'exercise': 0.15/0.9, 'stress': 0.10/0.9, 'spo2': 0.10/0.9}
        self.assertEqual(sum(pct(w).values()), 100)

    def test_integer_weights_sum_100(self):
        pct = self._fn()
        self.assertEqual(sum(pct({'a': 0.5, 'b': 0.3, 'c': 0.2}).values()), 100)

    def test_single_dim_is_100(self):
        pct = self._fn()
        self.assertEqual(pct({'a': 1.0}), {'a': 100})

    def test_empty_weights(self):
        pct = self._fn()
        self.assertEqual(pct({}), {})

    def test_thirds_allocation(self):
        """经典 1/3×3 用例：应分配 33/33/34 而非 33/33/33（合计99）"""
        pct = self._fn()
        self.assertEqual(sorted(pct({'a': 1/3, 'b': 1/3, 'c': 1/3}).values()), [33, 33, 34])


class TestInferColumnType(unittest.TestCase):
    """P9补充：P2/J1 类型推断修复的常驻回归防线。
    infer_column_type 为 convert() 内嵌套函数，无法直接 import，
    故走 json_to_sqlite.convert 端到端夹具，同时覆盖列类型与值保真。"""

    def _build_db(self, rows):
        import json as _json
        import shutil
        import sqlite3 as _sq
        import json_to_sqlite
        tmpdir = tempfile.mkdtemp(prefix="test_j2s_")
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        with open(os.path.join(tmpdir, "health_data.json"), "w", encoding="utf-8") as f:
            _json.dump({"Probe": {"columns": list(rows[0].keys()), "rows": rows}}, f)
        db = json_to_sqlite.convert(tmpdir)
        self.assertIsNotNone(db)
        conn = _sq.connect(db)
        try:
            types = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(Probe)")}
            vals = conn.execute("SELECT * FROM Probe").fetchall()
        finally:
            conn.close()
        return types, vals

    def test_leading_zero_text_small_float_real(self):
        """P2 修复核心断言：'0030'→TEXT 保留前导零；'0.5'→REAL 不再误判 TEXT"""
        types, vals = self._build_db([
            {"code": "0030", "ratio": "0.5"},
            {"code": "0031", "ratio": "0.3"},
        ])
        self.assertEqual(types["code"], "TEXT")
        self.assertEqual(types["ratio"], "REAL")
        self.assertEqual(sorted(v[0] for v in vals), ["0030", "0031"])  # 前导零未丢

    def test_scientific_notation_is_real(self):
        """'1e5' → REAL（float 转换无损；J1 注释已按实际行为修正）"""
        types, _ = self._build_db([{"v": "1e5"}, {"v": "2e5"}])
        self.assertEqual(types["v"], "REAL")

    def test_inf_nan_strings_are_text(self):
        types, _ = self._build_db([{"v": "inf"}, {"v": "nan"}])
        self.assertEqual(types["v"], "TEXT")

    def test_mixed_int_float_is_real(self):
        types, _ = self._build_db([{"v": 1}, {"v": 2.5}])
        self.assertEqual(types["v"], "REAL")


class TestIntegration(unittest.TestCase):
    """集成测试：测试各模块协同工作"""

    def test_config_and_utils_integration(self):
        """测试配置和工具模块的集成"""
        # 使用配置中的阈值进行数据校验
        test_value = "85"
        result = safe_int(test_value)
        self.assertGreaterEqual(result, CONFIG.get("min_heart_rate", 30))
        self.assertLessEqual(result, CONFIG.get("max_heart_rate", 220))

    def test_progress_bar_with_config(self):
        """测试使用配置的进度条"""
        pb = ProgressBar(
            total=100,
            width=CONFIG.get("progress_bar_width", 30),
            show_percent=CONFIG.get("progress_show_percent", True),
        )
        pb.update(50)
        self.assertEqual(pb.current, 50)
        pb.finish()


# ==================== 边界测试（V1.0 补充，2026-09-13）====================
# 覆盖：脏值/空值/边界日期的数值安全转换、分卷排序与缺口、schema 漂移降级、
#       类型推断边界、上下文管理器关闭、跨通道时间换算行为一致性。

class TestNumericHelpersBoundaries(unittest.TestCase):
    """generate_html_report 的 _num / _mmdd / avg 边界（脏值不再中断报告）"""

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "ghr_mod", os.path.join(os.path.dirname(os.path.abspath(__file__)), "generate_html_report.py"))
        self.ghr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.ghr)

    def test_num_dirty_values(self):
        for v, expect in [("--", 0.0), ("", 0.0), (None, 0.0), ("abc", 0.0),
                          ("12.5", 12.5), (7, 7.0), (" 3 ", 3.0)]:
            self.assertEqual(self.ghr._num(v), expect, f"_num({v!r})")

    def test_num_custom_default(self):
        self.assertEqual(self.ghr._num("--", -1.0), -1.0)

    def test_mmdd_8digit_and_dirty(self):
        self.assertEqual(self.ghr._mmdd("20260908"), "09/08")
        self.assertEqual(self.ghr._mmdd("2026-09-08"), "2026-09-08")   # 非 8 位原样返回
        self.assertEqual(self.ghr._mmdd(20260908), "09/08")            # int 入参
        self.assertEqual(self.ghr._mmdd(""), "")

    def test_avg_boundaries(self):
        self.assertIsNone(self.ghr.avg([]))
        self.assertEqual(self.ghr.avg([70, 80]), 75.0)
        self.assertEqual(self.ghr.avg(["70", "80"]), 75.0)   # 数字形字符串
        self.assertEqual(self.ghr.avg([None, 60]), 60.0)     # None 跳过
        self.assertEqual(self.ghr.avg(["--", 60]), 60.0)     # 盲审 L3：脏值剔除（与 avg_scaled 同源），不再按 0 参与


class TestSplitDbDateBoundaries(unittest.TestCase):
    """split_db_by_day 日期解析与毫秒换算边界"""

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "sdb_mod", os.path.join(os.path.dirname(os.path.abspath(__file__)), "split_db_by_day.py"))
        self.sdb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.sdb)

    def test_is_yyyymmdd_int(self):
        self.assertEqual(self.sdb.is_yyyymmdd_int(20260908), "20260908")
        self.assertIsNone(self.sdb.is_yyyymmdd_int("20260908"))  # 字符串不接受
        self.assertIsNone(self.sdb.is_yyyymmdd_int(202609))      # 位数不足
        self.assertIsNone(self.sdb.is_yyyymmdd_int(19990101))    # 年份越界
        self.assertIsNone(self.sdb.is_yyyymmdd_int(20261301))    # 月份越界
        # 20260231 通过 d<=31 检查（日历有效性由 main 的 _valid_day 过滤）——记录既有行为
        self.assertEqual(self.sdb.is_yyyymmdd_int(20260231), "20260231")

    def test_ymd_variants(self):
        vs = self.sdb.ymd_variants("20260908")
        self.assertIn(20260908, vs)
        self.assertIn("2026-09-08", vs)
        self.assertIn("2026_9_8", vs)
        self.assertIn("2026_09_08", vs)

    def test_day_bounds_and_epoch_roundtrip(self):
        import datetime as _dt
        d = _dt.date(2026, 9, 8)
        s_ms, e_ms = self.sdb.day_bounds_ms(d)
        self.assertEqual(e_ms - s_ms, 86400000)                    # 恰好一天
        self.assertEqual(self.sdb.epoch_to_date(s_ms).isoformat(), "2026-09-08")


class TestRejoinBoundaries(unittest.TestCase):
    """tools/rejoin.py 分卷数值排序 / 缺口拒绝 / 哈希缺条目边界"""

    def setUp(self):
        import importlib.util, shutil
        spec = importlib.util.spec_from_file_location(
            "rejoin_mod", os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "rejoin.py"))
        self.rejoin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.rejoin)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)   # ignore_errors=True
        self.rejoin.DIST = self.tmp

    def _parts(self, name, payloads):
        for i, b in enumerate(payloads):
            with open(os.path.join(self.tmp, f"{name}.part{i}"), "wb") as f:
                f.write(b)

    def test_two_digit_part_order(self):
        """≥10 卷时必须按数值排序（字典序会把 part10 排到 part2 前）"""
        payloads = [bytes([65 + i]) for i in range(12)]          # A..L
        self._parts("e", payloads)
        # M1/M2 后 rejoin 必须先通过哈希校验才会产出文件，
        #   故此处提供**正确**的哈希，让本用例专注被测点"卷序"
        #   （哈希缺失/不符的边界见 test_missing_hash_entry）。
        import hashlib
        with open(os.path.join(self.tmp, "SHA256SUMS.txt"), "w", encoding="utf-8") as f:
            f.write("%s  e\n" % hashlib.sha256(b"".join(payloads)).hexdigest())
        self.assertTrue(self.rejoin.rejoin("e"))
        with open(os.path.join(self.tmp, "e"), "rb") as f:
            self.assertEqual(f.read(), bytes(range(65, 77)))

    def test_part_gap_rejected(self):
        self._parts("b", [b"x"]); 
        with open(os.path.join(self.tmp, "b.part2"), "wb") as f: f.write(b"y")   # 缺 part1
        self.assertFalse(self.rejoin.rejoin("b"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "b")))

    def test_no_parts(self):
        self.assertFalse(self.rejoin.rejoin("c"))

    def test_missing_hash_entry(self):
        """清单缺条目 = 无法校验 → 必须判失败，且不得留下文件。
        M2：原实现把这种情况记为 "PASS(未校验)"
        并被 startswith("PASS") 判为成功——SHA256SUMS.txt 一旦被清空/损坏，
        脚本会报"全部完成"却一次校验都没做。未校验不等于通过。"""
        self._parts("d", [b"x", b"y"])
        self.assertFalse(self.rejoin.rejoin("d"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "d")))

    def test_hash_mismatch_discards_output(self):
        """分卷损坏（哈希不符）→ 判失败，且**不得**把坏文件留在磁盘上。
        M1：原实现先写出文件再校验，失败后坏文件残留，
        用户下次可能直接拿它去 push。现改为临时文件 + 校验通过才落盘。"""
        import hashlib
        self._parts("f", [b"good", b"bad"])
        with open(os.path.join(self.tmp, "SHA256SUMS.txt"), "w", encoding="utf-8") as f:
            f.write("%s  f\n" % hashlib.sha256(b"expected-but-different").hexdigest())
        self.assertFalse(self.rejoin.rejoin("f"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "f")))
        # 不得残留任何临时文件
        leftovers = [p for p in os.listdir(self.tmp) if p.endswith(".tmp")]
        self.assertEqual(leftovers, [])
        print("\n[说明] 上方 rejoin 测试中的 [FAIL] 均为预期场景输出（损坏/缺卷/哈希不符），")
        print("        不代表 unittest 失败。本测试类最终断言全部为 assertFalse/assertTrue PASS。")


class TestDataValidatorSchemaDrift(unittest.TestCase):
    """data_validation E1：缺表/缺列（schema 漂移）时降级告警而非崩溃"""

    def _mk(self, ddl=None):
        import sqlite3
        fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        con = sqlite3.connect(path)
        if ddl:
            for stmt in ddl:
                con.execute(stmt)
        con.commit(); con.close()
        return path

    def test_missing_columns_no_crash(self):
        from data_validation import DataValidator
        # 表存在但缺少 E1 涉及的列（模拟 App 版本 schema 漂移）
        path = self._mk(["CREATE TABLE DBSleepDataStatTable(date INTEGER, total_sleep_time INTEGER)"])
        v = DataValidator(path)
        try:
            self.assertIsInstance(v.validate_all(), bool)
        finally:
            v.close()

    def test_empty_db_no_crash(self):
        from data_validation import DataValidator
        path = self._mk()
        v = DataValidator(path)
        try:
            self.assertIsInstance(v.validate_all(), bool)   # 空库 → 有 ERROR 但不得抛异常
        finally:
            v.close()


class TestJsonToSqliteBoundaries(unittest.TestCase):
    """json_to_sqlite 类型推断边界：前导零 / 科学计数法 / inf/nan / 脏值 / 空表"""

    def setUp(self):
        import shutil
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_infer_types_and_keep_empty(self):
        import json as _json, sqlite3, json_to_sqlite
        data = {
            "T1": {"columns": ["date", "lead0", "sci", "dirty", "nanv"],
                   "rows": [{"date": 20260908, "lead0": "0030", "sci": "1e5",
                             "dirty": "--", "nanv": "nan"}]},
            "T2": {"columns": ["a"], "rows": []},          # 盲审 L5：空表 → 也建表（表集合与 JSON 一致）
        }
        with open(os.path.join(self.dir, "health_data.json"), "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False)
        db = json_to_sqlite.convert(self.dir)
        self.assertTrue(db and os.path.exists(db))
        con = sqlite3.connect(db)
        ty = {r[1]: r[2] for r in con.execute('PRAGMA table_info("T1")')}
        self.assertEqual(ty["date"], "INTEGER")
        self.assertEqual(ty["sci"], "REAL")        # 1e5 无损按 REAL
        self.assertEqual(ty["lead0"], "TEXT")      # 前导零保留为文本
        self.assertEqual(ty["dirty"], "TEXT")      # "--" 不崩溃
        self.assertEqual(ty["nanv"], "TEXT")       # nan 按文本
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # 盲审 L5：空表 T2 也建表——单列按 TEXT、0 行，与 health_data.json 表集合一致
        self.assertIn("T2", names)
        ty2 = {r[1]: r[2] for r in con.execute('PRAGMA table_info("T2")')}
        self.assertEqual(ty2.get("a"), "TEXT")
        self.assertEqual(con.execute('SELECT COUNT(*) FROM "T2"').fetchone()[0], 0)
        con.close()

    def test_missing_json_returns_none(self):
        import json_to_sqlite
        self.assertIsNone(json_to_sqlite.convert(self.dir))

    def test_corrupt_json_returns_none(self):
        # 第 8 轮外部审查发现：损坏/截断 JSON 在独立运行时曾抛未捕获
        # JSONDecodeError 崩栈（经导出主流程调用有上层兜底，独立调用没有）。
        # 修复后 convert() 应返回 None 而非抛异常。
        import json_to_sqlite
        with open(os.path.join(self.dir, "health_data.json"), "w", encoding="utf-8") as f:
            f.write('{"T1": {"incomplete')          # 截断的 JSON
        self.assertIsNone(json_to_sqlite.convert(self.dir))


class TestDataIntegrityCheckerContextManager(unittest.TestCase):
    """utils X11：DataIntegrityChecker 支持 with，异常路径也能关闭连接"""

    def test_with_statement_closes(self):
        import sqlite3
        fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        con = sqlite3.connect(path); con.execute("CREATE TABLE t(a)"); con.commit(); con.close()
        with DataIntegrityChecker(path) as c:
            self.assertIsNotNone(c.conn)
            c.check_all()
        with self.assertRaises(Exception):
            c.conn.execute("SELECT 1")      # 已关闭 → ProgrammingError


class TestServerHelperBehavior(unittest.TestCase):
    """跨通道时间换算行为一致性（与 TestServerHelperParity 的源码一致性互补）"""

    def _load(self, path, name):
        import importlib.util
        spec = importlib.util.spec_from_file_location(name, path)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    def test_to_epoch_ms_agreement_and_boundaries(self):
        root = os.path.dirname(os.path.abspath(__file__))
        # mcp 是 requirements 明确标为**可选**的依赖；未装时
        #   mcp_server 顶层 import 失败会让本用例 ERROR，测试总数与文档口径
        #   随之波动。现缺 mcp 时跳过本用例（而非报错）。
        try:
            import mcp  # noqa: F401
        except ImportError:
            self.skipTest("未安装可选依赖 mcp：跳过跨通道时间换算一致性用例")
        up = self._load(os.path.join(root, "server", "upload_server.py"), "up_srv_probe")
        mc = self._load(os.path.join(root, "server", "mcp_server.py"), "mc_srv_probe")
        cases = [None, "", "0", "-5", "20260908", "2026-09-08",
                 1757300000000, "1757300000000", "abc", 0, "nan"]
        for c in cases:
            self.assertEqual(up._to_epoch_ms(c), mc._to_epoch_ms(c), f"两侧不一致: {c!r}")
        self.assertIsNone(up._to_epoch_ms(0))        # 0 → 无效
        self.assertIsNone(up._to_epoch_ms("-1"))     # 负数 → 无效
        # YYYYMMDD 分支统一按 CN(UTC+8) 解释（与全项目时区口径一致）。
        self.assertEqual(up._to_epoch_ms("20260908"),
                         int(datetime(2026, 9, 8, tzinfo=CN).timestamp() * 1000))


def run_tests():
    """运行所有测试"""
    print("=" * 70)
    print("  OPPO 健康数据导出工具 - 单元测试")
    print(f"  测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)
    print()

    # 创建测试套件
    # V1.0 修正：原为硬编码类列表——新增测试类忘记登记时，`python test_auto_export.py`
    #   会静默漏跑（实测 TestServerHelperParity 曾被漏掉，与 `unittest` 发现结果不一致）。
    #   改为模块级自动发现，两个入口（本脚本 / python -m unittest）永远跑同一套用例。
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])

    # 运行测试
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    # 输出总结
    print()
    print("=" * 70)
    print(f"  测试总结: {result.testsRun} 个测试, "
          f"{len(result.failures)} 个失败, "
          f"{len(result.errors)} 个错误")
    print("=" * 70)

    return result.wasSuccessful()


class TestMedicalConstants(unittest.TestCase):
    """2026-09-14 终审新增：医疗常量一致性守卫（填补本轮新增代码的覆盖空白）。

    `MED_THRESHOLDS`（auto_export_and_analyze）与 `SLEEP_SCORE_WEIGHTS`
    （generate_html_report）是本轮由散落字面量提取而成的具名常量，就医建议
    判定与睡眠评分完全依赖它们。一旦被误改（缺键、阈值方向倒置、六维权重
    合计≠100），报告会**静默**产出错误结论而不抛异常，故在此加锁。
    """

    def test_medical_constants_coherent(self):
        from auto_export_and_analyze import MED_THRESHOLDS

        for k in ("ahi_severe", "ahi_mild", "spo2_low", "spo2_normal",
                  "rest_hr_high", "rest_hr_normal", "rmssd_low",
                  "rmssd_normal", "score_low"):
            self.assertIn(k, MED_THRESHOLDS, f"MED_THRESHOLDS 缺键: {k}")
            self.assertIsInstance(MED_THRESHOLDS[k], (int, float))
        # 阈值方向不得倒置：预警值必须"劣于"正常参考值
        self.assertLess(MED_THRESHOLDS["ahi_mild"], MED_THRESHOLDS["ahi_severe"])
        self.assertLess(MED_THRESHOLDS["spo2_low"], MED_THRESHOLDS["spo2_normal"])
        self.assertGreater(MED_THRESHOLDS["rest_hr_high"], MED_THRESHOLDS["rest_hr_normal"])
        self.assertLess(MED_THRESHOLDS["rmssd_low"], MED_THRESHOLDS["rmssd_normal"])

        # generate_html_report 有模块级副作用，按本文件既有惯例用文件路径加载
        import importlib.util
        _p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "generate_html_report.py")
        spec = importlib.util.spec_from_file_location("ghr_const_mod", _p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        w = mod.SLEEP_SCORE_WEIGHTS
        for k in ("duration", "deep", "rem", "rhythm", "continuity", "efficiency"):
            self.assertIn(k, w, f"SLEEP_SCORE_WEIGHTS 缺键: {k}")
        # 六维满分合计必须正好 100，否则 0~100 分量表失效
        self.assertEqual(sum(w.values()), 100, "睡眠评分六维权重合计必须为 100")


class TestServerHelperParity(unittest.TestCase):
    """server 侧重复实现的**一致性守护**测试。

    `_IDENT` / `_check_ident` / `_to_epoch_ms` 在 mcp_server.py、upload_server.py
    中各存一份——跨通道幂等去重的前提是两侧的标识符校验、时间换算
    **逐字节一致**。同类问题已实锤发生过一次："查找最新导出目录"的三份
    实现发生漂移（auto_export 未优先 DB\\combined，已于 C1 修复）。

    本测试用 AST 归一化（剔除 docstring 与注释）比对各份实现，
    任何一份被单独修改而其它未同步时测试即失败，起到漂移告警作用。

    注：`_normalize_cell` 原在 upload_server 与
    migrate_sink_add_t 各存一份，后者已随"纯净环境清理"移除，该项目现仅
    upload_server 一份、无漂移可比对象，故原 test_normalize_cell_consistent
    已删除（避免"静默跳过"造成"全部通过"的错觉）。未来若再出现第二份实现，
    应同步恢复该守卫测试。
    """

    ROOT = os.path.dirname(os.path.abspath(__file__))
    SERVER = os.path.join(ROOT, "server")

    def _normalized(self, filename, name):
        import ast
        path = os.path.join(self.SERVER, filename)
        if not os.path.isfile(path):
            self.skipTest(f"{filename} 不存在")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                body = [s for s in node.body
                        if not (isinstance(s, ast.Expr)
                                and isinstance(s.value, ast.Constant)
                                and isinstance(s.value.value, str))]
                node.body = body
                return ast.unparse(node)
        return None

    def test_to_epoch_ms_consistent(self):
        a = self._normalized("mcp_server.py", "_to_epoch_ms")
        b = self._normalized("upload_server.py", "_to_epoch_ms")
        self.assertIsNotNone(a); self.assertIsNotNone(b)
        self.assertEqual(a, b, "_to_epoch_ms 在 mcp_server 与 upload_server 中已漂移")

    def test_check_ident_consistent(self):
        a = self._normalized("mcp_server.py", "_check_ident")
        b = self._normalized("upload_server.py", "_check_ident")
        self.assertIsNotNone(a); self.assertIsNotNone(b)
        self.assertEqual(a, b, "_check_ident 在 mcp_server 与 upload_server 中已漂移")

    def test_ident_regex_consistent(self):
        import re
        pats = {}
        for fn in ("mcp_server.py", "upload_server.py"):
            path = os.path.join(self.SERVER, fn)
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as f:
                m = re.search(r"_IDENT\s*=\s*re\.compile\((.*?)\)\s*$",
                              f.read(), re.M)
            if m:
                pats[fn] = m.group(1)
        self.assertGreaterEqual(len(pats), 2, "未找到足够的 _IDENT 定义")
        self.assertEqual(len(set(pats.values())), 1, f"_IDENT 正则不一致: {pats}")


class TestCommentHygiene(unittest.TestCase):
    """注释完整性守卫（第 8 轮外部审查 P3-2）。

    V1.1 注释元信息清理曾在 10 处多行注释块首行把标签削成孤立的 `# ：`
    （大量功能测试完全测不到，靠独立审查才发现）。本守卫永久拦住该类破损：
    任何 .py 中不得存在以孤立标点开头的注释行。
    """

    ROOT = os.path.dirname(os.path.abspath(__file__))

    def test_no_orphan_punctuation_comments(self):
        import re
        pattern = re.compile(r"^\s*#\s*[：:，。、；]")
        offenders = []
        for sub in ("", "server", "tools"):
            d = os.path.join(self.ROOT, sub) if sub else self.ROOT
            for fn in sorted(os.listdir(d)):
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(d, fn)
                with open(path, encoding="utf-8") as f:
                    for i, line in enumerate(f, 1):
                        if pattern.match(line):
                            offenders.append(f"{os.path.relpath(path, self.ROOT)}:L{i}")
        self.assertEqual(offenders, [],
                         "存在孤立标点开头的注释行（疑似标签被剥离），请修复注释：\n"
                         + "\n".join(offenders))


class TestSplitDbFallbackListParity(unittest.TestCase):
    """split_db_by_day 的 EVENT_TIME_COLUMNS 兜底副本与 utils 主清单一致性守护。

    split_db_by_day.py 在 utils 缺失/损坏时降级使用内联副本；若两份清单漂移，
    降级场景下部分事件表会被误判为静态表（整表复制进每个日库）。正常导入
    路径下兜底分支不执行，故用 AST 提取 except 分支中的列表字面量来比对
    （与 TestServerHelperParity 同一思路：漂移即失败）。
    """

    def test_fallback_event_time_columns_match_utils(self):
        import ast
        split_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "split_db_by_day.py")
        if not os.path.isfile(split_path):
            self.skipTest("split_db_by_day.py 不存在")
        with open(split_path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        fallback = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                for handler in node.handlers:
                    for stmt in handler.body:
                        if (isinstance(stmt, ast.Assign)
                                and getattr(stmt.targets[0], "id", "") == "EVENT_TIME_COLUMNS"
                                and isinstance(stmt.value, ast.List)):
                            fallback = [elt.value for elt in stmt.value.elts
                                        if isinstance(elt, ast.Constant)]
        self.assertIsNotNone(
            fallback, "未在 split_db_by_day.py 兜底分支找到 EVENT_TIME_COLUMNS")
        from utils import EVENT_TIME_COLUMNS
        self.assertEqual(
            fallback, list(EVENT_TIME_COLUMNS),
            "split_db_by_day 兜底清单与 utils.EVENT_TIME_COLUMNS 漂移——请同步两处（或直接删兜底副本改为强制依赖 utils）")


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
