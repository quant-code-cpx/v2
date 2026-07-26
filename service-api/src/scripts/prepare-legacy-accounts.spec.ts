import { describe, expect, it } from 'vitest';

import { assertMappingCoverage, parseLegacyAccountMappings } from './prepare-legacy-accounts.js';

const firstUserId = '00000000-0000-4000-8000-000000000001';
const secondUserId = '00000000-0000-4000-8000-000000000002';

// Group mapping-file checks that make legacy account migration explicit and non-inferential.
describe('legacy account mapping preparation', () => {
  // Verify account values are caller-specified and normalized without consulting legacy email fields.
  it('parses explicit id-to-account mappings and normalizes account casing', () => {
    expect(
      parseLegacyAccountMappings(
        JSON.stringify([{ userId: firstUserId, account: '  Market.Admin  ' }]),
      ),
    ).toEqual([{ userId: firstUserId, account: 'market.admin' }]);
  });

  // Verify an email key cannot be smuggled into the input shape or used as a migration identity source.
  it('rejects mapping entries containing email fields', () => {
    expect(() =>
      parseLegacyAccountMappings(
        JSON.stringify([
          { userId: firstUserId, account: 'market.admin', email: 'legacy@example.test' },
        ]),
      ),
    ).toThrow('only userId and account');
  });

  // Verify every persisted legacy user is mapped exactly once before DDL can become non-nullable.
  it('requires complete and exact mapping coverage', () => {
    const mappings = parseLegacyAccountMappings(
      JSON.stringify([{ userId: firstUserId, account: 'market.admin' }]),
    );
    expect(() => assertMappingCoverage(mappings, [firstUserId, secondUserId])).toThrow(
      'exactly one account',
    );
    expect(() => assertMappingCoverage(mappings, [secondUserId])).toThrow(
      'absent from the database',
    );
  });
});
