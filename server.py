#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
协同写作服务 v0.2（多项目回合制）
- 静态页 + API：稿子读写、预算表、连接点图、diff 历史
- 多项目：每项目一个 data/<id>/ 目录，projects.json 索引
- 纯标准库，零依赖
启动：python3 server.py [端口]  (默认 8338)
"""
import json, os, re, sys, time, difflib, shutil, uuid, sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
os.makedirs(DATA, exist_ok=True)

PROJECTS = os.path.join(DATA, "projects.json")
LEAVES = os.path.join(BASE, "leaves.json")
CONTINUE_SIGNAL = os.path.join(DATA, "continue.signal")
CONTINUE_ACK = os.path.join(DATA, "continue.ack.json")
SIGNAL_TTL = 60.0  # 秒：过期信号不再消费
CUSTOM_PRESETS = os.path.join(DATA, "custom_presets.json")   # 全局自定义方案（跨项目）
BACKUP_KEEP = 5        # draft_backup.md 历史轮转保留版数（.1~.4 + 最新）
DIFFS_KEEP = 200       # diffs.json 最多保留条数
DIFF_OPS_KEEP = 500    # 单条 diff 的 ops 上限（超过记 truncated）

# Hermes 状态观察（只读）：gateway_state.json + state.db 都在 HERMES_HOME 下
HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
HERMES_ACTIVE_WINDOW = 300.0   # 秒：最后消息距今 < 该值且非 stop 视为「写作中」

# 每个项目目录内的数据文件
PROJ_FILES = ["draft.md", "draft_backup.md", "diffs.json", "plan.json",
              "links.json", "state.json", "outline.json", "changes.json"]

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)

# ---------- 项目索引 ----------
def _migrate_legacy():
    """旧版单项目（data/ 下直接放 draft.md 等）→ 迁移到第一个项目目录。"""
    legacy = os.path.join(DATA, "draft.md")
    if not os.path.exists(legacy):
        return None
    pid = "p_" + uuid.uuid4().hex[:12]
    proj_dir = os.path.join(DATA, pid)
    os.makedirs(proj_dir, exist_ok=True)
    for fname in PROJ_FILES:
        src = os.path.join(DATA, fname)
        if os.path.exists(src):
            shutil.move(src, os.path.join(proj_dir, fname))
    # 项目名：优先从 state.scene 取
    name = "未命名文章"
    st = load_json(os.path.join(proj_dir, "state.json"), {})
    if st.get("scene"):
        name = str(st["scene"]).replace("SCENE:", "").strip() or name
    return {"id": pid, "name": name, "created": time.strftime("%Y-%m-%d %H:%M")}

def load_projects():
    p = load_json(PROJECTS, None)
    if p and isinstance(p, dict) and p.get("items") and p.get("active"):
        return p
    migrated = _migrate_legacy()
    if migrated:
        p = {"active": migrated["id"], "items": [migrated]}
        save_json(PROJECTS, p)
        return p
    p = {"active": None, "items": []}
    save_json(PROJECTS, p)
    return p

def save_projects(p):
    save_json(PROJECTS, p)

def active_id():
    return load_projects().get("active")

def proj_dir(pid):
    d = os.path.join(DATA, pid)
    os.makedirs(d, exist_ok=True)
    return d

def fpath(pid, fname):
    return os.path.join(proj_dir(pid), fname)

def read_draft(pid):
    try:
        with open(fpath(pid, "draft.md"), encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""

def write_draft(pid, text):
    """写草稿前做历史轮转，保留 BACKUP_KEEP 版备份：
    draft_backup.md(最新) ← draft_backup.1.md ← .2 … ← .(KEEP-1)(最旧，落盘前丢弃)。"""
    p = fpath(pid, "draft.md")
    if os.path.exists(p):
        # 最旧一份直接丢弃
        oldest = fpath(pid, f"draft_backup.{BACKUP_KEEP - 1}.md")
        if os.path.exists(oldest):
            os.remove(oldest)
        # 从旧到新依次后移：.N → .(N+1)
        for i in range(BACKUP_KEEP - 2, 0, -1):
            src = fpath(pid, f"draft_backup.{i}.md")
            if os.path.exists(src):
                shutil.move(src, fpath(pid, f"draft_backup.{i + 1}.md"))
        # 最新备份 draft_backup.md → .1
        cur = fpath(pid, "draft_backup.md")
        if os.path.exists(cur):
            shutil.move(cur, fpath(pid, "draft_backup.1.md"))
        # 当前草稿 → 最新备份
        shutil.copy(p, fpath(pid, "draft_backup.md"))
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)

# ---------- Hermes 状态观察（只读） ----------
def read_hermes_status():
    """读 Hermes gateway 与最新 session 状态，供前端状态卡轮询。任何失败都降级为 available:false。"""
    out = {"available": False, "status": "unknown", "gateway": None,
           "round": 0, "session": None}
    try:
        # 1) gateway_state.json
        gw_path = os.path.join(HERMES_HOME, "gateway_state.json")
        if os.path.exists(gw_path):
            gw = load_json(gw_path, {})
            out["gateway"] = gw.get("gateway_state")
        # 2) state.db 最新未结束 session + 最后一条消息
        db_path = os.path.join(HERMES_HOME, "state.db")
        if not os.path.exists(db_path):
            return out
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=3)
        try:
            cur = conn.cursor()
            row = cur.execute(
                "SELECT id, title, message_count, input_tokens, output_tokens, last_activity_at, last_activity_description "
                "FROM sessions WHERE ended_at IS NULL ORDER BY last_activity_at DESC LIMIT 1"
            ).fetchone()
            if not row:
                return out
            sid, title, mc, it, ot, last_act_at, last_desc = row
            msg = cur.execute(
                "SELECT role, finish_reason, content, timestamp FROM messages "
                "WHERE session_id=? ORDER BY id DESC LIMIT 1", (sid,)
            ).fetchone()
            out["available"] = True
            out["session"] = {
                "title": (title or "")[:120],
                "message_count": mc or 0,
                "input_tokens": it or 0,
                "output_tokens": ot or 0,
                "last_finish": None,
                "last_content": "",
                "last_activity": last_desc or "",
            }
            if msg:
                role, finish, content, ts = msg
                out["session"]["last_finish"] = finish
                if role == "assistant" and content:
                    out["session"]["last_content"] = content[:80]
                # 状态判定：最后消息在活跃窗口内且未 stop → 写作中
                if ts is not None and (time.time() - ts) < HERMES_ACTIVE_WINDOW and finish != "stop":
                    out["status"] = "writing"
                else:
                    out["status"] = "idle"
            else:
                out["status"] = "idle"
        finally:
            conn.close()
        return out
    except Exception:
        return {"available": False, "status": "unknown", "gateway": None,
                "round": 0, "session": None}

# ---------- 续写信令 ----------
def take_continue_signal():
    """原子消费一条续写信号：先改名为临时文件再读，避免并发双消费。过期信号视为无信号。"""
    tmp = CONTINUE_SIGNAL + ".consuming"
    try:
        os.replace(CONTINUE_SIGNAL, tmp)
    except OSError:
        return {"text": None}
    try:
        with open(tmp, encoding="utf-8") as f:
            sig = json.load(f)
    except Exception:
        sig = {}
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    if not sig.get("text"):
        return {"text": None}
    if time.time() - sig.get("ts", 0) > SIGNAL_TTL:
        return {"text": None, "expired": True}
    return {"text": sig["text"], "round": sig.get("round", 0),
            "t": sig.get("t"), "ts": sig.get("ts", 0), "project": sig.get("project", "")}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        pid = active_id()
        if path == "/" or path == "/index.html":
            html = os.path.join(BASE, "index.html")
            with open(html, encoding="utf-8") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif path == "/api/projects":
            self._send(200, load_projects())
        elif path == "/api/draft":
            self._send(200, json.dumps({"draft": read_draft(pid) if pid else ""}, ensure_ascii=False))
        elif path == "/api/plan":
            self._send(200, json.dumps(load_json(fpath(pid, "plan.json"), {}) if pid else {}, ensure_ascii=False))
        elif path == "/api/links":
            self._send(200, json.dumps(load_json(fpath(pid, "links.json"), {"anchors": []}) if pid else {"anchors": []}, ensure_ascii=False))
        elif path == "/api/state":
            self._send(200, json.dumps(load_json(fpath(pid, "state.json"), {"round": 0, "scene": ""}) if pid else {"round": 0, "scene": ""}, ensure_ascii=False))
        elif path == "/api/diffs":
            self._send(200, json.dumps(load_json(fpath(pid, "diffs.json"), []) if pid else [], ensure_ascii=False))
        elif path == "/api/leaves":
            self._send(200, json.dumps(load_json(LEAVES, {}), ensure_ascii=False))
        elif path == "/api/presets":
            self._send(200, json.dumps({"custom": load_json(CUSTOM_PRESETS, [])}, ensure_ascii=False))
        elif path == "/api/hermes-status":
            st = read_hermes_status()
            if st.get("available") and pid:
                st["round"] = load_json(fpath(pid, "state.json"), {}).get("round", 0) or 0
            self._send(200, st)
        elif path == "/api/outline":
            self._send(200, json.dumps(load_json(fpath(pid, "outline.json"), {}) if pid else {}, ensure_ascii=False))
        elif path == "/api/changes":
            self._send(200, json.dumps(load_json(fpath(pid, "changes.json"), []) if pid else [], ensure_ascii=False))
        elif path == "/api/signal/continue/ack":
            self._send(200, load_json(CONTINUE_ACK, {"ok": None}))
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        body = self._read_body()
        pid = active_id()
        if path == "/api/projects":
            # 新建项目 {name} → 设为 active，返回完整索引
            name = str(body.get("name", "")).strip() or "未命名文章"
            pid = "p_" + uuid.uuid4().hex[:12]
            os.makedirs(proj_dir(pid), exist_ok=True)
            p = load_projects()
            item = {"id": pid, "name": name, "created": time.strftime("%Y-%m-%d %H:%M")}
            p["items"].append(item)
            p["active"] = pid
            save_projects(p)
            self._send(200, p)
        elif path == "/api/project/switch":
            # 切换 active {id}
            pid_new = str(body.get("id", ""))
            p = load_projects()
            if any(it["id"] == pid_new for it in p["items"]):
                p["active"] = pid_new
                save_projects(p)
                self._send(200, p)
            else:
                self._send(404, {"error": "project not found"})
        elif path == "/api/save":
            if not pid:
                self._send(400, {"error": "no active project"})
                return
            text = body.get("draft", "")
            prev = read_draft(pid)
            write_draft(pid, text)
            prev_lines = prev.splitlines()
            new_lines = text.splitlines()
            sm = difflib.SequenceMatcher(None, prev_lines, new_lines)
            ops = [{"tag": op, "a": prev_lines[i1:i2], "b": new_lines[j1:j2]}
                   for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal"]
            # ops 不再截 20，保住改动信息；仅对极端情况设上限并留 truncated 标志
            truncated = len(ops) > DIFF_OPS_KEEP
            if truncated:
                ops = ops[:DIFF_OPS_KEEP]
            diffs = load_json(fpath(pid, "diffs.json"), [])
            diffs.append({"t": time.strftime("%H:%M:%S"), "round": body.get("round", 0),
                          "ops": ops, "truncated": truncated})
            # diffs.json 限容：只留最近 DIFFS_KEEP 条
            if len(diffs) > DIFFS_KEEP:
                diffs = diffs[-DIFFS_KEEP:]
            save_json(fpath(pid, "diffs.json"), diffs)
            self._send(200, {"ok": True, "diff_count": len(ops)})
        elif path == "/api/plan":
            if not pid:
                self._send(400, {"error": "no active project"})
                return
            save_json(fpath(pid, "plan.json"), body.get("plan", {}))
            self._send(200, {"ok": True})
        elif path == "/api/links":
            if not pid:
                self._send(400, {"error": "no active project"})
                return
            save_json(fpath(pid, "links.json"), body.get("links", {"anchors": []}))
            self._send(200, {"ok": True})
        elif path == "/api/state":
            if not pid:
                self._send(400, {"error": "no active project"})
                return
            save_json(fpath(pid, "state.json"), body.get("state", {}))
            self._send(200, {"ok": True})
        elif path == "/api/outline":
            if not pid:
                self._send(400, {"error": "no active project"})
                return
            save_json(fpath(pid, "outline.json"), body.get("outline", {}))
            self._send(200, {"ok": True})
        elif path == "/api/signal/continue":
            # 网页「保存并继续」→ 写一条续写信号，供 continue_watcher.py 消费注入 Hermes
            sig = {"text": str(body.get("text", "")), "round": body.get("round", 0),
                   "t": time.strftime("%H:%M:%S"), "ts": time.time(), "project": pid or ""}
            save_json(CONTINUE_SIGNAL, sig)
            self._send(200, {"ok": True})
        elif path == "/api/signal/continue/take":
            # watcher 消费（POST：有副作用，读后即删，不用 GET）
            self._send(200, take_continue_signal())
        elif path == "/api/signal/continue/ack":
            # watcher 注入完成后回写 {ok, pane, err, ts}，前端轮询显示真实注入状态
            ack = {"ok": bool(body.get("ok")), "pane": body.get("pane", ""),
                   "err": body.get("err", ""), "ts": time.time()}
            save_json(CONTINUE_ACK, ack)
            self._send(200, {"ok": True})
        elif path == "/api/presets":
            # 保存 {name, desc, tags[], leaves{}} 覆盖同名；或 {delete: name} 删除
            custom = load_json(CUSTOM_PRESETS, [])
            if body.get("delete"):
                custom = [p for p in custom if p.get("name") != body["delete"]]
            else:
                name = str(body.get("name", "")).strip()
                if not name:
                    self._send(400, {"error": "name required"})
                    return
                entry = {"name": name, "desc": str(body.get("desc", "")),
                         "tags": body.get("tags", []) or [],
                         "leaves": body.get("leaves", {}) or {}}
                custom = [p for p in custom if p.get("name") != name] + [entry]
            save_json(CUSTOM_PRESETS, custom)
            self._send(200, {"custom": custom})
        elif path == "/api/changes":
            if not pid:
                self._send(400, {"error": "no active project"})
                return
            changes = load_json(fpath(pid, "changes.json"), [])
            changes.append(body.get("change", {}))
            save_json(fpath(pid, "changes.json"), changes)
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8338
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"协同写作服务: http://127.0.0.1:{port}")
    print(f"数据目录: {DATA}")
    srv.serve_forever()
