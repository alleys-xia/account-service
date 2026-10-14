# 账户进程运行与迁移

## 仓库迁移

原 `oms/` 重命名为 `account-service/`，保留其 Git 历史；Risk 源码移入 `src/risk_service/`。原 Risk 完整工作目录和迁移前 Git bundle 保存在父目录 `.migration-backups/2026-10-14-account-service/`，属于本机恢复备份，不上传。Portfolio 直接加入同仓库，不再单建 repo。

原 OMS/Risk 数据库没有搬迁或删除，原私有 `.env` 没有覆盖。风险配置和数据库仍须选择同一账户、环境、模式；不能把 `SIMULATION` 数据作为 `IB_PAPER` 使用。原工作目录中的启动路径、旧 venv 脚本须切换到新目录并重新 bootstrap。Execution、Strategies 的安装脚本已改为从 `account-service/packages/` 构建依赖。

共享协议 Python 导入名保持 `oms_protocol`、`oms_client`；消费者不需要导入服务代码。Portfolio 增加 `portfolio_client`，仍属于账户仓库。这里没有新建共享 Git 仓库。

已有 IB_PAPER NATS 数据目录可能位于旧 `execution/data/nats-paper/`。升级时先停止原进程，为新的 NATS 配置创建私有 `config/local.nats.conf` 并将 `store_dir` 指向原目录的绝对路径（或离线完整迁移后保持同一数据），不要带着原业务数据库启动一个空的 JetStream。原 `default / SIMULATION` 与 `paperib / IB_PAPER` 也不得混用存储。

## 配置与顺序

首次照 README 安装并创建模块独立数据库。统一入口检查账户／环境／执行模式、NATS 地址、策略名单、Risk/Portfolio 合约版本与乘数一致；每个 Store 再校验数据库身份和单写锁。生产化时给三个数据库配置不同 PostgreSQL 角色；本机使用同一个管理员角色不构成访问隔离。

启动顺序：PostgreSQL → NATS → account-runtime → Execution → 策略。Portfolio 在 Execution 尚未上线时返回来源不可用；Risk 的 IB_PAPER 示例因此拒绝新风险，待新鲜对账匹配后恢复审批资格。不会因为账户进程已启动就宣布可以交易。

- `config/nats.paper.conf` 是 IB_PAPER ACL 的唯一维护源，账户别名 `paperib`，默认端口 4223。
- `config/nats.dev.conf` 是共用网关的 SIMULATION 模板，使用网关的 `MD_*` 凭据，账户别名 `default`。加载网关和本仓库私有 `.env`；开发模式不内置模拟执行器或虚构券商快照。
- `portfolio.*.toml` 的 `catalog_file` 读取同一份 Risk 合约目录；Execution 仍独立核验 instrument_id、版本、conId、乘数和合约资格。
- 三模块数据库 DSN 可由 `OMS_DATABASE_URL`、`RISK_DATABASE_URL`、`PORTFOLIO_DATABASE_URL` 覆盖。切换配置前检查终端中旧环境变量，环境变量优先于 TOML。
- 新策略同步登记 OMS/Portfolio 策略名单、Risk 限额、NATS 账户服务 ACL；行情服务还需自身 ACL。策略只获自己的 Portfolio subject 权限。

## 快照与故障

Portfolio 每秒追平 OMS 账户 journal；默认每 30 秒通过 Execution 请求券商快照，90 秒过期。完整快照带 `feed_epoch`、`applied_account_seq`、来源会话和采集时间。策略轮询默认每秒，把变化送入串行回调；回调可能合并多个成交，必须使用快照替换视图，不能把它当作逐笔成交再次累加。

账户 RPC 是 `paper.v1.rpc.portfolio.<account>.account`，只给 Risk/operator；策略 RPC 为 `paper.v1.rpc.portfolio.<account>.strategy.<strategy>`，身份来自被 ACL 授权的 subject。普通策略无法查询账户资金、其他策略或原始券商仓位。

`safe_for_new_risk` 要求 OMS 回放新鲜、无冻结、最新稳定快照数量匹配；IB_PAPER Risk 再检查请求所需的账户水位。`trade_ready=false` 保留为当前非实盘边界。差异状态包括 `UNAVAILABLE`、`SOURCE_UNAVAILABLE`、`STALE`、`PENDING`、`UNMAPPED`、`EXTERNAL_OR_UNEXPLAINED`、`BLOCKED`。

已有 IB 仓位记外部／未分配。没有经过人工核对的归属迁移接口，不可通过修改数据库消除差异来恢复交易。资金摘要只是券商读数；尚无完整出入金、公司行动、行权指派账本。新增 Portfolio 不自动启用平仓或裸卖，不自动把已成交额度转换成持仓保证金。

SIGTERM / Ctrl-C 关闭三个模块连接；数据库或关键后台任务不可恢复故障导致整个账户进程非零退出。退出不撤销券商现存委托，也不是跨进程原子的全局 kill switch。Execution 在 OMS 授权不可用时拒绝新的授权请求；已经发送到券商的订单仍需按执行模块恢复流程核对。重启先读原库并补事件，不能清空库规避冻结。

同进程中的 CPU 密集规则会阻塞共享事件循环，插件应保持短时；需要重计算时另行隔离。Docker、长时间运行、生产容量和灾备演练尚未验收。
