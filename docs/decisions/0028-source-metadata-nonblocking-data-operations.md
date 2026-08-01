# ADR-0028：首批 A 股 equity 来源审计元数据不阻断控制面

- 状态：Accepted
- 日期：2026-08-01
- 关联决策：[ADR-0024](0024-data-operations-control-plane.md)、[ADR-0013](0013-equity-instrument-identity-lifecycle.md)
- 关联契约：[数据运维内部 API](../contracts/0022-data-sync-operations-internal.openapi.yaml)

## 背景

本 ADR 仅覆盖首批 A 股 `equity.master.cn-a`、`equity.lifecycle.explicit`、`equity.profile`、
`equity.bar.1d.raw`、`equity.bar.1w.raw`、`equity.bar.1mo.raw`、`equity.adjustment_factor` 与
`equity.corporate_action` 八条链路。它们的 `providerId`、`upstreamSource`、`adapterId`、
`methodologyVersion`、`approvalStatus`、rights/license 引用及来源标签属于可追溯的来源元数据；描述一次执行
使用的来源和映射，但不描述请求是否可执行、载荷是否可解析、canonical 写入是否原子或 publication 是否技术正确。

将这些 equity 元数据用作 `preflight`、command、dispatcher、fenced executor、checkpoint、publication 或跨服务
E2E 的拒绝条件，会把同一份技术输入按非技术分类产生不同的执行结果，并掩盖真正的 provider、schema、质量或持久化故障。

## 候选方案

1. 以首批 A 股 equity 的来源审批、rights/license 或来源归属状态决定命令是否可执行。
   - 缺点：同一网络响应、schema、质量和事务结果会因非技术标签不同而改变；无法把控制面失败归因到可复现的技术条件。
2. 将首批 A 股 equity 的来源元数据冻结到 source batch、run、publication 和 API lineage，但仅以技术证据决定执行与发布。
   - 优点：保留审计链；命令结果由可测的 request、provider 响应、schema、质量、fence、事务、coverage 和 publication 证据决定。

## 决策

采用方案 2。

1. 对上述八条 `equity.*` target，`preflight`、command 受理、dispatcher、fenced executor、canonical persist、
   checkpoint 和 publication 不得以来源负责人、`approvalStatus`、rights/license 或来源归属为拒绝、跳过、暂停或验收失败条件。
2. 这些 target 的 `SourceBinding`、`source_batch`、run snapshot 和公开 lineage 继续保存可用来源元数据；
   它们只能解释事实来源，不能改变同一技术输入的执行路径或 `dataVersion`。
3. 这些 target 的技术门只包括请求合同、功能开关与可用 adapter、凭据传输、网络响应、超时、schema/语义、
   质量规则、幂等、fencing、数据库事务、coverage 和 publication 一致性。上游实际返回的认证协议失败属于
   provider 获取故障；静态来源分类本身不是该故障的替代品。
4. `equity-production-source-not-approved` 一类仅由上述来源元数据触发的拒绝码与本决策冲突。它在实现保留期间
   应记录为首批 A 股 equity 控制面合同偏差，不能作为该范围内技术执行或 E2E 的阻断结论。

## 后果

- 首批 A 股 equity 的控制面和跨服务验收可在相同技术输入下获得确定结果；真实 provider、normalize、persist、
  publish、internal API 与 service-api 证据仍必须逐项验证。
- 该范围的来源、adapter、方法学和 rights/license 引用保留在审计与 lineage 投影中，不泄漏凭据、原始供应商响应或内部存储位置。
- 需要以契约和回归测试证明该范围内不同来源元数据状态不会改变 command 受理、checkpoint 推进或 publication 内容。

## 替代关系

本 ADR 细化 ADR-0024 的控制面语义，并在首批 A 股 equity 来源元数据参与受理或 publication 判断的范围内
约束 ADR-0013；其他数据集、来源路由、质量和原子发布决定保持不变。
