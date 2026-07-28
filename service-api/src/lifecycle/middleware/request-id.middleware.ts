import { randomUUID } from 'node:crypto';

import type { NextFunction, Request, Response } from 'express';

/** Reuse bounded client request ID or generate one, then expose it on request and response. */
export function requestIdMiddleware(
  request: Request,
  response: Response,
  next: NextFunction,
): void {
  const candidate = request.header('x-request-id');
  const requestId = candidate && candidate.length <= 128 ? candidate : randomUUID();
  request.requestId = requestId;
  response.setHeader('x-request-id', requestId);
  next();
}
