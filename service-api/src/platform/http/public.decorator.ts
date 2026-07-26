import { SetMetadata } from '@nestjs/common';

export const IS_PUBLIC_ROUTE = 'isPublicRoute';

/** Mark one HTTP handler as an explicit exception to global default-deny authentication. */
export const Public = (): ReturnType<typeof SetMetadata> => SetMetadata(IS_PUBLIC_ROUTE, true);
