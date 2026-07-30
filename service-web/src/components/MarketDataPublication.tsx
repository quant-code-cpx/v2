import { Box, Chip, Stack, Typography } from "@mui/material";

import type { MarketDataPageMeta } from "../types/etf";
import {
  formatPublicationTime,
  isAvailableRelease,
  releasePublisherLabel,
  releaseWarningLabel,
} from "../utils/etf-presentation";

/** publication 信息条需要展示的数据集标签与元数据。 */
interface MarketDataPublicationProps {
  datasetLabel: string;
  meta: MarketDataPageMeta;
}

/** 将 publication 公开质量状态转换为稳定中文标签与芯片颜色。 */
function publicationQuality(meta: MarketDataPageMeta): {
  label: string;
  color: "success" | "warning" | "default";
} {
  const qualityStatus =
    isAvailableRelease(meta.release) && typeof meta.release.quality.status === "string"
      ? meta.release.quality.status
      : undefined;
  if (qualityStatus === "PASSED") return { label: "质量通过", color: "success" };
  if (qualityStatus === "WARNED") return { label: "质量有警告", color: "warning" };
  return { label: "质量状态未披露", color: "default" };
}

/** 展示数据来源、publication 时间、完整度和公开 warning，不推测额外新鲜度。 */
export function MarketDataPublication({ datasetLabel, meta }: MarketDataPublicationProps) {
  if (!isAvailableRelease(meta.release)) {
    return null;
  }
  const warning = releaseWarningLabel(meta);
  const quality = publicationQuality(meta);

  return (
    <Stack direction="row" alignItems="center" spacing={1} useFlexGap sx={{ flexWrap: "wrap" }}>
      <Chip size="small" variant="outlined" label={datasetLabel} />
      <Typography variant="caption" color="text.secondary">
        {releasePublisherLabel(meta)} · 发布于 {formatPublicationTime(meta.release.publishedAt)}
      </Typography>
      <Chip
        size="small"
        color={meta.release.completeness === "COMPLETE" ? "success" : "warning"}
        variant="outlined"
        label={meta.release.completeness === "COMPLETE" ? "发布完整" : "部分发布"}
      />
      <Chip size="small" color={quality.color} variant="outlined" label={quality.label} />
      {warning === null ? null : (
        <Box component="span">
          <Typography variant="caption" color="warning.main">
            {warning}
          </Typography>
        </Box>
      )}
    </Stack>
  );
}
