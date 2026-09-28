#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
协同写作服务 v0.2（多项目回合制）
- 静态页 + API：稿子读写、预算表、连接点图、diff 历史
- 多项目：每项目一个 data/<id>/ 目录，projects.json 索引
- 纯标准库，零依赖
启动：python3 server.py [端口]  (默认 8338)
"""
import json, os, re, sys, time, difflib, shutil, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
os.makedirs(DATA, exist_ok=True)

PROJECTS = os.path.join(DATA, "projects.json")
LEAVES = os.path.join(BASE, "leaves.json")

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
    p = fpath(pid, "draft.md")
    if os.path.exists(p):
        shutil.copy(p, fpath(pid, "draft_backup.md"))
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)

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
        elif path == "/api/outline":
            self._send(200, json.dumps(load_json(fpath(pid, "outline.json"), {}) if pid else {}, ensure_ascii=False))
        elif path == "/api/changes":
            self._send(200, json.dumps(load_json(fpath(pid, "changes.json"), []) if pid else [], ensure_ascii=False))
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
            sm = difflib.SequenceMatcher(None, prev.splitlines(), text.splitlines())
            ops = [{"tag": op, "a": prev.splitlines()[i1:i2], "b": text.splitlines()[j1:j2]}
                   for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal"]
            diffs = load_json(fpath(pid, "diffs.json"), [])
            diffs.append({"t": time.strftime("%H:%M:%S"), "round": body.get("round", 0),
                          "ops": ops[:20]})
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
