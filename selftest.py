#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
零依赖回归自检：标准库 unittest + 随机端口，不触碰仓库 data/（全部写进临时沙箱）。

跑法：python3 selftest.py

多 AI 协作的回归防线：改完 server.py / continue_watcher.py 必须全绿再提交。
覆盖：保存链路（round/rev 推进、空保存短路、并发、备份轮转）、rev 乐观锁 409、
/api/ai-write（source=ai）、Host/Origin 校验、坏请求体拒绝（G2）、save_json 并发、
信号原子消费与 TTL、changes 限容、changes/presets 并发全落账（G4）、
leaves.json 引用完整性（G5）、缺 draft 键拒绝（G2）。
"""
import atexit
import http.client
import importlib.util
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))

_SANDBOXES = []

def _mkdtemp(*a, **k):
    """mkdtemp + 登记退出清理（曾每次跑测试漏几个临时目录在系统 temp 里）。"""
    d = tempfile.mkdtemp(*a, **k)
    _SANDBOXES.append(d)
    return d

atexit.register(lambda: [shutil.rmtree(d, ignore_errors=True) for d in _SANDBOXES])


def _load_server():
    """把 server.py 当模块加载，并把它的数据目录重定向到一次性沙箱。"""
    spec = importlib.util.spec_from_file_location("cw_server", os.path.join(ROOT, "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)   # 副作用仅 makedirs(BASE/data)；随后立即改写全部路径
    sandbox = _mkdtemp(prefix="cw-selftest-")
    mod.DATA = sandbox
    mod.PROJECTS = os.path.join(sandbox, "projects.json")
    mod.CUSTOM_PRESETS = os.path.join(sandbox, "custom_presets.json")
    mod.CONTINUE_SIGNAL = os.path.join(sandbox, "continue.signal")
    mod.CONTINUE_ACK = os.path.join(sandbox, "continue.ack.json")
    return mod


SRV = _load_server()


class Base(unittest.TestCase):
    """每个用例类起一个独立服务实例 + 独立项目，互不串数据。"""

    @classmethod
    def setUpClass(cls):
        cls.srv = SRV.Server(("127.0.0.1", 0), SRV.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        st, p = cls.req("POST", "/api/projects", {"name": "selftest"})
        assert st == 200, p
        cls.pid = p["active"]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    @classmethod
    def req(cls, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        r = urllib.request.Request("http://127.0.0.1:%d%s" % (cls.port, path),
                                   data=data, method=method,
                                   headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read().decode("utf-8"))
            except Exception:
                payload = {}
            return e.code, payload

    @classmethod
    def raw(cls, method, path, raw_bytes, extra=None):
        """完全控制字节与头部（伪造 Host / 坏编码体用）。返回 (status, body_bytes)。"""
        extra = dict(extra or {})
        conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=10)
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        # extra 可覆盖默认 Host（伪造 Host 测试），不得发出两个 Host 头
        if not any(k.lower() == "host" for k in extra):
            conn.putheader("Host", "127.0.0.1:%d" % cls.port)
        if not any(k.lower() == "content-type" for k in extra) and raw_bytes is not None:
            conn.putheader("Content-Type", "application/json")
        for k, v in extra.items():
            conn.putheader(k, v)
        if raw_bytes is not None:
            conn.putheader("Content-Length", str(len(raw_bytes)))
        conn.endheaders()
        if raw_bytes is not None:
            conn.send(raw_bytes)
        resp = conn.getresponse()
        out = resp.read()
        conn.close()
        return resp.status, out

    def proj_file(self, name):
        return os.path.join(SRV.DATA, self.pid, name)


class SaveFlow(Base):
    def test_save_round_and_rev(self):
        # 用例按字母序执行，round/rev 只做相对断言
        _, r0 = self.req("POST", "/api/save", {"draft": "A0\n"})
        st, r = self.req("POST", "/api/save", {"draft": "A1\nA2\n"})
        self.assertEqual(st, 200)
        self.assertTrue(r["ok"])
        self.assertEqual(r["round"], r0["round"] + 1)
        self.assertEqual(r["rev"], r0["rev"] + 1)
        st, r2 = self.req("POST", "/api/save", {"draft": "A1\nA2-changed\n"})
        self.assertEqual(r2["round"], r["round"] + 1)
        self.assertEqual(r2["rev"], r["rev"] + 1)
        st, d = self.req("GET", "/api/draft")
        self.assertEqual(d["rev"], r2["rev"])
        self.assertIn("A2-changed", d["draft"])

    def test_empty_save_short_circuit(self):
        _, r1 = self.req("POST", "/api/save", {"draft": "S1\n"})
        _, r2 = self.req("POST", "/api/save", {"draft": "S2\n"})
        self.assertEqual(r2["round"], r1["round"] + 1)
        _, before = self.req("GET", "/api/diffs")
        st, r = self.req("POST", "/api/save", {"draft": "S2\n"})
        self.assertTrue(r.get("unchanged"))
        self.assertEqual(r["round"], r2["round"])
        _, after = self.req("GET", "/api/diffs")
        self.assertEqual(len(after), len(before), "空保存不得新增 diff 记录")

    def test_concurrent_saves_all_recorded(self):
        self.req("POST", "/api/save", {"draft": "base\n"})
        _, before = self.req("GET", "/api/diffs")
        errs, n = [], 10

        def w(i):
            try:
                st, r = self.req("POST", "/api/save", {"draft": "c%d\n" % i})
                assert st == 200 and r["ok"], (st, r)
            except Exception as e:
                errs.append(e)

        ts = [threading.Thread(target=w, args=(i,)) for i in range(n)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errs, [])
        _, after = self.req("GET", "/api/diffs")
        self.assertEqual(len(after) - len(before), n, "并发保存不许丢记录")

    def test_expected_rev_conflict(self):
        _, d = self.req("GET", "/api/draft")
        rev = d["rev"]
        st, r = self.req("POST", "/api/save", {"draft": "X\n", "expected_rev": rev + 5})
        self.assertEqual(st, 409)
        self.assertEqual(r["error"], "stale")
        self.assertEqual(r["rev"], rev, "409 时稿件不得被改动")
        st, r = self.req("POST", "/api/save", {"draft": "X\n", "expected_rev": rev})
        self.assertEqual(st, 200)
        self.assertEqual(r["rev"], rev + 1)

    def test_save_without_expected_rev_still_works(self):
        _, d = self.req("GET", "/api/draft")
        st, r = self.req("POST", "/api/save", {"draft": d["draft"] + "more\n"})
        self.assertEqual(st, 200)
        self.assertTrue(r["ok"])

    def test_save_rejects_non_string_draft(self):
        st, r = self.req("POST", "/api/save", {"draft": 123})
        self.assertEqual(st, 400)

    def test_backup_rotation(self):
        for i in range(7):
            self.req("POST", "/api/save", {"draft": "r%d\n" % i})
        # 保存 r6 前，当前稿 r5 先被轮转进 draft_backup.md
        self.assertTrue(os.path.exists(self.proj_file("draft_backup.md")))
        self.assertFalse(os.path.exists(self.proj_file("draft_backup.5.md")))
        with open(self.proj_file("draft_backup.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "r5\n")
        with open(self.proj_file("draft.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "r6\n")

    def test_ai_write_source_and_rev(self):
        # 先由用户保存一版，AI 再写回（字母序本用例最先跑，项目此时还没有初稿）
        self.req("POST", "/api/save", {"draft": "用户稿\n"})
        _, d = self.req("GET", "/api/draft")
        st, r = self.req("POST", "/api/ai-write",
                         {"draft": d["draft"] + "AI段\n", "expected_rev": d["rev"]})
        self.assertEqual(st, 200)
        self.assertEqual(r["rev"], d["rev"] + 1)
        _, diffs = self.req("GET", "/api/diffs")
        self.assertEqual(diffs[-1].get("source"), "ai")
        with open(self.proj_file("draft_backup.md"), encoding="utf-8") as f:
            self.assertNotIn("AI段", f.read(), "写回前旧稿必须先轮转进备份")
        # AI 写回与用户保存互相冲突：rev 已被 AI 推进，用户旧 rev 应 409
        st, r = self.req("POST", "/api/save",
                         {"draft": "用户旧稿\n", "expected_rev": d["rev"]})
        self.assertEqual(st, 409)

    def test_changes_trimmed(self):
        total = SRV.CHANGES_KEEP + 10
        for i in range(total):
            self.req("POST", "/api/changes", {"change": {"what": str(i)}})
        _, ch = self.req("GET", "/api/changes")
        self.assertEqual(len(ch), SRV.CHANGES_KEEP)
        self.assertEqual(ch[-1]["what"], str(total - 1))


class Security(Base):
    def test_bad_host_403(self):
        st, _ = self.raw("GET", "/api/projects", None, {"Host": "evil.example.com"})
        self.assertEqual(st, 403)

    def test_cross_origin_403(self):
        st, _ = self.raw("POST", "/api/save", b'{"draft":"x"}',
                         {"Content-Type": "application/json",
                          "Origin": "http://evil.example.com"})
        self.assertEqual(st, 403)

    def test_local_host_with_port_ok(self):
        st, _ = self.raw("GET", "/api/projects", None, {"Host": "localhost:%d" % self.port})
        self.assertEqual(st, 200)

    def test_invalid_utf8_body_no_crash(self):
        # decode 曾在 try 外：坏编码体抛 UnicodeDecodeError 直接打断连接
        st, _ = self.raw("POST", "/api/plan", b'{"plan":"\xd1\xa7"}',
                         {"Content-Type": "application/json"})
        self.assertIn(st, (200, 400))


class StopBleed(Base):
    """批次 S 止血回归（升级计划四 §4.1）：堵死全部已知主动丢稿路径。"""

    def test_save_missing_draft_key_rejected(self):
        """G2：缺 draft 键曾默认空串——一个不带 body 的请求就能清空整篇草稿"""
        self.req("POST", "/api/save", {"draft": "正文不能丢\n"})
        _, before = self.req("GET", "/api/draft")
        _, diffs_before = self.req("GET", "/api/diffs")
        st, r = self.req("POST", "/api/save", {"expected_rev": before["rev"]})
        self.assertEqual(st, 400)
        st, _ = self.req("POST", "/api/save", {})
        self.assertEqual(st, 400)
        _, after = self.req("GET", "/api/draft")
        _, diffs_after = self.req("GET", "/api/diffs")
        self.assertEqual(after["draft"], before["draft"], "缺 draft 键不得清空草稿")
        self.assertEqual(after["rev"], before["rev"], "被拒的保存不得推进 rev")
        self.assertEqual(len(diffs_after), len(diffs_before), "被拒的保存不得新增 diff")

    def test_save_bad_body_rejected(self):
        """G2：坏 body 曾被吞成 {}，配合 draft 缺省空串会清稿；现在必须 400"""
        self.req("POST", "/api/save", {"draft": "正文不能丢\n"})
        st, _ = self.raw("POST", "/api/save", b"\xff\xfe not json",
                         {"Content-Type": "application/json"})
        self.assertEqual(st, 400)
        st, _ = self.req("POST", "/api/ai-write", {"expected_rev": 0})
        self.assertEqual(st, 400)
        # 稿件须完好
        _, d = self.req("GET", "/api/draft")
        self.assertIn("正文不能丢", d["draft"])

    def test_changes_and_presets_concurrent_all_recorded(self):
        """G4：changes/presets 读-改-写曾无锁，并发 POST 互相覆盖丢记录"""
        n = 8
        errs = []

        def w(i):
            try:
                st, r = self.req("POST", "/api/changes", {"change": {"what": "c%d" % i}})
                assert st == 200 and r.get("ok"), (st, r)
                st, r = self.req("POST", "/api/presets",
                                 {"name": "p%d" % i, "desc": "", "tags": [], "leaves": {}})
                assert st == 200, (st, r)
            except Exception as e:
                errs.append(e)

        ts = [threading.Thread(target=w, args=(i,)) for i in range(n)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errs, [])
        _, ch = self.req("GET", "/api/changes")
        whats = [c.get("what") for c in ch]
        for i in range(n):
            self.assertIn("c%d" % i, whats, "并发 changes 不许丢记录")
        _, ps = self.req("GET", "/api/presets")
        names = [p.get("name") for p in ps["custom"]]
        for i in range(n):
            self.assertIn("p%d" % i, names, "并发 presets 不许丢记录")

    def test_leaves_json_references(self):
        """G5：conditions.category 曾与标签体系错位，四条标签约束从未生效。
        锁死三件事：category 必须是已注册标签、叶 id 不悬空、方案引用存在"""
        _, data = self.req("GET", "/api/leaves")
        tags = set(data.get("tags", []))
        self.assertTrue(tags, "leaves.json 必须提供 tags 数组（裁决①方案A：标签清单唯一事实源）")
        leaf_ids = []
        for axis in data["axis"].values():
            for leaf in axis["leaves"]:
                leaf_ids.append(leaf["id"])
        self.assertEqual(len(leaf_ids), len(set(leaf_ids)), "叶 id 必须全局唯一")
        ids = set(leaf_ids)
        for cond in data["rules"]["conditions"]:
            self.assertIn(cond["category"], tags,
                          "conditions.category 必须是标签单词（曾因类目名错位全部失效）")
        for preset in data["presets"]:
            for k in preset["leaves"]:
                self.assertIn(k, ids, "方案引用的叶必须存在")
            for t in preset.get("tags", []):
                self.assertIn(t, tags, "方案的标签必须是已注册标签")


class SignalChain(Base):
    def test_take_is_atomic(self):
        self.req("POST", "/api/signal/continue", {"text": "hello", "round": 1})
        st, r = self.req("POST", "/api/signal/continue/take", {})
        self.assertEqual(r.get("text"), "hello")
        st, r = self.req("POST", "/api/signal/continue/take", {})
        self.assertIsNone(r.get("text"), "二次消费必须为空（原子消费）")

    def test_expired_signal(self):
        sig = {"text": "old", "round": 0, "t": "x", "ts": time.time() - 999, "project": ""}
        SRV.save_json(SRV.CONTINUE_SIGNAL, sig)
        st, r = self.req("POST", "/api/signal/continue/take", {})
        self.assertIsNone(r.get("text"))
        self.assertTrue(r.get("expired"))


class SaveJsonConcurrency(unittest.TestCase):
    def test_same_path_concurrent_writes(self):
        """B1 回归：save_json 固定 .tmp 名时并发互抢会 PermissionError/FileNotFoundError"""
        path = os.path.join(_mkdtemp(prefix="cw-sj-"), "x.json")
        errs = []

        def w(k):
            try:
                for i in range(30):
                    SRV.save_json(path, {"k": k, "i": i})
            except Exception as e:
                errs.append(e)

        ts = [threading.Thread(target=w, args=(k,)) for k in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errs, [])
        with open(path, encoding="utf-8") as f:
            self.assertIsInstance(json.load(f), dict)

    def test_corrupt_json_quarantined(self):
        """损坏的 JSON 必须先隔离再回默认值——静默吞掉会让下次保存覆盖真实历史"""
        d = _mkdtemp(prefix="cw-cj-")
        p = os.path.join(d, "x.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write('{"broken"')
        self.assertEqual(SRV.load_json(p, {"d": 1}), {"d": 1})
        self.assertFalse(os.path.exists(p), "原文件应已被改名隔离")
        quarantined = [f for f in os.listdir(d) if f.startswith("x.json.corrupt-")]
        self.assertEqual(len(quarantined), 1)
        with open(os.path.join(d, quarantined[0]), encoding="utf-8") as f:
            self.assertEqual(f.read(), '{"broken"')


if __name__ == "__main__":
    unittest.main(verbosity=2)
