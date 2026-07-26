import { z } from 'zod';

// Parse explicit environment strings so cookie policy receives deterministic booleans.
const booleanFromEnvironment = z.enum(['true', 'false']).transform((value) => value === 'true');

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
  REFRESH_RATE_LIMIT_WINDOW_SECONDS: z.coerce.number().int().min(10).max(3_600).default(60),
  REFRESH_RATE_LIMIT_MAX: z.coerce.number().int().min(1).max(100).default(30),
  BOOTSTRAP_ADMIN_EMAIL: z.string().email().optional(),
  BOOTSTRAP_ADMIN_PASSWORD: z.string().min(12).optional(),
});

export type Environment = z.infer<typeof environmentSchema>;

/** Parse environment variables and enforce secure cross-field cookie invariants. */
export function validateEnvironment(input: Record<string, unknown>): Environment {
  const result = environmentSchema.safeParse(input);
  if (result.success) {
    // Production and cross-site cookies both require HTTPS-only transport.
    if (result.data.NODE_ENV === 'production' && !result.data.COOKIE_SECURE) {
      throw new Error('COOKIE_SECURE must be true in production');
    }

    if (result.data.COOKIE_SAME_SITE === 'none' && !result.data.COOKIE_SECURE) {
      throw new Error('COOKIE_SECURE must be true when COOKIE_SAME_SITE is none');
    }

    return result.data;
  }

  throw new Error(
    // Keep only validation messages; Zod's raw input can contain secrets.
    `Invalid service-api environment: ${result.error.issues.map((issue) => issue.message).join('; ')}`,
  );
}
