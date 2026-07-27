# 0012：板块成分观测历史与跨服务读取模型

- 状态：Proposed
- 日期：2026-07-27
- 决策者：项目维护者（待评审）
- 关联方案：[板块成分股与观测历史技术方案](../service-data-sync/0013-sector-membership-history/index.html)
- 实施依赖：[证券主数据技术方案](../service-data-sync/0014-equity-instrument-master/index.html)
- 增量契约：
  [同步服务内部接口](../contracts/0007-data-sync-sector-membership-internal.openapi.yaml)、
  [service-api 公开接口](../contracts/0008-service-api-sector-membership.openapi.yaml)
- 继承决策：[ADR-0002](0002-data-sync-ownership-and-access.md)、
  [ADR-0004](0004-market-data-provider-adapters.md)、
  [ADR-0010](0010-sector-taxonomy-and-derived-data-boundary.md)、
  [ADR-0011](0011-sector-market-data-api-boundary.md)

## 背景

东方财富行业和概念成分接口只提供调用时的当前集合，不提供真实调入、调出、生效或公告时间。
若把首次抓到日期写成调入日，或把首次缺席日期写成真实调出日，会给回测制造虚假精度。
若在超时、分页不完整、空响应、schema 漂移或证券身份未解析时按差集关闭关系，又会把来源故障
写成市场事实。

板块目录和三周期行情现由 `service-data-sync` 持有，并通过版本化内部 HTTP 提供给
`service-api`。成分历史必须沿用相同所有权和读取边界；`service-api` 不得直连同步数据库、
对象存储或 AKShare。

## 候选方案

1. 仅保存最新成分集合。
   - 优点：表和任务简单。
   - 缺点：无法做无幸存者偏差的历史筛选，也无法审计来源变更。
2. 将相邻快照差异推断为真实调入、调出日期。
   - 优点：查询形态接近业务生效历史。
   - 缺点：上游没有提供该事实；采集故障、停跑和首次观测会产生伪日期。
3. 保存完整快照、不可变发布清单和半开观测区间；只在完整健康快照上更新区间。
   - 优点：事实边界清晰，可重放、可版本化、可按观测时点查询。
   - 缺点：消费者必须理解“观测到”不等于“真实生效”，存储和发布流程更复杂。

## 决策

采用方案 3；方案 2 明确禁止。

### 时间语义

- 成分关系只有 `observed_from` 与 `observed_to`，区间语义为
  `[observed_from, observed_to)`；字段和 API 不使用 `effective_from`、`joined_at`、
  `removed_at` 等暗示真实业务生效的名称。
- `observed_from` 是首次完整健康快照中看到该关系的时间；首次抓取时存在的证券一律从首次观测开始，
  不回造此前历史。
- `observed_to` 是首个后续完整健康快照中确认缺席的观测时间，只表示“到该次观测时已未看到”，
  不表示真实调出日。
- 超时、断连、限流、空响应、分页不完整、schema 漂移、重复代码、PENDING、身份 quarantine 或越过质量阻断阈值
  的快照只保留 raw 与质量证据，不增加、关闭或重写任何观测区间。
- 历史修订采用受控重放：从最早受影响的完整快照重建后继区间并发布新 `dataVersion`；
  不原地改写已发布清单。

### 身份与发布

- 成分原始代码必须通过 [0014 证券主数据方案](../service-data-sync/0014-equity-instrument-master/index.html)
  解析到 canonical `equity_instrument`。可确定交易所但尚未被权威主数据确认的身份可保存为
  `PENDING`；无法唯一解析的记录进入 quarantine。
- `PENDING` 与 quarantine 只供同步治理和重放，均不得进入内部消费投影或 `service-api` 公开响应。
  每个可发布板块快照必须达到 100% canonical 身份解析且重复为零；未达到时沿用上一完整快照，
  首次无完整版本则不可读。已发布 release 的 `excludedIdentityCount` 恒为 0。
- 每个分类体系发布不可变 release manifest。manifest 固定每个板块所引用的完整快照，
  `dataVersion`、游标和 ETag 均绑定该 manifest，避免分页期间混入新发布。
- 新运行中失败的板块只可在阈值内显式沿用上一完整快照，并标记 `warned` 与
  `carriedForwardSectorCount`；从未有完整快照的板块不能被静默补成空集合。
- 依赖 0014 的共享 `source_batch` expand：每次抓取建立独立 batch 与 `observed_at`，
  相同 payload hash 只复用 raw 对象，不能折叠不同日期的相同成分观测。

### 服务边界

- `service-data-sync` 独占 raw evidence、快照、观测区间、release manifest、迁移和内部读取。
- `service-api` 仅扩展现有 `SectorMarketDataModule`，通过内部 HTTP 读取、校验并投影公开响应；
  不新增 Prisma 行情表，不把 Redis 作为成分权威存储。
- `0007` 是 `0005` 板块内部契约的增量扩展，`0008` 是 `0006` 公开契约的增量扩展；
  它们只增加“板块到成分”和“证券到板块”查询，不复制、不替代目录或 K 线接口。
- 内部接口继续使用专用服务 Bearer；公开接口继续使用现有用户 JWT 与默认拒绝鉴权。
  PENDING、quarantine、raw URI、内部质量样本和数据库主键不对公开调用方暴露。

## 后果

- 从首次成功采集日起可得到可复验的观测历史，但无法回答首次采集前或两次观测之间的真实调整时刻。
- 完整快照质量判断成为关闭关系的安全闸；来源故障不会静默制造大规模“移出”。
- 反向查询可在同一 release manifest 内稳定返回证券所属板块，代价是增加 manifest 和快照明细存储。
- 公开能力依赖证券主数据先达到身份覆盖门；板块成分采集可先以 shadow 模式积累 raw 与 PENDING，
  但不能绕过该依赖提前公开。
- 将来若获得带真实生效日期的权威来源，必须建立独立事实表和新 ADR；不得把它覆盖进本观测区间。

## 兼容关系

本 ADR 细化 ADR-0010 已提出的“当前成分观测”语义，并沿用 ADR-0011 的内部 HTTP 与公开投影边界。
它不修改现有板块目录、日线、周线、月线的身份、周期或 API 契约。
