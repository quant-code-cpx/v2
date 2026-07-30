import {
  Alert,
  Button,
  Card,
  CardContent,
  Chip,
  Divider,
  Skeleton,
  Stack,
  Typography,
} from "@mui/material";

import type { EquityDetailModel } from "../hooks/useEquityDetail";
import { DatasetError, DatasetUnavailable } from "./DatasetStates";

/** 渲染 Eastmoney 行业/概念与独立 SW2021 归属，分类体系绝不互相替代。 */
export function SectorsTabPanel({ model }: { model: EquityDetailModel }) {
  const swStatus = model.status?.datasets.find(
    /** 申万归属使用自己的 publication 状态。 */
    (dataset) => dataset.family === "SW_INDUSTRY_MEMBERSHIP",
  );
  const swComponent = model.discovery?.components.find(
    /** discovery 中只接受显式声明的申万组件版本。 */
    (component) => component.family === "sw",
  );
  const swVersionMatches =
    swStatus?.availability === "AVAILABLE" &&
    swStatus.dataVersion !== null &&
    swStatus.dataVersion !== undefined &&
    swComponent?.availability === "AVAILABLE" &&
    swComponent.dataVersion === swStatus.dataVersion;
  const swMemberships = swVersionMatches
    ? (model.discoveryRecord?.memberships.filter(
        /** discovery 只选择 SW2021 三个层级，不混入 Eastmoney 分类。 */
        (membership) => membership.scheme.startsWith("SW2021_"),
      ) ?? [])
    : [];

  return (
    <Stack spacing={2}>
      <Card>
        <CardContent>
          <Typography variant="h6">Eastmoney 行业</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            固定 release 的当前观测归属，不等同于申万行业。
          </Typography>
          {model.statusQuery.isPending ? <Skeleton variant="rounded" height={96} /> : null}
          {model.statusQuery.isSuccess && model.industryStatus?.availability !== "AVAILABLE" ? (
            <DatasetUnavailable title="Eastmoney 行业" status={model.industryStatus} />
          ) : null}
          {model.industryStatus?.availability === "AVAILABLE" && model.industryQuery.isPending ? (
            <Skeleton variant="rounded" height={96} />
          ) : null}
          {model.industryQuery.isError ? (
            <DatasetError
              title="Eastmoney 行业"
              error={model.industryQuery.error}
              retry={() => void model.industryQuery.refetch()}
            />
          ) : null}
          {model.industry?.items.length === 0 ? (
            <Alert severity="info">该 release 确认没有行业归属。</Alert>
          ) : null}
          <Stack direction="row" spacing={1} useFlexGap flexWrap="wrap">
            {/* 行业标签保留供应商代码，避免同名 taxonomy 混淆。 */}
            {model.industry?.items.map((membership) => (
              <Chip
                key={membership.code}
                label={`${membership.name} · ${membership.code}`}
                color="primary"
                variant="outlined"
              />
            ))}
          </Stack>
          {model.industry !== undefined ? (
            <Typography variant="caption" color="text.secondary">
              dataVersion {model.industry.release.dataVersion} · publishedAt{" "}
              {model.industry.release.publishedAt}
            </Typography>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardContent>
          <Typography variant="h6">Eastmoney 概念</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            概念为供应商当前观察，不推断历史调入调出日。
          </Typography>
          {model.statusQuery.isPending ? <Skeleton variant="rounded" height={96} /> : null}
          {model.statusQuery.isSuccess && model.conceptStatus?.availability !== "AVAILABLE" ? (
            <DatasetUnavailable title="Eastmoney 概念" status={model.conceptStatus} />
          ) : null}
          {model.conceptStatus?.availability === "AVAILABLE" && model.conceptQuery.isPending ? (
            <Skeleton variant="rounded" height={96} />
          ) : null}
          {model.conceptQuery.isError ? (
            <DatasetError
              title="Eastmoney 概念"
              error={model.conceptQuery.error}
              retry={() => void model.conceptQuery.refetch()}
            />
          ) : null}
          {model.concepts?.items.length === 0 ? (
            <Alert severity="info">该 release 确认没有概念归属。</Alert>
          ) : null}
          <Stack direction="row" spacing={1} useFlexGap flexWrap="wrap">
            {/* 概念标签保留供应商代码并与行业分区展示。 */}
            {model.concepts?.items.map((membership) => (
              <Chip
                key={membership.code}
                label={`${membership.name} · ${membership.code}`}
                color="secondary"
                variant="outlined"
              />
            ))}
          </Stack>
        </CardContent>
      </Card>

      <Card>
        <CardContent>
          <Typography variant="h6">申万 2021 行业路径</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            只使用真实 SW membership publication；不回退到公司概况自由文本。
          </Typography>
          {model.statusQuery.isPending ? (
            <Skeleton variant="rounded" height={96} aria-label="正在读取申万行业状态" />
          ) : null}
          {model.statusQuery.isError ? (
            <DatasetError
              title="申万行业状态"
              error={model.statusQuery.error}
              retry={() => void model.statusQuery.refetch()}
            />
          ) : null}
          {model.statusQuery.isSuccess && swStatus?.availability !== "AVAILABLE" ? (
            <DatasetUnavailable title="申万行业归属" status={swStatus} />
          ) : null}
          {swStatus?.availability === "AVAILABLE" &&
          model.discovery?.availability === "AVAILABLE" &&
          !swVersionMatches ? (
            <Alert
              severity="warning"
              action={
                <Button
                  color="inherit"
                  size="small"
                  onClick={
                    /** 同时刷新状态和 discovery，避免单边重试继续引用不同 publication。 */
                    () => {
                      void Promise.all([
                        model.statusQuery.refetch(),
                        model.discoveryQuery.refetch(),
                      ]);
                    }
                  }
                >
                  重试
                </Button>
              }
            >
              申万状态与 discovery 组件版本不一致，暂不展示可能串版的归属。
            </Alert>
          ) : null}
          {swVersionMatches && swMemberships.length === 0 ? (
            model.discovery?.availability === "AVAILABLE" ? (
              <Alert severity="info">当前 SW publication 确认没有该证券归属。</Alert>
            ) : (
              <Alert severity="warning">
                SW 状态已发布，但 discovery 横截面不可用，当前不能确认证券归属。
              </Alert>
            )
          ) : null}
          <Stack divider={<Divider flexItem />}>
            {/* 按层级展示同一 discovery 行携带的申万路径。 */}
            {swMemberships
              .toSorted((left, right) => (left.level ?? 0) - (right.level ?? 0))
              .map((membership) => (
                <Stack
                  key={`${membership.scheme}:${membership.code}`}
                  direction="row"
                  justifyContent="space-between"
                  sx={{ py: 1.25 }}
                >
                  <Typography fontWeight={700}>
                    申万 {membership.level ?? "—"} 级 · {membership.name}
                  </Typography>
                  <Typography variant="body2" color="text.secondary">
                    {membership.code} · observedOn {membership.observedOn}
                  </Typography>
                </Stack>
              ))}
          </Stack>
        </CardContent>
      </Card>
    </Stack>
  );
}
