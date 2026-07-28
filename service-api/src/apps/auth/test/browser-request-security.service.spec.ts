import type { AppConfigService } from '../../../config/app-config.service.js';
import { describe, expect, it } from 'vitest';

import { BrowserRequestSecurityService } from '../browser-request-security.service.js';

const configuration = { corsOrigin: 'http://127.0.0.1:15173' } as AppConfigService;

// 汇集匿名鉴权端点对 Origin、Fetch Metadata 与 JSON body 的精确边界检查。
describe('BrowserRequestSecurityService', () => {
  // 验证携带 JSON body 的同站浏览器请求满足登录边界要求。
  it('accepts exact same-site JSON request', () => {
    const service = new BrowserRequestSecurityService(configuration);

    expect(() =>
      service.assertAllowed(
        {
          origin: 'http://127.0.0.1:15173',
          fetchSite: 'same-site',
          contentType: 'application/json; charset=utf-8',
        },
        true,
      ),
    ).not.toThrow();
  });

  // 验证缺少 Origin 或跨站 Fetch Metadata 的请求不能调用匿名鉴权端点。
  it.each([
    { fetchSite: 'same-site', origin: undefined },
    { fetchSite: 'cross-site', origin: 'http://127.0.0.1:15173' },
    { fetchSite: undefined, origin: 'http://127.0.0.1:15173' },
  ])('rejects untrusted browser boundary %#', (request) => {
    const service = new BrowserRequestSecurityService(configuration);

    expect(() => service.assertAllowed(request)).toThrow(expect.objectContaining({ status: 403 }));
  });
});
