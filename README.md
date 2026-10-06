# 企业双积分核算与交易服务

本项目是使用 Python、FastAPI 与 SQLite 实现的服务端应用，覆盖企业、车型、年度核算、订单撮合、交易结转和统计。它可在单个 Linux 应用容器内完成安装、测试、编译和接口验收，不依赖浏览器、外部数据库、缓存、消息队列或额外运行服务。

## 安装

```bash
python3 -m pip install -r requirements.txt -r requirements-dev.txt
```

## 测试

```bash
python3 -m pytest -q test_dual_credit_integration.py
```

## 编译

```bash
python3 -m compileall -q .
```

## 接口验收

```bash
python3 -c "from app.main import app; assert len(app.routes) > 5; print(len(app.routes))"
```

## 启动

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 积分批次台账

企业看到的单一积分余额被拆成可追踪的**积分批次（CreditBatch）**，记录：来源年度、最初核发年度、取得方式（当年核发 `annual_issue` / 历年结转 `carryover` / 市场购入 `market_purchase` / 退回恢复 `return`）、原始量/剩余量/冻结量/已消耗量/过期量、适用期限，以及选批规则版本。

守恒不变式（每批次恒成立，误差 0.01 内）：

```
原始量 = 剩余可用 + 冻结(预留未成交) + 已消耗 + 已过期
```

关键规则：

- **挂单即预留**：卖单挂出时按可解释规则选批并冻结；成交时冻结转消耗，撤单时释放，卖单改量同步增减冻结。
- **成交转移**：买方按卖方实际成交行逐批入库「市场购入」批次，继承有效期与最初核发年度。
- **退回**：卖方逐成交行恢复；原批次仍有效则恢复进原批次，否则生成带 `parent_batch_id` 的退回恢复批次；买方冲回购入批次，已耗用部分如实报告、不做假恢复。
- **结转**：父批次出库、子批次按政策比例（80%/60%/40%）缩量入库，`CreditBatchLink` 保留逐段来源链，可追溯到最初核发；结转按业务时点判定有效期。
- **过期保护**：过期作业只处理可用部分，已冻结给未完成交易的数量保留，成交/撤单后才结算。
- **可解释/可重放**：每次选批保存候选快照、规则版本与逐行理由；可用历史规则重放并比对，也可用新版本规则对同一快照对比重放。当前内置规则 `v1`（到期日→来源年度→结转优先）、`v2`（到期日→核发优先→来源年度）。

主要接口（前缀 `/api/v1/credit-batches`）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/credit-batches` | 批次列表（可按企业/状态/取得方式/来源年度过滤） |
| GET | `/credit-batches/balance/{enterprise_id}` | 余额口径：可用/冻结/已消耗/已过期，按取得方式分组 |
| GET | `/credit-batches/{id}/lineage` | 批次来源链（向上到最初核发、向下到派生批次） |
| POST | `/credit-batches/preview-selection` | 预览一次消费会选哪些批次及理由（不落库） |
| POST | `/credit-batches/fulfillment` | 年度履约，按规则选批消耗 |
| POST | `/credit-batches/transactions/{id}/return` | 交易退回，按原成交行恢复/冲回 |
| POST | `/credit-batches/expire` | 过期作业（只过期可用部分，保留冻结量） |
| GET | `/credit-batches/explain/group/{id}` | 一次选批为何选择这些批次 |
| GET | `/credit-batches/explain/transaction/{id}` | 一次成交的批次选择解释 |
| GET | `/credit-batches/replay/{id}` | 按原规则重放历史选批并比对 |
| POST | `/credit-batches/replay/{id}/with-rule` | 用指定规则版本对历史快照重放对比 |
| GET | `/credit-batches/rules/versions` | 可选规则版本及说明 |
| GET | `/credit-batches/audit/conservation` | 数量守恒审计（批次/冻结/买卖双边） |

批次相关测试：

```bash
python3 -m pytest -q test_batch_ledger.py
```
