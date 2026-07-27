/* eslint-disable @typescript-eslint/no-unsafe-assignment -- Vitest asymmetric matchers intentionally return `any`. */

import type { AppConfigService } from '../../../platform/config/app-config.service.js';
import type { RedisService } from '../../../platform/redis/redis.service.js';
import { describe, expect, it } from 'vitest';

import {
  CaptchaService,
  type CaptchaCodeGenerator,
  type CaptchaClientContext,
} from '../captcha.service.js';
import type { SecurityRateLimitService } from '../security-rate-limit.service.js';

const configuration = {
  captchaHmacSecret: 'test-only-captcha-hmac-secret-long-enough-for-hmac-use',
  captchaTtlSeconds: 120,
} as AppConfigService;

const clientA: CaptchaClientContext = { ip: '127.0.0.1', userAgent: 'fixture-browser' };
const clientB: CaptchaClientContext = { ip: '127.0.0.2', userAgent: 'fixture-browser' };

// 汇集测试专用验证码 fixture 下的单次使用、跨上下文与依赖失败关闭测试。
describe('CaptchaService', () => {
  // 验证公开挑战响应只包含 PNG 挑战数据，不暴露测试答案状态。
  it('issues PNG challenge without exposing answer and consumes correct answer once', async () => {
    const redis = new FakeRedis();
    const service = serviceWith(redis);

    const challenge = await service.createChallenge(clientA);

    expect(challenge).toMatchObject({
      imageDataUrl: expect.stringMatching(/^data:image\/png;base64,/),
    });
    expect(challenge).not.toHaveProperty('answer');
    await expect(service.verifyAndConsume(challenge.challengeId, '2468', clientA)).resolves.toBe(
      true,
    );
    await expect(service.verifyAndConsume(challenge.challengeId, '2468', clientA)).resolves.toBe(
      false,
    );
  });

  // 验证错误答案形成终态，不能对同一挑战 ID 重试。
  it('consumes an incorrect answer before returning failure', async () => {
    const service = serviceWith(new FakeRedis());
    const challenge = await service.createChallenge(clientA);

    await expect(service.verifyAndConsume(challenge.challengeId, '1111', clientA)).resolves.toBe(
      false,
    );
    await expect(service.verifyAndConsume(challenge.challengeId, '2468', clientA)).resolves.toBe(
      false,
    );
  });

  // 验证绑定 IP 的挑战不能由其他客户端上下文解答。
  it('rejects cross-IP CAPTCHA use and consumes the challenge', async () => {
    const service = serviceWith(new FakeRedis());
    const challenge = await service.createChallenge(clientA);

    await expect(service.verifyAndConsume(challenge.challengeId, '2468', clientB)).resolves.toBe(
      false,
    );
    await expect(service.verifyAndConsume(challenge.challengeId, '2468', clientA)).resolves.toBe(
      false,
    );
  });

  // 验证安全状态依赖不可用时阻止签发，而不是创建可绕过的挑战。
  it('fails closed with 503 when Redis cannot persist CAPTCHA state', async () => {
    const service = serviceWith(new ThrowingRedis());

    await expect(service.createChallenge(clientA)).rejects.toMatchObject({ status: 503 });
  });
});

/** 使用与 Redis Lua 脚本相同的单次比较语义保存短期测试挑战摘要。 */
class FakeRedis {
  private readonly values = new Map<string, string>();

  /** 在不透明挑战键下保存 fixture 摘要。 */
  public set(key: string, value: string): Promise<void> {
    this.values.set(key, value);
    return Promise.resolve();
  }

  /** 比较候选值前原子删除 fixture 摘要。 */
  public consumeMatchingValue(key: string, expectedValue: string): Promise<boolean> {
    const value = this.values.get(key);
    this.values.delete(key);
    return Promise.resolve(value === expectedValue);
  }
}

/** 仅为验证码失败关闭回归测试模拟 Redis 不可用。 */
class ThrowingRedis {
  /** 拒绝写入，要求服务返回依赖不可用而非可使用的挑战。 */
  public set(): Promise<void> {
    return Promise.reject(new Error('Redis unavailable'));
  }

  /** 保留接口形状，供未来测试流程进入验证阶段时使用。 */
  public consumeMatchingValue(): Promise<boolean> {
    return Promise.reject(new Error('Redis unavailable'));
  }
}

/** 仅通过单元测试直接注入返回 fixture 安全的静态测试码，禁止进入生产配置。 */
class FixtureCaptchaCodeGenerator implements CaptchaCodeGenerator {
  /** 生成本测试文件使用的已知答案，禁止成为 HTTP/API 响应字段。 */
  public generate(): string {
    return '2468';
  }
}

/** 使用测试专用生成器与始终允许本地 fixture 的签发限流器构造验证码服务。 */
function serviceWith(redis: FakeRedis | ThrowingRedis): CaptchaService {
  const rateLimit = {
    assertCaptchaIssueAllowed: (): Promise<void> => Promise.resolve(),
  } as unknown as SecurityRateLimitService;
  return new CaptchaService(
    redis as unknown as RedisService,
    rateLimit,
    configuration,
    new FixtureCaptchaCodeGenerator(),
  );
}
