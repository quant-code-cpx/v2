import {
  ForbiddenException,
  HttpStatus,
  Injectable,
  NotFoundException,
  ServiceUnavailableException,
} from '@nestjs/common';
import { createHmac, timingSafeEqual } from 'node:crypto';

import { Prisma, Role } from '../../generated/prisma/client.js';
import type { AuthContext } from '../../common/models/auth-context.js';
import { PublicProblemException } from '../../common/exceptions/problem.exception.js';
import { AppConfigService } from '../../config/app-config.service.js';
import { DatabaseService } from '../../shared/database/database.service.js';
import {
  actionsForCategory,
  AUDIT_ACTION_REGISTRY,
  routineAuditActions,
} from './audit-action-registry.js';
import type { ListAuditEventsDto } from './dto/list-audit-events.dto.js';
import type {
  AppliedTimeWindow,
  AuditEventDetail,
  AuditEventPage,
  AuditEventResource,
} from './audit.types.js';

const DEFAULT_WINDOW_MILLISECONDS = 7 * 24 * 60 * 60 * 1_000;
const MAX_WINDOW_MILLISECONDS = 90 * 24 * 60 * 60 * 1_000;

/** 保存审计游标所需的完整查询窗口、筛选指纹与倒序键。 */
type AuditCursorPayload = {
  actorId: string;
  fingerprint: string;
  id: string;
  occurredAt: string;
  occurredFrom: string;
  occurredTo: string;
  signature: string;
};

/** 描述 Prisma 查询返回且允许进入脱敏映射的审计行。 */
type AuditRow = {
  id: string;
  actorId: string | null;
  action: string;
  targetId: string | null;
  requestId: string | null;
  metadata: Prisma.JsonValue | null;
  occurredAt: Date;
  actor: {
    id: string;
    account: string;
    displayName: string;
  } | null;
};

@Injectable()
export class AuditService {
  /** 注入 PostgreSQL 权威存储和服务端游标签名配置。 */
  public constructor(
    private readonly database: DatabaseService,
    private readonly config: AppConfigService,
  ) {}

  /** 按冻结筛选与时间窗返回脱敏审计列表，仅允许超级管理员调用。 */
  public async listEvents(actor: AuthContext, input: ListAuditEventsDto): Promise<AuditEventPage> {
    this.assertReader(actor);
    const parsedCursor =
      input.cursor === undefined ? undefined : this.parseCursor(input.cursor, actor.userId);
    const window = this.resolveWindow(input, parsedCursor);
    const fingerprint = this.fingerprint(actor.userId, input, window);
    if (parsedCursor && parsedCursor.fingerprint !== fingerprint) {
      throw invalidCursor();
    }

    const where: Prisma.AuditLogWhereInput = {
      occurredAt: { gte: window.occurredFrom, lte: window.occurredTo },
      ...(input.actorId === undefined ? {} : { actorId: input.actorId }),
      ...(input.targetId === undefined ? {} : { targetId: input.targetId }),
      ...(input.category === undefined
        ? {}
        : { action: { in: actionsForCategory(input.category) } }),
      ...(input.includeRoutine
        ? {}
        : input.category === undefined
          ? { action: { notIn: routineAuditActions() } }
          : {
              AND: [
                { action: { in: actionsForCategory(input.category) } },
                { action: { notIn: routineAuditActions() } },
              ],
            }),
      ...(parsedCursor === undefined
        ? {}
        : {
            AND: [
              ...((input.category !== undefined && !input.includeRoutine
                ? [
                    { action: { in: actionsForCategory(input.category) } },
                    { action: { notIn: routineAuditActions() } },
                  ]
                : []) as Prisma.AuditLogWhereInput[]),
              {
                OR: [
                  { occurredAt: { lt: new Date(parsedCursor.occurredAt) } },
                  {
                    occurredAt: new Date(parsedCursor.occurredAt),
                    id: { lt: parsedCursor.id },
                  },
                ],
              },
            ],
          }),
    };

    let rows: AuditRow[];
    try {
      rows = await this.database.client.auditLog.findMany({
        where,
        orderBy: [{ occurredAt: 'desc' }, { id: 'desc' }],
        take: input.pageSize + 1,
        include: {
          actor: {
            select: {
              id: true,
              account: true,
              displayName: true,
            },
          },
        },
      });
    } catch {
      throw new ServiceUnavailableException('Audit storage is unavailable');
    }

    const hasMore = rows.length > input.pageSize;
    const pageRows = hasMore ? rows.slice(0, input.pageSize) : rows;
    const last = pageRows.at(-1);
    // 列表映射只经过 registry，不允许原始 metadata 进入返回对象。
    const items = pageRows.map((row) => this.toResource(row));
    return {
      items,
      page: {
        nextCursor:
          hasMore && last
            ? this.createCursor(actor.userId, input, window, last.occurredAt, last.id)
            : null,
      },
      appliedWindow: this.toAppliedWindow(window),
    };
  }

  /** 返回一个审计事件的脱敏详情，未知 action 只给通用摘要和空详情。 */
  public async getEvent(actor: AuthContext, eventId: string): Promise<AuditEventDetail> {
    this.assertReader(actor);
    let row: AuditRow | null;
    try {
      row = await this.database.client.auditLog.findUnique({
        where: { id: eventId },
        include: {
          actor: {
            select: {
              id: true,
              account: true,
              displayName: true,
            },
          },
        },
      });
    } catch {
      throw new ServiceUnavailableException('Audit storage is unavailable');
    }
    if (!row) {
      throw new NotFoundException('Audit event not found');
    }
    const definition = AUDIT_ACTION_REGISTRY[row.action];
    return {
      ...this.toResource(row),
      details: definition?.details(row.metadata) ?? {},
    };
  }

  /** 服务层再次校验读取角色，防止非 HTTP 调用绕过 Controller 元数据。 */
  private assertReader(actor: AuthContext): void {
    if (actor.role !== Role.SUPER_ADMIN) {
      throw new ForbiddenException('Audit access requires super-administrator authority');
    }
  }

  /** 将数据库行映射为稳定公开事件，绝不展开原始 metadata。 */
  private toResource(row: AuditRow): AuditEventResource {
    const definition = AUDIT_ACTION_REGISTRY[row.action];
    return {
      id: row.id,
      category: definition?.category ?? 'SYSTEM',
      severity: definition?.severity ?? 'INFO',
      action: row.action,
      summary: definition?.summary ?? '未识别的审计操作',
      actor:
        row.actor === null
          ? null
          : {
              id: row.actor.id,
              account: row.actor.account,
              displayName: row.actor.displayName,
            },
      target: {
        type: definition?.targetType ?? 'UNKNOWN',
        id: row.targetId,
      },
      requestId: row.requestId,
      occurredAt: row.occurredAt.toISOString(),
    };
  }

  /** 解析默认七天、最大九十天的闭合审计时间窗。 */
  private resolveWindow(
    input: ListAuditEventsDto,
    cursor: AuditCursorPayload | undefined,
  ): { occurredFrom: Date; occurredTo: Date } {
    const occurredTo = new Date(input.occurredTo ?? cursor?.occurredTo ?? Date.now());
    const occurredFrom = new Date(
      input.occurredFrom ??
        cursor?.occurredFrom ??
        occurredTo.getTime() - DEFAULT_WINDOW_MILLISECONDS,
    );
    const duration = occurredTo.getTime() - occurredFrom.getTime();
    if (
      Number.isNaN(occurredFrom.getTime()) ||
      Number.isNaN(occurredTo.getTime()) ||
      duration <= 0 ||
      duration > MAX_WINDOW_MILLISECONDS
    ) {
      throw new PublicProblemException(
        HttpStatus.BAD_REQUEST,
        'invalid-time-window',
        'Audit time window must be greater than zero and no longer than 90 days',
      );
    }
    return { occurredFrom, occurredTo };
  }

  /** 创建与调用方、筛选条件和实际时间窗绑定的审计游标。 */
  private createCursor(
    actorId: string,
    input: ListAuditEventsDto,
    window: { occurredFrom: Date; occurredTo: Date },
    occurredAt: Date,
    id: string,
  ): string {
    const payload = {
      actorId,
      fingerprint: this.fingerprint(actorId, input, window),
      id,
      occurredAt: occurredAt.toISOString(),
      occurredFrom: window.occurredFrom.toISOString(),
      occurredTo: window.occurredTo.toISOString(),
    };
    const signature = this.signCursor(payload);
    return Buffer.from(JSON.stringify({ ...payload, signature })).toString('base64url');
  }

  /** 验证审计游标签名、调用方绑定、UUID 与所有时间字段。 */
  private parseCursor(value: string, actorId: string): AuditCursorPayload {
    try {
      const payload = JSON.parse(
        Buffer.from(value, 'base64url').toString('utf8'),
      ) as AuditCursorPayload;
      const unsigned = {
        actorId: payload.actorId,
        fingerprint: payload.fingerprint,
        id: payload.id,
        occurredAt: payload.occurredAt,
        occurredFrom: payload.occurredFrom,
        occurredTo: payload.occurredTo,
      };
      const expected = Buffer.from(this.signCursor(unsigned));
      const actual = Buffer.from(payload.signature ?? '');
      if (
        payload.actorId !== actorId ||
        !isUuid(payload.id) ||
        [payload.occurredAt, payload.occurredFrom, payload.occurredTo].some(isInvalidDateString) ||
        actual.length !== expected.length ||
        !timingSafeEqual(actual, expected)
      ) {
        throw new Error('invalid cursor');
      }
      return payload;
    } catch {
      throw invalidCursor();
    }
  }

  /** 对游标中除签名外的全部稳定字段计算 HMAC。 */
  private signCursor(payload: Omit<AuditCursorPayload, 'signature'>): string {
    return createHmac('sha256', this.config.jwtAccessSecret)
      .update(JSON.stringify(payload))
      .digest('base64url');
  }

  /** 对调用方可见筛选和实际窗口生成稳定摘要，阻止跨查询复用游标。 */
  private fingerprint(
    actorId: string,
    input: ListAuditEventsDto,
    window: { occurredFrom: Date; occurredTo: Date },
  ): string {
    return createHmac('sha256', this.config.jwtAccessSecret)
      .update(
        JSON.stringify({
          actorId,
          category: input.category ?? null,
          filterActorId: input.actorId ?? null,
          includeRoutine: input.includeRoutine,
          occurredFrom: window.occurredFrom.toISOString(),
          occurredTo: window.occurredTo.toISOString(),
          targetId: input.targetId ?? null,
        }),
      )
      .digest('base64url');
  }

  /** 将内部日期对象转换为契约要求的 RFC 3339 窗口。 */
  private toAppliedWindow(window: { occurredFrom: Date; occurredTo: Date }): AppliedTimeWindow {
    return {
      occurredFrom: window.occurredFrom.toISOString(),
      occurredTo: window.occurredTo.toISOString(),
    };
  }
}

/** 构造不泄露游标内容的稳定 400 Problem Details。 */
function invalidCursor(): PublicProblemException {
  return new PublicProblemException(
    HttpStatus.BAD_REQUEST,
    'invalid-cursor',
    'Invalid audit cursor',
  );
}

/** 在进入 Prisma UUID 查询前验证游标主键格式。 */
function isUuid(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}

/** 判断游标日期字符串是否无法形成有效时间键。 */
function isInvalidDateString(value: string): boolean {
  return Number.isNaN(new Date(value).getTime());
}
