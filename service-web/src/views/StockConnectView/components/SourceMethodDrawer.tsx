import { Box, Button, Chip, Divider, Drawer, Stack, Typography } from "@mui/material";

import type { StockConnectPublication } from "../../../types/stock-connect";
import {
  formatStockConnectDateTime,
  stockConnectSourceSummary,
} from "../utils/stock-connect-presentation";

/** 描述来源与 publication 口径抽屉的受控状态。 */
interface SourceMethodDrawerProps {
  open: boolean;
  publication: StockConnectPublication;
  onClose: () => void;
}

/** 展示真实 publication、来源文件摘要和质量问题，不泄露内部路径。 */
export function SourceMethodDrawer({ open, publication, onClose }: SourceMethodDrawerProps) {
  return (
    <Drawer anchor="right" open={open} onClose={onClose}>
      <Stack sx={{ height: "100%" }}>
        <Box sx={{ p: 3 }}>
          <Typography variant="h5">来源与口径</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
            日终正式 publication · 原币基础单位
          </Typography>
        </Box>
        <Divider />
        <Stack spacing={3} sx={{ p: 3, overflowY: "auto", flex: 1 }}>
          <Stack spacing={1}>
            <Typography variant="subtitle2">交易日</Typography>
            <Typography>{publication.tradeDate}</Typography>
          </Stack>
          <Stack spacing={1}>
            <Typography variant="subtitle2">平台发布时间</Typography>
            <Typography>{formatStockConnectDateTime(publication.publishedAt)}</Typography>
          </Stack>
          <Stack spacing={1}>
            <Typography variant="subtitle2">dataVersion</Typography>
            <Typography variant="body2" sx={{ overflowWrap: "anywhere", fontFamily: "monospace" }}>
              {publication.dataVersion}
            </Typography>
          </Stack>
          <Stack spacing={1}>
            <Typography variant="subtitle2">官方来源</Typography>
            <Typography variant="body2">{stockConnectSourceSummary(publication)}</Typography>
          </Stack>
          <Stack spacing={1.5}>
            <Typography variant="subtitle2">来源产品</Typography>
            {publication.sourceRefs.map(
              /** 分开呈现来源发布时间与接收时间，禁止把观察时间冒充 publication。 */
              (source) => (
                <Box
                  key={`${source.sourceCode}-${source.productName}`}
                  sx={{ p: 1.5, borderRadius: 1, bgcolor: "grey.100" }}
                >
                  <Typography variant="body2" fontWeight={700}>
                    {source.productName}
                  </Typography>
                  <Typography variant="caption" color="text.secondary">
                    {source.sourcePublicationAvailability === "REPORTED"
                      ? `来源发布：${formatStockConnectDateTime(source.sourcePublicationAt)}`
                      : `来源未提供 publication；接收于 ${formatStockConnectDateTime(source.sourceObservedAt)}`}
                  </Typography>
                  {source.sourcePublicationAvailability === "REPORTED" ? (
                    <Typography variant="caption" color="text.secondary" display="block">
                      接收于 {formatStockConnectDateTime(source.sourceObservedAt)}
                    </Typography>
                  ) : null}
                </Box>
              ),
            )}
          </Stack>
          <Stack spacing={1.5}>
            <Stack direction="row" spacing={1} alignItems="center">
              <Typography variant="subtitle2">质量</Typography>
              <Chip
                size="small"
                color={publication.qualityStatus === "APPROVED" ? "success" : "warning"}
                label={publication.qualityStatus === "APPROVED" ? "已批准" : "批准并带提示"}
              />
            </Stack>
            {publication.qualityIssues.length === 0 ? (
              <Typography variant="body2" color="text.secondary">
                本 publication 无公开质量提示。
              </Typography>
            ) : (
              publication.qualityIssues.map(
                /** 呈现已通过 API 脱敏的质量问题。 */
                (issue) => (
                  <Box
                    key={`${issue.code}-${issue.component}`}
                    sx={{ p: 1.5, borderRadius: 1, bgcolor: "action.hover" }}
                  >
                    <Typography variant="body2" fontWeight={700}>
                      {issue.component}
                    </Typography>
                    <Typography variant="caption" color="text.secondary">
                      {issue.detail}
                    </Typography>
                  </Box>
                ),
              )
            )}
          </Stack>
          <Typography variant="caption" color="text.secondary">
            净额仅在同源、同交易日、同通道且同币种的买入和卖出均已报告时按 buy − sell
            派生；成交额不代表资金净流入。
          </Typography>
        </Stack>
        <Divider />
        <Box sx={{ p: 3 }}>
          <Button fullWidth variant="outlined" onClick={onClose}>
            关闭
          </Button>
        </Box>
      </Stack>
    </Drawer>
  );
}
