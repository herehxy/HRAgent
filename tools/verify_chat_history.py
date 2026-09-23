"""需求 2 实机验证：清空（软）/ 恢复 / 到期清理，以及 agent_runs 未在清空时被物理删除。

口径（用户原话）：「删除三十天之后自动清理，30 天之内可以恢复」——
点「清空」只推进清空点并开出 30 天保留期；保留期内 `POST /api/agent/restore` 可找回；
到期由后台任务（启动一次 + 每 24 小时）执行 `db.purge_due_chats()` 真正清掉。

做法：清空点是三个设置项（时间戳 + 运行序号 + 到期时间），先快照、跑完原样还原；
为了造出"可清空的可见历史"，脚本会真的问几句，这些**测试问答在收尾时按水位线删掉**，
所以这次验证既不会弄丢真实对话、也不会在 HR 的历史里留下测试痕迹。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import db  # noqa: E402

BASE = "http://127.0.0.1:8765"
DB_PATH = os.path.join(ROOT, "data", "workbench.db")


def http(method: str, path: str, body: dict | None = None) -> dict:
    cmd = ["curl", "-s", "--noproxy", "*", "-m", "30", "-X", method, BASE + path]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    return json.loads(out)


def main() -> int:
    conn = db.connect(DB_PATH)
    # 清空点是三个设置项（时间戳 + 运行序号 + 到期时间），一并快照，跑完原样还原，
    # 所以这次验证不会真的弄丢工作台里的对话。
    _keys = (db.CHAT_CLEARED_KEY, db.CHAT_CLEARED_ID_KEY, db.CHAT_PURGE_AFTER_KEY)
    snapshot = {k: db.get_setting(conn, k) for k in _keys}
    print(f"清空点快照：{snapshot!r}")
    # 本脚本为了验证必须真的问几句（要造出"可清空的可见历史"）。这些问答是**测试产物**，
    # 不该混进 HR 的对话历史里，所以记下水位线，收尾时把水位线之后的运行记录删掉。
    # 注意：这是验证脚本的自我清理，不是产品行为——产品侧的清理走 purge_due_chats（先汇总再删）。
    mark = conn.execute("SELECT COALESCE(MAX(id), 0) AS n FROM agent_runs").fetchone()["n"]

    ok = True
    try:
        # 前置条件：要有"可清空的可见历史"。工作台可能本来就处于已清空状态
        # （上一轮验证留下了清空点），此时先问一句把清空点之后的历史造出来，
        # 否则"清空前有 N 条、清空后 0 条"这条验证就没有意义。
        h0 = http("GET", "/api/agent/history")
        if not h0["messages"]:
            print("清空前无可清历史（工作台处于已清空状态），先问一句把基线造出来…")
            http("POST", "/api/agent/chat", {"message": "人才库有多少人？"})
            h0 = http("GET", "/api/agent/history")
        # 基线要在"造完基线之后"读，否则会把自己造的那条算进差值
        runs_before = db.agent_cost_summary(conn)["runs"]
        print(f"清空前：展示 {len(h0['messages'])} 条消息 / {h0['runs_shown']} 次问答"
              f"（库内共 {h0['runs_total']} 次，agent_runs 物理记录 {runs_before}）")
        assert h0["messages"], "清空前应已有历史（否则这条验证没有意义）"

        c = http("POST", "/api/agent/clear")
        print(f"清空返回：{c}")

        h1 = http("GET", "/api/agent/history")
        print(f"清空后：展示 {len(h1['messages'])} 条消息 / 展示问答 {h1['runs_shown']}"
              f"（库内共 {h1['runs_total']} 次）")
        print(f"        保留期：到期 {h1.get('purge_after')!r} / 还剩 {h1.get('days_left')} 天"
              f" / 可恢复 {h1.get('restorable')}")

        conn2 = db.connect(DB_PATH)
        runs_after = db.agent_cost_summary(conn2)["runs"]
        conn2.close()

        last = db.list_audit(conn, limit=1)
        checks = [
            ("清空后界面消息为 0", len(h1["messages"]) == 0),
            ("清空后展示问答为 0", h1["runs_shown"] == 0),
            ("清空不物理删除 agent_runs（软清空）", runs_after == runs_before),
            ("runs_total 反映库内真实总量", h1["runs_total"] == runs_before),
            ("清空动作写入审计（最新一条即 chat_clear）",
             bool(last) and last[0].get("action") == "chat_clear"),
            ("清空后给出到期时间 purge_after",
             bool(h1.get("purge_after"))),
            ("保留期天数与配置一致",
             h1.get("retention_days") == db.PURGE_AFTER_DAYS),
            ("刚清空时倒计时为完整保留期（不因取整少一天）",
             h1.get("days_left") == db.PURGE_AFTER_DAYS),
            ("清空后标记可恢复 restorable", bool(h1.get("restorable"))),
        ]
        for name, good in checks:
            print(f"  {'PASS' if good else 'FAIL'}  {name}")
            ok = ok and good

        # 未到期时后台清理不得动手（否则 30 天保留期是假的）
        pd = http("POST", "/api/agent/purge-due")
        print(f"未到期手动清理：{pd.get('reason') or pd}")
        good = (pd.get("purged") == 0 and pd.get("reason") == "未到期")
        print(f"  {'PASS' if good else 'FAIL'}  保留期内不清理（拒绝理由：未到期）")
        ok = ok and good

        # 清空点推进后，新的一问应当重新出现在历史里（"清空后还能继续用"）
        http("POST", "/api/agent/chat", {"message": "目前发布了几个岗位？"})
        h2 = http("GET", "/api/agent/history")
        good = len(h2["messages"]) == 2 and h2["runs_shown"] == 1
        print(f"  {'PASS' if good else 'FAIL'}  清空后新问答重新进入历史"
              f"（{len(h2['messages'])} 条消息 / {h2['runs_shown']} 次问答）")
        ok = ok and good

        # 恢复：被清空的对话回到界面（含清空前那批）
        r = http("POST", "/api/agent/restore")
        print(f"恢复返回：{r}")
        h3 = http("GET", "/api/agent/history")
        good = (r.get("ok") is True and len(h3["messages"]) > len(h2["messages"])
                and not h3.get("restorable"))
        print(f"  {'PASS' if good else 'FAIL'}  保留期内可恢复"
              f"（恢复 {r.get('restored')} 条 → 展示 {len(h3['messages'])} 条消息，"
              f"恢复后 restorable={h3.get('restorable')}）")
        ok = ok and good
        lst = db.list_audit(conn, limit=1)
        good = bool(lst) and lst[0].get("action") == "chat_restore"
        print(f"  {'PASS' if good else 'FAIL'}  恢复动作写入审计（最新一条即 chat_restore）")
        ok = ok and good

        # 恢复后不再有到期时间：后台清理也不该动它
        pd2 = http("POST", "/api/agent/purge-due")
        good = (pd2.get("purged") == 0 and pd2.get("reason") == "没有清空记录")
        print(f"  {'PASS' if good else 'FAIL'}  恢复后无到期时间、不会被清理"
              f"（{pd2.get('reason')}）")
        ok = ok and good
    finally:
        # 收尾：① 删掉本次验证问出来的测试问答（水位线之后）；② 还原清空点快照。
        # 顺序不能反——先把测试记录删掉，再还原清空点，"HR 原来能看到什么"就还是什么。
        # 用游标 rowcount 而不是 conn.total_changes——后者含这条连接上此前的所有写
        # （清空/恢复已经写过审计），会把"删了几条测试问答"报大。
        cur = conn.execute("DELETE FROM agent_runs WHERE id > ?", (mark,))
        removed = cur.rowcount
        for k, v in snapshot.items():
            if v in (None, ""):
                conn.execute("DELETE FROM settings WHERE k = ?", (k,))
            else:
                db.set_setting(conn, k, v)
        conn.commit()
        conn.close()
        if removed:
            print(f"已清理本次验证产生的 {removed} 条测试问答（水位线 #{mark} 之后）")
        h4 = http("GET", "/api/agent/history")
        print(f"\n还原后：展示 {len(h4['messages'])} 条消息 / {h4['runs_shown']} 次问答"
              f"（清空点 {h4['cleared_at']!r}）")

    print("\n结果：" + ("全部通过" if ok else "存在失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
