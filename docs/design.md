# OMS 首期设计

日期：2026-10-14。适用个人单账户、多策略独立进程；Python 3.11、NATS JetStream、PostgreSQL 16，本机优先。OMS 是第三个独立业务仓库；协议和客户端置于其 packages/，不新增公共库仓库。

## 目标与边界

OMS 负责订单受理、数据库幂等、状态机、风险请求、执行命令、订单／逐腿成交 journal、策略归属及恢复快照。Risk 决定风险政策和预占，Execution 连接券商并核对真实订单，Portfolio 核算账户资产。OMS 不直接连接 IB，不实现资金风控规则，不把订单记录声称为持仓账本。

本轮仅启用 paper + SIMULATION 执行域。独立模拟 Risk／Execution 仅用于临时验收，不随交付保留，不能被当作已实现的真实模块。无真实下单，trade_ready=false。真实 Risk／Execution 尚未部署时，受理订单可以停留 RISK_PENDING，绝不自动批准。

首期接受 LIMIT 整股股票、单腿期权、开仓 bear call/bull put 两腿等比例价差；同一个组合 order_id 始终携带全部 legs。修改只允许净限价变化，其余合约、方向、数量、期限不变，重新风控且原单保持工作，待执行确认后生效；更广的改单通过未来明确能力扩展。撤单请求失败不等于原订单拒绝。组合摘要只表进度，实际成交必须逐腿记录；不产生双重账本。

## 目录与状态所有权

```text
oms/
├── src/oms_service/
│   ├── domain.py          纯订单状态转换
│   ├── storage.py         PostgreSQL 事务、inbox/journal/outbox
│   ├── service.py         受理、归属、时效与会话校验
│   ├── transport.py       NATS 可靠消费、发布、查询
│   ├── config.py / app.py 配置和进程生命周期
│   └── sql/               版本化数据库 schema
├── packages/oms-protocol/ 公共订单模型和消息
├── packages/oms-client/   策略端命令发送、可靠回报、恢复查询
├── config/               无秘密配置与 NATS ACL
├── scripts/              安装、初始化和协议导出
└── docs/                 设计、运行与验收记录
```

策略现有 strategy_api.orders 保留兼容导入，类型的唯一来源迁到 oms_protocol。行情库不依赖 OMS。策略自身 SQLite 继续负责回调事务和待发送命令；OMS 只能写自己的 PostgreSQL schema。

## 消息与事务

账户固定使用环境和 account_alias，subject 包含环境、主版本、账户和策略身份。NATS ACL 与消息体身份双重核对，策略不能发布 Risk 或 Execution 回报。

- 命令：{environment}.v1.cmd.oms.{account}.{strategy}。
- 风险请求／决定：cmd.risk.{account}.check、evt.risk.{account}.decision。
- 执行请求／回报：cmd.exec.{account}.order、evt.exec.{account}.broker。
- 账户 journal／策略流：evt.oms.{account}.journal、evt.oms.{account}.strategy.{strategy}。
- 会话和只读查询：rpc.oms.{account}.{strategy}.{operation}。

所有上述主题包含环境与 v1 前缀。命令和事件使用 JetStream File + LimitsPolicy + DiscardNew，不用 WorkQueue；数据库长期去重，不依赖消息去重窗口。流不自动过期，容量满拒绝写入，积压导致关闭新受理并报警，不静默删可靠事件。共享广播使用独立持久消费者。

每个命令／回报在单个数据库事务中写入 inbox、订单状态、journal、outbox，提交后才 ack。outbox 等到 PubAck 后标为已发布；重复出站保持 message_id。不承诺跨 PostgreSQL、NATS、券商的 exactly-once。

一个数据库有一个活动 OMS，通过 session advisory lock 保持单写者；同一连接的操作串行，事务与查询受超时限制。失去数据库连接则停止进程，不在丢锁后继续发布。account_seq 和 strategy_seq 在事务中生成，feed_epoch 持久保存，重启不重置。

## 幂等、权限与会话

command_id 在账户内长期唯一，相同内容重投返回原结果，内容不同拒绝并记录隔离。client_order_id 在策略内唯一。执行 execution_id 保留提供方完整标识，完全重复不重复计数，不同内容或无法关联的回报隔离并冻结账户。

策略获取有期限的 OMS 会话；同一 run 重试获取原会话，另一个活跃 run 被拒绝。租约过期后可由新 run 获取更高 epoch；新命令必须携带有效 token。命令业务摘要排除传输会话 token，使恢复后的原命令重发仍具有相同业务身份。审批到达时再次检查原会话、意图摘要、revision 和 deadline；失效不能进入执行队列。会话租约不替代未来 Execution 券商会话 fencing。

## 状态与风险批准

独立记录 lifecycle、dispatch_certainty、cancel_state、consistency，以及订单视图 revision 与 intent_revision。RECEIVED/RISK_PENDING/SUBMITTED/PARTIALLY_FILLED/FILLED/CANCELLED/REJECTED 与请求失败分开。

RiskDecision 必须关联 risk_request_id、order_id、intent_revision、intent_hash、required_account_seq、reservation_id、批准边界和有效期。OMS 只对完全匹配且尚有效的批准生成一次执行命令；重复决定不重复下发。批准到执行之间由 Execution 再校验有效期；未发单、未知和已取消必须作为明确事实回报，预占释放由 Risk 消费 journal 决定，OMS 不自行修改额度。

创建执行 outbox 后记录 PREPARED；未收到执行确认时不得假定订单不存在。UNKNOWN 会冻结相关订单／账户的新风险动作，只保留核对和撤单路径。重投执行命令保留同一 command_id；真实 Execution 未来必须具备持久化映射与券商对账能力，不能把 NATS 重投直接变成新的 placeOrder。

价格修改期间仍可能成交或撤单。延迟状态不能覆盖已经确认的终态；成交先于提交状态也可入账。每腿累计成交不得超过原数量，价差只有所有腿数量一致且完成时才 FILLED；腿不齐标记 LEGS_PENDING。更正必须明确 correction_of，保留原事件并按新版本重新计算；费用更新只改费用，不增加成交。不可识别或矛盾回报进入隔离，不通过猜测修改账本。

## 策略恢复

策略端获取 OMS 快照和连续策略水位；恢复 journal 到快照水位时禁止生成新订单。实时回报在本地回调事务成功后确认；断线重投靠本地 inbox 去重。缺口从 OMS 数据库 journal 补齐，超过有界分页容量时继续分页，不跳过。Portfolio 尚未实现，持仓快照明确 UNAVAILABLE。

停止实例只释放会话／订阅，不自动撤销已工作订单。OMS 重启恢复数据库状态并继续发布原 outbox；不会创建第二个券商订单身份。

## 验证与交付

验证将覆盖重复命令、不同载荷冲突、错账户／策略、过期会话、迟到审批、撤单竞争、修改被拒、乱序状态、重复逐腿成交、更正、快照水位、服务重启和两个策略隔离。实际依赖用专用测试数据库和临时 NATS；模拟 Risk／Execution 独立进程。不会关闭用户 OpenD 或访问交易 API。

沿用用户清理要求：测试与临时策略仅用于验证，完成后删除源码和临时产物；本地保存可恢复记录。Docker、真实券商与资金账本留给后续模块。GitHub 地址尚未指定，本轮在本机独立 Git 仓库完成。

依据：NATS [JetStream 语义](https://docs.nats.io/concepts/jetstream)、PostgreSQL [锁与事务](https://www.postgresql.org/docs/16/explicit-locking.html)。
