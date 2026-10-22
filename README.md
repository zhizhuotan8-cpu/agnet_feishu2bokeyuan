# 飞书笔记 Agent

写一句知识点，模型补成一篇能直接发出去的技术博客；先打分，不到 8 分就按评估建议自动重写（最多 3 次），之后（或你强行通过）才交给你确认并写进飞书。

设计文档见 `docs/飞书笔记-Agent-架构设计.md`（v0.3）。**链路 A 已经打通到真实飞书**：扩写 → 评分（不达标自动返工）→ 审阅（本地网页或飞书卡片）→ 导入成飞书文档，并把文档授权给你。

## 当前进度

已实现：

- 知识点 → 结构化扩写（JSON schema 校验 + 代码语法检查 + 失败回灌重试）
- 博客模板渲染（掘金/CSDN 教程风，Markdown）
- 五维评分器（加权 + 一票否决 + 扣分必须引用原文 + 引用回查 + 1-3 条可执行的修改建议）
- 自动返工：不到 8 分按建议重写，最多 `MAX_REWRITES`（默认 3）次，之后挂起等人
- LangGraph 状态机：`normalize → expand → critique → render → evaluate →（auto_rewrite → expand）→ prepare_review → review`，`interrupt()` 挂起等人
- 审阅动作：通过 / 重写一次（和自动返工共用上限）/ 拒绝，条件 UPDATE 挡重复点击
- 写入适配器：`fake` 落本地文件；`live` 走 `medias/upload_all → import_tasks` 导入成 docx，建好后加协作者
- 文档回读：docx blocks → Markdown（供链路 B 的 hash 校验用）
- 飞书审阅卡片：卡片上直接点「通过 / 重写 / 丢弃」，回调走长连接
- CLI + 本地审阅页（FastAPI，带按钮）
- SQLite 事件流、评分历史、笔记快照（内容 hash 冻结）

未实现（下一期）：

- 链路 B：飞书文档 → 博客园 MetaWeblog 发布（设计文档里有完整方案，回读接口已就位）

## 快速开始

需要 Python 3.10+。

```bash
cd F:\python\agent_feishu_bokeyuan
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"

copy .env.example .env
```

不填 `LLM_API_KEY` 会自动用 FakeLLM，整条链路能跑通，只是正文是模板占位内容。

```bash
note-agent new "Python 的 GIL 到底是什么"
```

打分不到 8 分时会自动重写，终端会逐轮打印 `评分 6.0 未达标，自动返工 1/3 次…`；每轮要调三次模型，慢是正常的。最多 3 轮后停在你面前。输出类似：

```
草稿 d_1a2b3c4d5e  ·  8.0/10  达标  ·  待审阅
标题  Python 的 GIL 到底是什么：一次讲清楚
维度  准确性 8 / 具体性 7 / 信息密度 8 / 结构 9 / 可读性 8
扣分项  1 条
  - 具体性 -1.0
    原文：Python 的 GIL 到底是什么：一次讲清楚
    原因：fake 模式没有给出真实可验证的例子，扣具体性分。
稿件  data/drafts/d_1a2b3c4d5e.md
重写  已用 0 / 3 次
```

然后二选一：

```bash
note-agent serve
# 打开 http://127.0.0.1:8000/ ，点「通过并写入」

# 或者纯命令行
note-agent approve d_1a2b3c4d5e
note-agent rewrite d_1a2b3c4d5e
note-agent reject d_1a2b3c4d5e --note "太浅了"
```

fake 模式下「写入飞书」= 在 `data/fake_feishu/` 下生成一个 Markdown 文件，同时把冻结快照写到 `data/notes/<note_id>/note.md`。

## 接真实模型

所有配置都在项目根目录的 `.env` 里，由 `python-dotenv` 加载（`app/config.py`），再用 pydantic 做类型校验。已经存在的系统环境变量优先，临时覆盖不用改文件；相对路径的 `DATA_DIR` 按项目根目录解析。改完 `.env` 要重开命令或重启 `serve` 才生效。

编辑 `.env`：

```dotenv
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-...
LLM_MODEL=deepseek-chat
JUDGE_MODEL=            # 建议填另一个模型，别让生成模型给自己打分
```

任何 OpenAI 兼容的 `/chat/completions` 服务都行（DeepSeek、通义、智谱、本地 Ollama 等）。配置后 `note-agent new` 会走真实模型；评分器用 `JUDGE_MODEL`，`temperature=0`。

`deepseek-reasoner` 这类推理模型可以用，但思维链也消耗 `max_tokens`，预算小的时候正文会是空的（`finish_reason=length`）。客户端会在这种情况下自动翻倍重试（1800 → 3600 → 7200），想更快就让评分器用 `deepseek-chat`。

## 接入真实飞书

先在[开放平台](https://open.feishu.cn/app)建一个企业自建应用（已有就跳过）。凭证只写进 `.env`，不要贴到聊天里、也不要写进代码。

**1. 开权限**（权限管理 → 开通后要发布版本才生效）：

| 权限 | 用途 |
| --- | --- |
| `docx:document` | 读写新版文档 |
| `docs:document:import` | 上传 Markdown 并导入成 docx |
| `drive:drive` | 素材上传、文件删除、云空间 |
| `docs:permission.member:create` | 文档建好后加你为协作者 |
| `im:message:send_as_bot` | 发审阅卡片 / 测试消息 |
| `im:chat:readonly` | `feishu-chats` 列出机器人所在的群 |

**2. 填 `.env`**：

```dotenv
FEISHU_MODE=live
FEISHU_APP_ID=cli_...
FEISHU_APP_SECRET=...
FEISHU_OWNER_OPEN_ID=ou_...   # 建好的文档授权给你
FEISHU_FOLDER_TOKEN=          # 留空 = 应用云空间根目录
FEISHU_CHAT_ID=oc_...         # 留空 = 不发卡片，只用本地审阅
```

`FEISHU_CHAT_ID` 和 `FEISHU_OWNER_OPEN_ID` 都不用手抄：

```bash
note-agent feishu-chats                      # 列出机器人所在的群 → oc_ 开头的 chat_id
note-agent feishu-members --chat-id oc_xxx   # 列出群成员的 open_id + 姓名 → 找到你自己那行
```

机器人在群里时，`feishu-members` 用的是 `im:chat:readonly`，不需要通讯录权限。另一个办法是 API 调试台调 `contact/v3/users/batch_get_id`（邮箱/手机号换 open_id），需要额外开 `contact:user.id:readonly`。

`FEISHU_OWNER_OPEN_ID` 填的是**你本人**的 open_id（`ou_` 开头），不是应用的。没配的话文档归应用所有，你在飞书里搜不到。

**3. 自检**：

```bash
note-agent feishu-check              # 只验凭证和配置，不发消息
note-agent feishu-check --send-test  # 往群里发一条测试消息
note-agent feishu-chats              # 列出机器人所在的群，拿 chat_id
note-agent feishu-members --chat-id oc_xxx  # 拿你自己的 open_id
```

**4. 开卡片回调**：开发者后台 → 事件与回调 → 订阅方式选「长连接」→ 添加事件 `card.action.trigger`。不需要公网地址、不需要回调 URL。`lark-oapi` 必须 `>= 1.6.4`（更早的版本会丢卡片回调帧，报 200340）。

```bash
note-agent feishu-listen
```

长连接保持期间，卡片上的按钮才会生效。回调里只做状态预检和 ack（飞书要求 3 秒内响应），真正的 resume 在后台线程里跑；重复点按会被草稿状态挡掉。

完整流程：

```bash
note-agent new "Python 的 GIL 到底是什么"
# 群里的卡片 → 点「通过写入」→ 飞书云空间出现文档，你有完全访问权限
# 没开 feishu-listen 也没关系：note-agent approve <id> 或网页审阅照样能用
```

**写入时会发生什么**：Markdown 先上传为素材（`parent_type=ccm_import_open`），再创建导入任务（`file_extension=md`、`type=docx`），然后轮询 ticket（最长 90 秒）。成功后调权限接口把 `FEISHU_OWNER_OPEN_ID` 加成 `full_access`。加协作者失败不会让整篇作废，只记一条 `doc_warning` 事件。

已知的转换损耗：飞书导入不保留表格和图片，回读时它们会变成 `<!-- 表格未同步 -->` 这类注释，而不是悄悄消失。

## 命令

| 命令 | 作用 |
| --- | --- |
| `note-agent new "知识点"` | 建草稿，一路跑到审阅挂起 |
| `note-agent list` | 最近草稿与分数 |
| `note-agent show <id> [--full] [--events]` | 看评分、扣分项、Markdown、事件流 |
| `note-agent approve <id>` | 通过并写入飞书 |
| `note-agent rewrite <id>` | 手动再重写一次（和自动返工共用 `MAX_REWRITES`） |
| `note-agent reject <id> [--note]` | 拒绝 |
| `note-agent retry-write <id>` | 写入飞书失败后重试（不重新生成） |
| `note-agent preview <id>` | 导出静态 HTML 预览 |
| `note-agent serve [--port]` | 启动带按钮的审阅页 |
| `note-agent feishu-check [--send-test]` | 检查凭证、权限、落点和群配置 |
| `note-agent feishu-chats` | 列出机器人所在的群，拿 `chat_id` |
| `note-agent feishu-members [--chat-id]` | 列出群成员的 `open_id`，拿 `FEISHU_OWNER_OPEN_ID` |
| `note-agent feishu-listen` | 启动卡片回调长连接（Ctrl+C 退出） |

## 评分规则

五维加权，总分 10：

| 维度 | 权重 | 看什么 |
| --- | --- | --- |
| 准确性 | 0.30 | 事实对不对 |
| 具体性 | 0.20 | 有没有可验证的例子、数字、版本号 |
| 信息密度 | 0.20 | 有没有废话 |
| 结构 | 0.15 | 是否按模板走、能不能扫读 |
| 可读性 | 0.15 | 句子是否过长、术语有没有解释 |

- `total >= PASS_SCORE`（默认 8.0）才算达标
- 一票否决（事实错误、把概念讲反）直接把总分压到 5.0
- 每条扣分必须带 `quote`，且能在正文里逐字找到；找不到会标黄提示「引用未在正文中找到」
- **不到 8 分会自动返工**：用评估器给的 `suggestions` 加扣分项重写，最多 `MAX_REWRITES`（默认 3）次，每轮都重新评分
- 3 次后仍然不到 8 分（或者评估器自己报错、没有建议可用），就停下把稿子交给你：可以强行通过，也可以再点一次重写，或者丢弃
- 自动返工和手动重写共用同一个计数器，卡片标题会显示「重写 已用 x / 3 次」

## 目录结构

```
app/
  config.py              # python-dotenv 读 .env + pydantic 校验
  cli.py                 # typer 命令
  main.py                # FastAPI 审阅页
  preview.py             # 预览页渲染（Jinja + markdown-it）
  agents/
    prompts.py           # 扩写 / 自检 / 评分 三套 prompt
    schemas.py           # DraftContent / Evaluation
    llm.py               # OpenAI 兼容客户端 + FakeLLM
    expander.py          # 生成 + 校验 + 重试
    critic.py            # 只补 uncertain_claims
    checks.py            # Python ast 校验，其他语言括号配对
    renderer.py          # 模板渲染
    scoring.py           # 权重、总分、门槛、引用回查
    evaluator.py         # 评分 + 失败降级
  graph/
    state.py             # NoteState
    note_ingest.py       # 链路 A 状态机
    runner.py            # 图实例、checkpoint、start/resume
  adapters/feishu/
    base.py              # FeishuClient 协议 + DocRef
    fake.py              # 本地模拟（文件 + 日志）
    live.py              # 真飞书：token、导入、权限、回读、发消息
    docx_md.py           # docx blocks → Markdown
    cards.py             # 审阅卡片 JSON
  feishu_listener.py     # 卡片回调长连接
  store/
    db.py                # SQLite：drafts/evaluations/notes/events
    content.py           # 草稿与快照文件
  templates/
    blog_post.md.j2      # 博客模板
    preview.html.j2      # 审阅页
tests/
```

运行时数据都在 `data/`（已 gitignore）：`app.db`、`graph.sqlite`、`drafts/`、`notes/`、`snapshots/`、`fake_feishu/`。

## 状态机

```
normalize → expand → critique → render → evaluate
                                           ├─ 达标 → prepare_review
                                           ├─ 未达标且次数没用完 → auto_rewrite（计数 +1）→ expand
                                           └─ 未达标但次数用完 / 评估器报错 → prepare_review

prepare_review → review(interrupt) ├─ approve → write_feishu → commit
                                   ├─ rewrite → expand（计数 +1）
                                   └─ reject  → END
```

自动返工这条边是有条件的：只有 `verdict != "pass"`、评估器没有报错、且 `rewrite_count < MAX_REWRITES` 时才回 `expand`；否则一律挂起等人。`recursion_limit` 调到 50，3 轮返工不会撞默认上限。

草稿状态：`awaiting_review → approved → creating_doc → written`，另有 `rejected` / `discarded`。状态迁移用 `UPDATE ... WHERE status = ?`，重复点「通过」不会写两篇。

审阅拆成两个节点是有意的：LangGraph 在 `resume` 时会重跑被打断的节点，所以发通知、记事件放在 `prepare_review`，`review` 节点里只有 `interrupt()`。

## 测试

```bash
pytest -q
```

- `test_config.py`：dotenv / 环境变量优先级、相对路径锚定、模型回落
- `test_scoring.py`：权重、加权、否决封顶、引用回查
- `test_db.py`：条件状态更新、重写计数、评分详情、事件顺序
- `test_renderer.py`：模板变量与 schema 对齐、必需小节、参考/待核实的换行
- `test_critic.py`：待核实去重（标点差异、子串包含）
- `test_evaluator.py`：评估失败与一票否决分开
- `test_graph_flow.py`：fake 模式端到端（通过 / 拒绝 / 重写上限 / 低分强行通过 / 自动返工两轮后达标 / 评估器报错跳过返工 / 重复点击 / 导入失败后 `retry-write`）
- `test_preview.py`：XSS 转义、按钮显隐
- `test_feishu_live.py`：导入链路、token 缓存、限流重试、协作者失败降级（`httpx.MockTransport`，不发真实请求）
- `test_docx_md.py`：标题/列表/代码/引用/图片表格占位
- `test_cards.py`：按钮、颜色、重写上限禁用、预览用 plain_text

## 已知限制

- 评估器和生成器共用一个模型时会偏松，靠 `JUDGE_MODEL` 和 20 篇人工校准拉回来
- 本地审阅页没有鉴权，只监听 `127.0.0.1`；卡片里「看全文」链接同理，手机审阅要改 `REVIEW_BASE_URL`
- 单进程、单用户；`data/` 下 SQLite 不适合多实例同时写
- 写入阶段抛异常时草稿会回滚到 `approved`，用 `note-agent retry-write <id>` 重试，不需要重新生成和评分；但进程如果在导入中途被杀，草稿会停在 `creating_doc`（飞书可能已经建好文档），目前要人工确认后把状态改回 `approved` 再重试
- 飞书导入是异步任务，失败码（112/118 等）只做转述，没有针对性的排查建议；导入的内容不保留表格和图片
- 文档的所有者是应用，必须靠 `FEISHU_OWNER_OPEN_ID` 授权才能在你自己的云空间里看到
- 生成阶段失败（模型连续返回不合法 JSON）会把草稿标成 `discarded`，不占用审阅列表
- 自动返工只保留最后一版正文：每轮都覆盖 `data/drafts/<id>.md`，如果分数一路走低（比如 7 → 6 → 5），停在面前的是最后一版而不是最高分那版。中间版本只能从评分历史（`note-agent show <id> --events`）看到分数和扣分，正文本身没有留档
