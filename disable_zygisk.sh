#!/system/bin/sh
# 关闭 Magisk Zygisk，解决 OPPO 健康启动失败问题
# C-P2-19：失败即停（set -e）+ 前置检查 + UPDATE 后回读校验——
#   旧版任何一步静默失败都会照打印"已关闭"，误导用户重启了事。
set -e

# sqlite3 是本脚本唯一依赖，缺失时明确报错而不是半途而废
if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "❌ 设备上没有 sqlite3 命令，无法修改 magisk.db（请先在 adb shell 里确认 which sqlite3）"
    exit 1
fi

DB=/data/adb/magisk.db
if [ ! -f "$DB" ]; then
    echo "❌ 未找到 $DB（非 Magisk 环境？），放弃修改"
    exit 1
fi

echo "=== 关闭前的配置 ==="
sqlite3 "$DB" "SELECT * FROM settings;"

echo ""
echo "=== 正在关闭 Zygisk ==="
sqlite3 "$DB" "UPDATE settings SET value=0 WHERE key='zygisk';"

# 回读校验：UPDATE 失败/键不存在时旧版仍会打印"已关闭"
ZY="$(sqlite3 "$DB" "SELECT value FROM settings WHERE key='zygisk';" 2>/dev/null || true)"
if [ "$ZY" != "0" ]; then
    echo "❌ 回读校验失败：zygisk 当前值='$ZY'（期望 0）。Zygisk 未确认关闭，请勿重启，先排查。"
    exit 1
fi

echo ""
echo "=== 关闭后的配置 ==="
sqlite3 "$DB" "SELECT * FROM settings;"

echo ""
echo "✅ Zygisk 已关闭（已回读校验）！请硬重启模拟器使配置生效。"
echo "   注意：不要用 adb reboot（可能会挂住），请手动重启模拟器。"
