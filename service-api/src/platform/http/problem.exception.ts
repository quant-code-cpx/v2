import { HttpException, type HttpStatus } from '@nestjs/common';

/** Preserve a stable public problem code without exposing internal error details. */
export class PublicProblemException extends HttpException {
  /** Create an RFC 9457-compatible exception with an intentionally safe detail string. */
  public constructor(status: HttpStatus, code: string, detail: string, retryAfter?: number) {
    super(
      {
        code,
        message: detail,
        ...(retryAfter === undefined ? {} : { retryAfter }),
      },
      status,
    );
  }
}
