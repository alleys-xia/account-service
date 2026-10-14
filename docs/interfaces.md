# OMS v1 接口契约

模型定义以 `packages/oms-protocol/src/oms_protocol/` 为准。所有金额为 Decimal / 十进制字符串，拒绝 float；时间为带时区 UTC 时间。`environment / account_id / strategy_instance_id` 都是消息隔离维度，subject 的身份必须与 payload 一致。

## 消息路径

默认账户 `default`、策略 `alpha`：

| Subject | 发送者 → 接收者 | 作用 |
|---|---|---|
| `paper.v1.cmd.oms.default.alpha` | 策略 → OMS | 新单、撤单、改单，持久化 |
| `paper.v1.rpc.oms.default.alpha` | SDK → OMS | 会话、快照、分页历史、指令查询、健康检查 |
| `paper.v1.cmd.risk.default.check` | OMS → Risk | 含完整意图及版本的风控请求 |
| `paper.v1.evt.risk.default.decision` | Risk → OMS | 有期限及明确额度边界的批准／拒绝 |
| `paper.v1.cmd.exec.default.order` | OMS → Execution | 已通过风控的执行命令或撤单 |
| `paper.v1.evt.exec.default.broker` | Execution → OMS | 标准化执行回报 |
| `paper.v1.evt.oms.default.journal` | OMS → Risk／后续账户模块 | 带 account_seq 的账户事件流 |
| `paper.v1.evt.oms.default.strategy.alpha` | OMS → alpha | 带 strategy_seq 的专属回报 |

使用文件型 `OMS_paper_default` stream；SDK 仅绑定 OMS 预建的 `strategy_paper_default_alpha` durable。消费者独立，禁止以一个消费组在多个策略间分摊订单回报。账户名区分大小写，stream 名不会强制转成大写。

## 指令与版本

`command_id` 标识一次逻辑动作；同 ID / 同业务内容返回原结果，同 ID / 不同业务内容隔离为冲突。会话 token / epoch 不计入业务摘要，使进程重启后可以沿用原命令重发。已记录命令不会重新执行，即使旧命令的期限已经过去。`OMS_BUSY` 是已持久化的准入拒绝，指令查询结果标记 retryable；重发同一 ID 仍返回原拒绝。策略应退避、重新评估有效行情与风险后创建新的决策／动作 ID，不能无限自动换 ID 重试。

订单的 `revision` 是策略可见状态版本，`intent_revision` 是当前生效意图版本，`last_intent_revision` 为意图版本分配器。失败的改单尝试也不会重用旧意图版本。

限价改单接受完整的新意图，但只准改变限价、决策时间、期限和引用行情；合约、数量、方向、TIF 等变化需另行设计。风控批准并不立即覆盖原委托，收到 `REPLACED` 才更新生效意图。撤单拒绝保留原订单；收到全部成交后，迟到的 ACK / 撤成回报不会把订单改回未成交。

## 风控、执行交接

RiskRequest 包含 request_id、完整意图摘要、意图版本和必须追平的账户流水位置。RiskDecision 必须原样关联，并提供 reservation_id、明确数量／限价、valid_until 和 applied_account_seq。OMS 在生成执行命令时复核这些字段、策略租约和期限；不接受“批准了一个大概相同的订单”。

`risk_resolution: NOT_DISPATCHED` 表示该批准未生成对应执行命令，可作为未来 Risk 释放相应预占的依据。`DISPATCH_PREPARED` / `UNKNOWN` 不证明券商未受理，不能据此释放额度或重新生成新单。成交、撤单的最终额度调整由未来 Risk 消费账户流水完成。

ExecutionCommand 的稳定 `command_id` 是未来执行器的幂等键。执行器必须持久化指令与券商 ID 映射、确认合约元数据和权限、复核 SIMULATION 域／有效期限／策略 epoch，并在自身不确定时查询券商、报告 `UNKNOWN`。**消息总线的去重窗口不能提供券商下单的 exactly-once 保证。** 本版本不包含这些券商侧功能。

订单生命周期、发送确定性、撤单状态和一致性分开记录。通过风控后的 `DISPATCH_PENDING / PREPARED` 表示执行命令已写入事务；收到 ACK 才是 `SUBMITTED / ACKNOWLEDGED`。未知结果或冲突执行回报会冻结账户的新单和改单，撤单入口保留。

## 成交与费用

- 股票按股、期权按张计数；期权两腿各有独立 instrument_id。组合层汇总不再记一次成交。
- `execution_id` 使用完整原始标识，不截断 IB execId。相同原始成交 ID 的经济内容不得变化。
- 成交更正使用新的 execution_id，并显式提供 correction_of；原始记录保留，聚合时旧记录不再计入。
- 费用消息为原成交的**当前费用总额**，不是增量。Execution 持久化并递增该成交的 `fee_revision`，OMS 忽略旧费用版本、拒绝同版本不同内容。策略通过 `update_kind = FEE` 区分，数量为 0。
- source_session / ingress_seq 保留来源审计；不以跨会话序号猜测成交更正关系。

## 策略 SDK 与恢复

`OmsClient.connect/send/poll/snapshot/history/command_result/close` 为 SDK 边界。`poll` 返回 Delivery；调用者完成本地事务后才调用 `Delivery.ack()`。策略运行器已经负责这个顺序。

启动恢复先取得 OMS 快照水位，再从 PostgreSQL journal 补齐本地尚未处理的回报到该水位；随后执行 on_recover / on_start。JetStream 重复投递按 event_id、成交 ID、版本去重，不重复调用已提交回调。发现 feed_epoch 变化、序号缺口或连接丢失会停止决策并要求重新恢复，不能猜测跳过。

快照当前上限为每策略 1,000 个订单、512 KiB；超过时明确拒绝，避免返回截断状态。历史回放每页最多 500 条且限制为 512 KiB。意图上限 16 KiB。扩大规模前应实现快照分页／终态订单归档并升级契约，不得直接删除订单或流水绕过容量限制。
