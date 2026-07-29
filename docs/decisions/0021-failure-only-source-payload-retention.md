# 0021：同步来源载荷仅在失败时留存

- 状态：Accepted
- 日期：2026-07-29
- 决策者：项目维护者
- 所有者：`service-data-sync`

## 背景

`service-data-sync` 过去会在每个成功批次把 AKShare 原始响应和 adapter 标准化载荷同时写入 S3，
而 PostgreSQL 的 canonical 表又保存相同业务事实。这会形成接近两份的长期数据成本。个人自用场景
不需要成功批次的离线 raw replay，但仍需要在 schema 漂移、校验或数据库发布失败时保留足以排障的字节。

## 候选方案

| 方案 | 存储成本 | 失败排障 | 成功历史 replay | 结论 |
|---|---:|---|---|---|
| 继续全部归档 | 高 | 完整 | 支持 | 不采用 |
| 只保留摘要和 URI | 低 | 无原始响应 | 不支持 | 不采用 |
| 仅失败归档 | 低 | 保留失败批次的 raw 与标准化载荷 | 新成功批次不支持 | **采用** |

## 决策

1. 成功同步只写 canonical 数据、摘要、来源身份和 `unretained://sha256/<digest>` 留证标记；不再向 S3
   写 AKShare 原始或标准化对象。
2. 适配器返回 `ProviderBatch` 后立即仅在内存暂存 raw 与标准化字节。因此即使解码在应用层归档步骤前
   失败，仍可留证。
3. 同步异常时才把暂存字节写到私有 S3 的 `failures/YYYY/MM/DD/<failure-id>/`，并写入不含异常原文的
   `manifest.json`；异常本身继续向上抛出。
4. 成功标记不可作为 replay 输入。旧成功归档在清理前仍可按旧机制读取；新成功批次的 raw replay 明确失败。
5. `source_batch.raw_uri`、`raw_payload_manifest.object_uri`、checkpoint 的 `raw_uri`/`normalized_uri` 和
   lineage 外键保留为摘要或失败证据引用，不存放 AKShare 大字段，也不需要数据库迁移。

## 后果

- 成功批次不再产生 S3 原始/标准化副本，长期对象存储主要只承担失败排障。
- 新成功批次失去确定性 raw replay；回填改为重新抓取可用来源，历史已归档数据可在清理前完成必要 replay。
- 失败证据仍可能较大，应通过对象存储生命周期策略设置保留期；本决策不自动删除失败目录。
- 旧成功归档可安全删除 `raw/` 与 `normalized/` 前缀；不得删除 `failures/`，删除前应先做清单和备份确认。

## 替代关系

本决策收紧 [0004：数据源适配器](./0004-market-data-provider-adapters.md) 中成功批次长期 raw 归档的实现策略，
不改变 canonical 数据所有权或 provider-neutral 边界。
