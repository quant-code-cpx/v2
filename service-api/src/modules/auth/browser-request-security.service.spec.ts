import type { AppConfigService } from '../../platform/config/app-config.service.js';
import { describe, expect, it } from 'vitest';

import { BrowserRequestSecurityService } from './browser-request-security.service.js';

const configuration = { corsOrigin: 'http://127.0.0.1:15173' } as AppConfigService;

// Group exact Origin, Fetch Metadata, and JSON body boundary checks for anonymous auth endpoints.
describe('BrowserRequestSecurityService', () => {
  // Verify same-site browser requests with JSON body meet login boundary requirements.
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

  // Verify missing Origin and cross-site Fetch Metadata cannot call anonymous authentication endpoints.
  it.each([
    { fetchSite: 'same-site', origin: undefined },
    { fetchSite: 'cross-site', origin: 'http://127.0.0.1:15173' },
    { fetchSite: undefined, origin: 'http://127.0.0.1:15173' },
  ])('rejects untrusted browser boundary %#', (request) => {
    const service = new BrowserRequestSecurityService(configuration);

    expect(() => service.assertAllowed(request)).toThrow(expect.objectContaining({ status: 403 }));
  });
});
