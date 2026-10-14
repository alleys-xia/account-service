# 运行与恢复

## 单机进程

OMS 使用 PostgreSQL 会话级 advisory lock 保证一个数据库只有一个活动写入者。数据库身份（schema_version、environment、account_id）及 feed_epoch 持久化。数据库连接不可用或后台关键任务失败时进程退出；不自动换连接继续发送。每个账户使用独立数据库和 stream。

PostgreSQL 的 inbox、订单聚合、journal、outbox 在一个事务中更新。消息只有在提交后才确认。outbox 收到 JetStream PubAck 后才标记 published；失去确认时沿用原消息 ID 重发。服务故障之后可重启同一配置，禁止通过清空库或换 feed_epoch 来“恢复”。

NATS 使用文件持久化、LimitsPolicy、DiscardNew、无自动过期，默认容量 128 MiB。达到上限时拒绝新写入，OMS outbox 保留未完成发送并重试；新单／改单会在 outbox 压力达到阈值后延迟处理，仍允许处理撤单和回报。该取舍避免自动丢弃未处理订单，但需要监控容量；**当前未提供在线归档／安全截断工具**。提高容量前先停止 OMS、用管理身份扩容 stream 并同步配置，然后重启。不能删除 stream、consumer 或 PostgreSQL 历史来释放空间。

现有 stream / consumer 的关键配置不匹配时拒绝启动，不静默改写。单节点开发配置不提供主机故障级高可用；Linux Docker 阶段需要持久卷、备份、资源和进程重启策略。

## 会话与健康

一个 strategy_instance_id 同时只有一个运行会话。默认租约 30 秒，SDK 周期续期；正常退出主动释放。进程崩溃后，新实例需等待旧租约到期；启动报 STRATEGY_ALREADY_ACTIVE 时按租约窗口退避重启，不要复用旧 token。

行情不可用时不准新增风险动作；已启动且 OMS 会话有效的策略仍能在合法串行回调中请求撤单。停止／初始化／恢复期间不产生新动作。订单端连接丢失需要重启实例，恢复 journal 后才重新开放决策。

`trade_ready=false` 是本版本固定边界。策略状态中的 `oms_ready` 表示连接及租约可用，`simulation_ready` 表示本地模拟域可提交决策，不代表真实券商可交易或风控批准。订单是否进入执行端，以回报为准。Positions / Portfolio 接口仍返回 UNAVAILABLE。

通过 OMS SDK 的 `rpc("health")` 可检查 blocked、pending_outbox、feed_epoch。策略状态可使用原有 `strategy-runner status` 查看。以下只读 SQL 用于本机排查：

```sql
SELECT key, value FROM oms.meta WHERE key IN ('blocked', 'account_seq', 'feed_epoch');
SELECT count(*) FROM oms.outbox WHERE NOT published;
SELECT strategy, count(*) FROM oms.orders GROUP BY strategy;
SELECT channel, message_id, reason, recorded_at FROM oms.quarantine ORDER BY id DESC LIMIT 20;
```

发生 UNKNOWN、原始成交 ID 冲突或非法回报时，保留原始数据并冻结账户新增风险动作。本版本没有自动清除冻结按钮；真实执行接入前需补齐券商对账和有审计记录的恢复流程。不要手动把 blocked 改空来继续下单。

## 配置与升级

密钥放在进程环境变量，`.env` / 本地配置均不进 Git。开发 NATS 配置绑定 localhost；跨机器访问需加 TLS、网络控制和独立凭据。

订单数据库、策略 SQLite 和 JetStream 数据必须备份并一致恢复。单独回退一个组件可能产生 feed 水位冲突，此时停止而非跳过。协议当前 schema_version=1，未来升级以迁移脚本和兼容版本发布；禁止运行未知 schema 的程序。

Risk / Execution 接入前还必须完成：合约资格与乘数、券商原始成交/费用转换、券商 ID 持久化、预占与账户对账、执行 fencing、kill switch、完整的到期/行权/指派与股票持仓事件。OMS v0.1 不接管这些职责。
