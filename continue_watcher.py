#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
续写信号监视器：轮询协同写作服务的信号接口，把「继续」指令自动注入 Hermes 所在 tmux 窗格。
纯标准库，零依赖。运行在 WSL 内（与 tmux 同环境）。

用法：
  python3 continue_watcher.py                    # 自动探测运行 hermes 的窗格
  python3 continue_watcher.py --target %2        # 手动指定窗格（tmux pane_id）
  python3 continue_watcher.py --clipboard        # 备用：写 Windows 剪贴板，不碰 tmux
  python3 continue_watcher.py --interval 1       # 轮询间隔秒数（默认 2）
  python3 continue_watcher.py --port 8338        # 服务端口（默认 8338）
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

CLIP_EXE = "/mnt/c/Windows/System32/clip.exe"


def poll_once(port):
    url = f"http://127.0.0.1:{port}/api/continue-signal/poll"
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return {"text": None}


def list_panes():
    """返回 [(pane_id, command)]，tmux 不在时返回空表。"""
    try:
        out = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_id} #{pane_current_command}"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        return []
    panes = []
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            panes.append((parts[0], parts[1]))
    return panes


def detect_target():
    # 优先找正在跑 hermes 的窗格；找不到再放宽到 python/node 类（可能是其他名字的 agent CLI）
    panes = list_panes()
    for pid, cmd in panes:
        if "hermes" in cmd.lower():
            return pid
    for pid, cmd in panes:
        if cmd.lower() in ("python", "python3", "node"):
            return pid
    return None


def inject_tmux(target, text):
    subprocess.run(["tmux", "send-keys", "-t", target, "-l", text], check=True, timeout=5)
    subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], check=True, timeout=5)


def inject_clipboard(text):
    subprocess.run([CLIP_EXE], input=text.encode("utf-16-le"), check=True, timeout=5)


def main():
    ap = argparse.ArgumentParser(description="续写信号监视器：注入 Hermes (tmux)")
    ap.add_argument("--target", help="tmux 目标窗格，如 %2 或 write:0.0")
    ap.add_argument("--interval", type=float, default=2)
    ap.add_argument("--port", type=int, default=8338)
    ap.add_argument("--clipboard", action="store_true", help="备用模式：写 Windows 剪贴板")
    args = ap.parse_args()

    target = args.target
    if not args.clipboard and not target:
        target = detect_target()
        if target:
            print(f"[watcher] 已自动探测到目标窗格 {target}")
        else:
            print("[watcher] 未找到 tmux 或未探测到 hermes 窗格。")
            for pid, cmd in list_panes():
                print(f"  {pid}  {cmd}")
            print("请用 --target %N 指定窗格后重试。")
            sys.exit(1)

    mode = "剪贴板" if args.clipboard else f"tmux:{target}"
    print(f"[watcher] 启动：每 {args.interval}s 轮询 127.0.0.1:{args.port}，注入方式={mode}")
    while True:
        sig = poll_once(args.port)
        if sig.get("text"):
            text = sig["text"]
            try:
                if args.clipboard:
                    inject_clipboard(text)
                    print(f"[watcher] {sig.get('t', '')} 已复制到剪贴板，请到 Hermes 里粘贴回车")
                else:
                    inject_tmux(target, text)
                    print(f"[watcher] {sig.get('t', '')} 已注入 {target}：{text[:40]}…")
            except Exception as e:
                print(f"[watcher] 注入失败：{e}")
                # 注入失败把信号放回去（写到服务的数据目录），下轮重试
                sig_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "continue.signal")
                try:
                    with open(sig_path, "w", encoding="utf-8") as f:
                        json.dump(sig, f, ensure_ascii=False)
                except OSError:
                    pass
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
