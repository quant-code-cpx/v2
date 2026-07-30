import {
  CheckCircleOutlineRounded as CheckCircleOutlineRoundedIcon,
  WarningAmberRounded as WarningAmberRoundedIcon,
} from "@mui/icons-material";
import { Alert, Card, CardContent, Chip, Divider, Stack, Typography } from "@mui/material";

import type { MarketOverview } from "../../../types/market";

/** 渲染可追溯规则产生的市场关注项和完整包质量摘要。 */
export function MarketAttentionCard({ overview }: { overview: MarketOverview }) {
  let externalCount = 0;
  let derivedCount = 0;
  const externalUpstreams = new Set<string>();
  for (const binding of overview.quality.sourceBindings) {
    if (binding.role === "external") {
      externalCount += 1;
      externalUpstreams.add(binding.upstreamSource);
    } else {
      derivedCount += 1;
    }
  }
  const sourceSummary = [
    externalCount === 0
      ? null
      : `外部 ${externalCount} 项（tushare-pro：${Array.from(externalUpstreams).join(" / ")}）`,
    derivedCount === 0 ? null : `平台派生 ${derivedCount} 项（quant-v2-derivation）`,
  ]
    .filter(
      /** 去除不存在的来源角色摘要。 */
      (item): item is string => item !== null,
    )
    .join(" · ");

  return (
    <Card component="section" aria-label="市场关注与质量">
      <CardContent>
        <Stack direction="row" justifyContent="space-between" alignItems="center">
          <Typography variant="h6">值得关注</Typography>
          <Chip
            size="small"
            color="success"
            variant="outlined"
            icon={<CheckCircleOutlineRoundedIcon />}
            label={`${overview.quality.passedCount}/${overview.quality.componentCount} 项质量检查通过`}
          />
        </Stack>
        {overview.attentionSignals.length === 0 ? (
          <Alert severity="success" sx={{ mt: 2 }}>
            当前规则版本未发现达到阈值的市场异常。
          </Alert>
        ) : (
          <Stack divider={<Divider flexItem />} sx={{ mt: 1 }}>
            {overview.attentionSignals.slice(0, 8).map(
              /** 每条信号保留规则身份和首项量化证据。 */
              (signal) => (
                <Stack key={signal.signalId} direction="row" spacing={1.5} sx={{ py: 1.25 }}>
                  <WarningAmberRoundedIcon
                    fontSize="small"
                    color={signal.severity === "warning" ? "warning" : "info"}
                  />
                  <Stack spacing={0.25}>
                    <Typography variant="body2" fontWeight={700}>
                      {signal.title}
                    </Typography>
                    <Typography variant="caption" color="text.secondary">
                      {signal.ruleId} v{signal.rulesVersion} ·{" "}
                      {signal.evidence
                        .slice(0, 2)
                        .map(
                          /** 将合同中的证据字段组合为紧凑可审计文本。 */
                          (evidence) =>
                            `${evidence.metric} ${evidence.currentValue}${evidence.unit} / 阈值 ${evidence.threshold}${evidence.unit}`,
                        )
                        .join("；")}
                    </Typography>
                  </Stack>
                </Stack>
              ),
            )}
          </Stack>
        )}
        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1.5 }}>
          股票范围版本 {overview.quality.universeVersion} · 数据版本 {overview.dataVersion}
        </Typography>
        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.5 }}>
          来源绑定：{sourceSummary}
        </Typography>
      </CardContent>
    </Card>
  );
}
