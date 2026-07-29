# 0022：空观测是可发布状态，不是跨服务阻断

- 状态：Accepted
- 日期：2026-07-29
- 决策者：项目维护者
- 所有者：`service-data-sync`
- 消费者：`service-api`、`service-web`

## 背景

现有同步链路把 AKShare 返回空集、首个 publication 尚未建立和 Provider 暂时不可用混同为
`schema` 或 `dependency-unavailable`。这会令 `service-api` 返回 `503`，即使用户只需要得到空数据。
数据字段覆盖不完整也因此被错误地当作推进同步、数据库和 API 链路的前置条件。

个人自用场景允许字段或整段数据暂缺；缺失必须可观察，但不能阻塞其他 capability 或消费者读取。

## 候选方案

| 方案 | 结果 | 结论 |
|---|---|---|
| 在事实表插入全空行 | 破坏业务主键、单位和时间语义 | 不采用 |
| 无 publication 时一律 `503` | 将“没有数据”误报为系统故障 | 不采用 |
| 记录空观测，读取返回成功空结果 | 保持 canonical 事实准确，同时允许链路继续 | **采用** |

## 决策

1. 对每个 capability 与请求分区，区分 `AVAILABLE`、`EMPTY`、`SOURCE_UNAVAILABLE`、`FAILED` 四种结果。
2. `EMPTY` 表示来源请求成功、载荷合法，但没有匹配事实；写入空观测/空发布，不在 canonical 事实表伪造
   含 `NULL` 业务字段的行。
3. `SOURCE_UNAVAILABLE` 表示 AKShare 未配置、网络失败、限流或上游暂不可用。它写入诊断性观测；若已有
   可用 publication，保留该 publication，不用“空”覆盖历史事实；若尚无 publication，读取接口仍返回
   `200`、空 `records/items` 与该状态。
4. 解析、身份、质量、迁移和数据库事务失败仍是 `FAILED`。仅此类失败在有来源字节时遵循 ADR-0021 留存
   raw/normalized 失败证据；空观测和来源不可用不得写 AKShare 原始载荷。
5. 内部与公开读取合同将 `availability` 作为显式元数据。调用方不必把空状态当异常：列表返回空数组，
   可选详情字段返回 `null`；前端默认显示为空。`availability` 只用于状态提示和诊断，不能据此编造数值。
6. 这项语义适用于方案 0019–0029 中所有 capability。先以个股日线验证全链路，再按 capability 复制；
   AKShare 字段覆盖或来源确认是数据质量跟进项，不是实现链路的阻断条件。

## 后果

- canonical 表继续保存理想、强约束的事实模型；空状态进入独立 observation/publication 元数据。
- 首次使用任何已注册 capability 都可以完成“同步尝试 → 数据库状态 → data-sync → service-api”闭环。
- `503` 只保留给 data-sync 自身不可达、合同损坏或无法安全执行读取等真实服务故障；不能再用来表示
  AKShare 没有某条数据。
- API 需要更新 runtime schema、条件缓存和测试，以接受没有 `dataVersion` 的成功空结果。

## 替代关系

本决策收紧 ADR-0020 中“发布不存在返回 `404`、依赖不可用返回 `503`”的读取语义：对已注册且授权的
dataset/capability，缺少首个可用发布改为成功空结果；未知 dataset、权限拒绝和服务自身不可用不改变。

