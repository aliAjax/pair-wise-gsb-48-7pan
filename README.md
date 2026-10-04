# 证券结算与企业行动处理

纯Python标准库实现的证券结算与企业行动处理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、净额结算、交收完整性、公司行动调整、净额批次与回执匹配规则。
- `src/repository.py`：SQLite建表、事务和查询（指令/批次/批次明细/托管回执/差异/审计）。
- `src/service.py`：用例编排、权限与机构隔离、条件写入（先到先得）和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败场景、净额批次与并发确认测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8324
```

默认端口为`8324`，默认数据库位于项目目录。服务启动时自动建表。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，批次与指令按`X-Org`做机构隔离（缺省为`DEFAULT`）。

## 日终净额批次规则

1. **组批**：结算主管（`settlement_officer`）按账户、币种、交收日把本公司指令组成净额批次；买入为应付正额、卖出为应收负额，批次净额为逐笔代数和。会员只能提交本公司指令（以`X-Org`判定），跨机构批次返回`403 permission_denied`。
2. **确认冻结**：确认时固定公司行动版本（`ca_version`）和每笔净额（`frozen_net_amount`/`frozen_quantity`）以及批次总额（`frozen_total`）。确认后指令再改动、公司行动换版（`revise_corporate`）或托管回执晚到，都不改变已确认金额。
3. **未确认失效**：批次内指令在确认前被改动，开放批次立即变为`invalid`，确认请求被拒绝，需调用重算接口按当前指令重建后再确认。
4. **回执对账**：托管回执可能重复、乱序、早到、晚到或引用不存在的指令。
   - 相同`custodian_receipt_id`的重复回执只记一次（`409 conflict`）。
   - 确认前到达的回执挂起（`pending`），确认瞬间统一对账。
   - 引用不存在指令记为`orphan`，不挂接批次。
   - 版本（或数量/金额）对不上时保留`receipt_discrepancies`差异并把批次置为`settlement_held`，交收停住（错误码`settlement_held`），后续补齐回执也不自动解除。
5. **并发确认**：两名主管同时确认同一批次，条件写入（`state='open' AND version=?`）保证只有先到者成功；失败者按原批次号重试是幂等读，不会重复记账。
6. **读取一致性**：列表、详情、统计和审计全部读取同一份批次行与冻结明细。

## 指令接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：指令状态统计。
- `POST /api/records`：创建指令，`data`需含`instrument/side/quantity/price/fees/currency/settlement_day/corporate_action/action_ratio/account`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。动作：`apply_corporate`、`approve`、`settle`、`fail`、`reverse`、`revise_corporate`（公司行动换版，推进`ca_version`并重算拆股/股息）。

## 批次接口

- `POST /api/batches`：组批，请求体`{"batch_no":"NB-001","references":["I1","I2"]}`。批次号幂等：写入失败后用同一批次号重试返回既有批次，不重复建批。
- `GET /api/batches`：批次列表，可带`state`、`limit`。
- `GET /api/batches/{id}`：批次详情，含冻结明细、回执与差异。
- `GET /api/batches/{id}/audit`：批次审计时间线。
- `GET /api/batch-stats`：按状态统计批次数与冻结金额合计。
- `POST /api/batches/{id}/confirm`：确认冻结，请求体`{"expected_version":1}`。
- `POST /api/batches/{id}/recompute`：失效批次重算，可省略`references`沿用原成员，或传入新成员列表。
- `POST /api/batches/{id}/settle`：全部明细匹配且无差异时完成交收。
- `POST /api/receipts`：登记托管回执，请求体：
  ```json
  {"custodian_receipt_id":"RCP-1","reference":"I1","received_version":1,
   "delivered_quantity":100,"net_amount":1000.0}
  ```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整指令流程、规则计算、重复引用、权限拒绝、版本冲突、净额组批、确认冻结、失效重算、回执重复/乱序/孤儿、版本差异停住交收、两名主管并发确认。
