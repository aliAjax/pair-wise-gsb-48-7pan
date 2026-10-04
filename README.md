# 日终净额结算批次

纯 Python 标准库实现的日终结算净额批次原型，使用 SQLite 持久化，HTTP 接口由 `http.server` 提供。

## 业务规则

- **净额组批**：日终结算前，结算主管按账户、币种、交收日把结算指令组成净额批次；同批必须同机构，禁止跨机构批次。
- **会员权限**：会员只能提交/改动本机构（`X-Org`）指令，越权提交或访问跨机构批次返回 403；会员不能组批。
- **确认即冻结**：批次确认时固定所用公司行动版本和每笔净额（快照指纹）。确认后指令再改动、公司行动换版或托管回执晚到，已确认金额保持冻结。
- **未确认失效**：指令改动或公司行动换版时，相关未确认批次立即置为 `invalid`，按原批次号重算可重建；已确认/已交收批次禁止重复记账。
- **回执对账**：外部托管回执可能重复、乱序或引用不存在指令。重复回执只记一次（幂等返回原记录）；版本不符、金额不符、引用不存在或缺失回执均保留为差异，交收停住挂起。
- **并发确认**：两名主管同时确认同一批次，先到者成功，后者冲突；写入失败可按原批次号重试，内容不变时幂等。
- **读取同源**：列表、详情、统计与审计均读取同一份批次结果。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：身份、错误类型与输入校验。
- `src/rules.py`：净额计算、公司行动版本固定、批次/分录指纹、回执对账（纯函数）。
- `src/repository.py`：SQLite 建表、事务、状态 CAS、失效联动与回执去重。
- `src/service.py`：用例编排、角色与机构权限、对账挂起。
- `src/http_api.py`：HTTP 路由与统一错误响应。
- `src/audit.py`：批次/指令/回执审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：净额与对账纯规则、端到端流程、失败场景、HTTP 接口测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8324
```

默认端口 `8324`，服务启动时自动建表。

## 角色

| 角色 (`X-Role`) | 能力 |
| --- | --- |
| `member` | 提交/改动本机构指令（需带 `X-Org`）；只读本机构批次 |
| `settlement_officer` | 组批、确认、对账、交收；可访问全部机构 |
| `custodian` | 登记外部托管回执 |
| `corporate_actions` | 发布公司行动新版本 |
| `admin` | 超级角色，可执行任意操作 |

## 主要接口

除 `/health` 和 `/` 外，请求需提供 `X-User-Id`、`X-Role`，会员需提供 `X-Org`。

- `POST /api/instructions`：会员提交结算指令。
- `POST /api/instructions/{reference}`：会员改动指令（带 `expected_version`），自动失效相关未确认批次。
- `GET /api/instructions`：指令列表，可按账户/币种/交收日过滤。
- `GET /api/instructions/{reference}`：指令详情。
- `POST /api/corporate-actions`：发布公司行动新版本（因子/现金因子），自动失效相关未确认批次。
- `POST /api/batches`：按 `references` 组净额批次，请求体 `{"batch_no":"...","references":[...]}`；同批必须同账户、币种、交收日、机构。
- `GET /api/batches`：批次列表。
- `GET /api/batches/{batchNo}`：批次详情（含固定分录与未决差异数）。
- `POST /api/batches/{batchNo}/confirm`：确认批次，带可选 `expected_version`；并发下先到者成功。
- `POST /api/batches/{batchNo}/reconcile`：回执对账，返回差异计数与挂起标志。
- `POST /api/batches/{batchNo}/settle`：交收前自动对账；存在差异则 409 挂起。
- `GET /api/batches/{batchNo}/audit`：批次审计时间线。
- `POST /api/receipts`：托管回执登记，重复 `receipt_no` 返回 200 原记录（不重复入账）。
- `GET /api/receipts`：回执列表，可按 `reference` 过滤。
- `GET /api/stats`：批次状态、指令、回执与未决差异统计。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：净额带方向计算、公司行动版本固定、重复/乱序/幽灵回执对账、缺失回执挂起、确认后冻结、未确认失效与原批次号重建、跨机构拒绝、并发确认先到者成功、重复记账拒绝与 HTTP 端到端。
