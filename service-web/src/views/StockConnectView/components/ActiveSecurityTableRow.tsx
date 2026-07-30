import { Chip, Link, Stack, TableCell, TableRow, Typography } from "@mui/material";
import { Link as RouterLink } from "react-router-dom";

import { marketColors } from "../../../styles/design-tokens";
import type {
  StockConnectActiveSecurity,
  StockConnectChannelCode,
} from "../../../types/stock-connect";
import {
  formatStockConnectMoneyFact,
  formatStockConnectNetFact,
  stockConnectMoneyDirection,
} from "../utils/stock-connect-presentation";
import { resolveStockConnectSecurityLinks } from "../utils/stock-connect-security-links";

/** 描述一条来源活跃证券记录的上下文深链接参数。 */
interface ActiveSecurityTableRowProps {
  item: StockConnectActiveSecurity;
  channel: StockConnectChannelCode;
  resolvedTradeDate: string;
}

/** 渲染官方来源榜记录；历史身份缺失时保留源代码且不生成错误链接。 */
export function ActiveSecurityTableRow({
  item,
  channel,
  resolvedTradeDate,
}: ActiveSecurityTableRowProps) {
  const identity = item.identity;
  const direction =
    item.netBuyAmount.value === null
      ? "flat"
      : stockConnectMoneyDirection(item.netBuyAmount.value.amount);
  const netColor =
    direction === "positive"
      ? marketColors.up
      : direction === "negative"
        ? marketColors.down
        : "text.secondary";
  const links = resolveStockConnectSecurityLinks(identity, channel, resolvedTradeDate);

  return (
    <TableRow hover>
      <TableCell>{item.rankingRank}</TableCell>
      <TableCell>{item.sourceRank}</TableCell>
      <TableCell>
        <Stack spacing={0.25}>
          {links.primaryPath === null ? (
            <Typography fontWeight={700}>{identity.displayName ?? "名称未提供"}</Typography>
          ) : (
            <Link component={RouterLink} to={links.primaryPath} underline="hover" fontWeight={700}>
              {identity.displayName ?? identity.sourceSecurityCode}
            </Link>
          )}
          <Stack direction="row" alignItems="center" spacing={1}>
            <Typography variant="caption" color="text.secondary">
              {identity.sourceSecurityCode} · {identity.listingVenue}
            </Typography>
            {identity.identityAvailability === "SOURCE_UNRESOLVED" ? (
              <Chip size="small" color="warning" label="来源身份未解析" />
            ) : null}
            {links.contextPath === null ? null : (
              <Link
                component={RouterLink}
                to={links.contextPath}
                variant="caption"
                underline="hover"
              >
                互联互通记录
              </Link>
            )}
          </Stack>
        </Stack>
      </TableCell>
      <TableCell align="right">{formatStockConnectMoneyFact(item.buyAmount)}</TableCell>
      <TableCell align="right">{formatStockConnectMoneyFact(item.sellAmount)}</TableCell>
      <TableCell align="right">{formatStockConnectMoneyFact(item.turnoverAmount)}</TableCell>
      <TableCell
        align="right"
        sx={{ color: netColor, fontWeight: 700, fontVariantNumeric: "tabular-nums" }}
      >
        {formatStockConnectNetFact(item.netBuyAmount)}
      </TableCell>
    </TableRow>
  );
}
