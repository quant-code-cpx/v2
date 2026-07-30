import { Alert, Box, Button, Chip, Stack, Typography } from "@mui/material";
import { useCallback, useState } from "react";

import { brandColors } from "../../../styles/design-tokens";
import type { StockConnectPublication } from "../../../types/stock-connect";
import {
  formatStockConnectDateTime,
  stockConnectSourceSummary,
} from "../utils/stock-connect-presentation";
import { SourceMethodDrawer } from "./SourceMethodDrawer";

/** 描述页面顶部 publication、共同交易日和复核状态。 */
interface PublicationBarProps {
  publication: StockConnectPublication;
  resolvedTradeDate: string;
  resolutionLabel: string;
  isFetching: boolean;
  isStaleBecauseError: boolean;
}

/** 显示真实数据版本和来源，并允许打开完整来源口径抽屉。 */
export function PublicationBar({
  publication,
  resolvedTradeDate,
  resolutionLabel,
  isFetching,
  isStaleBecauseError,
}: PublicationBarProps) {
  const [drawerOpen, setDrawerOpen] = useState(false);

  /** 打开当前 publication 的来源与质量详情。 */
  const handleOpenDrawer = useCallback(() => {
    setDrawerOpen(true);
  }, []);

  /** 关闭来源抽屉并把焦点交还触发按钮。 */
  const handleCloseDrawer = useCallback(() => {
    setDrawerOpen(false);
  }, []);

  return (
    <>
      {isStaleBecauseError ? (
        <Alert severity="warning" sx={{ mb: 2 }}>
          连接异常，当前保留已验证 publication：{publication.dataVersion}
        </Alert>
      ) : null}
      <Box
        sx={{
          px: 2,
          py: 1.25,
          border: 1,
          borderColor: "primary.light",
          borderRadius: 1,
          bgcolor: brandColors.primaryLighter,
        }}
      >
        <Stack direction="row" justifyContent="space-between" alignItems="center" spacing={2}>
          <Stack direction="row" alignItems="center" spacing={1.5}>
            <Typography variant="body2" fontWeight={700}>
              {resolutionLabel}：{resolvedTradeDate}
            </Typography>
            <Chip
              size="small"
              color={publication.qualityStatus === "APPROVED" ? "success" : "warning"}
              label={publication.qualityStatus === "APPROVED" ? "质量已批准" : "质量带提示"}
            />
            {isFetching ? <Chip size="small" color="info" label="正在复核版本" /> : null}
          </Stack>
          <Stack direction="row" alignItems="center" spacing={1.5}>
            <Typography variant="caption" color="text.secondary">
              {stockConnectSourceSummary(publication)} ·{" "}
              {formatStockConnectDateTime(publication.publishedAt)}
            </Typography>
            <Button size="small" variant="text" onClick={handleOpenDrawer}>
              来源与口径
            </Button>
          </Stack>
        </Stack>
      </Box>
      <SourceMethodDrawer open={drawerOpen} publication={publication} onClose={handleCloseDrawer} />
    </>
  );
}
