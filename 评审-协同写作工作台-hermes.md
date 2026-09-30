# 协同写作工作台 · 评审报告（第二轮）

> 签名：[hermes-deepseek-deepseek-flash娘]
> 审查时间：2026-09-30
> 审查对象：commit `c942a1b`（main，工作区干净）
> 审查范围：`server.py`(388) / `index.html`(1242) / `continue_watcher.py`(216) / `leaves.json` / 全部文档
> 方法：临时副本实测，未改动仓库任何文件
> 　　`cp -r collab-writing /tmp/cw-review && rm -rf data && python3 server.py 8399`
> 　　Python 3.12.3；全部结论附复现输出，未实测的项会显式标注

---

## 零、结论摘要

第一轮（`修改意见.md`，opencode）提出的多数问题是可维护性意见，方向没错；但有两条判断需要修正，另有一条被列为「已知限制」的问题实际是**静默数据丢失**。

按收益排序，本人认为下一步该做的是：

| # | 事项 | 类型 | 收益 |
|---|---|---|---|
| A1 | 空保存（内容未变）仍轮转备份 + 记空 diff → 5 次误点即可冲掉全部改稿历史 | **缺陷·数据风险** | 高 |
| A2 | 并发保存：静默丢 diff 记录 + `write_draft` 未捕获异常导致请求 500 | **缺陷·数据风险** | 高 |
| A3 | AI 写回 `draft.md` 不走服务端 → 该次覆盖无备份、无 diff 记录 | **缺陷·设计缺口** | 高 |
| A4 | 回合数 `round` 永不推进（diff 记录、状态卡永远是「第 0 回合」） | 缺陷·功能 | 中 |
| A5 | 状态卡读的是「最新未结束的任意 Hermes 会话」 | 缺陷·语义 | 中 |
| A6 | 段落「合并」是假合并：不去标记，每次保存重新合并，编辑区可见裸标记 | 缺陷·功能 | 中 |
| A7 | 内联 `onclick` 字符串插值：标签名/方案名含引号即破坏 DOM 并可执行任意 JS | 缺陷·安全 | 中 |
| A8 | 无 Host/Origin 校验：本机任意页面可覆写稿件 | 加固 | 中 |
| A9 | 交接文档与代码脱节（行数、API 清单、已删路由仍标「已废弃」） | 文档 | 中·低成本 |

以下逐条给现象、复现、证据、最小修法。

---

## 一、实测确认的缺陷

### A1 空保存污染 diff 历史，并冲掉备份轮转（数据风险）

`server.py:293-316` 的 `/api/save` 无条件执行 `write_draft()` 与 diff 追加，没有「内容是否变化」的判断；前端 `index.html:1162` 的 `saveAll()` 也没有脏标记，按钮点几次就发几次。

复现（副本实测）：

```
T3b 用户改稿一次 → draft_backup.md = "A1\nA2\nA3\n"（改前版本，可回退）
T4  连续 5 次 POST /api/save，draft 与当前内容完全相同：
    返回 {"ok": true, "diff_count": 0}
    diffs 条数 = 7，各条 ops 数 = [1, 1, 0, 0, 0, 0, 0]
    仍含旧版本(A2 未改)的备份文件数 = 0   ← 改稿历史被空保存全部冲掉
```

两个后果：

1. `diffs.json` 被 `ops: []` 的空记录占满，`DIFFS_KEEP=200` 的额度被无意义记录消耗，「最近改动」面板出现空条目；
2. 备份轮转（`BACKUP_KEEP=5`）每次保存都空转一格，误点 5 次就把 5 版历史全部变成同一份当前稿——**这是回滚能力的实际丧失**，不是「锦上添花」。

顺带一个现场证据：仓库外的实例数据里已存在这种记录形态（`data/p_c6ac19c10ffd/diffs.json` 一条 `ops` 为空的记录，`draft.md` 长度为 0）。

最小修法（服务端一处判断即可，前端不用动）：

```python
elif path == "/api/save":
    if not pid:
        self._send(400, {"error": "no active project"}); return
    text = body.get("draft", "")
    prev = read_draft(pid)
    if text == prev:                      # 内容未变：不落盘、不轮转、不记 diff
        self._send(200, {"ok": True, "diff_count": 0, "unchanged": True}); return
    write_draft(pid, text)
    ...
```

前端配合（可选）：`saveAll()` 收到 `unchanged: true` 时不发续写信号，状态栏提示「无改动」。

---

### A2 并发保存：丢记录 + 未捕获异常（数据风险）

`ThreadingHTTPServer`（`server.py:385`）＋ `difflib` 读写 `diffs.json` 全程无锁，`shutil.move` 的轮转序列（`server.py:107-120`）不是原子的。

复现一：10 个线程同时 POST `/api/save`（各写不同内容）

```
并发前 diffs 条数 = 10，10 次并发保存后 = 14，新增 = 4（期望 10）
全部返回 ok = True
```

**10 次保存全部回 `{"ok": true}`，却只有 4 条记录落盘**——读-改-写竞态静默丢数据。

复现二：同一次并发里服务端抛未捕获异常（服务端日志原文）

```
File "/tmp/cw-review/server.py", line 114, in write_draft
  shutil.move(src, fpath(pid, f"draft_backup.{i + 1}.md"))
FileNotFoundError: [Errno 2] No such file or directory: '.../draft_backup.3.md'
```

线程 A 已把 `.3` 移走，线程 B 再移时报错，`write_draft` 中途抛出 → 请求失败（客户端侧表现为 `RemoteDisconnected`），**且备份链处于半移动的不一致状态**。

这不是「多标签页才会踩」的边缘情况：`saveAll()` 是 async 且按钮无禁用，**同一标签页连点两次「保存修改并继续」就是两次并发请求**。

最小修法：

```python
import threading
SAVE_LOCK = threading.Lock()

def save_json(path, obj):                 # 原子写，避免读到半截文件
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
```

`/api/save` 分支内 `with SAVE_LOCK:` 包住 `read_draft → write_draft → load/save diffs` 整段；启动处补 `srv.request_queue_size = 64`（`socketserver` 默认 backlog 是 5，10 并发时部分连接直接被拒）。前端给 `saveBtn2` 加 `disabled` 防连点（一行）。

---

### A3 AI 写回不走服务端：无备份、无 diff（设计缺口）

交接文档第十二节的续写链路里，AI 收到指令后是**直接写 `draft.md`**（`写回后告诉我`）。这意味着：

- 那次覆盖不经过 `write_draft()` → **没有备份轮转**，AI 若误覆盖整篇，回滚点丢失；
- 该次改动不进 `diffs.json` → 用户下次保存时，AI 写的内容与用户手改的内容**混进同一条 diff**，「AI 读 diff 了解用户改了什么」的设计意图被稀释（AI 会读到自己的输出）。

建议：新增 `POST /api/ai-write {draft, round}`（内部复用 `write_draft` + diff 逻辑，diff 记录加 `"source": "ai"`），交接文档里把「写回后告诉我」改成「写回请走 `/api/ai-write`」。这样每一版稿子都有账可查，回滚链完整。

---

### A4 `round` 永不推进

前端没有任何地方对 `state.round` 做自增；`saveAll()` 只是把 `state.round || 0` 原样发回去（`index.html:1166`）。

实测：14 条 diff 记录的 `round` 全为 0；状态卡显示「第 0 回合」。

最小修法（服务端自增，前端用返回值，避免两边都改）：

```python
st = load_json(fpath(pid, "state.json"), {})
st["round"] = int(st.get("round", 0)) + 1
save_json(fpath(pid, "state.json"), st)
self._send(200, {"ok": True, "diff_count": len(ops), "round": st["round"]})
```

前端 `saveAll()` 里 `state.round = res.round`，状态栏与续写文案的回合数随之正确。

---

### A5 状态卡读的不是协同写作会话

`server.py:142-145` 取 `WHERE ended_at IS NULL ORDER BY last_activity_at DESC LIMIT 1`，即**任意最新活跃会话**。实测（本机）：

```
{"available": true, "status": "writing", "gateway": "running",
 "session": {"title": "D:\\Desktop\\GLM-d4\\collab-writing这是glm最新维护的代码…",
             "message_count": 33, "input_tokens": 71344, "output_tokens": 11970,
             "last_finish": "tool_calls"}}
```

它显示的是我这次改代码的 CLI 会话，不是写作会话——用户会把它当成「协同写作的进度」。

修法（择一）：

- 低成本：SQL 加过滤，例如 `AND (title LIKE '%协同写作%' OR last_activity_description LIKE '%协同写作%' OR cwd LIKE '%collab-writing%')`；`sessions.cwd` 实测有值（96 个会话非空 cwd），可用。
- 稳妥：`state.json` 里存 `hermes_session_id`，接口优先按 id 查，查不到再降级到模糊匹配。

另外 `finish != "stop"` 的判定把「最后一条是 tool 消息」一律算作 writing（实测 `last_finish: "tool_calls"` 即被判为写作中），这是有意为之的粗判，但和上一条叠加后会放大误报。

---

### A6 段落「合并」是假合并

`index.html:972-979` 的 `mergeOldest()` 只把两段文本首尾拼接，**不删除后一段的 `<!-- [SCENE:x] -->` 标记**。等值复现（逻辑与前端同构，12 段标记草稿经 `/api/save` 真实落盘）：

```
标记数 = 12 | mergeOldest 后 tab 数 = 9
合并后第 1 个 seg 里仍含标记数 = 4      ← 编辑区能看到 4 行裸标记
拼回全文再 parse → 段数 = 12            ← 与合并前相同，不收敛
```

后果：① 编辑区出现本不该给用户看的标记行；② 「9 个标签」只是显示层的结果，每次保存重新解析又回到 12 段、再合并一次，`from/to` 每次重算；③ 交接文档里「合并后不可再拆」的痛点，根因就是这里——合并没有留下任何可拆的凭证。

修法：合并时删掉后段的标记，把合并关系写成**一个合成标记**，让拼回全文后能收敛：

```javascript
function mergeOldest(segs) {
  while (segs.length > 9) {
    const a = segs[0], b = segs[1];
    const bodyB = b.content.replace(/^\s*<!--\s*\[SCENE:[^\]]+\]\s*-->\s*\n?/, '');
    segs = [{ start: a.start, end: b.end, from: a.from, to: b.to,
              content: a.content.replace(/\s*$/, '\n') +
                       `<!-- [SCENE:第${a.from}-${b.to}段] -->\n` + bodyB },
            ...segs.slice(2)];
  }
  return segs;
}
```

合成标记同时是「可拆分」的入口：以后做拆分功能，按 `第X-Y段` 标记切回去即可（对应交接文档痛点 5）。

---

### A7 内联 `onclick` 字符串插值（可执行任意 JS）

真实注入点是属性位置的两个地方，第一轮报告指的 `escapeHtml` 不是：

- `index.html:736-741` `renderTags()`：`onclick="toggleTag('${t}')"`，而 `t` 可以是用户自定义标签（`addTag()`，`index.html:747-753`）；
- `index.html:906-916` `renderCustomPresets()`：`onclick="applyCustomPreset('${p.name.replace(/'/g,"\\'")}')"` 只处理了单引号，**没有处理双引号与反斜杠**。

标签名填 `');alert(1);//` 时，生成的属性是 `onclick="toggleTag('');alert(1);//')"`，点击即执行；方案名含 `"` 时属性被提前闭合，DOM 结构直接坏掉。

（此项为静态推导，未在浏览器实测——本机 Chrome 未开远程调试。）

修法：去掉内联事件字符串，改事件委托 + `dataset`：

```html
<div class="tag-list" id="tagList"></div>
```
```javascript
$('#tagList').addEventListener('click', e => {
  const el = e.target.closest('.tag');
  if (el) toggleTag(el.dataset.tag);
});
// renderTags 里： `<span class="tag" data-tag="${escapeAttr(t)}">${escapeHtml(t)}</span>`
```

自定义方案列表同理（把 name 放进 `data-name`）。

---

### A8 无 Host / Origin 校验

实测：伪造 Host 与跨源 Origin 都被接受。

```
[fake-host]       200 {"active": "p_682bc5aed381", ...}       （Host: evil.example.com）
[fake-origin-post] 200 {"ok": true}                            （Origin: http://evil.example.com）
```

`do_POST` 也不校验 `Content-Type`，所以恶意页面可用 `text/plain` 简单请求绕过预检，直接覆写 `draft/plan/state`。读不到（无 CORS 头，浏览器拦响应），但**私人创作稿可被删除式覆写**。

修法（五行）：

```python
def _host_ok(self):
    return (self.headers.get("Host") or "").split(":")[0] in ("127.0.0.1", "localhost")
```

`do_GET`/`do_POST` 开头 `if not self._host_ok(): self._send(403, {"error": "bad host"}); return`。

---

### A9 文档与代码脱节

| 位置 | 现状 | 实际 |
|---|---|---|
| `升级交接-给外部模型.md:31` | `index.html` 969 行 / 40 KB | 1242 行 / 52.8 KB |
| 同上:32 | `server.py` 252 行 / 10.4 KB | 388 行 / 17.1 KB |
| 同上:36 | 文件清单含 `server.py.bak` | 已于 `8977318` 删除 |
| 同上:98 | `/api/continue`「**已废弃**」 | 代码中已不存在，应为「已删除」 |
| 同上:66 起 API 清单 | 缺 `/api/signal/continue`、`/take`、`/ack`、`/api/presets`、`/api/changes`、`/api/hermes-status` | 见 `README.md` 与代码 |
| 同上:17-19 | 路径写 `HERMES-d4/production/collab-writing` | 本仓副本在 `GLM-d4/collab-writing` |
| `README.md:82` | 结构表仍列 `server.py.bak`；API 表缺 signal/presets/hermes-status | — |
| `README.md:70-73` | 文档清单没有 `continue_watcher.py`、`署名规则.md`、`评审-*` | — |
| `.gitignore` | 无 `.zcode/`（`.zcode/plans/*.md` 已入库） | 建议加 |

这份交接文档是「交给外部模型做升级设计」的唯一入口，过时信息的代价是让下一个模型基于错误前提工作——成本极低、收益明确，建议与 A1/A2 一并提交。

---

## 二、对第一轮意见（`修改意见.md`）的核对

| 一轮条目 | 我的结论 |
|---|---|
| 1.1 `read_hermes_status` 拆函数 | 同意。但比拆分更急的是 A5 的语义修正——拆完仍是错的查询条件 |
| 1.2 SQLite `timeout=3` → 5 | 实测未复现锁竞争，保持现状或加注释即可；`uri=True` 的版本检查不需要（README 已写 3.8+） |
| 1.4 `DIFF_OPS_KEEP=500`，最坏 100 MB | **估算需修正**。实测 400 行全改 = 单条 12.7 KB，200 条 ≈ 2.5 MB；且真正的膨胀源是 A1 的空记录。优先级由「高」降为「低」 |
| 2.5 `escapeHtml` 漏单引号 = XSS | **结论需修正**。`escapeHtml` 只用在元素内容位置（`index.html:1067/1070`），单引号在文本节点无注入意义；真正的属性注入点是 A7。修 A7，`escapeHtml` 可保持原样 |
| 2.1 前端拆 CSS/JS | 同意，但零依赖前提下收益主要是可读性，排在 A1-A7 之后 |
| 3.1 watcher 单实例 | 同意，见下方 U4 |
| 5.x 文档过时 | 同意，具体清单见 A9 |
| 6.1 配置外部化 / 6.2 错误格式统一 / 6.3 数据版本号 | 同意，单人使用场景下优先级低 |
| 1.5「多标签页并发保存是已知限制，写进文档即可」 | **不应降级为文档条目**：实测是静默丢数据 + 未捕获异常（A2），且同一标签页连点即可触发 |

`leaves.json` 我另做了完整性校验，结果是干净的：42 个叶子 id 无重复，`exclusive`(4)/`synergy`(7)/`conditions`(4) 与 presets 中出现的键**全部**能在叶子表里找到，两轴权重和均为 100。这块不需要动。

---

## 三、升级建议（按收益排序，不含上面已列为修法的部分）

### U1 连接点图可视化（原设计核心，至今未落地）

`links.json` 已有数据、有 API、有侧栏区域，但界面只有裸 JSON。零依赖做法：侧栏渲染锚点列表（`{id, text, kind, related:[anchorId]}`），点击高亮关联项；需要图时用一段内联 SVG 画有向边，不需要任何库。这是痛点 3，也是「AI 查图给连锁修改建议」流程的前置条件——建议数据格式先在文档里定死，再动界面。

### U2 导出补一条「打印 / 存 PDF」路径

现在是 Blob 下载 `.md`。加一段 `@media print` 样式 + `window.print()`，即成稿可直接存 PDF，零依赖、零新文件。

### U3 watcher：单实例锁 + 探测重试 + 不再吞信号

`continue_watcher.py` 三处：

- `109-125` 探测失败直接 `sys.exit(1)`。tmux/服务启动顺序稍有偏差就得手动重跑，建议退避重试 5 次（每次 2s）；
- `60-65` `put_signal_back()` 是覆盖写：若期间网页又发了一条新信号，回写会把它覆盖掉，建议回写前比对 `ts`，旧的不覆盖新的；
- `179-183` 启动时无条件 `os.remove(SIGNAL_FILE)`：网页先保存、后启动 watcher 时，那条刚发出的信号会被吞掉。建议只在 `ts` 已超 TTL 时删除。

信号链路本身实测是干净的：`post → take` 返回文本、二次 `take` 返回 `{text: null}`、`ts` 过期返回 `{text: null, expired: true}`，原子消费（`os.replace`）有效。

### U4 双项目同开（痛点 9）

最小改动是让 active 变成「默认值」而不是「唯一值」：`/api/*` 全部接受 `?pid=`（缺省回落 active），前端顶栏的切换器变成「在新标签页打开」。这样不动现有 Electron 入口（端口与路径不变，符合硬约束 2）。

### U5 AI 侧写回纳入备份体系

即 A3 的正向版本：一旦 AI 写回走 API，「预算 vs 实际」就能按回合切分核对，`changes.json` 也能自动关联到具体 diff 记录（现在还靠 AI 自己填 `round`，而 round 恒 0）。

---

## 四、建议的实施顺序

1. **第一批（一次提交，全是服务端小改动）**：A1 + A2 + A4 + A8，加 `save_json` 原子写。改动集中在 `server.py` 的 `save_json` / `/api/save` / `do_GET` 入口，约 30 行。
2. **第二批（前端一轮）**：A6 真合并 + A7 事件委托 + `saveBtn2` 防连点与脏标记。
3. **第三批（文档与语义）**：A9 文档同步 + A5 会话过滤 + A3 的 `/api/ai-write`（需要同时改交接文档的续写流程描述，所以放在一起）。
4. 之后才是 U1/U2/U4 这类升级项。

---

## 五、可直接复用的验证命令

```bash
# 副本实测（不碰仓库 data/）
cp -r /mnt/d/Desktop/GLM-d4/collab-writing /tmp/cw-check && cd /tmp/cw-check
rm -rf data && python3 server.py 8399 &

# A1：空保存是否污染历史
curl -s -X POST localhost:8399/api/projects -H 'Content-Type: application/json' -d '{"name":"t"}'
curl -s -X POST localhost:8399/api/save -H 'Content-Type: application/json' -d '{"draft":"A1\nA2\n"}'
curl -s -X POST localhost:8399/api/save -H 'Content-Type: application/json' -d '{"draft":"A1\nA2-changed\n"}'
for i in 1 2 3 4 5; do curl -s -X POST localhost:8399/api/save -H 'Content-Type: application/json' \
  -d '{"draft":"A1\nA2-changed\n"}'; echo; done
python3 -c "import json,glob;d=glob.glob('data/p_*/diffs.json')[0];print([len(x['ops']) for x in json.load(open(d))])"
# 修复后应只剩 2 条非空记录，且 draft_backup.md 仍含改前的 "A1\nA2\n"

# A2：并发是否丢记录（修复后新增应等于并发数）
python3 - <<'PY'
import json,threading,urllib.request
def w(i):
    r=urllib.request.Request("http://127.0.0.1:8399/api/save",
        data=json.dumps({"draft":f"c{i}\n","round":0}).encode(),
        headers={"Content-Type":"application/json"},method="POST")
    urllib.request.urlopen(r,timeout=10).read()
ts=[threading.Thread(target=w,args=(i,)) for i in range(10)]
[t.start() for t in ts];[t.join() for t in ts]
d=json.load(open([p for p in __import__('glob').glob('data/p_*/diffs.json')][0]))
print("条数 =",len(d))
PY

# A8：Host 校验
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: evil.example.com' localhost:8399/api/projects
# 修复后应返回 403

# 回归：信号链路
curl -s -X POST localhost:8399/api/signal/continue -H 'Content-Type: application/json' -d '{"text":"x","round":0}'
curl -s -X POST localhost:8399/api/signal/continue/take -H 'Content-Type: application/json' -d '{}'
curl -s -X POST localhost:8399/api/signal/continue/take -H 'Content-Type: application/json' -d '{}'   # {"text":null}
```

---

## 六、附：本次审查未覆盖的部分

- 浏览器端视觉与交互（本机 Chrome 未开远程调试，A6/A7 的前端后果以等值复现与静态推导给出，标注在各自条目里）；
- Electron 宿主 `apps/desktop/src/plugins/collab-writing/plugin.tsx` 与 `workbenchBridge` 的实际契约（不在本仓，仅按交接文档描述对待）；
- 忙时 `/queue` 注入的真实落点（交接文档第十二节遗留项，需要用户在场，会消耗一轮模型调用）；
- `data/` 内真实稿件内容的正确性与创作质量（不属于本次技术审查范围）。

---

**签名：[hermes-deepseek-deepseek-flash娘]**

> 署名按 `署名规则.md`：工具=hermes、平台=deepseek、模型=deepseek-flash、后缀=娘。
> 若仓库希望换用别的后缀，请指定，我在下一次提交里改。
