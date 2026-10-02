#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
续写信号监视器 v0.2（按 评审-给GLM-续写信号方案.md 的最小修正清单落实）

- 消费 POST /api/signal/continue/take 信号（服务端已做 60s TTL，过期返回 expired）
- 注入文本统一拼 `/queue [auto] ` 前缀：忙时排队不打断当前 run（busy_input_mode
  默认 interrupt，裸 Enter 会掐断正在跑的轮次）；[auto] 供事后审计区分脚本注入
- 注入前 capture-pane 检查输入行：非空（用户正在打字）则跳过本轮，信号放回重试
- 注入文本折成单行，send-keys 加 `--`，避免文本内换行提前提交 / 被当选项
- 注入完成回写 ack（POST /api/signal/continue/ack），前端显示真实注入状态
- 单实例锁（flock）：两个 watcher 会双重注入同一窗格
- 信号放回前比对 ts：不覆盖等待期间网页新发出的信号
- 启动只清过期残留信号：网页先保存、watcher 后启动时新鲜信号不被吞
- 探测失败退避重试 5 次：tmux/服务启动顺序有偏差时自愈

纯标准库，零依赖。运行在 WSL（与 tmux 同环境）。

用法：
  python3 continue_watcher.py                    # 进程树自动探测跑 hermes 的窗格
  python3 continue_watcher.py --target %2        # 手动指定窗格
  python3 continue_watcher.py --clipboard        # 备用：写 Windows 剪贴板，不碰 tmux
  python3 continue_watcher.py --interval 1 --port 8338
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

try:
    import fcntl   # POSIX 单实例锁；Windows（剪贴板备用模式）没有，跳过
except ImportError:
    fcntl = None

BASE = os.path.dirname(os.path.abspath(__file__))
SIGNAL_FILE = os.path.join(BASE, "data", "continue.signal")
LOCK_FILE = os.path.join(BASE, "data", "continue.watcher.lock")
SIGNAL_TTL = 60.0   # 与服务端 SIGNAL_TTL 一致
CLIP_EXE_CANDIDATES = ["/mnt/c/Windows/System32/clip.exe", r"C:\Windows\System32\clip.exe"]

# busy 提示行后缀（输入行为空时 Hermes 会在 ❯ 后显示的暗色提示，不算"用户在打字"）
BUSY_HINTS = ("msg=", "/queue", "/bg", "/steer", "Ctrl+C")


def http_json(url, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.loads(r.read().decode("utf-8"))


def take_signal(port):
    try:
        return http_json(f"http://127.0.0.1:{port}/api/signal/continue/take", body={})
    except Exception:
        return {"text": None}


def send_ack(port, ok, pane="", err=""):
    try:
        http_json(f"http://127.0.0.1:{port}/api/signal/continue/ack",
                  body={"ok": ok, "pane": pane, "err": err, "ts": time.time()})
    except Exception:
        pass


def put_signal_back(sig):
    """信号放回（跳过本轮重试用）。回写前比对 ts：文件里已有更新的信号时
    不动它——覆盖写曾会把等待期间网页新发出的信号冲掉。"""
    try:
        if os.path.exists(SIGNAL_FILE):
            try:
                with open(SIGNAL_FILE, encoding="utf-8") as f:
                    cur = json.load(f)
                if cur.get("ts", 0) > sig.get("ts", 0):
                    return
            except Exception:
                pass
        tmp = SIGNAL_FILE + ".back"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sig, f, ensure_ascii=False)
        os.replace(tmp, SIGNAL_FILE)
    except OSError:
        pass


def acquire_single_instance():
    """flock 单实例锁：两个 watcher 会双重注入同一窗格。无 fcntl（Windows
    剪贴板模式）或锁文件写不了时不阻塞主流程。"""
    if fcntl is None:
        return True
    try:
        os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
        fp = open(LOCK_FILE, "w")
        try:
            fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fp.close()
            return False
        return True   # fp 故意不关闭：锁随进程存活
    except OSError:
        return True


# ---------- 窗格探测：进程树匹配（pane_current_command 只会是 python，不可用） ----------
def _ppid_table():
    """{pid: ppid}，解析 /proc/<pid>/stat（comm 带括号，先剥掉再取第 4 字段）。"""
    table = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as f:
                tail = f.read().rsplit(b")", 1)[1].split()
            table[int(name)] = int(tail[1])
        except Exception:
            pass
    return table


def _cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except Exception:
        return ""


def list_panes():
    """[{id, pid, cmd}]；tmux 不在时返回空表。"""
    try:
        out = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_id}|#{pane_pid}|#{pane_current_command}"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        return []
    panes = []
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) == 3:
            panes.append({"id": parts[0], "pid": int(parts[1]), "cmd": parts[2]})
    return panes


def find_hermes_pane(panes):
    """返回进程树（pane_pid 的全部子孙）cmdline 含 hermes 的 pane_id。"""
    parents = _ppid_table()
    children = {}
    for pid, ppid in parents.items():
        children.setdefault(ppid, []).append(pid)
    for p in panes:
        stack, seen = [p["pid"]], set()
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            if "hermes" in _cmdline(pid):
                return p["id"]
            stack.extend(children.get(pid, []))
    return None


# ---------- 注入 ----------
def input_line_busy(pane):
    """True = 用户正在输入行打字，本轮应跳过。找不到提示符时不阻塞注入。"""
    try:
        out = subprocess.run(["tmux", "capture-pane", "-p", "-t", pane],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return False
    for line in reversed(out.splitlines()):
        if "❯" in line:
            content = line.split("❯", 1)[1].strip()
            if not content or any(content.startswith(h) for h in BUSY_HINTS):
                return False
            return True
    return False


def inject_tmux(pane, text):
    # 单行化：send-keys -l 遇换行会在那一行提前提交（等效半个回车）
    line = " ".join(text.split())
    subprocess.run(["tmux", "send-keys", "-t", pane, "-l", "--", line],
                   check=True, timeout=5)
    subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True, timeout=5)


def inject_clipboard(text):
    exe = next((p for p in CLIP_EXE_CANDIDATES if os.path.exists(p)), None)
    if not exe:
        raise FileNotFoundError("找不到 clip.exe")
    subprocess.run([exe], input=text.encode("utf-16-le"), check=True, timeout=5)


def main():
    ap = argparse.ArgumentParser(description="续写信号监视器：注入 Hermes (tmux)")
    ap.add_argument("--target", help="tmux 目标窗格，如 %%2 或 main:%%2")
    ap.add_argument("--interval", type=float, default=2)
    ap.add_argument("--port", type=int, default=8338)
    ap.add_argument("--clipboard", action="store_true", help="备用模式：写 Windows 剪贴板")
    args = ap.parse_args()

    if not acquire_single_instance():
        print("[watcher] 已有另一个 watcher 实例在运行（data/continue.watcher.lock），退出。")
        sys.exit(1)

    pane = None
    if not args.clipboard:
        if args.target:
            pane = args.target
        else:
            # 探测退避重试：先 5 次快试（2s），之后转入 10s 慢轮询常驻等待——
            # 用户常是「先起 watcher、后起 hermes」，直接退出会让信号链路悄悄失效
            found_at = None
            attempt = 0
            while True:
                attempt += 1
                pane = find_hermes_pane(list_panes())
                if pane:
                    break
                if attempt <= 5:
                    print(f"[watcher] 未探测到跑 hermes 的 tmux 窗格（第 {attempt}/5 次快试），2s 后重试…")
                    time.sleep(2)
                else:
                    if (attempt - 5) % 6 == 1:
                        print(f"[watcher] 已等待 {attempt - 5} 个慢轮询周期（10s/次），继续等 hermes 出现在 tmux；"
                              f"若走桌面端/网关续写可 Ctrl+C 停掉本进程")
                    time.sleep(10)
        if not pane:
            panes = list_panes()
            print("[watcher] 未探测到跑 hermes 的 tmux 窗格。当前窗格：")
            for p in panes:
                print(f"  {p['id']}  pid={p['pid']}  cmd={p['cmd']}  tree_root={_cmdline(p['pid'])[:60]}")
            print("请用 --target %%N 指定窗格后重试。")
            sys.exit(1)

    # 启动只清理已过期的残留信号：网页先保存、watcher 后启动时，新鲜信号
    # 应被立即消费注入，不能吞掉（读不了/不存在交给服务端 take 的 TTL 兜底）
    try:
        with open(SIGNAL_FILE, encoding="utf-8") as f:
            old = json.load(f)
        if time.time() - old.get("ts", 0) > SIGNAL_TTL:
            os.remove(SIGNAL_FILE)
            print("[watcher] 已清理一条过期残留信号")
    except Exception:
        pass

    mode = "剪贴板" if args.clipboard else f"tmux:{pane}"
    print(f"[watcher] 启动：每 {args.interval}s 轮询 127.0.0.1:{args.port}，注入方式={mode}")
    while True:
        sig = take_signal(args.port)
        text = sig.get("text")
        if not text:
            if sig.get("expired"):
                print("[watcher] 丢弃一条过期信号（>60s 未消费）")
            time.sleep(args.interval)
            continue
        try:
            if args.clipboard:
                inject_clipboard(text)
                print(f"[watcher] {sig.get('t', '')} 已复制到剪贴板，请到 Hermes 里粘贴回车")
                send_ack(args.port, True, pane="clipboard")
            else:
                if input_line_busy(pane):
                    print(f"[watcher] {sig.get('t', '')} 输入行非空（用户在打字），跳过本轮重试")
                    put_signal_back(sig)
                else:
                    inject_tmux(pane, f"/queue [auto] {text}")
                    print(f"[watcher] {sig.get('t', '')} 已注入 {pane}：/queue [auto] {text[:36]}…")
                    send_ack(args.port, True, pane=pane)
        except Exception as e:
            print(f"[watcher] 注入失败：{e}")
            put_signal_back(sig)  # 下轮重试（TTL 到期自动放弃）
            send_ack(args.port, False, pane=pane or "", err=str(e))
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
