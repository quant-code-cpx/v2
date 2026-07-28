import type { Role } from '../../generated/prisma/client.js';
import type { CurrentUserResource } from '../user/user.types.js';

export type { AuthContext, AuthenticatedRequest } from '../../common/models/auth-context.js';

/** 表示 JWT 解码后仍需数据库复验的安全声明。 */
export type JwtPayload = {
  sub: string;
  sid: string;
  role: Role;
  sv: number;
};

/** 表示登录或 refresh 成功后返回的 access/refresh 凭据组合。 */
export type TokenPair = {
  accessToken: string;
  refreshToken: string;
  refreshExpiresAt: Date;
  user: CurrentUserResource;
};

/** 表示本人一个活动 `Session family` 的最小化安全视图。 */
export type SessionFamilyResource = {
  familyId: string;
  current: boolean;
  lastActiveAt: string;
  absoluteExpiresAt: string;
};

/** 表示本人活动会话族的有界游标页。 */
export type SessionFamilyPage = {
  items: SessionFamilyResource[];
  page: {
    nextCursor: string | null;
  };
  total: number;
};

/** 表示“撤销其他会话”实际发生状态变化的会话族数量。 */
export type RevokeOtherSessionsResult = {
  revokedFamilyCount: number;
};
