import { z } from 'zod';

// 解析显式环境字符串，确保 Cookie 策略得到确定布尔值。
const booleanFromEnvironment = z.enum(['true', 'false']).transform((value) => value === 'true');
const ACCOUNT_PATTERN = /^[a-z0-9][a-z0-9._-]{4,31}$/;
const optionalBootstrapValue = <Schema extends z.ZodType>(schema: Schema) =>
  z.preprocess(
    (value) => (typeof value === 'string' && value.trim() === '' ? undefined : value),
    schema.optional(),
  );

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
  DATA_SYNC_INTERNAL_BEARER_TOKEN: z.string().min(32),
  DATA_SYNC_INTERNAL_REQUEST_TIMEOUT_MS: z.coerce
    .number()
    .int()
    .min(100)
    .max(30_000)
    .default(5_000),
  BOOTSTRAP_ADMIN_ACCOUNT: optionalBootstrapValue(
    z.string().trim().toLowerCase().regex(ACCOUNT_PATTERN),
  ),
  BOOTSTRAP_ADMIN_PASSWORD: optionalBootstrapValue(z.string().min(12).regex(/\d/)),
});

export type Environment = z.infer<typeof environmentSchema>;

/** Parse environment variables and enforce secure cross-field cookie invariants. */
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

    return result.data;
  }

  throw new Error(
    // 仅保留校验信息；Zod 原始输入可能包含密钥。
    `Invalid service-api environment: ${result.error.issues.map((issue) => issue.message).join('; ')}`,
  );
}
