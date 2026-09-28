# coowrite · 协同写作工作台

一个本地运行的**回合制协同写作**工具：AI 写一段 → 人在网页里直接删改 → 保存产生 diff → AI 读 diff 续写下一段。

不是实时多人协同（那需要 OT/CRDT），而是「改完接着写」的回合制——用「编辑器 + 保存 + diff 记录 + 语义变化记录」实现。

> 面向长文协作编辑，内含成人向内容的评价维度（18+）。

## 特性

- **四步流程**：主页 → 文章规划（表单）→ 叶子选型（42 个评价指标滑块 + 标签 + 5 个预制方案）→ 协同编辑
- **预算制写作**：写前用滑块定目标档位＝施工图，写后跑评分对照「预算 vs 实际」。维度间存在互斥，全满分不可达，只求目标气质内的取舍
- **诚实的改动记录**：每次保存用 `difflib` 生成行级 diff；AI 读 diff 后把「什么故事事实变了」写成语义变化条目（`/api/changes`），带影响的锚点
- **浏览器式段落标签页**：按 `<!-- [SCENE:名] -->` 标记切分段落，编辑区只显示当前段，保存时合并回全文
- **多项目管理**：每篇文章一套数据目录，可新建与切换
- **零依赖**：前端单文件原生 JS（无框架、无构建），后端 Python 标准库
- **Apple 风 UI**：毛玻璃顶栏、大圆角、系统字体、胶囊按钮

## 快速开始

```bash
python3 server.py          # 默认端口 8338
python3 server.py 9000     # 或用参数换端口
```

然后浏览器打开 <http://127.0.0.1:8338/>

不需要 `pip install`，不需要 node，不需要构建步骤。Python 3.8+ 即可（实测 3.12）。服务只监听 `127.0.0.1`。

## 数据落盘

首次运行自动创建 `data/`：

```
data/
  projects.json                 # 项目索引 {active, items:[{id,name,created}]}
  p_<uuid12>/                   # 每篇文章一个目录
    draft.md                    # 全文，每段以 <!-- [SCENE:名] --> 开头
    draft_backup.md             # 每次写入前的上一版
    diffs.json                  # 保存时生成的行级 diff
    changes.json                # 语义变化记录（AI 读 diff 后写入）
    plan.json                   # 叶子目标档位（预算表）
    outline.json                # 文章规划表单
    state.json                  # 轮次 / 场景 / 标签 / 建议
    links.json                  # 连接点图（伏笔与设定影响关系）
```

**`data/` 不入库**——内含文章正文与项目状态，已在 `.gitignore` 中排除。请在本地自行保管。

## API

全部返回 JSON，无鉴权，仅监听本机。作用于当前 active 项目；无 active 项目时 GET 返回空默认值，POST 返回 `400`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/`、`/index.html` | 前端页面 |
| GET | `/api/leaves` | 42 个叶子定义（轴、权重、档位上限、联动规则、预制方案），与项目无关 |
| GET | `/api/projects` | 项目索引 |
| POST | `/api/projects` | 新建项目并设为 active，body `{name}` |
| POST | `/api/project/switch` | 切换 active，body `{id}` |
| GET/POST | `/api/draft` | 全文读写 |
| GET/POST | `/api/plan` | 叶子预算表 |
| GET/POST | `/api/outline` | 规划表单 |
| GET/POST | `/api/state` | 轮次 / 场景 / 标签 / 建议 |
| GET/POST | `/api/links` | 连接点图 |
| GET | `/api/diffs` | diff 历史 |
| POST | `/api/save` | 存稿 + 备份 + 生成 diff 记录，body `{draft, round}` |
| GET/POST | `/api/changes` | 语义变化记录（追加式） |

## 文档

- `升级交接-给外部模型.md` —— 自包含的完整交接：地址与启动、界面元素、API 全清单、数据结构、外部集成、升级硬约束、已知痛点、评价体系背景、验证步骤。交给其他模型做升级设计时用这份。
- `site-summary.md` —— 早期设计汇总（已废止，仅作历史追溯）。

## 结构

| 文件 | 说明 |
|---|---|
| `index.html` | 单页前端：四个视图 + 三个弹窗同在一个 DOM，原生 JS |
| `server.py` | 后端：标准库 `http.server`，静态页 + JSON API |
| `leaves.json` | 42 个叶子（评价指标）定义与联动规则 |
| `server.py.bak` | 单项目时代的旧版服务，仅作追溯 |

## 许可

未指定许可证（默认保留所有权利）。如需开源授权，请补充 `LICENSE`。
