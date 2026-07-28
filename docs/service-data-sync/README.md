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
- [0011：个股数据同步、存储与查询 API 技术方案](0011-equity-data-sync-and-api/index.html) — Proposed
- [0012：行业与板块数据同步技术方案](0012-industry-and-sector-data-sync/index.html) — Proposed（明确排除分时数据）
- [0013：板块成分股与观测历史技术方案](0013-sector-membership-history/index.html) — Proposed
- [0014：A 股证券主数据与上市生命周期技术方案](0014-equity-instrument-master/index.html) — Proposed
- [0015：板块 EOD 横截面快照与排行技术方案](0015-sector-eod-snapshot-ranking/index.html) — Accepted（核心实现已验证；生产准入待决）
- [0016：财务报表与估值技术方案](0016-financial-statements-valuation/index.html) — Proposed
- [0017：日频资金流向技术方案](0017-daily-money-flow/index.html) — Proposed（明确排除分钟与分时）
- [0018：同步服务 SQLAlchemy ORM 全量迁移技术方案](0018-sqlalchemy-orm-persistence-models/index.html) — Accepted（阶段 0–2 实施中）

0013–0017 按业务价值优先级排列。实施依赖顺序不同：先完成 0014 的证券身份与共享
`source_batch` expand，再实施 0013/0015，随后实施依赖证券身份的 0016/0017；各来源通过生产准入门后
才能打开生产同步与公开发布。

0018 是持久化实现重构方案，不改变上述业务能力优先级、数据语义或生产准入状态；评审通过后按切片覆盖
全部现有同步表与仓储。
