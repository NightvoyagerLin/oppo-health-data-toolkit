# SPDX-License-Identifier: MIT
"""
OPPO 健康数据合理性测试
========================
检查导出的数据库中是否存在逻辑错误、异常值、数据不一致等问题。

检查项：
1. 睡眠数据合理性（时长、深睡比例、入睡时间）
2. 心率数据合理性（范围、静息心率）
3. 血氧数据合理性（范围、异常低值）
4. 运动数据合理性（步数、卡路里、距离）
5. 压力数据合理性
6. 日期范围一致性
7. 异常值检测（离群值）
8. 数据完整性（空表、缺失字段）

使用方法：
    python data_validation.py [数据库路径]
    python data_validation.py <工作目录>\\DB\\combined\\database_decrypted.db
"""

import os
import re
import sys

# 强制 UTF-8 输出，避免 Windows 控制台/重定向乱码（V1.1 修复）
from utils import ensure_utf8_stdout
ensure_utf8_stdout()

import sqlite3
from datetime import datetime, timedelta, timezone

# 本项目统一以中国时区(UTC+8)为"今天"与所有日期口径的基准
#   （与 auto_export / generate_html_report / split_db_by_day 的 CN 保持一致），
#   避免跨时区机器上出现日期偏移或漏报一天。
CN = timezone(timedelta(hours=8))


def _q_ident(name):
    """SQL 标识符安全引用：OPPO 健康表名均为 [A-Za-z0-9_] 风格，直接放行；
    含特殊字符的名字（异常库/伪造库文件）用双引号包裹并把内部引号翻倍，
    杜绝 PRAGMA table_info / FROM 拼接被 `"`、`]` 等字符破坏。"""
    s = str(name)
    if re.fullmatch(r"[A-Za-z0-9_]+", s):
        return s
    return '"' + s.replace('"', '""') + '"'

# 添加本地模块路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 清理：移除未使用的 defaultdict / safe_float 导入
from utils import safe_int


class DataValidator:
    """数据合理性校验器"""

    def __init__(self, db_path):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.cursor = self.conn.cursor()
        # sqlite3.connect 惰性成功，非 SQLite 文件（半截文件 / 错误密钥产物）
        #   要到首个查询才抛 DatabaseError 且未被捕获，排错工具反而以堆栈崩溃收场。此处提前探测。
        try:
            self.conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlite3.DatabaseError as e:
            self.conn.close()  # C-P3-10：探测失败时先释放连接再抛出，避免句柄泄漏
            raise sqlite3.DatabaseError(
                "不是一个可用的 SQLite 数据库（可能解密失败 / 文件不完整 / 密钥错误）：%s" % e) from e
        self.issues = []  # (级别, 类别, 描述, 详情)
        self.stats = {}

    def log(self, level, category, description, detail=""):
        """记录问题"""
        self.issues.append({
            "level": level,  # ERROR, WARNING, INFO
            "category": category,
            "description": description,
            "detail": detail,
        })

    def table_exists(self, table_name):
        """检查表是否存在"""
        self.cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        )
        return self.cursor.fetchone() is not None

    def missing_columns(self, table_name, cols):
        """返回表中**缺失**的列名列表：先做列存在性校验，缺列时降级为告警
        （避免 App 版本列名漂移导致整个校验工具崩溃）。"""
        try:
            self.cursor.execute(f'PRAGMA table_info({_q_ident(table_name)})')
            have = {r[1] for r in self.cursor.fetchall()}
            return [c for c in cols if c not in have]
        except Exception:
            # 连 PRAGMA 都失败（表不存在/库异常）时按"全部缺失"处理，交由上层告警
            return list(cols)

    def get_table_row_count(self, table_name):
        """获取表行数"""
        try:
            self.cursor.execute(f"SELECT COUNT(*) FROM {_q_ident(table_name)}")
            return self.cursor.fetchone()[0]
        except Exception:
            return 0

    # 各检查项用 fetchall() 一次性取回结果。
    #   本工具目标表均为"日汇总"表（每表≈天数行），规模可控；但若库结构变化
    #   （如指向采样明细表）或数据量异常，峰值内存仍会随行数线性膨胀。
    #   此处显式设定阈值并在超限时告警，把该风险从"静默"变为"可见"。
    _BIG_TABLE_ROWS = 200000

    def _warn_if_huge(self, table_name, n):
        """行数超过阈值时记录性能告警（不中断校验）。"""
        if n > self._BIG_TABLE_ROWS:
            self.log("WARNING", "性能",
                     f"{table_name} 行数 {n} 超过阈值 {self._BIG_TABLE_ROWS}",
                     "一次性 fetchall() 会占用较多内存，建议按日期区间分批校验")

    def validate_all(self):
        """执行所有校验"""
        print("=" * 70)
        print("  OPPO 健康数据合理性测试")
        print(f"  数据库: {self.db_path}")
        print(f"  测试时间: {datetime.now(CN).strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 70)
        print()

        # 基本信息
        self._check_basic_info()

        # 各维度数据校验
        self._check_sleep_data()
        self._check_heart_rate_data()
        self._check_spo2_data()
        self._check_exercise_data()
        self._check_stress_data()

        # 跨表一致性校验
        self._check_date_consistency()
        self._check_outliers()

        # 输出报告
        self._print_report()

        return len([i for i in self.issues if i["level"] == "ERROR"]) == 0

    def _check_basic_info(self):
        """检查基本信息"""
        print("📊 1. 基本信息检查...")

        # 排除 SQLite 内部表 sqlite_sequence
        #   （与 health_export_gui / split_db_by_day 的口径一致），避免表计数被虚增。
        self.cursor.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name <> 'sqlite_sequence'")
        table_count = self.cursor.fetchone()[0]
        self.stats["table_count"] = table_count
        print(f"   表数量: {table_count}")

        if table_count < 30:
            self.log("WARNING", "基本信息", f"表数量偏少: {table_count} < 30",
                     "可能数据不完整")

        # 统计总行数（C-P3-6：排除 sqlite_sequence，与上方表数量口径一致，
        #   避免内部表计入 total_rows / 被列进空表清单）
        self.cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name <> 'sqlite_sequence'")
        tables = [row[0] for row in self.cursor.fetchall()]
        total_rows = 0
        empty_tables = []
        for table in tables:
            count = self.get_table_row_count(table)
            total_rows += count
            if count == 0:
                empty_tables.append(table)

        self.stats["total_rows"] = total_rows
        self.stats["empty_tables"] = empty_tables
        print(f"   总行数: {total_rows}")
        print(f"   空表数量: {len(empty_tables)}")

        # 表结构齐全但**全部零行**的库（空库 / 解密失败残留）此前
        #   只报 INFO/WARNING，validate_all() 仍返回 True、退出码 0，给出虚假的安全信号。
        #   本工具的核心用途是"识别坏库"，零行库必须升级为 ERROR。
        if total_rows == 0:
            self.log("ERROR", "基本信息", "数据库不含任何数据行（0 行）",
                     "可能是空库、解密失败残留或导出中断产物；请重新导出后再校验")

        if empty_tables:
            self.log("INFO", "基本信息", f"空表: {', '.join(empty_tables[:10])}",
                     f"共 {len(empty_tables)} 个空表（可能是正常的未使用功能表）")

        print()

    def _check_sleep_data(self):
        """检查睡眠数据合理性"""
        print("😴 2. 睡眠数据检查...")

        if not self.table_exists("DBSleepDataStatTable"):
            self.log("ERROR", "睡眠", "睡眠数据表不存在", "DBSleepDataStatTable")
            print("   ❌ 睡眠数据表不存在")
            print()
            return

        # 列存在性校验——缺列时降级告警并跳过，
        #   不再因 `no such column` 的 OperationalError 穿透 validate_all 崩溃。
        _miss = self.missing_columns("DBSleepDataStatTable", [
            "date", "total_sleep_time", "total_deep_sleep_time",
            "total_lightly_sleep_time", "total_rem_time",
            "sleep_score", "fall_asleep", "sleep_out"])
        if _miss:
            self.log("WARNING", "睡眠", f"缺少列: {_miss}",
                     "库结构与预期不一致（可能 App 版本差异），跳过该项检查")
            print(f"   ⚠️  睡眠数据缺少列 {_miss}，跳过该项检查")
            print()
            return

        self.cursor.execute("""
            SELECT date, total_sleep_time, total_deep_sleep_time,
                   total_lightly_sleep_time, total_rem_time,
                   sleep_score, fall_asleep, sleep_out
            FROM DBSleepDataStatTable
            ORDER BY date
        """)
        rows = self.cursor.fetchall()

        if not rows:
            self.log("WARNING", "睡眠", "睡眠数据为空", "DBSleepDataStatTable 无数据")
            print("   ⚠️  睡眠数据为空")
            print()
            return

        self._warn_if_huge("DBSleepDataStatTable", len(rows))
        print(f"   睡眠记录数: {len(rows)}")
        print(f"   日期范围: {rows[0][0]} ~ {rows[-1][0]}")

        abnormal_sleep = []
        abnormal_deep_ratio = []
        abnormal_fall_asleep = []
        sleep_scores = []

        for row in rows:
            date, total, deep, light, rem, score, fall_asleep, sleep_out = row
            total = safe_int(total)
            deep = safe_int(deep)
            score = safe_int(score)
            fall_asleep = safe_int(fall_asleep)

            # 睡眠时长检查（0-16小时 = 0-960分钟）
            # total=0 表示当日无监测/未佩戴（"无数据"），
            #   不是"异常的睡眠时长"，不应与真实短睡（1-59分钟）混为一谈。
            if total > 960 or 0 < total < 60:
                abnormal_sleep.append((date, total))

            # 深睡比例数据合理性检查（超出 5%~50% 视为异常值）
            #   deep==0 视为"该晚无深睡记录/缺失"跳过：0 作分子无比例意义，
            #   且库中"缺失"与"真实 0 深睡"不可区分（T-6 口径备忘）。
            if total > 0 and deep > 0:
                ratio = deep / total * 100
                if ratio < 5 or ratio > 50:
                    abnormal_deep_ratio.append((date, ratio))

            # 入睡时间检查：白天入睡（06:00-18:00 = 360-1080分钟）视为异常
            if fall_asleep > 0:
                fa_mod = fall_asleep % 1440
                hours = fa_mod / 60
                if 6 <= hours < 18:
                    abnormal_fall_asleep.append((date, fall_asleep, hours))

            if score > 0:
                sleep_scores.append(score)

        # 输出异常
        if abnormal_sleep:
            self.log("WARNING", "睡眠", f"睡眠时长异常: {len(abnormal_sleep)} 天",
                     f"例如: {abnormal_sleep[0][0]} = {abnormal_sleep[0][1]}分钟")
            print(f"   ⚠️  睡眠时长异常: {len(abnormal_sleep)} 天")

        if abnormal_deep_ratio:
            self.log("WARNING", "睡眠", f"深睡比例异常: {len(abnormal_deep_ratio)} 天",
                     f"例如: {abnormal_deep_ratio[0][0]} = {abnormal_deep_ratio[0][1]:.1f}%")
            print(f"   ⚠️  深睡比例异常: {len(abnormal_deep_ratio)} 天")

        if abnormal_fall_asleep:
            self.log("INFO", "睡眠", f"入睡时间异常（白天入睡）: {len(abnormal_fall_asleep)} 天",
                     f"可能是午睡或数据异常")
            print(f"   ℹ️  白天入睡: {len(abnormal_fall_asleep)} 天")

        # 睡眠评分统计
        if sleep_scores:
            avg_score = sum(sleep_scores) / len(sleep_scores)
            print(f"   平均睡眠评分: {avg_score:.1f}")
            if avg_score < 60:
                self.log("WARNING", "睡眠", f"平均睡眠评分偏低: {avg_score:.1f}",
                         "可能睡眠质量较差")

        print()

    def _check_heart_rate_data(self):
        """检查心率数据合理性"""
        print("❤️ 3. 心率数据检查...")

        if not self.table_exists("DBHeartRateDataStatTable"):
            self.log("ERROR", "心率", "心率数据表不存在", "DBHeartRateDataStatTable")
            print("   ❌ 心率数据表不存在")
            print()
            return

        # 列存在性校验（缺列降级告警，不再崩溃）
        _miss = self.missing_columns("DBHeartRateDataStatTable", [
            "date", "min_hr", "max_hr", "average_hr", "rest_hr"])
        if _miss:
            self.log("WARNING", "心率", f"缺少列: {_miss}",
                     "库结构与预期不一致（可能 App 版本差异），跳过该项检查")
            print(f"   ⚠️  心率数据缺少列 {_miss}，跳过该项检查")
            print()
            return

        self.cursor.execute("""
            SELECT date, min_hr, max_hr, average_hr,
                   rest_hr
            FROM DBHeartRateDataStatTable
            ORDER BY date
        """)
        rows = self.cursor.fetchall()

        if not rows:
            self.log("WARNING", "心率", "心率数据为空", "")
            print("   ⚠️  心率数据为空")
            print()
            return

        self._warn_if_huge("DBHeartRateDataStatTable", len(rows))
        print(f"   心率记录数: {len(rows)}")

        abnormal_hr = []
        resting_hrs = []
        avg_hrs = []

        for row in rows:
            date, min_hr, max_hr, avg_hr, resting_hr = row
            min_hr = safe_int(min_hr)
            max_hr = safe_int(max_hr)
            avg_hr = safe_int(avg_hr)
            resting_hr = safe_int(resting_hr)

            # 心率范围检查（30-220）
            for label, val in [("最低", min_hr), ("最高", max_hr), ("平均", avg_hr)]:
                if val > 0 and (val < 30 or val > 220):
                    abnormal_hr.append((date, label, val))

            # 逻辑检查：最低 <= 平均 <= 最高
            if min_hr > 0 and max_hr > 0 and avg_hr > 0:
                if not (min_hr <= avg_hr <= max_hr):
                    abnormal_hr.append((date, "逻辑", f"min={min_hr}, avg={avg_hr}, max={max_hr}"))

            if resting_hr > 0:
                resting_hrs.append(resting_hr)
            if avg_hr > 0:
                avg_hrs.append(avg_hr)

        if abnormal_hr:
            self.log("WARNING", "心率", f"心率异常: {len(abnormal_hr)} 条",
                     f"例如: {abnormal_hr[0]}")
            print(f"   ⚠️  心率异常: {len(abnormal_hr)} 条")

        if resting_hrs:
            avg_resting = sum(resting_hrs) / len(resting_hrs)
            print(f"   平均静息心率: {avg_resting:.1f} bpm")
            if avg_resting < 40:
                self.log("WARNING", "心率", f"静息心率偏低: {avg_resting:.1f}",
                         "可能是运动员心脏或数据异常")
            elif avg_resting > 100:
                self.log("WARNING", "心率", f"静息心率偏高: {avg_resting:.1f}",
                         "可能是心动过速或数据异常")

        print()

    def _check_spo2_data(self):
        """检查血氧数据合理性"""
        print("🩸 4. 血氧数据检查...")

        if not self.table_exists("DBBloodOxygenSaturationDataStat"):
            self.log("INFO", "血氧", "血氧数据表不存在", "可能设备不支持血氧监测")
            print("   ℹ️  血氧数据表不存在")
            print()
            return

        # 列存在性校验（缺列降级告警，不再崩溃）
        _miss = self.missing_columns("DBBloodOxygenSaturationDataStat", [
            "date", "min_blood_oxygen_saturation", "max_blood_oxygen_saturation",
            "average_blood_oxygen_saturation", "low_blood_oxygen_saturation_total_time"])
        if _miss:
            self.log("WARNING", "血氧", f"缺少列: {_miss}",
                     "库结构与预期不一致（可能 App 版本差异），跳过该项检查")
            print(f"   ⚠️  血氧数据缺少列 {_miss}，跳过该项检查")
            print()
            return

        self.cursor.execute("""
            SELECT date, min_blood_oxygen_saturation, max_blood_oxygen_saturation,
                   average_blood_oxygen_saturation, low_blood_oxygen_saturation_total_time
            FROM DBBloodOxygenSaturationDataStat
            ORDER BY date
        """)
        rows = self.cursor.fetchall()

        if not rows:
            self.log("INFO", "血氧", "血氧数据为空", "")
            print("   ℹ️  血氧数据为空")
            print()
            return

        self._warn_if_huge("DBBloodOxygenSaturationDataStat", len(rows))
        print(f"   血氧记录数: {len(rows)}")

        abnormal_spo2 = []
        suspicious_low = []

        for row in rows:
            date, min_spo2, max_spo2, avg_spo2, low_time = row
            min_spo2 = safe_int(min_spo2)
            max_spo2 = safe_int(max_spo2)
            avg_spo2 = safe_int(avg_spo2)
            low_time = safe_int(low_time)

            # 血氧范围检查（70-100）
            for label, val in [("最低", min_spo2), ("最高", max_spo2), ("平均", avg_spo2)]:
                if val > 0 and (val < 70 or val > 100):
                    abnormal_spo2.append((date, label, val))

            # 可疑低值：最低 < 90 但低血氧时长 = 0
            if min_spo2 > 0 and min_spo2 < 90 and low_time == 0:
                suspicious_low.append((date, min_spo2))

        if abnormal_spo2:
            self.log("WARNING", "血氧", f"血氧值异常: {len(abnormal_spo2)} 条",
                     f"例如: {abnormal_spo2[0]}")
            print(f"   ⚠️  血氧异常: {len(abnormal_spo2)} 条")

        if suspicious_low:
            self.log("INFO", "血氧", f"疑似单次误测: {len(suspicious_low)} 天",
                     f"最低血氧<90%但低血氧时长=0，可能是手表松动导致的测量误差。例如: {suspicious_low[0]}")
            print(f"   ℹ️  疑似单次误测: {len(suspicious_low)} 天（最低<90%但时长=0）")

        print()

    def _check_exercise_data(self):
        """检查运动数据合理性"""
        print("🏃 5. 运动数据检查...")

        if not self.table_exists("DBSportDataStat"):
            self.log("ERROR", "运动", "运动数据表不存在", "DBSportDataStat")
            print("   ❌ 运动数据表不存在")
            print()
            return

        # 列存在性校验（缺列降级告警，不再崩溃）
        _miss = self.missing_columns("DBSportDataStat", [
            "date", "total_steps", "total_distance", "total_calories",
            "total_static_cal", "total_duration", "sport_mode"])
        if _miss:
            self.log("WARNING", "运动", f"缺少列: {_miss}",
                     "库结构与预期不一致（可能 App 版本差异），跳过该项检查")
            print(f"   ⚠️  运动数据缺少列 {_miss}，跳过该项检查")
            print()
            return

        # 用 sport_mode = -2（日汇总）
        self.cursor.execute("""
            SELECT date, total_steps, total_distance, total_calories,
                   total_static_cal, total_duration, sport_mode
            FROM DBSportDataStat
            WHERE CAST(sport_mode AS INTEGER) = -2
            ORDER BY date
        """)
        rows = self.cursor.fetchall()

        if not rows:
            self.log("WARNING", "运动", "运动日汇总数据为空", "sport_mode=-2 无数据")
            print("   ⚠️  运动日汇总数据为空")
            print()
            return

        self._warn_if_huge("DBSportDataStat", len(rows))
        print(f"   运动记录数: {len(rows)} (sport_mode=-2)")

        abnormal_steps = []
        abnormal_calories = []
        abnormal_distance = []

        for row in rows:
            date, steps, distance, calories, static_cal, duration, mode = row
            steps = safe_int(steps)
            distance = safe_int(distance)
            calories = safe_int(calories)
            duration = safe_int(duration)

            # 步数检查（0-100000）
            if steps > 100000:
                abnormal_steps.append((date, steps))

            # 距离检查（0-100km = 0-100000m）
            if distance > 100000:
                abnormal_distance.append((date, distance))

            # 卡路里检查（阈值 10,000,000 毫千卡 = 10,000 千卡）
            # 原注释"单位是0.001卡/毫卡"口径写错——
            #   按手册 3.3，DBSportDataStat.total_calories 原始单位为毫千卡（0.001 千卡），
            #   10,000,000 毫千卡 = 10,000 千卡（阈值本身正确，仅注释误导）。
            if calories > 10000000:
                abnormal_calories.append((date, calories))

            # 逻辑检查：步数 > 0 但距离 = 0
            if steps > 0 and distance == 0:
                abnormal_distance.append((date, f"步数={steps}但距离=0"))

        if abnormal_steps:
            self.log("WARNING", "运动", f"步数异常: {len(abnormal_steps)} 天",
                     f"例如: {abnormal_steps[0]}")
            print(f"   ⚠️  步数异常: {len(abnormal_steps)} 天")

        if abnormal_calories:
            self.log("WARNING", "运动", f"卡路里异常: {len(abnormal_calories)} 天",
                     f"例如: {abnormal_calories[0]}")
            print(f"   ⚠️  卡路里异常: {len(abnormal_calories)} 天")

        # 平均步数
        # 与报告统一"是否含 0 步天"口径——
        #   佩戴但未走路是真实数据，不应剔除（原 `>0` 过滤会把日均抬高，
        #   实测同一库本校验值明显高于报告值）。同时显式标注时间范围差异：
        #   本校验为全历史日均，分析报告为最近 7 个完整日均值，数值本就不同。
        all_steps = [safe_int(r[1]) for r in rows]
        if all_steps:
            avg_steps = sum(all_steps) / len(all_steps)
            print(f"   平均步数: {avg_steps:.0f}（全部 {len(all_steps)} 天均值，含 0 步天；"
                  f"分析报告口径为最近 7 个完整日，时间段不同数值本就不同）")

        print()

    def _check_stress_data(self):
        """检查压力数据合理性"""
        print("😰 6. 压力数据检查...")

        if not self.table_exists("DBStressDataStatTable"):
            self.log("INFO", "压力", "压力数据表不存在", "可能设备不支持压力监测")
            print("   ℹ️  压力数据表不存在")
            print()
            return

        # 列存在性校验（缺列降级告警，不再崩溃）
        _miss = self.missing_columns("DBStressDataStatTable", [
            "date", "average_hr", "relax_stress_total_time",
            "normal_stress_total_time", "middle_stress_total_time",
            "high_stress_total_time"])
        if _miss:
            self.log("WARNING", "压力", f"缺少列: {_miss}",
                     "库结构与预期不一致（可能 App 版本差异），跳过该项检查")
            print(f"   ⚠️  压力数据缺少列 {_miss}，跳过该项检查")
            print()
            return

        self.cursor.execute("""
            SELECT date, average_hr, relax_stress_total_time,
                   normal_stress_total_time, middle_stress_total_time,
                   high_stress_total_time
            FROM DBStressDataStatTable
            ORDER BY date
        """)
        rows = self.cursor.fetchall()

        if not rows:
            self.log("INFO", "压力", "压力数据为空", "")
            print("   ℹ️  压力数据为空")
            print()
            return

        self._warn_if_huge("DBStressDataStatTable", len(rows))
        print(f"   压力记录数: {len(rows)}")

        # 检查"压力监测心率"字段（实际不是心率，值通常很低）
        hr_values = [safe_int(r[1]) for r in rows if safe_int(r[1]) > 0]
        if hr_values:
            avg_hr = sum(hr_values) / len(hr_values)
            print(f"   '压力监测心率'平均值: {avg_hr:.1f}（注意：此字段并非真实心率）")
            if avg_hr < 40:
                self.log("INFO", "压力", f"'压力监测心率'字段值偏低: {avg_hr:.1f}",
                         "此字段并非真实心率，实际含义未知，请勿按心率解读")

        print()

    def _check_date_consistency(self):
        """检查跨表日期一致性"""
        print("📅 7. 日期范围一致性检查...")

        date_ranges = {}
        tables_to_check = [
            ("DBSleepDataStatTable", "睡眠"),
            ("DBHeartRateDataStatTable", "心率"),
            ("DBSportDataStat", "运动"),
            ("DBStressDataStatTable", "压力"),
        ]

        for table_name, label in tables_to_check:
            if self.table_exists(table_name):
                try:
                    self.cursor.execute(f"SELECT MIN(date), MAX(date) FROM {_q_ident(table_name)}")
                    min_date, max_date = self.cursor.fetchone()
                    if min_date and max_date:
                        date_ranges[label] = (min_date, max_date)
                        print(f"   {label}: {min_date} ~ {max_date}")
                except Exception as e:
                    self.log("WARNING", "日期一致性", f"{label}日期查询失败: {e}", "")

        # 检查日期格式
        for label, (min_date, max_date) in date_ranges.items():
            for d in [min_date, max_date]:
                if d and len(str(d)) != 8:
                    self.log("WARNING", "日期一致性", f"{label}日期格式异常: {d}",
                             "期望 YYYYMMDD 格式")

        # 检查是否有未来日期（L4修复：按 CN(UTC+8) 取"今天"）
        today = datetime.now(CN).strftime("%Y%m%d")
        for label, (min_date, max_date) in date_ranges.items():
            if max_date and str(max_date) > today:
                self.log("WARNING", "日期一致性", f"{label}存在未来日期: {max_date}",
                         f"今天是 {today}")

        print()

    def _check_outliers(self):
        """检查异常值（离群值）"""
        print("📈 8. 异常值检测...")

        # 检查睡眠时长的离群值
        if self.table_exists("DBSleepDataStatTable"):
            self.cursor.execute("SELECT total_sleep_time FROM DBSleepDataStatTable WHERE CAST(total_sleep_time AS REAL) > 0")
            sleep_times = [safe_int(r[0]) for r in self.cursor.fetchall()]
            if len(sleep_times) >= 5:
                sleep_times.sort()
                q1 = sleep_times[len(sleep_times) // 4]
                q3 = sleep_times[3 * len(sleep_times) // 4]
                iqr = q3 - q1
                lower = q1 - 1.5 * iqr
                upper = q3 + 1.5 * iqr
                outliers = [t for t in sleep_times if t < lower or t > upper]
                if outliers:
                    # L10修复：原实现用 outliers[:5] 截断却不说明，且"范围"实为 IQR 上下界
                    #   （非数据真实范围），却与列在界外的离群值同句罗列，语义冲突。
                    #   现在显式标注 IQR 上下界、完整列出离群值（超长折行）。
                    more = "" if len(outliers) <= 10 else f" …（仅显示前10个，共{len(outliers)}个）"
                    self.log("INFO", "异常值",
                             f"睡眠时长离群值: {len(outliers)} 个",
                             f"IQR 正常区间: {lower:.0f}~{upper:.0f}分钟（Q1-1.5IQR ~ Q3+1.5IQR）；"
                             f"超出该区间的离群值: {outliers[:10]}{more}")
                    print(f"   ℹ️  睡眠时长离群值: {len(outliers)} 个（正常区间 {lower:.0f}~{upper:.0f}分钟）")

        print()

    def _print_report(self):
        """打印校验报告"""
        print("=" * 70)
        print("  校验结果汇总")
        print("=" * 70)

        errors = [i for i in self.issues if i["level"] == "ERROR"]
        warnings = [i for i in self.issues if i["level"] == "WARNING"]
        infos = [i for i in self.issues if i["level"] == "INFO"]

        print(f"\n❌ 错误 (ERROR): {len(errors)}")
        for issue in errors:
            print(f"   [{issue['category']}] {issue['description']}")
            if issue['detail']:
                print(f"      详情: {issue['detail']}")

        print(f"\n⚠️  警告 (WARNING): {len(warnings)}")
        for issue in warnings:
            print(f"   [{issue['category']}] {issue['description']}")
            if issue['detail']:
                print(f"      详情: {issue['detail']}")

        print(f"\nℹ️  信息 (INFO): {len(infos)}")
        for issue in infos:
            print(f"   [{issue['category']}] {issue['description']}")
            if issue['detail']:
                print(f"      详情: {issue['detail']}")

        print()
        # L8修复：有INFO时也不应该打印未发现异常
        if errors:
            print("❌ 校验未通过，存在致命错误！")
        elif warnings:
            print("⚠️  校验通过（有警告但无致命错误）")
        elif infos:
            print(f"ℹ️  校验通过（有 {len(infos)} 条提示信息，无错误/警告）")
        else:
            print("✅ 校验通过，未发现异常！")

        print("=" * 70)

    def close(self):
        """关闭数据库连接"""
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass


def main():
    """主函数"""
    # 自定位：搜索根 = 本脚本所在目录下的 DB 子目录，随文件夹整体移动
    _root = os.path.dirname(os.path.abspath(__file__))
    # 无参运行时的库选择见下方 candidates 逻辑：优先 DB\combined，其次 DB\<日期>。
    if len(sys.argv) > 1:
        db_path = sys.argv[1]
    else:
        db_root = os.path.join(_root, "DB")
        candidates = []
        if os.path.isdir(db_root):
            for d in os.listdir(db_root):
                # 排除基准快照目录（test_ 前缀）——
                #   快照是测试资产而非导出数据，无参运行不应静默把它当校验对象
                #   （实测全新环境中无参校验的就是快照，与"自动取最新导出目录"语义不符）。
                if d.startswith("test_"):
                    continue
                full = os.path.join(db_root, d)
                # 修复：L5 首版把「目录」而不是「库文件路径」塞进
                #   candidates，导致无参运行时 sqlite3.connect(目录) 必现
                #   "unable to open database file"。此处必须存文件路径本身。
                db_file = os.path.join(full, "database_decrypted.db")
                if os.path.isfile(db_file):
                    candidates.append((os.path.getmtime(db_file), db_file))
        if candidates:
            # 说明：合并全量库 DB\combined 恒为最新且完整，
            #   优先校验它；否则 split_db_by_day.py 产出的日库（mtime=拆分时间）
            #   会比 combined 新，导致按 mtime 误选单日库做全量校验（实测：
            #   某单日库无压力日汇总 → 误报"压力数据为空"）。
            combined_file = os.path.join(db_root, "combined", "database_decrypted.db")
            if os.path.isfile(combined_file):
                db_path = combined_file
            else:
                candidates.sort(reverse=True)
                db_path = candidates[0][1]
        else:
            #   用户按附录C 无参运行会看到指向"不存在的测试资产"的提示，误以为自己的环境坏了。
            #   现直接给出明确指引：先跑导出。
            print("=" * 60)
            print("❌ 缺少待校验的数据库（这是预期行为，不是脚本故障）")
            print("=" * 60)
            print("未找到以下任一文件：")
            print("   • DB\\combined\\database_decrypted.db")
            print("   • DB\\<日期>\\database_decrypted.db")
            print()
            print("请按下列顺序操作：")
            print("   1. 先完成导出：python auto_export_and_analyze.py")
            print("   2. 再运行校验：python data_validation.py")
            print()
            print("若已有解密库，也可显式指定路径：")
            print("   python data_validation.py <path\\to\\database_decrypted.db>")
            print("=" * 60)
            sys.exit(1)

    if not os.path.exists(db_path):
        print(f"❌ 数据库文件不存在: {db_path}")
        print("用法: python data_validation.py [数据库路径]")
        sys.exit(1)

    # 给非库文件一个可读的失败信息，而不是 Python 堆栈
    try:
        validator = DataValidator(db_path)
    except sqlite3.DatabaseError as e:
        print(f"❌ 无法读取数据库: {db_path}")
        print(f"   {e}")
        print("   提示：请确认该文件是导出流程产出的 database_decrypted.db（已解密且完整）。")
        sys.exit(1)
    try:
        success = validator.validate_all()
        sys.exit(0 if success else 1)
    finally:
        validator.close()


if __name__ == "__main__":
    main()
