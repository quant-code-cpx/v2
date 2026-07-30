import { Alert, Button, Card, CardContent, Skeleton, Stack, Typography } from "@mui/material";
import { Navigate, Link as RouterLink, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { conditionalBody, legacyEquityResolutionQueryOptions } from "../../api/equity-market";
import { isApiError } from "../../api/http";

/** 用目录 publication 的身份日期构造一次解析即可复验的 canonical 详情地址。 */
export function legacyEquityCanonicalTarget(
  exchange: "SSE" | "SZSE" | "BSE",
  symbol: string,
  identityAsOf: string,
): string {
  const search = new URLSearchParams({ asOf: identityAsOf });
  return `/market/equities/${exchange}/${symbol}?${search.toString()}`;
}

/** 迁移旧 symbol-only fixture 路由，只有目录唯一命中时才进入 canonical URL。 */
export function LegacyInstrumentRedirectView() {
  const { symbol = "" } = useParams();
  const validSymbol = /^\d{6}$/.test(symbol);
  const query = useQuery({
    ...legacyEquityResolutionQueryOptions(symbol),
    enabled: validSymbol,
  });
  const response = conditionalBody(query.data);
  const matches =
    response?.items.filter(
      /** 前缀目录结果必须再次精确匹配代码，名称命中不能参与迁移。 */
      (item) => item.identifier.symbol === symbol,
    ) ?? [];

  if (!validSymbol) {
    return (
      <Alert severity="error">
        旧链接中的证券代码无效。请从
        <Button component={RouterLink} to="/market/equities" size="small">
          股票中心
        </Button>
        重新选择证券。
      </Alert>
    );
  }

  if (query.isPending) {
    return <Skeleton variant="rounded" height={260} aria-label="正在解析旧证券链接" />;
  }

  if (query.isError) {
    const noPublication =
      isApiError(query.error) &&
      query.error.status === 503 &&
      query.error.code === "publication-unavailable";
    return (
      <Alert
        severity="error"
        action={
          <Button color="inherit" size="small" onClick={() => void query.refetch()}>
            重试
          </Button>
        }
      >
        {noPublication
          ? "证券目录尚无可用 publication，当前无法安全解析旧链接。页面不会按代码前缀猜交易所。"
          : "旧链接暂时无法解析，请稍后重试。"}
      </Alert>
    );
  }

  if (response !== undefined && matches.length === 1 && matches[0] !== undefined) {
    return (
      <Navigate
        replace
        to={legacyEquityCanonicalTarget(
          matches[0].identifier.exchange,
          matches[0].identifier.symbol,
          response.effectiveAsOf,
        )}
      />
    );
  }

  return (
    <Card>
      <CardContent>
        <Stack spacing={2}>
          <Typography variant="h5">
            {matches.length === 0 ? "未找到该证券" : "请选择证券交易所"}
          </Typography>
          <Typography color="text.secondary">
            旧链接缺少 exchange，系统只使用已发布目录确认身份，不根据代码前缀推断。
          </Typography>
          {matches.length === 0 || response === undefined ? (
            <Button component={RouterLink} to="/market/equities" variant="contained">
              返回股票中心
            </Button>
          ) : (
            <Stack direction="row" spacing={1}>
              {/* 多交易所命中时由用户显式确认 canonical 身份。 */}
              {matches.map((item) => (
                <Button
                  key={item.identifier.exchange}
                  component={RouterLink}
                  to={legacyEquityCanonicalTarget(
                    item.identifier.exchange,
                    item.identifier.symbol,
                    response.effectiveAsOf,
                  )}
                  variant="outlined"
                >
                  {item.identifier.exchange} · {item.name.value}
                </Button>
              ))}
            </Stack>
          )}
        </Stack>
      </CardContent>
    </Card>
  );
}
