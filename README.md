# 场馆开放预约履约台

为公共场馆运营与预约权益核验员提供离线合成公开来源的时效和证据核验。核心结果：判定公开开放变更下的既有预约入场权和补偿。公开文本经来源身份和版本采信后，分别驱动业务案件与履约核验；这三个子系统有独立业务实体、状态和 CLI 入口。

## 运行

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m venuecase demo
python -m pytest -q
```

手动运行：`python -m venuecase adopt --db state.db --input fixtures/opening_normal.json`，`python -m venuecase assess --db state.db --input fixtures/booking_normal.json`，`python -m venuecase settle --db state.db --input fixtures/entry_normal.json`，`python -m venuecase report --db state.db --out report.json`。

## 状态与证据

- 开放采信：确认哪一版开放计划对预约时段生效，留存原文摘要、修订、冲突和取消；同来源同版本异文不能覆盖旧证据。
- 预约重排：在容量与权益优先级下分配原场次或改期，案件绑定来源修订，公共信息变更会把旧案件标为失效。
- 入场补偿：核销实际入场并决定未履约预约的补偿，回执逐项验版本、目标和时点；部分履约与逾期保留为独立结论。

正常和失败输入位于 `fixtures`。SQLite 使用事务保存新修订、业务评估与回执；`report` 从库中重建来源摘要、案件和履约状态。模拟来源不代表真实法律、医疗或安全结论；生产接入需要对外部来源真实性和凭据管理另行评估。

## 替代场次容量持有

`slot-config` 独立登记替代场次容量，`slot-claim` 为已有预约持有票额；不足时进入 waiting。优先权益可抢占未使用的普通持有，`slot-release` 释放后依优先级提升候补。`slot_inventory` 和 `slot_claim` 保存竞争结果；预约重排读取真实持有量而非输入自称容量，因此抢占会把旧改期结论与补偿义务标失效，重算后可从改期转为补偿。正常和冲突数据见 `fixtures/slot_normal.json`、`fixtures/slot_conflict.json`。

