import {
  ArgumentsHost,
  Catch,
  ExceptionFilter,
  HttpException,
  HttpStatus,
  Logger,
} from '@nestjs/common';

import type { Request, Response } from 'express';

type ProblemResponse = {
  code?: string;
  error?: string;
  message?: string | string[];
  retryAfter?: number;
};

@Catch()
export class ProblemDetailsFilter implements ExceptionFilter {
  private readonly logger = new Logger(ProblemDetailsFilter.name);

  /** Translate every thrown error into RFC 9457-style problem response and log server failures. */
  public catch(exception: unknown, host: ArgumentsHost): void {
    const context = host.switchToHttp();
    const response = context.getResponse<Response>();
    const request = context.getRequest<Request>();
    const status =
      exception instanceof HttpException ? exception.getStatus() : HttpStatus.INTERNAL_SERVER_ERROR;
    const payload = exception instanceof HttpException ? exception.getResponse() : undefined;
    const detail = this.detailFrom(payload, status);
    const code = this.codeFrom(payload, status);
    const retryAfter = this.retryAfterFrom(payload);

    if (status >= 500) {
      this.logger.error({
        err: exception,
        requestId: request.requestId,
        path: request.originalUrl,
      });
    }

    if (retryAfter !== undefined) {
      response.setHeader('Retry-After', String(retryAfter));
    }

    response
      .status(status)
      .setHeader('Cache-Control', 'no-store')
      .type('application/problem+json')
      .json({
        type: `https://apex.local/problems/${code}`,
        title: this.titleFor(status),
        status,
        detail,
        instance: request.originalUrl,
        requestId: request.requestId,
        code,
      });
  }

  /** Extract safe human-readable detail from Nest exception response payload. */
  private detailFrom(payload: string | object | undefined, status: number): string {
    if (typeof payload === 'string') {
      return payload;
    }

    if (payload && typeof payload === 'object') {
      const message = (payload as ProblemResponse).message;
      if (Array.isArray(message)) {
        return message.join('; ');
      }
      if (typeof message === 'string') {
        return message;
      }
    }

    return this.titleFor(status);
  }

  /** Read a safe explicit public code or derive a stable code from HTTP status. */
  private codeFrom(payload: string | object | undefined, status: number): string {
    if (payload && typeof payload === 'object') {
      const code = (payload as ProblemResponse).code;
      if (typeof code === 'string' && /^[A-Za-z][A-Za-z0-9_-]*$/.test(code)) {
        return code;
      }
    }

    const types: Record<number, string> = {
      400: 'validation-error',
      401: 'unauthorized',
      403: 'forbidden',
      404: 'not-found',
      409: 'conflict',
      412: 'precondition-failed',
      422: 'unprocessable-content',
      428: 'precondition-required',
      429: 'rate-limited',
      503: 'dependency-unavailable',
    };
    return types[status] ?? 'internal-error';
  }

  /** Read optional bounded retry instruction emitted by a deliberate public problem. */
  private retryAfterFrom(payload: string | object | undefined): number | undefined {
    if (!payload || typeof payload !== 'object') {
      return undefined;
    }
    const retryAfter = (payload as ProblemResponse).retryAfter;
    return typeof retryAfter === 'number' && Number.isSafeInteger(retryAfter) && retryAfter > 0
      ? retryAfter
      : undefined;
  }

  /** Return standard HTTP status title with safe fallback for unknown status. */
  private titleFor(status: number): string {
    return HttpStatus[status] ?? 'Internal Server Error';
  }
}
