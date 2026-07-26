import { describe, expect, it } from 'vitest';

import { validateEnvironment } from './env.validation.js';

const minimumEnvironment = {
  DATABASE_URL: 'postgresql://api:password@127.0.0.1:15433/api',
  REDIS_URL: 'redis://:password@127.0.0.1:16380',
  JWT_ACCESS_SECRET: 'a-32-character-development-secret-key',
};

describe('validateEnvironment', () => {
  it('applies safe development defaults', () => {
    const environment = validateEnvironment(minimumEnvironment);

    expect(environment.PORT).toBe(3000);
    expect(environment.API_PREFIX).toBe('api/v1');
    expect(environment.COOKIE_SECURE).toBe(false);
    expect(environment.REDIS_KEY_PREFIX).toBe('quant-v2:api');
  });

  it('rejects insecure production cookies', () => {
    expect(() => validateEnvironment({ ...minimumEnvironment, NODE_ENV: 'production' })).toThrow(
      'COOKIE_SECURE must be true in production',
    );
  });

  it('requires secure cookies for SameSite=None', () => {
    expect(() => validateEnvironment({ ...minimumEnvironment, COOKIE_SAME_SITE: 'none' })).toThrow(
      'COOKIE_SECURE must be true when COOKIE_SAME_SITE is none',
    );
  });
});
