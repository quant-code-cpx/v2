# 数据同步服务技术方案

服务实现目录：[service-data-sync/](../../service-data-sync/)。

本目录记录数据源、市场范围、交易日历、时区、数据质量、幂等、重试与恢复方案。

## 方案

- [0001：同步服务工程基础设施](0001-data-sync-foundation/index.html) — Implemented（基础设施范围）
- [0002：同步数据总纲与 AKShare 分批调研计划](0002-data-catalog-and-akshare-research/index.html) — Draft（B00/R2 已完成；R3 待验收）
- [0003：AKShare B01 主数据与参考数据首轮调研](0003-akshare-b01-initial-research/index.html) — Draft（第二轮短探针完成；R3 待验收）
- [0004：AKShare B02 核心日频行情首轮调研](0004-akshare-b02-initial-research/index.html) — Draft（第二轮短探针完成；R3 待验收）
- [0005：AKShare B03 市场规则与因子首轮调研](0005-akshare-b03-initial-research/index.html) — Draft（第二轮短探针完成；R3 待验收）
- [0006：AKShare B04 财务核心首轮调研](0006-akshare-b04-initial-research/index.html) — Draft（第二轮短探针完成；R3 待验收）
- [0007：AKShare B05 股东与公司行动首轮调研](0007-akshare-b05-initial-research/index.html) — Draft（第二轮短探针完成；R3 待验收）
- [0008：AKShare B06 资金流与特殊交易首轮调研](0008-akshare-b06-initial-research/index.html) — Draft（第二轮短探针完成；R3 待验收）
- [0009：AKShare B07 基金、宏观与机构调研首轮调研](0009-akshare-b07-initial-research/index.html) — Draft（第二轮短探针完成；R3 待验收）
- [0010：AKShare 第二轮独立验证汇总](0010-akshare-round2-verification/index.html) — Draft（R2 完成；无能力获 FULL 准入）
- [0011：个股数据同步、存储与查询 API 技术方案](0011-equity-data-sync-and-api/index.html) — Implemented（日/周/月独立直采、复权因子、公司行动、公司概况、建表、同步调度、内部 API 与公开 POST API 已打通）
- [0012：行业与板块数据同步技术方案](0012-industry-and-sector-data-sync/index.html) — Implemented（东方财富 P0 与申万三级 taxonomy、父级闭包、估值、方法学、同步、迁移和双层 API 已实现；资金流完整归属 0017）
- [0013：板块成分股与观测历史技术方案](0013-sector-membership-history/index.html) — Implemented
- [0014：A 股证券主数据与上市生命周期技术方案](0014-equity-instrument-master/index.html) — Implemented（Contract C1、日期感知身份解析、交易所生命周期 adapter、双时间修订、恢复和契约已实现）
- [0015：板块 EOD 横截面快照与排行技术方案](0015-sector-eod-snapshot-ranking/index.html) — Implemented（技术链路完成；运行参数待连续样本校准）
- [0016：财务报表与估值技术方案](0016-financial-statements-valuation/index.html) — Implemented（AKShare 东财三表、报告期指标、历史估值、平台派生指标、raw evidence、双时态 revision、publication 与双层 API 已实现）
- [0017：日频资金流向技术方案](0017-daily-money-flow/index.html) — Implemented（固定版本 AKShare 技术验证、方法学、同步、迁移和五条双层 API 已实现；未通过来源门禁的方法学保持 research 并 fail-closed）
- [0018：同步服务 SQLAlchemy ORM 全量迁移技术方案](0018-sqlalchemy-orm-persistence-models/index.html) — Implemented
- [0019：指数成分与权重数据接入技术方案](0019-index-constituents/index.html) — Proposed
- [0020：ETF 市场数据接入技术方案](0020-etf-market-data/index.html) — Proposed
- [0021：融资融券数据接入技术方案](0021-margin-trading/index.html) — Proposed
- [0022：沪深港通数据接入技术方案](0022-stock-connect/index.html) — Proposed
- [0023：上市公司主营构成数据接入技术方案](0023-business-composition/index.html) — Proposed
- [0024：公司事件数据接入技术方案](0024-corporate-events/index.html) — Proposed
- [0025：龙虎榜与大宗交易数据接入技术方案](0025-trading-events/index.html) — Proposed
- [0026：期货与期权独立资产域技术方案](0026-derivatives/index.html) — Proposed
- [0027：高价值市场数据 Canonical Model 与 PostgreSQL 设计](0027-canonical-data-model/index.html) — Proposed
- [0028：data-sync 市场数据访问契约方案](0028-data-access-contract/index.html) — Proposed
- [0029：高价值市场数据扩展路线图](0029-market-data-expansion-roadmap/index.html) — Proposed
- [0030：同步来源载荷仅失败留存方案](0030-failure-only-source-payload-retention/index.html) — Implemented
- [0031：数据运维控制面与全局串行同步方案](0031-data-operations-control-plane/index.html) — Proposed

从 0011 开始，方案状态与实施完成度只由技术证据决定：AKShare 接口签名和返回事实、连续探针与 fixture、
同步幂等和恢复、Declarative 模型与 migration、数据质量、内部读取以及 `service-api` 的 POST 公开契约。
组织流程或其他非技术事项不进入技术方案完成条件，也不阻塞 adapter、同步任务、建表和 API 链路实施。

0012、0014、0016、0017 的技术链路已经落地。0014 的 Contract C1 使所有证券消费者按业务日期解析不可变身份，
历史代码复用会安全阻断；0017 复用该身份、运行账本与独立 `source_batch` 观测契约。
每个 capability 仅在其接口稳定性、schema、单位、方法学、完整性、容量与恢复测试通过后发布。

0018 已完成全部现有逻辑表和业务仓储的 ORM 迁移，不改变上述业务能力优先级或数据语义。
未来新增逻辑表必须继续采用一表一 Declarative 模型、显式 registry、短生命周期 `Session`、显式 Alembic migration
与 PostgreSQL schema parity；物理日期分区继续由 migration 或专用 partition manager 管理。
