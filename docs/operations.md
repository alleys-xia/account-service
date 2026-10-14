# 账户模块边界与状态

OMS、Risk、Portfolio 同仓库同进程，独立持有数据库、事务与消息身份。账户、模式、NATS 地址、策略集合及合约版本必须一致；配置文件和自动化脚本继续保留，后续由助手负责目标服务器部署。

## 快照与故障

Portfolio 每秒追平 OMS 账户 journal；默认每 30 秒通过 Execution 请求券商快照，90 秒过期。完整快照带 `feed_epoch`、`applied_account_seq`、来源会话和采集时间。策略轮询默认每秒，把变化送入串行回调；回调可能合并多个成交，必须使用快照替换视图，不能把它当作逐笔成交再次累加。

账户 RPC 是 `paper.v1.rpc.portfolio.<account>.account`，只给 Risk/operator；策略 RPC 为 `paper.v1.rpc.portfolio.<account>.strategy.<strategy>`，身份来自被 ACL 授权的 subject。普通策略无法查询账户资金、其他策略或原始券商仓位。

`safe_for_new_risk` 要求 OMS 回放新鲜、无冻结、最新稳定快照数量匹配；IB_PAPER Risk 再检查请求所需的账户水位。`trade_ready=false` 保留为当前非实盘边界。差异状态包括 `UNAVAILABLE`、`SOURCE_UNAVAILABLE`、`STALE`、`PENDING`、`UNMAPPED`、`EXTERNAL_OR_UNEXPLAINED`、`BLOCKED`。

已有 IB 仓位记外部／未分配。没有经过人工核对的归属迁移接口，不可通过修改数据库消除差异来恢复交易。资金摘要只是券商读数；尚无完整出入金、公司行动、行权指派账本。新增 Portfolio 不自动启用平仓或裸卖，不自动把已成交额度转换成持仓保证金。

SIGTERM / Ctrl-C 关闭三个模块连接；数据库或关键后台任务不可恢复故障导致整个账户进程非零退出。退出不撤销券商现存委托，也不是跨进程原子的全局 kill switch。Execution 在 OMS 授权不可用时拒绝新的授权请求；已经发送到券商的订单仍需按执行模块恢复流程核对。重启先读原库并补事件，不能清空库规避冻结。

同进程中的 CPU 密集规则会阻塞共享事件循环，插件应保持短时；需要重计算时另行隔离。Docker、长时间运行、生产容量和灾备演练尚未验收。
