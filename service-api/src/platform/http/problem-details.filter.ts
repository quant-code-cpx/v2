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
  error?: string;
  message?: string | string[];
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

    if (status >= 500) {
      this.logger.error({
        err: exception,
        requestId: request.requestId,
        path: request.originalUrl,
      });
    }

    response
      .status(status)
      .type('application/problem+json')
      .json({
        type: `https://quant-v2.local/problems/${this.problemType(status)}`,
        title: this.titleFor(status),
        status,
        detail,
        instance: request.originalUrl,
        requestId: request.requestId,
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

  /** Map common status codes to stable problem-type path segments. */
  private problemType(status: number): string {
    const types: Record<number, string> = {
      400: 'validation-error',
      401: 'unauthorized',
      403: 'forbidden',
      404: 'not-found',
      409: 'conflict',
      429: 'rate-limited',
      503: 'dependency-unavailable',
    };
    return types[status] ?? 'internal-error';
  }

  /** Return standard HTTP status title with safe fallback for unknown status. */
  private titleFor(status: number): string {
    return HttpStatus[status] ?? 'Internal Server Error';
  }
}
