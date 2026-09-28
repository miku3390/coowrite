# 对「续写信号 · tmux 全自动注入」方案的评审

> 面向：产出该方案的外部模型（GLM）。
> 背景：本地协同写作工作台（`production/collab-writing/`，前端 index.html + 标准库 http.server 后端，端口 8338）。该方案的目标是消灭「网页里保存完一轮之后，还要切到终端手动说一声才续写」这一步。
> 本文所有结论都在真机实测过，附命令与证据，不是纸面推演。评审时间 2026-09-28。

---

## 零、总评

方案方向成立，机制不是空想——tmux 注入确实能进 Hermes 的输入行，这一点已实测确认。

但按当前写法上线会踩三处硬伤，其中两处会导致功能直接不可用或误伤正在进行的生成；另有若干设计缺口；验证步骤漏掉了唯一真正难验的那一项。

三处硬伤一句话：自动探测窗格必然失败（字段值不对）；忙时注入会打断正在跑的 run；注入会与用户正在输入的内容拼接。

---

## 一、环境事实（实测，可直接复现）

目标终端环境：

```
tmux 已安装：/usr/bin/tmux
session：main（attached），窗格：%2
Hermes CLI：pid 593  ← 父进程 bash pid 488 ← 窗格 %2（pane_pid=488）← tmux server 461
当前只有 1 个窗格，Hermes 独占
```

复现命令：

```bash
tmux list-panes -a -F '#{pane_id} | #{pane_current_command} | #{pane_pid} | #{pane_current_path}'
# 实测输出： %2 | python | 488 | /mnt/c/Users/MIKU
```

注意 `pane_current_command` 的值是 **`python`**。Hermes 的可执行体是 `venv/bin/python .../hermes`，tmux 只把它认作解释器进程。

**注入可行性实测**（发探针文本、不发 Enter、随后清除）：

```
注入前屏幕：  ⚕ ❯ msg=interrupt · /queue · /bg · /steer · Ctrl+C cancel
注入后屏幕：  ⚕ ❯ SIGNAL-PROBE-12345          ← 文本确实进了输入行
C-u 之后：    ⚕ ❯                              ← 清得掉，无残留
```

结论：`tmux send-keys -t %2 -l "<text>"` + `Enter` 这条路物理上成立。方案的核心假设站得住。

---

## 二、硬伤一：窗格自动探测必然失败

方案写的是「优先选 `pane_current_command` 含 `hermes` 的窗格」——见第一节实测，该字段是 `python`，不含 hermes。这段逻辑会永远返回空，然后每次都退出、要求用户手填 `--target`。等于自动化没做成。

改法：用 `pane_pid` 遍历 `/proc` 建 ppid 表，向下递归找 cmdline 里含 `hermes` 的子孙进程。参考实现：

```python
import os, subprocess

def _ppid_table():
    """{pid: ppid}，由 /proc/<pid>/stat 解析（第 3 字段是 ppid，但要先剥掉括号里的 comm）"""
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
            return f.read().decode("utf-8", "replace")
    except Exception:
        return ""

def find_hermes_pane(pane_pids):
    """pane_pids: {pane_id: pane_pid}；返回 cmdline 含 hermes 的 pane_id"""
    parents = _ppid_table()
    children = {}
    for pid, ppid in parents.items():
        children.setdefault(ppid, []).append(pid)
    for pane_id, base in pane_pids.items():
        stack, seen = [base], set()
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            if "hermes" in _cmdline(pid):
                return pane_id
            stack.extend(children.get(pid, []))
    return None
```

在当前环境会正确命中 `%2`。仍然保留 `--target` 手动覆盖，并且探测失败时把候选窗格连 cmdline 一起打印出来，别只报一句"找不到"。

---

## 三、硬伤二：忙时注入会打断正在跑的 run

这是最需要改的一处。

`/queue`、`/steer`、`msg=interrupt` 这些字样不是装饰，是 Hermes 的 busy-input 机制。它的行为由配置 `display.busy_input_mode` 决定，取值为 `interrupt | queue | steer`：

```
源码：hermes_cli/config_defaults.py:1153
      "busy_input_mode": "interrupt",  # interrupt | queue | steer

命令：/busy [queue|steer|interrupt|status]
      queue     → Enter 把输入排队到下一轮，不打断当前 run
      steer     → Enter 把输入注入当前 run（下一个工具调用之后生效）
      interrupt → Enter 立刻重定向、打断当前 run（默认值）
```

状态栏那行 `msg=interrupt` 与这个默认值相互印证（配置默认值已逐行确认；显示字符串与配置项的绑定属于合理推断，未追到渲染处）。

于是：watcher 每 2 秒轮询，一旦拿到信号就 `send-keys` 文本 + `Enter`。如果此刻 Hermes 正在写上一段（或正在跑任何别的任务），这个 Enter 会**把正在进行的那一轮掐断**。用户在扇还在输出时点「保存并继续」就会命中这个场景。

修法，按可靠度排序：

1. **最省事且最可靠**：注入的文本前面拼一个 `/queue `（注意有空格）。依据是命令表定义：

   ```
   commands.py:157
     CommandDef("queue", "Queue a prompt for the next turn (doesn't interrupt)", "Session",
                aliases=("q",), args_hint="<prompt>",
                busy_policy="dispatch", busy_handler="queue")
   tips.py:21
     "/queue <prompt> queues a message for the next turn without interrupting the current one."
   ```

   忙时排队、不打断当前 run，这一点由源码语义确认；**空闲时的具体行为未实测**（预计是立刻开始新一轮，因为「下一轮」就是现在），实测方法就是发一条 `/queue 测试文本`，代价是一轮模型调用——要不要验由用户定。无论空闲时是哪种，都不影响这条修正的价值。别名 `/q` 同样可用。对话历史里留下的是纯文本，`/queue` 本身不进入消息内容。
2. 或把 `display.busy_input_mode` 设成 `queue`。代价是用户自己手打的消息也变成排队行为，属于全局行为改变，应由用户决定，不应由这个功能擅自改。
3. 或在 watcher 里用 `capture-pane` 判断忙闲再决定等不等。最脆弱（依赖屏幕文字），不建议作为主路径。

---

## 四、硬伤三：输入行是共享资源

输入行不是 watcher 独占的。用户随时可能在输入框里打字。watcher 无脑 `send-keys` 会把双方的字拼在一起，回车后整串（用户的半句话 + 续写指令）作为一条消息发出去。

第一节的探针测试没有暴露这个问题，因为当时输入行是空的。

修法：注入前先 `tmux capture-pane -p -t <pane>`，检查提示行。提示行形如 `⚕ ❯ <内容>`，其内容为空才注入；非空则本轮跳过、等下一轮（并可在 watcher 日志里记一笔「输入行非空，跳过」）。若为了健壮性愿意多做一步，可在注入前发一次 `C-u` 清行——但那会吃掉用户正在打的字，不能默认开。

---

## 五、设计缺口

1. **信号没有时效（TTL）**。watcher 没在运行时，信号留在文件里；几小时后启动 watcher，会消费一条早已过期的信号，把一条无意义的续写指令敲进对话流。既然信号里已经写了 `t`，就必须用它：`if time.time() - sig["t"] > 60: 丢弃`。另外 watcher 启动时应先清空一次已有信号文件。

2. **没有闭环确认**。前端提示「已保存，续写信号已发出」只代表那次 HTTP POST 返回 200，不代表真的注入了 tmux 窗格。watcher 崩了、目标窗格不存在、注入失败，界面照样显示"已发出"。要闭环，得让 watcher 在完成后回写一条状态（例如 `POST /api/continue-ack {ok, pane, t, err}`），前端读它显示「已注入 %2」或「注入失败：原因」。

3. **`GET /api/continue-signal/poll` 语义是坏的**。GET 应当幂等、无副作用，而这个 GET 读一下就删文件。浏览器预取、任何中间层重试，都可能白白吃掉一次信号。改成 `POST /api/continue-signal/take`。消费用「先 `os.replace` 到临时名再读」的思路是对的，保留。

4. **命名过于相近**。`/api/continue`（旧路，本项目已删除）与 `/api/continue-signal` 只差一个词。建议改成 `/api/signal/continue` 与 `/api/signal/continue/take`，将来排障不用读两遍。

5. **文本必须折成单行**。`send-keys -l` 遇到文本里的换行会在那一行提前提交（等效于半个回车）。进 `send-keys` 之前应先 `" ".join(text.split())`。另外文本以 `-` 开头时会被当成选项，`send-keys` 要加 `--`：

   ```bash
   tmux send-keys -t %2 -l -- "/queue 继续协同写作：……"
   tmux send-keys -t %2 Enter
   ```

6. **双通道会重复触发**。Electron 场景下已经有 `window.workbenchBridge?.continue`（注入 3939 信令）这条通路。方案在保留它的同时又追加 POST 信号，于是两个通路同时活着：桌面端会话收到 3939，tmux 里的会话收到续写指令——两边跑同一篇稿，`draft.md` 会互相覆盖。必须二选一：检测到 bridge 存在就不发信号，或者明确约定信号只服务 tmux 场景（并把这个约定写进文档）。

7. **保存失败也会发信号**。方案没写条件分支。应当只在 `/api/save` 返回 `ok` 之后才发信号。

8. **无人值守的老问题仍在**。注入触发之后，若 agent 调用 clarify 提问，界面无人应答就会永久停在那里——上一版 webhook 方案就是这么死的。tmux 只是把"看不见的卡死"变成"看得见的卡死"，用户不在场时结果一样。这部分只能靠 agent 侧纪律（续写流程禁止提问）兜。另外建议注入文本带一个 `[auto]` 前缀，让事后审计能一眼区分这是脚本注入的、不是用户手打的。

9. **信号建议带上项目标识**。信号文件是全局的（`data/` 根下），多项目切换时存在串台的可能。前端生成的文本里已含场景名，勉强够用，但加一个 `project` 字段的成本几乎为零，诊断时有用。（顺带确认一点：`data/` 已在 `.gitignore` 中排除，信号文件放那里不会进 git，这个位置选得对。）

---

## 六、方案没算的落地成本

方案要求三个窗格：hermes / server / watcher。

而现状是**只有一个窗格 `main:%2`，且 Hermes 独占**。照这个方案跑，用户得先改变自己的工作方式——要么在 tmux 里再开两个窗格，要么把 server 与 watcher 丢进后台（`nohup`）。这部分成本应该写进方案的"使用方式"一节，现在的写法默认了用户已经在用多窗格 tmux。

---

## 七、验证步骤里缺的那一项

方案列的五步，第 1、2、4、5 步没问题。第 3 步用 `cat` 验证的只是「`send-keys` 能把字打进去」，不是「Hermes 能正确消费这条输入」——而后者才是整套方案真正的未知点。

真正要验的是**忙时注入会发生什么**：Hermes 正在跑一轮时收到注入 + Enter，是打断、是排队、还是丢弃。这个答案随 `busy_input_mode` 而变，而且验一次就要真花掉一轮模型调用，或者真打断一次正在跑的 run。这是整套方案唯一无法离线拿到答案的点，应该单独列出来并说明代价，由用户挑时机执行。

建议补全后的验证清单：

1. 备份目录，起服务。
2. 冒烟：`POST /api/signal/continue` → `POST /api/signal/continue/take` 返回文本；第二次 take 返回 `null`。
3. `send-keys` 通路：开一个临时 session 跑 `cat`，`--target` 指向它，发信号，确认文本被敲入且是单行。
4. 窗格探测：在只有一个 Hermes 窗格的环境里，确认 `find_hermes_pane()` 命中 `%2`；确认失败时输出候选清单。
5. TTL：写一条 `t` 为 10 分钟前的信号，确认 watcher 丢弃并记录。
6. **忙时注入（需用户在场，会消耗一轮调用）**：让 Hermes 跑一个长任务，期间发信号，观察是排队还是打断——据此确认 `/queue` 前缀是否必要。
7. 浏览器全链路：编辑页保存 → watcher 终端输出 → tmux 窗格收到指令。
8. 回归：跑项目 `升级交接-给外部模型.md` 第十一节的既有冒烟（`/api/leaves`、`/api/projects`、`/api/draft`、`/api/save`），确认无回归。

---

## 八、最小修正清单（按改动量从小到大）

1. 注入文本前拼 `/queue `（硬伤二）。
2. `send-keys` 前把文本折成单行，并加 `--`（缺口五）。
3. 信号加 60 秒 TTL，watcher 启动时清空一次（缺口一）。
4. 窗格探测换成进程树匹配（硬伤一）。
5. 注入前检查输入行是否为空（硬伤三）。
6. `GET /poll` 改 `POST /take`（缺口三）。
7. 加保存成功的条件分支（缺口七）。
8. Electron 通路加互斥或明确分工（缺口六）。
9. watcher 回写 ack，前端显示真实注入状态（缺口二）。
10. 文档补上「需要多窗格 tmux」这一前提（第六节）。

前四条做完，方案就能稳定跑；后面六条属于把可靠性补齐。硬伤二和三不做的话，功能会在真实使用中随机失效或误伤。

---

## 九、本文各项的依据等级

为了让对方知道哪些结论可以依赖、哪些还需要自己验，逐项标注：

**实测（本机执行过命令，有输出）**
- tmux 已安装、session 结构与进程链（Hermes pid 593 ← bash 488 ← pane %2）
- `pane_current_command` 的实际取值为 `python`
- `send-keys -l` 的文本确实进入 Hermes 输入行；`C-u` 可清除，无残留

**读源码确认（可复查行号）**
- `display.busy_input_mode` 的三个取值与默认值 `interrupt`：`config_defaults.py:1153`
- `/busy [queue|steer|interrupt|status]` 的行为文案：`cli_commands_mixin.py:3361-3402`
- `/queue`、`/steer`、`/background` 的命令定义与语义：`commands.py:157-161`、`tips.py:13,21`

**推断（未实测，已在正文标注）**
- 空闲时 `/queue` 的具体落点
- 状态栏 `msg=interrupt` 字符串与 `busy_input_mode` 配置项的绑定

**必须由用户在场实测（无法离线拿答案）**
- 忙时注入 + Enter 的真实结果：打断、排队还是丢弃（会消耗一轮模型调用或真打断一次 run）
