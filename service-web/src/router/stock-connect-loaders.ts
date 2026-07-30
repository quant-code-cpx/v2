import type { LoaderFunctionArgs } from "react-router-dom";

import type { CurrentUser } from "../types/access";
import { stockConnectChannelSlugs } from "../views/StockConnectView/utils/stock-connect-url";
import type { StockConnectChannelSlug } from "../views/StockConnectView/utils/stock-connect-url";
import { requireSession } from "./guards";

/** 在认证后拒绝未知通道短名，避免错误参数触发真实 API。 */
export async function requireStockConnectChannel(
  arguments_: LoaderFunctionArgs,
): Promise<CurrentUser | Response> {
  const userOrRedirect = await requireSession(arguments_);
  if (userOrRedirect instanceof Response) {
    return userOrRedirect;
  }

  if (!stockConnectChannelSlugs.includes(arguments_.params.channel as StockConnectChannelSlug)) {
    throw new Response("互联互通通道不存在。", {
      status: 404,
      statusText: "Not Found",
    });
  }

  return userOrRedirect;
}

/** 在认证后校验稳定证券引用边界，空值或超长值不发送下游请求。 */
export async function requireStockConnectSecurity(
  arguments_: LoaderFunctionArgs,
): Promise<CurrentUser | Response> {
  const userOrRedirect = await requireSession(arguments_);
  if (userOrRedirect instanceof Response) {
    return userOrRedirect;
  }

  const instrumentEntityRef = arguments_.params.instrumentEntityRef;
  if (
    instrumentEntityRef === undefined ||
    instrumentEntityRef.length === 0 ||
    instrumentEntityRef.length > 160
  ) {
    throw new Response("证券互联互通身份不存在。", {
      status: 404,
      statusText: "Not Found",
    });
  }

  return userOrRedirect;
}
