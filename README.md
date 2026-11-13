# cc-data-lab

CC_Codex 二期（26.11）**数据工程**主线基线：**采集任务控制台**（Flask 后端 + HTMX 前端页 + 站内 Mock 目录）。

## 领域

- **所属领域**：数据工程  
- **子方向**：数据采集、爬虫与 RPA  

## 技术栈

- Python 3.12+、Flask 3、SQLite  
- HTMX（CDN）+ Jinja 模板  
- pytest  

## 本地运行

```powershell
cd claude-workspace\cc-data-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

浏览器打开 http://127.0.0.1:5590 ，登录 **admin / 123456**（另有 `alice` / `bob` 同为 `123456`，用于演练交班与接手）。

流程：创建任务 → 选种子 `/mock/catalog/alpha` 或 `beta` → 详情页「运行采集」→ 查看落库（alpha 含重复标题，会演示去重跳过）。

## Docker 一键启动（推荐用于快照前验收）

需已打开 **Docker Desktop**：

```powershell
cd claude-workspace\cc-data-lab
docker compose up -d --build
docker compose ps
```

- 页面：http://127.0.0.1:5590/login  
- Mock：http://127.0.0.1:5590/mock/catalog/alpha  
- 停止：`docker compose down`（保留库卷）· 清卷：`docker compose down -v`

`docker compose ps` 中 `STATUS` 含 **healthy** 表示服务可访问。线下表「环境可复现等级」可填 **已容器化，可一键起环境**。

## 前后端说明

| 层 | 说明 |
|----|------|
| 后端 | `app/` 路由、任务状态机、班次与交接单、同步 runner、SQLite |
| 前端 | `templates/` + `static/`，浏览器可点操作 |
| Mock 源站 | `/mock/catalog/<id>` HTML，无需外网 |

## 交班 / 接手（跨班权属转移）

交班不是改顶栏文字，而是把**编码权属**从本班转到下一班。四处同时生效：本班账册、下一班账册、交接单、顶栏状态。

### 已定口径（不再更改）

**整班原子移交**：交班必须把本班全部未移交编码放进同一笔交接单，缺任何一条都整笔失败并回滚到交班前，本班仍是「本班」。接手时若发现某编码已不在本班，同样整笔不搬，未交出去的留在本班并标「未交清」。

### 规则

- **权属只落一班**：`crawl_jobs.code` 全表唯一；只有接手动作会改写 `crawl_jobs.shift_id`，且只在窗口内发生。`shifts` 表的唯一索引保证「在班」永远只有一班。
- **接班人不能是建档人**：班次建档人或本班任一编码的建档人都不能当接班人。
- **窗口按后台时刻判断**：`HH:MM` 按服务器时刻的当天解析；测试用 `app.config["FIXED_NOW"]` 钉死时刻。
- **移交方式**：当日首次交班写下 `day_registrations.mode` 作为当日口径；之后交班或接手提交的方式与之不一致，直接拒绝并提示「方式不对」。
- **交班后本班顶栏改为待接手**：题名与追加（建档、改题名、运行采集）全部锁住；交接单列出待移交编码。
- **窗口内接手**：编码进入下一班账册，两班分列；本班转「已移交」。
- **窗口过后未接手**：本班自动解封、交接单作废，不能再点接手；解封后的本班可重新交班或继续建档。
- **并发**：两笔同时交班或接手，只许一笔成功，另一笔写「争用失败」（`shift_events.outcome=conflict`）。
- **采集未停稳**：交班入口在页面上不可见，后端也兜底拒绝。

### 顶栏与列表

顶栏可区分 `本班` / `待接手` / `已移交` / `已解封`（`data-shift-status`），并显示交接单状态。列表展示班次账册、交接单状态、未交清条数与当日口径；每行任务显示编码归属班次。

### 演示流程

1. `admin` 登录，创建两个任务（编码 `CJ-0001`、`CJ-0002`）。
2. 交班给 `alice`，窗口填 `09:40` → `10:10`（按后台钟点）。
3. 顶栏变「待接手」，交接单列出两个编码，题名与追加锁住。
4. 用 `/switch` 切到 `alice`（或直接以 alice 登录），窗口内点「接手」。
5. 编码进入下一班账册，本班顶部转「已移交」，新一班在班。

## 初始快照

在首次 Claude 单轮跑题前 commit + push 本树（不含 `.venv/`、`instance/*.db`）。后续题目在同 repo 上叠加调度、RPA 步骤、增量水位等能力。

## 基线 vs 跑题目录

| 路径 | 角色 |
|------|------|
| `claude-workspace/cc-data-lab`（本目录） | **基线**：改功能、docker 验收 |
| `ccproject/cc-data-lab/data-XX` | **单题**：复制基线后挂容器、出快照 |

复制：`..\scripts\Copy-BaselineToQuestion.ps1 -QuestionId data-01`

## 跑题 Harness

见 `CCcodex/docs/ClaudeCode_Windows使用说明.md`（镜像 2.0）。容器挂载 **ccproject 下该题目录**，不是本基线路径。
