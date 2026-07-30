/** 定义公开合同允许持久化的数据运维主动操作。 */
export const DATA_OPERATION_ACTIONS = [
  'SYNC_SUBMIT',
  'SYNC_CANCEL',
  'SYNC_RETRY',
  'HEALTH_CHECK_SUBMIT',
  'SCHEDULE_UPSERT',
  'SCHEDULE_SET_ENABLED',
] as const;

/** 表示一类可持久化的数据运维主动操作。 */
export type DataOperationAction = (typeof DATA_OPERATION_ACTIONS)[number];

/** 表示公开投影使用的非权威交付状态。 */
export type DeliveryStatus = 'PENDING' | 'DELIVERING' | 'ACCEPTED' | 'REJECTED' | 'DEAD_LETTER';

/** 表示操作动作而非资源本身的最终或中间结论。 */
export type OperationResult =
  | 'UNKNOWN'
  | 'QUEUED'
  | 'RUNNING'
  | 'CANCEL_REQUESTED'
  | 'SUCCEEDED'
  | 'PARTIAL'
  | 'FAILED'
  | 'CANCELLED'
  | 'INTERRUPTED'
  | 'SKIPPED'
  | 'REJECTED';

/** 表示可公开跳转到 data-sync 权威资源的最小引用。 */
export type AuthorityResource = {
  resourceType: 'COMMAND' | 'RUN' | 'HEALTH_CHECK' | 'SCHEDULE';
  resourceId: string;
};

/** 表示经 API 投影后允许 Web 显示的操作者。 */
export type ActorDisplay = {
  actorType: 'USER' | 'SYSTEM';
  systemKind: 'SCHEDULE' | 'LEGACY' | 'RECOVERY' | 'OTHER' | null;
  actorId: string | null;
  displayName: string;
  deleted: boolean;
};

/** 表示可跨服务保存和返回的脱敏错误摘要。 */
export type SafeError = {
  code: string;
  stage:
    | 'PREFLIGHT'
    | 'QUEUE'
    | 'DELIVERY'
    | 'PROVIDER_FETCH'
    | 'NORMALIZE'
    | 'QUALITY_GATE'
    | 'HEALTH_EVALUATION'
    | 'PERSIST'
    | 'PUBLISH'
    | 'CHECKPOINT'
    | 'SCHEDULE'
    | 'CANCEL'
    | 'RECOVERY';
  retryable: boolean;
  message: string;
};

/** 表示公开写操作返回的本地 Submission 收据。 */
export type SubmissionReceipt = {
  submissionId: string;
  action: DataOperationAction;
  deliveryStatus: DeliveryStatus;
  operationResult: OperationResult;
  authorityResource: AuthorityResource | null;
  queuePosition: number | null;
  authorizedAt: string;
  updatedAt: string;
  requestId: string;
  error: SafeError | null;
};

/** 将主动操作映射到唯一允许的 data-sync 内部 mutation 路径。 */
export const DATA_OPERATION_INTERNAL_PATH: Readonly<Record<DataOperationAction, string>> = {
  SYNC_SUBMIT: '/internal/v1/data-operations/commands/submit',
  SYNC_CANCEL: '/internal/v1/data-operations/commands/cancel',
  SYNC_RETRY: '/internal/v1/data-operations/commands/retry',
  HEALTH_CHECK_SUBMIT: '/internal/v1/data-operations/health/checks/submit',
  SCHEDULE_UPSERT: '/internal/v1/data-operations/schedules/upsert',
  SCHEDULE_SET_ENABLED: '/internal/v1/data-operations/schedules/set-enabled',
};

/** 将未知值安全收窄为键值对象，不把数组当成合同对象。 */
export function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

/** 从合同对象读取必填字符串字段，缺失时返回 null 供调用方判定合同漂移。 */
export function requiredString(record: Record<string, unknown>, key: string): string | null {
  const value = record[key];
  return typeof value === 'string' && value.length > 0 ? value : null;
}

/** 从合同对象读取可空字符串字段，非字符串值一律视为合同漂移。 */
export function nullableString(
  record: Record<string, unknown>,
  key: string,
): string | null | undefined {
  const value = record[key];
  return value === null ? null : typeof value === 'string' ? value : undefined;
}

/** 从合同对象读取可空整数字段，避免把布尔值或浮点数当队列位置。 */
export function nullableInteger(
  record: Record<string, unknown>,
  key: string,
): number | null | undefined {
  const value = record[key];
  return value === null
    ? null
    : typeof value === 'number' && Number.isSafeInteger(value)
      ? value
      : undefined;
}

/** 从合同对象读取数组字段，缺失或非数组时返回 null。 */
export function requiredArray(record: Record<string, unknown>, key: string): unknown[] | null {
  const value = record[key];
  return Array.isArray(value) ? value : null;
}

/** 对 JSON 值递归排序后序列化，用于稳定公开幂等请求摘要。 */
export function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.map((item) => canonicalJson(item)).join(',')}]`;
  }
  const record = asRecord(value);
  if (record) {
    return `{${Object.keys(record)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`)
      .join(',')}}`;
  }
  return JSON.stringify(value);
}

/** 将 Date 转换为合同要求的 RFC 3339 字符串。 */
export function iso(value: Date): string {
  return value.toISOString();
}
