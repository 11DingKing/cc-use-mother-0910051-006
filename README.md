# 企业双积分核算与交易服务

本项目是使用 Python、FastAPI 与 SQLite 实现的服务端应用，覆盖企业、车型、年度核算、订单撮合、交易结转和统计。它可在单个 Linux 应用容器内完成安装、测试、编译和接口验收，不依赖浏览器、外部数据库、缓存、消息队列或额外运行服务。

## 积分批次台账

企业不再只看到一个积分余额：余额被拆分为可追踪的**积分批次**，每个批次记录来源年度、取得方式（当年核发/历年结转/市场购入）、剩余量与适用期限。履约、出售、撤单、退回、结转、过期等每一次数量变动都写入分配台账（`BatchAllocation` + 明细），可解释、可重放、数量守恒。

- **批次生成**：积分记录确认→核发批次；交易成交→卖方批次核销、买方获得购入批次（继承来源年度与适用期限）；年度结转→来源批次转出、生成结转新批次并保留来源链。
- **可解释选择**：卖单挂单即按规则预留批次；履约按规则选择批次抵偿缺口；每次分配记录规则版本、候选快照与逐项选择原因（`GET /api/v1/credit-batches/allocations/{id}`）。
- **撤单与退回**：撤单按预留记录原路释放；履约/出售退回按台账明细原路恢复（出售退回同时追回买方批次，买方已使用则拒绝）。
- **过期保护**：过期处理只核销未预留余额，已预留给未完成交易的数量保留不动；释放/退回落在已过期批次上的数量直接核销。
- **规则换版与重放**：选择规则版本化且不可变，换版后历史分配仍按原规则重放校验（`POST /api/v1/credit-batches/allocations/{id}/replay`）。
- **数量守恒**：每个批次满足 初始量 = 剩余 + 预留 + 消耗 + 过期，且台账明细可独立重算验证（`GET /api/v1/credit-batches/conservation/{enterprise_id}`）。

主要接口（前缀 `/api/v1/credit-batches`）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/balance/{enterprise_id}` | 余额拆分视图（按取得方式/来源年度/到期年） |
| GET | `/batches` `/batches/{id}` `/batches/{id}/lineage` | 批次查询与来源链追溯 |
| POST | `/compliance/fulfill` | 履约：按规则选择批次抵偿年度缺口 |
| POST | `/expire` | 过期处理（不动预留量） |
| GET | `/allocations` `/allocations/{id}` | 分配台账与"为何选择这些批次" |
| POST | `/allocations/{id}/reverse` | 退回：按明细原路恢复 |
| POST | `/allocations/{id}/replay` | 按原规则（或指定版本）重放 |
| POST | `/transactions/{id}/return` | 交易退回（卖方恢复+买方追回） |
| GET/POST | `/rules` `/rules/{id}/activate` | 规则版本管理与启用 |
| GET | `/conservation/{enterprise_id}` | 数量守恒校验 |
| POST | `/sync-issued` | 存量数据补建核发批次 |

## 安装

```bash
python3 -m pip install -r requirements.txt -r requirements-dev.txt
```

## 测试

```bash
python3 -m pytest -q test_dual_credit_integration.py test_credit_batch_integration.py
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
