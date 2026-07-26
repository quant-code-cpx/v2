import { BadRequestException, ValidationPipe } from '@nestjs/common';
import type { Type } from '@nestjs/common';
import { describe, expect, it } from 'vitest';

import { ChangePasswordDto } from './change-password.dto.js';
import { CreateUserDto } from './create-user.dto.js';
import { ListUsersQueryDto } from './list-users-query.dto.js';
import { UpdateProfileDto } from './update-profile.dto.js';
import { UpdateUserDto } from './update-user.dto.js';

// Group HTTP-bound DTO regressions so service methods never receive blank names or nullable optional fields.
describe('user DTO validation', () => {
  // Verify account creation trims display names before rejecting values that would persist as empty strings.
  it('returns 400 for an all-whitespace create displayName', async () => {
    await expectBadRequest(
      transformDto(CreateUserDto, {
        account: 'market.user',
        displayName: '   ',
        password: 'safe-password-2026',
      }),
    );
  });

  // Verify managed-user edits reject all-whitespace input before UserService can issue an update.
  it('returns 400 for an all-whitespace update displayName', async () => {
    await expectBadRequest(transformDto(UpdateUserDto, { displayName: '\t  ' }));
  });

  // Verify self-service profile edits share the same trim-and-nonempty boundary.
  it('returns 400 for an all-whitespace self profile displayName', async () => {
    await expectBadRequest(transformDto(UpdateProfileDto, { displayName: '\n' }));
  });

  // Verify the contract-required current password cannot reach Argon2 as an empty credential.
  it('returns 400 for an empty currentPassword', async () => {
    await expectBadRequest(
      transformDto(ChangePasswordDto, {
        currentPassword: '',
        newPassword: 'safe-password-2026',
      }),
    );
  });

  // Verify optional mutation fields skip only undefined; null must not reach service defaults or Prisma.
  it('returns 400 for null optional user mutation fields', async () => {
    await Promise.all([
      expectBadRequest(transformDto(UpdateUserDto, { displayName: null })),
      expectBadRequest(transformDto(UpdateUserDto, { role: null })),
      expectBadRequest(transformDto(UpdateUserDto, { status: null })),
      expectBadRequest(
        transformDto(CreateUserDto, {
          account: 'market.user',
          displayName: 'Market User',
          password: 'safe-password-2026',
          role: null,
        }),
      ),
      expectBadRequest(
        transformDto(CreateUserDto, {
          account: 'market.user',
          displayName: 'Market User',
          password: 'safe-password-2026',
          status: null,
        }),
      ),
    ]);
  });

  // Verify required profile and optional query values reject null or blank filters before service query construction.
  it('returns 400 for null profile and null or blank query fields', async () => {
    await Promise.all([
      expectBadRequest(transformDto(UpdateProfileDto, { displayName: null })),
      expectBadRequest(transformDto(ListUsersQueryDto, { role: null })),
      expectBadRequest(transformDto(ListUsersQueryDto, { q: '   ' })),
    ]);
  });
});

/** Apply the application's exact body/query validation policy to one DTO fixture. */
async function transformDto<T>(metatype: Type<T>, value: unknown): Promise<T> {
  const pipe = new ValidationPipe({
    transform: true,
    whitelist: true,
    forbidNonWhitelisted: true,
    transformOptions: { enableImplicitConversion: false },
  });
  return pipe.transform(value, { type: 'body', metatype } as never) as Promise<T>;
}

/** Assert validation follows the public HTTP 400 boundary rather than failing later in application code. */
async function expectBadRequest(promise: Promise<unknown>): Promise<void> {
  try {
    await promise;
  } catch (error: unknown) {
    expect(error).toBeInstanceOf(BadRequestException);
    expect((error as BadRequestException).getStatus()).toBe(400);
    return;
  }
  throw new Error('Expected DTO validation to reject input');
}
