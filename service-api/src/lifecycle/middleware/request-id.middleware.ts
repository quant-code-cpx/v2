import { randomUUID } from 'node:crypto';
import { HttpStatus } from '@nestjs/common';

import type { NextFunction, Request, Response } from 'express';

import { PublicProblemException } from '../../common/exceptions/problem.exception.js';

/** 原样复用合法客户端请求标识；仅在请求未携带时生成一次并贯穿整条调用链。 */
export function requestIdMiddleware(
  request: Request,
  response: Response,
  next: NextFunction,
): void {
  const candidate = request.header('x-request-id');
  if (candidate !== undefined && !/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/.test(candidate)) {
    // 非法来值不能向下游传播，但错误响应仍须获得可追踪且可安全回显的服务端标识。
    const requestId = randomUUID();
    request.requestId = requestId;
    response.setHeader('x-request-id', requestId);
    throw new PublicProblemException(
      HttpStatus.BAD_REQUEST,
      'VALIDATION_FAILED',
      'X-Request-Id is invalid',
    );
  }
  const requestId = candidate ?? randomUUID();
  request.requestId = requestId;
  response.setHeader('x-request-id', requestId);
  next();
}
