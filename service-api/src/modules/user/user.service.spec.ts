import { Role, UserStatus } from '../../generated/prisma/client.js';
import { describe, expect, it, vi } from 'vitest';

import { normalizeEmail, UserService } from './user.service.js';

describe('normalizeEmail', () => {
  it('trims and lowercases login identifiers', () => {
    expect(normalizeEmail('  USER@Example.COM ')).toBe('user@example.com');
  });

  it('uses the final returned item as the next cursor without dropping the extra record', async () => {
    const findMany = vi
      .fn()
      .mockResolvedValue([
        user('00000000-0000-4000-8000-000000000001'),
        user('00000000-0000-4000-8000-000000000002'),
        user('00000000-0000-4000-8000-000000000003'),
      ]);
    const service = new UserService({ client: { user: { findMany } } } as never);

    const result = await service.listUsers({ pageSize: 2 });

    expect(result.items.map((item) => item.id)).toEqual([
      '00000000-0000-4000-8000-000000000001',
      '00000000-0000-4000-8000-000000000002',
    ]);
    expect(result.page.nextCursor).toBe('00000000-0000-4000-8000-000000000002');
    expect(findMany).toHaveBeenCalledWith({ orderBy: { id: 'asc' }, take: 3 });
  });
});

function user(id: string) {
  const now = new Date('2026-07-26T00:00:00.000Z');
  return {
    id,
    email: `${id}@example.com`,
    displayName: id,
    role: Role.USER,
    status: UserStatus.ACTIVE,
    createdAt: now,
    updatedAt: now,
  };
}
