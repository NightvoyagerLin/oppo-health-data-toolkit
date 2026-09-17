// SPDX-License-Identifier: MIT
// Frida 自适应取密钥脚本：兼容 OPPO 健康 6.6.7（g/b）与最新版（getInstance/deCryptData）
// 用法：frida -D <序列号> -p $(adb shell pidof com.heytap.health) -l frida_get_dbkey.js
// ⚠️ 本脚本会向终端输出**明文数据库密钥**——禁止在共享屏幕 / 录屏 / 公共终端场景运行。
// 输出：DB_KEY_START:<密钥>:DB_KEY_END（供脚本解析）；同时打印诊断信息。
Java.perform(function() {
    var CLS = "com.heytap.health.base.encrypt.AesGcmAndroidKeyStore";
    console.log("[*] Hooking " + CLS + " ...");
    try {
        var Cls = Java.use(CLS);
        console.log("[+] Found class");

        // 1) 取单例：6.6.7 为 g()；最新版混淆后为 getInstance()
        var inst = null;
        try { inst = Cls.g(); console.log("[+] g() OK"); } catch (e) {}
        if (inst === null || inst === undefined) {
            try { inst = Cls.getInstance(); console.log("[+] getInstance() OK"); } catch (e) {}
        }
        if (inst === null || inst === undefined) {
            console.log("DB_KEY_ERROR:no factory method worked");
            return;
        }

        // 2) 解密 db_key：6.6.7 为 b(alias, ssoid)；最新版为 deCryptData(alias, ssoid)
        var key = null, errs = [];
        try { key = inst.b("db_key", null); } catch (e) { errs.push("b:" + e.message); }
        if (key === null || key === undefined || key === "") {
            try { key = inst.deCryptData("db_key", null); } catch (e) { errs.push("deCryptData:" + e.message); }
        }
        if (key === null || key === undefined || key === "") {
            console.log("DB_KEY_ERROR:" + errs.join(" | "));
            return;
        }
        console.log("[+] Key length: " + key.length);
        // 机器通道：此行输出完整密钥供 export_health_data.py 解析，不能脱敏；
        //   手工用 frida CLI 运行时终端会显示明文，请勿在共享屏幕/录屏场景使用。
        console.log("DB_KEY_START:" + key + ":DB_KEY_END");
        console.log("[+] db_key(脱敏): " + (key && key.length > 8
            ? key.slice(0, 4) + "…" + key.slice(-4)
            : "(长度异常)"));
    } catch (e) {
        console.log("DB_KEY_ERROR:" + e.message);
    }
});
