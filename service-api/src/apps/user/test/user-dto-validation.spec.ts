import { BadRequestException, ValidationPipe } from '@nestjs/common';
import type { Type } from '@nestjs/common';
import { describe, expect, it } from 'vitest';

import { ChangePasswordDto } from '../dto/change-password.dto.js';
import { CreateUserDto } from '../dto/create-user.dto.js';
import { ListUsersQueryDto } from '../dto/list-users-query.dto.js';
import { UpdateProfileDto } from '../dto/update-profile.dto.js';
import { UpdateUserDto } from '../dto/update-user.dto.js';

// 汇集 HTTP 边界 DTO 回归测试，避免服务方法收到空名称或可空的可选字段。
describe('user DTO validation', () => {
  // 验证创建账号先修剪显示名，再拒绝会以空字符串持久化的值。
  it('returns 400 for an all-whitespace create displayName', async () => {
    await expectBadRequest(
      transformDto(CreateUserDto, {
        account: 'market.user',
        displayName: '   ',
        password: 'safe-password-2026',
      }),
    );
  });

  // 验证管理用户编辑在 UserService 更新前拒绝全空白输入。
  it('returns 400 for an all-whitespace update displayName', async () => {
    await expectBadRequest(transformDto(UpdateUserDto, { displayName: '\t  ' }));
  });

  // 验证自助资料编辑复用相同的修剪与非空边界。
  it('returns 400 for an all-whitespace self profile displayName', async () => {
    await expectBadRequest(transformDto(UpdateProfileDto, { displayName: '\n' }));
  });

  // 验证契约要求的当前密码不能以空凭据进入 Argon2。
  it('returns 400 for an empty currentPassword', async () => {
    await expectBadRequest(
      transformDto(ChangePasswordDto, {
        currentPassword: '',
        newPassword: 'safe-password-2026',
      }),
    );
  });

  // 验证可选变更字段只跳过 undefined，null 不得进入服务默认值或 Prisma。
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

  // 验证必填资料和可选查询值在构造服务查询前拒绝 null 或空白筛选值。
  it('returns 400 for null profile and null or blank query fields', async () => {
    await Promise.all([
      expectBadRequest(transformDto(UpdateProfileDto, { displayName: null })),
      expectBadRequest(transformDto(ListUsersQueryDto, { role: null })),
      expectBadRequest(transformDto(ListUsersQueryDto, { q: '   ' })),
    ]);
  });
});

/** 对一个 DTO fixture 应用应用程序完整的 body/query 校验策略。 */
async function transformDto<T>(metatype: Type<T>, value: unknown): Promise<T> {
  const pipe = new ValidationPipe({
    transform: true,
    whitelist: true,
    forbidNonWhitelisted: true,
    transformOptions: { enableImplicitConversion: false },
  });
  return pipe.transform(value, { type: 'body', metatype } as never) as Promise<T>;
}

/** 断言校验遵循公开 HTTP 400 边界，而不是延后到应用代码中失败。 */
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
