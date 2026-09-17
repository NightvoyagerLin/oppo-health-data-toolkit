# SPDX-License-Identifier: MIT
# -*- coding: utf-8 -*-
"""tools/rejoin.py —— 把 dist/ 下的分卷文件还原为原始大文件，并做 SHA256 校验。

用法：
    python tools/rejoin.py
效果：
    dist/frida-server            ← dist/frida-server.part0 + part1 + ...
    （APK 安装包未随仓库分发，不参与重组——来源见 README 与手册 1.4 节）
校验：与 dist/SHA256SUMS.txt 中记录的官方哈希逐一比对，全部一致输出 PASS。
"""
import hashlib
import os
import sys
import tempfile

# 强制 UTF-8 输出，避免 Windows 控制台/重定向乱码（V1.1 修复）
try:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from utils import ensure_utf8_stdout
    ensure_utf8_stdout()
except Exception:
    pass

DIST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dist")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def expected_hash(name):
    sums = os.path.join(DIST, "SHA256SUMS.txt")
    if not os.path.isfile(sums):
        return None
    with open(sums, "r", encoding="utf-8") as f:
        for line in f:
            parts = line.split(None, 1)
            # coreutils 二进制模式清单写作 `<hash>  *name`，
            #   原精确匹配会因星号失败并静默降级为"未校验"（用户误以为已校验）。现剥离前导 *。
            if len(parts) == 2 and parts[1].strip().lstrip("*") == name:
                return parts[0].strip()
    return None


def rejoin(out_name):
    # 按卷号数值排序（原字典序在 ≥10 卷时 part10 会排在
    #   part2 之前导致拼接错序），并校验卷号连续无缺漏。
    def _part_no(p):
        try:
            return int(p.rsplit("part", 1)[1])
        except ValueError:
            return -1
    parts = sorted(
        (p for p in os.listdir(DIST)
         if p.startswith(out_name + ".part")),
        key=_part_no,
    )
    if not parts:
        print("[跳过] %s：无分卷" % out_name)
        return False
    nums = [_part_no(p) for p in parts]
    if nums != list(range(len(nums))):
        print("[FAIL] %s：分卷缺漏（实际卷号：%s），拒绝拼接" % (out_name, nums))
        return False
    out = os.path.join(DIST, out_name)

    # 清单缺条目 = 无法校验，必须判为失败。
    #   status.startswith("PASS")——于是"SHA256SUMS.txt 被清空/损坏"时脚本会报
    #   "全部完成"，实际一次完整性校验都没做。未校验不等于通过。
    want = expected_hash(out_name)
    if want is None:
        print("[FAIL] %s：SHA256SUMS.txt 中无该条目，无法校验完整性，拒绝交付" % out_name)
        print("       请确认 dist/SHA256SUMS.txt 存在且含 %s 的哈希行。" % out_name)
        return False

    #   一旦哈希不符，损坏/被篡改的文件已经落在 dist/ 下且不会被删除，用户下次
    #   可能直接拿它去 push。现改为：先写临时文件 → 校验通过才 os.replace 到最终
    #   路径；任何一步失败都只删临时文件，**绝不把未校验的内容写到 out**。
    tmp_fd, tmp_path = tempfile.mkstemp(dir=DIST, prefix=out_name + ".", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "wb") as w:
            for p in parts:
                src = os.path.join(DIST, p)
                with open(src, "rb") as r:
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        w.write(chunk)
        got = sha256(tmp_path)
        if got != want:
            print("[FAIL] %s：哈希不一致，已丢弃拼接结果（分卷可能损坏，请重新下载）" % out_name)
            print("       期望 %s" % want)
            print("       实际 %s" % got)
            _report_stale(out, out_name)
            return False
        os.replace(tmp_path, out)
        tmp_path = None  # 已移交，finally 不再删除
        print("[OK] %s  sha256=%s  PASS" % (out_name, got[:16] + "..."))
        return True
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _report_stale(out, out_name):
    """拼接/校验失败时，检查是否残留了历史文件，避免用户误用未校验的二进制。"""
    if not os.path.exists(out):
        return
    old = sha256(out)
    want = expected_hash(out_name)
    if want and old == want:
        print("       （注：dist/%s 已存在且哈希正确，可继续使用）" % out_name)
    else:
        print("       ⚠️ dist/%s 存在但**未通过校验**，请勿使用；"
              "确认无用后可手动删除。" % out_name)


if __name__ == "__main__":
    if not os.path.isdir(DIST):
        print("未找到 dist/ 目录")
        sys.exit(1)
    ok = True
    ok &= rejoin("frida-server")
    print("全部完成" if ok else "存在校验失败，请重新下载对应分卷")
    sys.exit(0 if ok else 1)
