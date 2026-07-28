# 0017：同步服务采用 SQLAlchemy Declarative ORM 作为持久化模型

- 状态：Implemented
- 日期：2026-07-27
- 接受日期：2026-07-28
- 决策者：项目维护者
- 关联方案：[同步服务 SQLAlchemy ORM 全量迁移技术方案](../service-data-sync/0018-sqlalchemy-orm-persistence-models/index.html)
- 细化决策：[ADR-0003](0003-data-sync-runtime-stack.md)

## 背景

`service-data-sync` 在本决策启动时已采用 SQLAlchemy 2 与 Alembic，但持久化实现只有 `Engine`、`Connection`
和 `text()` SQL。当时 32 张逻辑业务表分散在 6 个历史迁移中，应用持久化层约 6,733 行，包含约
152 个 `text()` 调用。维护者无法像阅读 `service-api` 的 Prisma schema 一样，从一个稳定目录直接看到：

- 当前有哪些逻辑表；
- 每张表包含哪些字段、数据库类型、可空性和业务含义；
- 主键、外键、唯一约束、CHECK、EXCLUDE、索引和分区策略；
- 哪些表属于同一业务能力。

这已经影响产品决策。表结构可读性是本决策的第一优先级，高于减少模型文件、避免一次性迁移成本或继续保留
手写 SQL 的局部便利。

现有 PostgreSQL 结构还依赖部分唯一索引、覆盖索引、`TSTZRANGE`、生成列、`EXCLUDE USING gist`、
`JSONB`、父分区表、运行时月分区、`ON CONFLICT`、`RETURNING`、行锁和 advisory lock。迁移方案必须保留
这些语义，不能为了 ORM 化弱化数据约束、幂等、修订历史、质量门或事务原子性。

2026-07-27 事实校准：0015 板块 EOD 方案已进入 Implemented。上述迁移启动统计已经包含其 4 张逻辑表和 1,703 行
`sector_eod_repository.py`；动态排行、raw replay、candidate/publish、lease/reaper、fencing、受控回滚与
原子 publication 已成为回归契约。0015 尚待完成的来源许可、
连续探针和生产观测准入属于生产启用条件，不阻断 ORM 模型建设，也不能作为 ORM 行为验证的替代品。

2026-07-28 增量校准：0016 财务 schema expand 通过 revision `202607280002` 新增 12 张逻辑表，
且从创建时即采用一表一 Declarative 模型、显式 registry 和 Alembic/schema parity。当前 registry 共登记
44 张逻辑表；最初 32 表与后续 12 表受同一 ORM 约束。财务真实来源同步、canonical 发布和成功读取尚未完成，
不因表模型已落地而改变业务方案状态。

## 候选方案

1. 继续使用手写 SQL，只补数据字典或 ER 图。
   - 优点：不改运行时代码，实施成本最低。
   - 缺点：结构与说明形成第二份事实来源；无法从持久化代码直接导航全部表；不满足维护者明确提出的
     “模型可读性优先”目标。
2. 只建立 SQLAlchemy Core `Table` metadata，仓储继续使用 `Connection`。
   - 优点：能集中表达字段与约束；迁移成本低于完整 ORM。
   - 缺点：仍缺少按表组织的类型化实体和 `Session` 边界；仓储继续大量依赖字符串 SQL；只部分解决问题。
3. 全量建立 SQLAlchemy 2 Declarative ORM 模型，并将全部仓储数据访问迁移到 `Session` 与
   ORM-enabled SQL expressions。
   - 优点：每张逻辑表形成可导航、可类型检查的模型；模型 metadata 可成为 Alembic 未来迁移目标；
     支持 PostgreSQL 批量 DML、upsert、RETURNING 和锁；可用架构测试禁止结构再次散落。
   - 缺点：迁移范围大；Alembic autogenerate 对 CHECK、EXCLUDE、重命名和物理分区并不完整；
     需要保留少量 PostgreSQL DDL 例外并增加 schema parity 测试。
4. 引入 Django ORM、SQLModel 或另一套 ORM。
   - 优点：部分方案提供更高层封装。
   - 缺点：增加第二套持久化框架或应用框架；现有 SQLAlchemy/Alembic、连接池和测试资产无法直接复用；
     对复杂 PostgreSQL 结构没有决定性优势。

## 决策

采用方案 3。项目维护者于 2026-07-28 确认以“模型可读性优先”为约束，开始全量 ORM 分阶段迁移。

### 实施状态（2026-07-28）

- 当前 44 张逻辑表均有一表一文件模型，metadata 已接入 Alembic；数据库 COMMENT migration 与财务 schema
  migration 已在独立 PostgreSQL
  完成升级、回滚、再升级与零意外差异校验。
- `DatabaseClient` 提供短生命周期 `Session` 与显式事务；publication、目录、行情、主数据、membership、EOD、
  source batch、identity resolver 和财务只读选择器均使用模型表达式。
- `infrastructure/persistence/` 已无 `text()` 调用，架构测试持续约束该边界；仅 Alembic migration 与专用
  partition manager 保留受控 PostgreSQL DDL。
- 首批 32 表收口时已重建测试镜像并通过 Ruff、Pyright、203 项非集成测试；独立 PostgreSQL/Redis/MinIO 项目
  完成迁移、8 项集成测试、schema parity 与 `alembic check` 验证。后续 12 张财务表的迁移与 parity 验证记录
  归属 0016；AKShare 外部 smoke 按默认策略跳过。

### 模型可读性规则

- 使用 SQLAlchemy 2 `DeclarativeBase`、`Mapped[...]` 与 `mapped_column()`。
- 每张逻辑业务表一个模型文件；模型按 provenance、execution、publication、equity、financial、sector catalog、
  sector market data、sector membership 和 sector EOD 分目录。
- 每个模型文件完整声明表名、字段、数据库类型、可空性、主键、外键、默认值、表级约束、索引、分区策略、
  中文表注释和中文字段注释。
- 即使 SQLAlchemy 能从类型注解推断，也显式书写数据库类型和 `nullable`，优先保证阅读者一眼可见。
- `models/registry.py` 显式列出全部模型；禁止运行时扫描目录或依赖隐式 import 副作用。
- `alembic_version`、PostgreSQL extension 和物理分区子表不是独立业务实体，不建立 ORM class；
  它们在父表模型与专用 partition manager 中明确列出。

### ORM 使用边界

- 当前 44 张逻辑表全部建立 Declarative class；关联表、质量表和运行账本也不省略。
- 仓储使用短生命周期同步 `Session` 和显式事务；不使用全局 Session，不跨线程、请求或 Celery task 共享。
- 简单读写可使用 ORM entity；批量行情、快照成员和质量结果使用
  `Session.execute(insert(Model), rows)` 等 ORM-enabled bulk DML，禁止逐行 `add()` 导致吞吐退化。
- PostgreSQL upsert 使用 dialect `insert(...).on_conflict_do_update()`；
  `RETURNING`、`with_for_update()`、CTE、范围条件和 advisory lock 使用 SQLAlchemy expression。
- 默认不建立可写 `relationship()` cascade，也不允许隐式 lazy loading。跨表查询继续显式 join；
  确有只读导航价值时，单独评审 `viewonly=True` relationship。
- 最终状态下，`infrastructure/persistence/` 禁止直接 `text()`；仅 Alembic migration 和专用
  partition manager 可保留受测试约束的 PostgreSQL DDL。

### Schema 与迁移边界

- 已发布历史 migration 不重写；它们继续负责从空库或旧版本升级。
- Declarative metadata 表达当前目标 schema，并接入 Alembic `target_metadata`。
- 首次接入必须在真实 PostgreSQL 上达到“历史 migration 升级到 head 后，metadata 零意外差异”。
- Alembic autogenerate 只生成候选 revision，必须人工检查；CHECK、EXCLUDE、重命名、extension 和物理分区
  必须使用显式 migration 与专用 schema parity 测试。
- 2026-07-28 实测发现：`ix_equity_name_current_prefix` 的 `lower(name) text_pattern_ops` 带 PostgreSQL
  操作符类，Alembic 自动比较会跳过并假定相等；在切换相关仓储前必须补 `pg_catalog` 断言，不能把
  `alembic check` 的通过视为该索引定义已被完整验证。
- 模型中的 table/column `comment` 是字段含义权威；首次只新增数据库 COMMENT 的 migration 不改业务行。
- 现有匿名或数据库自动命名约束先按实际名称映射，不在 ORM 切换中顺手重命名；新约束使用统一
  naming convention。

## 后果

### 正面

- `models/` 成为维护者查看全部表、字段、类型、含义、约束和索引的直接入口。
- 一表一文件与按能力分组同时成立；新增表必须进入显式 registry。
- Alembic 从 `target_metadata = None` 转为可检查的目标 schema，CI 可执行 `alembic check`。
- 仓储查询从字符串 SQL 转为可引用模型字段的表达式，重命名和类型变化更容易被静态检查发现。
- table/column comment 可同步进入 PostgreSQL，数据库工具也能显示字段含义。

### 成本与风险

- EOD 和 membership 仓储包含锁、upsert、发布切换和分区 DDL；已分阶段迁移并保留既有事务边界，后续修改仍须遵守
  无字符串 SQL 架构门。
- EOD 生产开关仍保持关闭；ORM 回归不依赖生产流量，已通过 0015 fixture、仓储集成、迁移、raw replay、排行、
  租约恢复、rollback 与故障路径测试验证。
- SQLAlchemy model 与历史 migration 可能不一致；需要独立 schema parity 集成测试，不能只依赖
  Alembic autogenerate。
- ORM identity map、autoflush、lazy loading 和 cascade 若使用不当，会造成额外查询或意外写入；
  本决策通过显式事务、禁 lazy、禁默认 cascade 和 bulk DML 约束风险。
- 月分区子表不能合理地一表一 class；它们是物理存储切片，不是产品数据模型。父表文件必须显示分区键与策略，
  partition manager 必须显式、受限且可测试。
- 模型注释会增加维护工作；任何新增表或字段若没有中文含义、registry 条目和 parity 测试，CI 必须失败。

## 替代关系

本 ADR 细化 ADR-0003 已选定的 SQLAlchemy 2 + Alembic 技术栈，不替代 provider、数据所有权、时间语义、
发布版本或跨服务读取边界。后续持久化变更必须继续以 Declarative 模型和 Session 表达式实现。

## 官方依据

- [SQLAlchemy 2 Declarative Mapping](https://docs.sqlalchemy.org/en/20/orm/declarative_styles.html)
- [SQLAlchemy PostgreSQL dialect](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html)
- [SQLAlchemy ORM-enabled DML](https://docs.sqlalchemy.org/en/20/orm/queryguide/dml.html)
- [Alembic autogenerate 及限制](https://alembic.sqlalchemy.org/en/latest/autogenerate.html)
