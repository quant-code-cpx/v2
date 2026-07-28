import { HttpStatus, Injectable } from '@nestjs/common';

import { AppConfigService } from '../../config/app-config.service.js';
import { PublicProblemException } from '../../common/exceptions/problem.exception.js';

/** Carry only request headers needed to defend browser authentication endpoints. */
export type BrowserRequest = {
  contentType?: string | undefined;
  fetchSite?: string | undefined;
  origin?: string | undefined;
};

@Injectable()
export class BrowserRequestSecurityService {
  /** Read trusted browser-origin policy from validated configuration. */
  public constructor(private readonly config: AppConfigService) {}

  /** Require exact origin and safe Fetch Metadata; optionally require a JSON request body. */
  public assertAllowed(request: BrowserRequest, requireJsonBody = false): void {
    if (request.origin !== this.config.corsOrigin || !isTrustedFetchSite(request.fetchSite)) {
      throw crossSiteRejected();
    }
    if (requireJsonBody && request.contentType?.split(';', 1)[0]?.trim() !== 'application/json') {
      throw crossSiteRejected();
    }
  }
}

/** Accept only Fetch Metadata modes that cannot represent a cross-site credential request. */
function isTrustedFetchSite(value: string | undefined): boolean {
  return value === 'same-origin' || value === 'same-site' || value === 'none';
}

/** Return one uniform browser-boundary failure with no origin-policy detail. */
function crossSiteRejected(): PublicProblemException {
  return new PublicProblemException(
    HttpStatus.FORBIDDEN,
    'cross-site-request-rejected',
    'Browser request rejected',
  );
}
