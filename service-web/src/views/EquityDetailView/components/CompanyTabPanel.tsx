import { Alert, Box, Card, CardContent, Divider, Skeleton, Stack, Typography } from "@mui/material";

import type { EquityDetailModel } from "../hooks/useEquityDetail";
import { listingStatusLabel } from "../../EquityMarketView/utils/equity-market-formatters";
import { DatasetError, DatasetStaleNotice, DatasetUnavailable } from "./DatasetStates";

/** 渲染公司概况、当前双时态身份和上市生命周期历史。 */
export function CompanyTabPanel({ model }: { model: EquityDetailModel }) {
  return (
    <Stack spacing={2}>
      <Card>
        <CardContent>
          <Typography variant="h6">公司概况</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5, mb: 2 }}>
            来源自由文本行业只在公司资料中展示，不替代行业 taxonomy。
          </Typography>
          {model.statusQuery.isPending ? <Skeleton variant="rounded" height={260} /> : null}
          {model.statusQuery.isError ? (
            <DatasetError
              title="公司概况状态"
              error={model.statusQuery.error}
              retry={() => void model.statusQuery.refetch()}
            />
          ) : null}
          {model.statusQuery.isSuccess && model.profileStatus?.availability !== "AVAILABLE" ? (
            <DatasetUnavailable title="公司概况" status={model.profileStatus} />
          ) : null}
          {model.profileStatus?.freshness === "STALE" ? (
            <DatasetStaleNotice status={model.profileStatus} />
          ) : null}
          {model.profileStatus?.availability === "AVAILABLE" && model.profileQuery.isPending ? (
            <Skeleton variant="rounded" height={260} />
          ) : null}
          {model.profileQuery.isError ? (
            <DatasetError
              title="公司概况"
              error={model.profileQuery.error}
              retry={() => void model.profileQuery.refetch()}
            />
          ) : null}
          {model.profile !== undefined ? (
            <Stack spacing={2}>
              <Box
                component="dl"
                sx={{
                  display: "grid",
                  gridTemplateColumns: "repeat(4, minmax(0, 1fr))",
                  gap: 2,
                  m: 0,
                  "& dt": { color: "text.secondary", fontSize: 13 },
                  "& dd": {
                    m: 0.5,
                    ml: 0,
                    fontWeight: 600,
                    overflowWrap: "anywhere",
                  },
                }}
              >
                <div>
                  <dt>公司全称</dt>
                  <dd>{model.profile.profile.companyName}</dd>
                </div>
                <div>
                  <dt>英文名称</dt>
                  <dd>{model.profile.profile.englishName ?? "—"}</dd>
                </div>
                <div>
                  <dt>法定代表人</dt>
                  <dd>{model.profile.profile.legalRepresentative ?? "—"}</dd>
                </div>
                <div>
                  <dt>成立日期</dt>
                  <dd>{model.profile.profile.establishedOn ?? "—"}</dd>
                </div>
                <div>
                  <dt>来源行业文本</dt>
                  <dd>{model.profile.profile.industry ?? "—"}</dd>
                </div>
                <div>
                  <dt>电话</dt>
                  <dd>{model.profile.profile.phone ?? "—"}</dd>
                </div>
                <div>
                  <dt>邮箱</dt>
                  <dd>{model.profile.profile.email ?? "—"}</dd>
                </div>
                <div>
                  <dt>网站</dt>
                  <dd>{model.profile.profile.website ?? "—"}</dd>
                </div>
              </Box>
              <Divider />
              <Box>
                <Typography variant="subtitle2">主营业务</Typography>
                <Typography sx={{ mt: 0.75 }}>
                  {model.profile.profile.mainBusiness ?? "当前 publication 未提供主营业务。"}
                </Typography>
              </Box>
              <Box>
                <Typography variant="subtitle2">公司简介</Typography>
                <Typography sx={{ mt: 0.75 }}>
                  {model.profile.profile.summary ?? "当前 publication 未提供公司简介。"}
                </Typography>
              </Box>
              <Typography variant="caption" color="text.secondary">
                身份日期 {model.profile.identityAsOf} · dataVersion {model.profile.dataVersion} ·
                publishedAt {model.profile.publishedAt}
              </Typography>
            </Stack>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardContent>
          <Typography variant="h6">上市生命周期</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5, mb: 2 }}>
            “暂停上市”属于生命周期；普通交易停牌由独立日状态表达。
          </Typography>
          {model.listingHistoryQuery.isPending ? <Skeleton variant="rounded" height={160} /> : null}
          {model.listingHistoryQuery.isError ? (
            <DatasetError
              title="上市生命周期"
              error={model.listingHistoryQuery.error}
              retry={() => void model.listingHistoryQuery.refetch()}
            />
          ) : null}
          {model.listingHistory?.items.length === 0 ? (
            <Alert severity="info">当前 publication 没有可展示的生命周期历史。</Alert>
          ) : null}
          <Stack divider={<Divider flexItem />}>
            {/* 生命周期按 API 稳定顺序展示，不在浏览器重排双时态事实。 */}
            {model.listingHistory?.items.map((period) => (
              <Stack
                key={`${period.status}:${period.effectiveFrom}:${period.knownFrom}`}
                direction="row"
                justifyContent="space-between"
                sx={{ py: 1.5 }}
              >
                <Typography fontWeight={700}>{listingStatusLabel(period.status)}</Typography>
                <Typography variant="body2">
                  有效期 {period.effectiveFrom} 至 {period.effectiveTo ?? "当前"} ·{" "}
                  {period.effectiveDatePrecision === "OFFICIAL_DATE" ? "官方日期" : "观测日期"}
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  knownFrom {period.knownFrom}
                </Typography>
              </Stack>
            ))}
          </Stack>
        </CardContent>
      </Card>
    </Stack>
  );
}
