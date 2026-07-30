import { skipToken, useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";

import {
  queryEtfDailyBars,
  queryEtfProfile,
  queryEtfTradingStates,
  queryEtfUnitNavs,
} from "../../../api/etfs";
import { parseEtfRouteIdentity } from "../utils/etf-detail";

/** 先读取 ETF 身份，再以永久 entity ref 并行查询三个独立数据集。 */
export function useEtfDetail() {
  const params = useParams();
  const identity = parseEtfRouteIdentity(params.exchange, params.symbol);
  const profileQuery = useQuery({
    queryKey: ["market-data", "etf", "profile", identity?.exchange, identity?.symbol] as const,
    queryFn:
      identity === null
        ? skipToken
        : /** 将身份查询取消信号传递到共享 POST 传输层。 */
          ({ signal }) => queryEtfProfile(identity.exchange, identity.symbol, signal),
    staleTime: 60_000,
  });
  const profile =
    profileQuery.data?.meta.availability === "AVAILABLE"
      ? profileQuery.data.records.find(
          /** 路由身份必须与来源明确给出的交易所和代码同时一致。 */
          (record) =>
            record.values.exchange === identity?.exchange &&
            record.values.symbol === identity.symbol,
        )?.values
      : undefined;
  const etfEntityRef = profile?.etfEntityRef;
  const barsQuery = useQuery({
    queryKey: ["market-data", "etf", "bars", etfEntityRef] as const,
    queryFn:
      etfEntityRef === undefined
        ? skipToken
        : /** 身份解析成功后读取真实未复权日线。 */
          ({ signal }) => queryEtfDailyBars(etfEntityRef, signal),
    staleTime: 5 * 60_000,
  });
  const navsQuery = useQuery({
    queryKey: ["market-data", "etf", "navs", etfEntityRef] as const,
    queryFn:
      etfEntityRef === undefined
        ? skipToken
        : /** 身份解析成功后与日线并行读取单位 NAV。 */
          ({ signal }) => queryEtfUnitNavs(etfEntityRef, signal),
    staleTime: 5 * 60_000,
  });
  const statesQuery = useQuery({
    queryKey: ["market-data", "etf", "states", etfEntityRef] as const,
    queryFn:
      etfEntityRef === undefined
        ? skipToken
        : /** 身份解析成功后独立读取三维状态。 */
          ({ signal }) => queryEtfTradingStates(etfEntityRef, signal),
    staleTime: 60_000,
  });

  return {
    identity,
    profile,
    profileQuery,
    barsQuery,
    navsQuery,
    statesQuery,
  };
}
