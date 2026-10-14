# Risk 运行与扩展

Risk 已并入 `account-service`，通过 `account-runtime` 与 OMS、Portfolio 同进程启动；配置仍由 Risk 自己管理，数据库独立。模块状态和数据边界见 [账户说明](../operations.md)。

`config/risk.example.toml` 为本机 SIMULATION 参数；`config/risk.paper.toml` 为 IB_PAPER 参数并启用 `require_portfolio=true`。真实环境阈值需由账户所有者配置，示例值不代表资金与保证金已验证。配置 hash 记录于审批审计；不得删除 Risk 数据库来清除已成交风险。

规则分为账户级和策略级，`extra_rules` 可加载已安装的规则类。插件仅作账户服务内部模块扩展，不形成安全沙箱；不要在规则中执行阻塞 IO 或长期计算。风险审批与预占先落库，再发布结果。

启用 Portfolio 门控时，审批要求 Portfolio 账户／模式／feed_epoch 匹配、账户事件水位足够、快照新鲜且持仓数量匹配。未分配仓位、差异或来源不可用将拒绝新增风险。所有平仓强制执行该门控。平仓按原开仓批次原子预占数量，成交后按剩余持仓释放配置预算；不是真实保证金计算。详见 [平仓设计](../close-risk-design.md)。
