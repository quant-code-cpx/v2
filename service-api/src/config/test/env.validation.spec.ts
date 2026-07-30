import { describe, expect, it } from 'vitest';

import { validateEnvironment } from '../env.validation.js';

const minimumEnvironment = {
  DATABASE_URL: 'postgresql://api:password@127.0.0.1:15433/api',
  REDIS_URL: 'redis://:password@127.0.0.1:16380',
  JWT_ACCESS_SECRET: 'a-32-character-development-secret-key',
  CAPTCHA_HMAC_SECRET: 'a-32-character-captcha-hmac-development-secret-key',
  DATA_SYNC_INTERNAL_API_BEARER_TOKEN: 'a-32-character-data-sync-internal-bearer-token',
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
    expect(environment.DATA_SYNC_INTERNAL_READ_API_BEARER_TOKEN).toBe(
      minimumEnvironment.DATA_SYNC_INTERNAL_API_BEARER_TOKEN,
    );
    expect(environment.DATA_SYNC_INTERNAL_OPERATIONS_API_BEARER_TOKEN).toBe(
      minimumEnvironment.DATA_SYNC_INTERNAL_API_BEARER_TOKEN,
    );
    expect(environment.DATA_SYNC_INTERNAL_PREFLIGHT_TIMEOUT_MS).toBe(310_000);
    expect(environment.STOCK_CONNECT_API_ENABLED).toBe(false);
    expect(environment.DATA_SYNC_STOCK_CONNECT_TIMEOUT_MS).toBe(3000);
    expect(environment.DATA_SYNC_STOCK_CONNECT_CIRCUIT_FAILURES).toBe(5);
  });

  // 验证全窗预检预算既可覆盖官方源探针，也不能失去绝对上限。
  it('bounds the dedicated data-operations preflight timeout', () => {
    expect(
      validateEnvironment({
        ...minimumEnvironment,
        DATA_SYNC_INTERNAL_PREFLIGHT_TIMEOUT_MS: '3610000',
      }).DATA_SYNC_INTERNAL_PREFLIGHT_TIMEOUT_MS,
    ).toBe(3_610_000);
    expect(() =>
      validateEnvironment({
        ...minimumEnvironment,
        DATA_SYNC_INTERNAL_PREFLIGHT_TIMEOUT_MS: '3610001',
      }),
    ).toThrow('Invalid service-api environment');
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

  // 验证沪深港通可选拆分地址和凭据允许 Compose 使用空占位。
  it('treats blank stock-connect endpoint overrides as absent', () => {
    const environment = validateEnvironment({
      ...minimumEnvironment,
      DATA_SYNC_STOCK_CONNECT_BASE_URL: '',
      DATA_SYNC_STOCK_CONNECT_API_BEARER_TOKEN: '',
    });

    expect(environment.DATA_SYNC_STOCK_CONNECT_BASE_URL).toBeUndefined();
    expect(environment.DATA_SYNC_STOCK_CONNECT_API_BEARER_TOKEN).toBeUndefined();
  });

  // 验证互联互通读取总预算和断路器参数不能绕过安全边界。
  it('rejects unsafe stock-connect reliability settings', () => {
    expect(() =>
      validateEnvironment({
        ...minimumEnvironment,
        DATA_SYNC_STOCK_CONNECT_TIMEOUT_MS: '499',
      }),
    ).toThrow('Invalid service-api environment');
    expect(() =>
      validateEnvironment({
        ...minimumEnvironment,
        DATA_SYNC_STOCK_CONNECT_CIRCUIT_FAILURES: '0',
      }),
    ).toThrow('Invalid service-api environment');
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

  // 验证生产环境不能将数据运维读写权限退回到既有通用服务身份。
  it('requires split data-operations service identities in production', () => {
    expect(() =>
      validateEnvironment({
        ...minimumEnvironment,
        NODE_ENV: 'production',
        COOKIE_SECURE: 'true',
      }),
    ).toThrow('DATA_SYNC_INTERNAL_READ_API_BEARER_TOKEN');
  });

  // 验证生产读身份不能与写身份复用同一个 secret，否则读调用可越权访问写路由。
  it('rejects equal data-operations service identities in production', () => {
    const sharedToken = 'shared-data-operations-service-token-0000000000000000001';

    expect(() =>
      validateEnvironment({
        ...minimumEnvironment,
        NODE_ENV: 'production',
        COOKIE_SECURE: 'true',
        DATA_SYNC_INTERNAL_READ_API_BEARER_TOKEN: sharedToken,
        DATA_SYNC_INTERNAL_OPERATIONS_API_BEARER_TOKEN: sharedToken,
      }),
    ).toThrow('must differ in production');
  });
});
