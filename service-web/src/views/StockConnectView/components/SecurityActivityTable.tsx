import {
  Chip,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
} from "@mui/material";

import { marketColors } from "../../../styles/design-tokens";
import type { StockConnectSecurityChannelActivity } from "../../../types/stock-connect";
import {
  formatStockConnectMoneyFact,
  formatStockConnectNetFact,
  stockConnectChannelLabel,
  stockConnectMoneyDirection,
} from "../utils/stock-connect-presentation";
import { StockConnectEmptyState } from "./StockConnectRemoteState";

/** 描述证券在来源活跃榜内的历史出现记录。 */
interface SecurityActivityTableProps {
  activities: readonly StockConnectSecurityChannelActivity[];
}

/** 返回证券活动净额的中国市场语义颜色，文字仍保留完整方向。 */
function netActivityColor(activity: StockConnectSecurityChannelActivity): string {
  if (activity.netBuyAmount.value === null) {
    return marketColors.flat;
  }

  const direction = stockConnectMoneyDirection(activity.netBuyAmount.value.amount);
  if (direction === "positive") {
    return marketColors.up;
  }
  if (direction === "negative") {
    return marketColors.down;
  }

  return marketColors.flat;
}

/** 渲染逐通道、逐交易日原币活动，不对 CNY/HKD 或不同日期求和。 */
export function SecurityActivityTable({ activities }: SecurityActivityTableProps) {
  if (activities.length === 0) {
    return (
      <StockConnectEmptyState
        title="所选范围没有来源活跃榜记录"
        description="该结果只说明证券未出现在官方来源活跃榜，不代表证券没有全市场成交。"
      />
    );
  }

  return (
    <TableContainer sx={{ overflowX: "auto" }}>
      <Table size="small" aria-label="证券互联互通来源活跃榜历史" sx={{ minWidth: 920 }}>
        <TableHead>
          <TableRow>
            <TableCell scope="col">交易日</TableCell>
            <TableCell scope="col">通道</TableCell>
            <TableCell scope="col">来源名次</TableCell>
            <TableCell scope="col" align="right">
              成交额
            </TableCell>
            <TableCell scope="col" align="right">
              净额
            </TableCell>
            <TableCell scope="col">数据版本</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {activities.map(
            /** 使用通道、交易日和来源名次组成稳定业务键。 */
            (activity) => (
              <TableRow
                key={`${activity.channel}-${activity.tradeDate}-${activity.sourceRank}`}
                hover
              >
                <TableCell>{activity.tradeDate}</TableCell>
                <TableCell>
                  <Chip size="small" label={stockConnectChannelLabel(activity.channel)} />
                </TableCell>
                <TableCell>{activity.sourceRank}</TableCell>
                <TableCell align="right">
                  {formatStockConnectMoneyFact(activity.turnoverAmount)}
                </TableCell>
                <TableCell
                  align="right"
                  sx={{
                    color: netActivityColor(activity),
                    fontWeight: 700,
                    fontVariantNumeric: "tabular-nums",
                  }}
                >
                  {formatStockConnectNetFact(activity.netBuyAmount)}
                </TableCell>
                <TableCell
                  sx={{
                    fontFamily: "monospace",
                    maxWidth: 180,
                    overflowWrap: "anywhere",
                  }}
                >
                  {activity.dataVersion}
                </TableCell>
              </TableRow>
            ),
          )}
        </TableBody>
      </Table>
    </TableContainer>
  );
}
