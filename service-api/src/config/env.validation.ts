import { z } from 'zod';

// 解析显式环境字符串，确保 Cookie 策略得到确定布尔值。
const booleanFromEnvironment = z.enum(['true', 'false']).transform((value) => value === 'true');
const ACCOUNT_PATTERN = /^[a-z0-9][a-z0-9._-]{4,31}$/;

/** 将空白环境变量视为未配置，避免 Compose 空占位覆盖安全回退。 */
function blankEnvironmentValueToUndefined(value: unknown): unknown {
  return typeof value === 'string' && value.trim() === '' ? undefined : value;
}

/** 构造可由空字符串安全省略的环境变量 schema。 */
const optionalEnvironmentValue = <Schema extends z.ZodType>(schema: Schema) =>
  z.preprocess(blankEnvironmentValueToUndefined, schema.optional());

const environmentSchema = z.object({
  NODE_ENV: z.enum(['development', 'test', 'production']).default('development'),
  PORT: z.coerce.number().int().min(1).max(65_535).default(30_00),
  API_PREFIX: z.string().trim().min(1).default('api/v1'),
  DATABASE_URL: z.string().url(),
  REDIS_URL: z.string().url(),
  REDIS_KEY_PREFIX: z.string().trim().min(1).default('quant-v2:api'),
  JWT_ISSUER: z.string().trim().min(1).default('quant-v2'),
  JWT_AUDIENCE: z.string().trim().min(1).default('quant-v2-web'),
  JWT_ACCESS_SECRET: z.string().min(32),
  JWT_ACCESS_TTL_SECONDS: z.coerce.number().int().min(60).max(3_600).default(900),
  REFRESH_TOKEN_TTL_SECONDS: z.coerce
    .number()
    .int()
    .min(3_600)
    .max(60 * 60 * 24 * 90)
    .default(60 * 60 * 24 * 30),
  COOKIE_SAME_SITE: z.enum(['lax', 'strict', 'none']).default('lax'),
  COOKIE_SECURE: booleanFromEnvironment.default(false),
  CORS_ORIGIN: z.string().url().default('http://127.0.0.1:15173'),
  TRUST_PROXY: booleanFromEnvironment.default(false),
  LOGIN_FAILURE_WINDOW_SECONDS: z.coerce.number().int().min(60).max(86_400).default(900),
  LOGIN_LOCK_SECONDS: z.coerce.number().int().min(60).max(86_400).default(900),
  LOGIN_MAX_FAILURES: z.coerce.number().int().min(1).max(20).default(5),
  CAPTCHA_TTL_SECONDS: z.coerce.number().int().min(30).max(300).default(120),
  CAPTCHA_RATE_LIMIT_WINDOW_SECONDS: z.coerce.number().int().min(10).max(3_600).default(60),
  CAPTCHA_RATE_LIMIT_MAX: z.coerce.number().int().min(1).max(100).default(20),
  CAPTCHA_HMAC_SECRET: z.string().min(32),
  REFRESH_RATE_LIMIT_WINDOW_SECONDS: z.coerce.number().int().min(10).max(3_600).default(60),
  REFRESH_RATE_LIMIT_MAX: z.coerce.number().int().min(1).max(100).default(30),
  REFRESH_RACE_GRACE_SECONDS: z.coerce.number().int().min(1).max(30).default(5),
  DATA_SYNC_INTERNAL_BASE_URL: z.string().url().default('http://127.0.0.1:8000'),
  // 既有内部 API 仍使用此服务身份；数据运维 0022 路由改用下方最小权限凭据。
  DATA_SYNC_INTERNAL_API_BEARER_TOKEN: z.string().min(32),
  DATA_SYNC_INTERNAL_READ_API_BEARER_TOKEN: z.string().min(32).optional(),
  DATA_SYNC_INTERNAL_OPERATIONS_API_BEARER_TOKEN: z.string().min(32).optional(),
  DATA_SYNC_INTERNAL_REQUEST_TIMEOUT_MS: z.coerce
    .number()
    .int()
    .min(100)
    .max(30_000)
    .default(5_000),
  DATA_SYNC_INTERNAL_PREFLIGHT_TIMEOUT_MS: z.coerce
    .number()
    .int()
    .min(1_000)
    .max(3_610_000)
    .default(310_000),
  STOCK_CONNECT_API_ENABLED: booleanFromEnvironment.default(false),
  DATA_SYNC_STOCK_CONNECT_BASE_URL: optionalEnvironmentValue(z.string().url()),
  DATA_SYNC_STOCK_CONNECT_API_BEARER_TOKEN: optionalEnvironmentValue(z.string().min(32)),
  DATA_SYNC_STOCK_CONNECT_TIMEOUT_MS: z.coerce.number().int().min(500).max(10_000).default(3_000),
  DATA_SYNC_STOCK_CONNECT_CIRCUIT_FAILURES: z.coerce.number().int().min(1).max(20).default(5),
  DATA_SYNC_STOCK_CONNECT_CIRCUIT_WINDOW_MS: z.coerce
    .number()
    .int()
    .min(1_000)
    .max(300_000)
    .default(30_000),
  DATA_SYNC_STOCK_CONNECT_CIRCUIT_OPEN_MS: z.coerce
    .number()
    .int()
    .min(1_000)
    .max(300_000)
    .default(30_000),
  BOOTSTRAP_ADMIN_ACCOUNT: optionalEnvironmentValue(
    z.string().trim().toLowerCase().regex(ACCOUNT_PATTERN),
  ),
  BOOTSTRAP_ADMIN_PASSWORD: optionalEnvironmentValue(z.string().min(12).regex(/\d/)),
});

/** 表示完成本地回退或生产 split 校验后的运行环境。 */
export type Environment = Omit<
  z.infer<typeof environmentSchema>,
  'DATA_SYNC_INTERNAL_READ_API_BEARER_TOKEN' | 'DATA_SYNC_INTERNAL_OPERATIONS_API_BEARER_TOKEN'
> & {
  DATA_SYNC_INTERNAL_READ_API_BEARER_TOKEN: string;
  DATA_SYNC_INTERNAL_OPERATIONS_API_BEARER_TOKEN: string;
};

/** 解析环境变量，并强制执行 Cookie 与下游服务身份的跨字段安全约束。 */
export function validateEnvironment(input: Record<string, unknown>): Environment {
  const result = environmentSchema.safeParse(input);
  if (result.success) {
    // 生产环境和跨站 Cookie 均必须限制为 HTTPS 传输。
    if (result.data.NODE_ENV === 'production' && !result.data.COOKIE_SECURE) {
      throw new Error('COOKIE_SECURE must be true in production');
    }

    if (result.data.COOKIE_SAME_SITE === 'none' && !result.data.COOKIE_SECURE) {
      throw new Error('COOKIE_SECURE must be true when COOKIE_SAME_SITE is none');
    }

    const readToken = result.data.DATA_SYNC_INTERNAL_READ_API_BEARER_TOKEN;
    const operationsToken = result.data.DATA_SYNC_INTERNAL_OPERATIONS_API_BEARER_TOKEN;
    if (result.data.NODE_ENV === 'production' && (!readToken || !operationsToken)) {
      throw new Error(
        'DATA_SYNC_INTERNAL_READ_API_BEARER_TOKEN and DATA_SYNC_INTERNAL_OPERATIONS_API_BEARER_TOKEN are required in production',
      );
    }
    if (
      result.data.NODE_ENV === 'production' &&
      readToken !== undefined &&
      operationsToken !== undefined &&
      readToken === operationsToken
    ) {
      throw new Error(
        'DATA_SYNC_INTERNAL_READ_API_BEARER_TOKEN and DATA_SYNC_INTERNAL_OPERATIONS_API_BEARER_TOKEN must differ in production',
      );
    }

    // 开发与测试仅为平滑本地升级回退到既有服务身份；生产环境绝不复用旧 token。
    return {
      ...result.data,
      DATA_SYNC_INTERNAL_READ_API_BEARER_TOKEN:
        readToken ?? result.data.DATA_SYNC_INTERNAL_API_BEARER_TOKEN,
      DATA_SYNC_INTERNAL_OPERATIONS_API_BEARER_TOKEN:
        operationsToken ?? result.data.DATA_SYNC_INTERNAL_API_BEARER_TOKEN,
    };
  }

  throw new Error(
    // 仅保留校验信息；Zod 原始输入可能包含密钥。
    `Invalid service-api environment: ${result.error.issues.map((issue) => issue.message).join('; ')}`,
  );
}
