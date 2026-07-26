import { describe, expect, it } from 'vitest';

import { validateEnvironment } from './env.validation.js';

const minimumEnvironment = {
  DATABASE_URL: 'postgresql://api:password@127.0.0.1:15433/api',
  REDIS_URL: 'redis://:password@127.0.0.1:16380',
  JWT_ACCESS_SECRET: 'a-32-character-development-secret-key',
  CAPTCHA_HMAC_SECRET: 'a-32-character-captcha-hmac-development-secret-key',
  DATA_SYNC_INTERNAL_BEARER_TOKEN: 'a-32-character-data-sync-internal-bearer-token',
};

// 按默认值和跨字段 Cookie 安全策略组织回归断言。
describe('validateEnvironment', () => {
  // 验证开发默认值保持明确且稳定。
  it('applies safe development defaults', () => {
    const environment = validateEnvironment(minimumEnvironment);

    expect(environment.PORT).toBe(3000);
    expect(environment.API_PREFIX).toBe('api/v1');
    expect(environment.COOKIE_SECURE).toBe(false);
    expect(environment.REDIS_KEY_PREFIX).toBe('quant-v2:api');
    expect(environment.CAPTCHA_TTL_SECONDS).toBe(120);
  });

  // 验证初始化身份只接受自定义账号，绝不接受邮箱形式登录名。
  it('normalizes explicit bootstrap account and rejects email-form bootstrap input', () => {
    expect(
      validateEnvironment({ ...minimumEnvironment, BOOTSTRAP_ADMIN_ACCOUNT: '  APEX.ADMIN  ' })
        .BOOTSTRAP_ADMIN_ACCOUNT,
    ).toBe('apex.admin');
    expect(() =>
      validateEnvironment({ ...minimumEnvironment, BOOTSTRAP_ADMIN_ACCOUNT: 'admin@example.test' }),
    ).toThrow('Invalid service-api environment');
  });

  // 验证 Compose 的空初始化变量不会阻断正常迁移执行。
  it('treats blank bootstrap environment values as absent', () => {
    const environment = validateEnvironment({
      ...minimumEnvironment,
      BOOTSTRAP_ADMIN_ACCOUNT: '',
      BOOTSTRAP_ADMIN_PASSWORD: '',
    });

    expect(environment.BOOTSTRAP_ADMIN_ACCOUNT).toBeUndefined();
    expect(environment.BOOTSTRAP_ADMIN_PASSWORD).toBeUndefined();
  });

  // 验证生产环境不能通过不安全传输发送刷新 Cookie。
  it('rejects insecure production cookies', () => {
    expect(() => validateEnvironment({ ...minimumEnvironment, NODE_ENV: 'production' })).toThrow(
      'COOKIE_SECURE must be true in production',
    );
  });

  // 验证跨站 Cookie 在任何环境都必须使用安全传输。
  it('requires secure cookies for SameSite=None', () => {
    expect(() => validateEnvironment({ ...minimumEnvironment, COOKIE_SAME_SITE: 'none' })).toThrow(
      'COOKIE_SECURE must be true when COOKIE_SAME_SITE is none',
    );
  });
});
