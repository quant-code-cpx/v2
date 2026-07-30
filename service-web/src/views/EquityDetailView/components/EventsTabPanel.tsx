import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  Divider,
  Skeleton,
  Stack,
  Typography,
} from "@mui/material";

import type { EquityEventFamily } from "../../../types/equity-market";
import type { EquityDetailModel } from "../hooks/useEquityDetail";
import { DatasetError } from "./DatasetStates";

/** 统一事件页签支持的数据状态 family。 */
const eventFamilies: readonly EquityEventFamily[] = [
  "CORPORATE_ACTION",
  "EARNINGS_FORECAST",
  "EARNINGS_EXPRESS",
  "DRAGON_TIGER",
  "BLOCK_TRADE",
];

/** 判断数据状态 family 是否属于统一证券事件。 */
function isEventFamily(family: string): family is EquityEventFamily {
  return eventFamilies.some(
    /** family 只按公开稳定枚举精确匹配。 */
    (candidate) => candidate === family,
  );
}

/** 对已筛选的数据状态 family 返回安全标签。 */
function datasetEventFamilyLabel(family: string): string {
  return isEventFamily(family) ? eventFamilyLabel(family) : family;
}

/** 返回统一证券事件族的稳定中文标签。 */
function eventFamilyLabel(family: EquityEventFamily): string {
  if (family === "CORPORATE_ACTION") return "公司行动";
  if (family === "EARNINGS_FORECAST") return "业绩预告";
  if (family === "EARNINGS_EXPRESS") return "业绩快报";
  if (family === "DRAGON_TIGER") return "龙虎榜";
  return "大宗交易";
}

/** 为结构化事件事实保留原始数值、区间、单位与文本语义。 */
function eventFactValue(fact: {
  value?: string | null;
  valueLow?: string | null;
  valueHigh?: string | null;
  unit?: string | null;
  currency?: string | null;
  text?: string | null;
}): string {
  if (fact.text !== null && fact.text !== undefined) return fact.text;
  if (fact.value !== null && fact.value !== undefined) {
    return `${fact.value}${fact.unit === null || fact.unit === undefined ? "" : ` ${fact.unit}`}${
      fact.currency === null || fact.currency === undefined ? "" : ` ${fact.currency}`
    }`;
  }
  if (
    fact.valueLow !== null &&
    fact.valueLow !== undefined &&
    fact.valueHigh !== null &&
    fact.valueHigh !== undefined
  ) {
    return `${fact.valueLow} – ${fact.valueHigh}${
      fact.unit === null || fact.unit === undefined ? "" : ` ${fact.unit}`
    }`;
  }
  return "—";
}

/** 渲染五类真实证券事件，并保持事件 publication 的 cursor 一致性。 */
export function EventsTabPanel({ model }: { model: EquityDetailModel }) {
  const eventStatuses =
    model.status?.datasets.filter(
      /** 事件状态只读取五个公开 family。 */
      (status) => isEventFamily(status.family),
    ) ?? [];
  const unavailableFamilies = eventStatuses
    .filter(
      /** AVAILABLE 和合法 EMPTY 不需要不可用告警。 */
      (status) => !["AVAILABLE", "EMPTY"].includes(status.availability),
    )
    .map(
      /** 对用户显示中文 family，同时保留状态表中的稳定代码。 */
      (status) => datasetEventFamilyLabel(status.family),
    );
  const staleFamilies = eventStatuses
    .filter(
      /** 陈旧与 availability 独立，不把 last-good 误判为不可用。 */
      (status) => status.freshness === "STALE",
    )
    .map(
      /** 陈旧提醒按公开 family 展示。 */
      (status) => datasetEventFamilyLabel(status.family),
    );

  if (model.eventsQuery.isPending) {
    return <Skeleton variant="rounded" height={420} aria-label="正在加载证券事件" />;
  }

  if (model.eventsQuery.isError) {
    return (
      <DatasetError
        title="证券事件"
        error={model.eventsQuery.error}
        retry={() => void model.eventsQuery.refetch()}
      />
    );
  }

  if (model.events?.availability === "UNAVAILABLE") {
    return (
      <Alert
        severity="warning"
        action={
          <Button color="inherit" size="small" onClick={() => void model.eventsQuery.refetch()}>
            重新检查
          </Button>
        }
      >
        证券事件尚无合格 publication（{model.events.reasonCode ?? "NO_PUBLICATION"}）。
        页面不会以公司行动或其他数据集推断缺失事件。
      </Alert>
    );
  }

  return (
    <Card>
      <CardContent>
        <Stack direction="row" justifyContent="space-between" alignItems="flex-start" spacing={2}>
          <Box>
            <Typography variant="h6">证券事件</Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
              公司行动、业绩预告、业绩快报、龙虎榜与大宗交易统一排序；各事实保留供应商口径。
              查询窗口 {model.eventWindow.start} 至 {model.eventWindow.end}。
            </Typography>
          </Box>
          {model.events?.release !== null && model.events?.release !== undefined ? (
            <Stack alignItems="flex-end">
              <Chip
                size="small"
                variant="outlined"
                label={`有效日 ${model.events.release.effectiveAsOf ?? "未知"}`}
              />
              <Typography variant="caption" color="text.secondary" sx={{ mt: 0.5 }}>
                {model.events.release.dataVersion}
              </Typography>
            </Stack>
          ) : null}
        </Stack>

        {unavailableFamilies.length > 0 ? (
          <Alert severity="warning" sx={{ mt: 2 }}>
            以下事件族当前独立不可用：{unavailableFamilies.join("、")}。其余已发布事件仍正常展示。
          </Alert>
        ) : null}
        {staleFamilies.length > 0 ? (
          <Alert severity="info" sx={{ mt: 2 }}>
            以下事件族正在展示 last-good publication：{staleFamilies.join("、")}。
          </Alert>
        ) : null}
        {model.eventsQuery.isPlaceholderData ? (
          <Alert severity="info" sx={{ mt: 2 }}>
            新 cursor 页正在读取；当前事件仍属于上一页，完成前不会视为第 {model.eventPage} 页结果。
          </Alert>
        ) : null}

        {model.events?.events.length === 0 ? (
          <Alert severity="info" sx={{ mt: 2 }}>
            当前日期窗口内没有已发布证券事件；这是 publication 内的合法空结果。
          </Alert>
        ) : null}

        <Stack divider={<Divider flexItem />} sx={{ mt: 1.5 }}>
          {/* 事件顺序完全沿用服务端 publication，不在浏览器按缺失日期二次排序。 */}
          {model.events?.events.map((event) => (
            <Box key={event.eventRef} sx={{ py: 2 }}>
              <Stack direction="row" justifyContent="space-between" spacing={2}>
                <Box sx={{ minWidth: 0 }}>
                  <Stack direction="row" alignItems="center" spacing={1}>
                    <Chip
                      size="small"
                      color="primary"
                      variant="outlined"
                      label={eventFamilyLabel(event.family)}
                    />
                    <Typography variant="subtitle1" fontWeight={700} noWrap>
                      {event.title ?? event.kind}
                    </Typography>
                  </Stack>
                  <Typography variant="caption" color="text.secondary">
                    {event.kind}
                    {event.stage === null || event.stage === undefined ? "" : ` · ${event.stage}`}
                    {event.status === null || event.status === undefined
                      ? ""
                      : ` · ${event.status}`}
                  </Typography>
                </Box>
                <Stack alignItems="flex-end" sx={{ flexShrink: 0 }}>
                  <Typography variant="body2">
                    {event.occurredOn ?? event.announcedOn ?? event.reportPeriod ?? "日期未提供"}
                  </Typography>
                  <Typography variant="caption" color="text.secondary">
                    {event.sourceLabel ?? "来源标签未提供"}
                  </Typography>
                </Stack>
              </Stack>
              {event.facts.length > 0 ? (
                <Box
                  component="dl"
                  sx={{
                    display: "grid",
                    gridTemplateColumns: "repeat(3, minmax(0, 1fr))",
                    gap: 1,
                    mt: 1.5,
                    mb: 0,
                    "& div": { bgcolor: "grey.100", borderRadius: 1, px: 1.5, py: 1 },
                    "& dt": { color: "text.secondary", fontSize: 12 },
                    "& dd": { m: 0, mt: 0.5, overflowWrap: "anywhere" },
                  }}
                >
                  {/* facts 是供应商结构化事实，不把未知 code 翻译成臆测业务含义。 */}
                  {event.facts.map((fact, index) => (
                    <div key={`${event.eventRef}:${fact.code}:${index}`}>
                      <dt>{fact.code}</dt>
                      <dd>{eventFactValue(fact)}</dd>
                    </div>
                  ))}
                </Box>
              ) : null}
              <Typography variant="caption" color="text.disabled">
                eventRef {event.eventRef} · dataVersion {event.dataVersion}
              </Typography>
            </Box>
          ))}
        </Stack>

        <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mt: 2 }}>
          <Typography variant="body2" color="text.secondary">
            第 {model.eventPage} 页 · 每页最多 {model.events?.page.limit ?? 50} 条
          </Typography>
          <Stack direction="row" spacing={1}>
            <Button variant="outlined" onClick={model.firstEvents} disabled={model.eventPage === 1}>
              第一页
            </Button>
            <Button
              variant="contained"
              onClick={model.nextEvents}
              disabled={
                model.events?.page.nextCursor === null ||
                model.eventsQuery.isPlaceholderData ||
                model.eventsQuery.isFetching
              }
            >
              下一页
            </Button>
          </Stack>
        </Stack>
      </CardContent>
    </Card>
  );
}
