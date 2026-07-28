import { AssignmentOutlined as AssignmentOutlinedIcon } from "@mui/icons-material";
import { Alert, Button, Card, CardContent, Skeleton, Stack, Typography } from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link as RouterLink } from "react-router-dom";

import { auditEventListQueryOptions } from "../../../api/audit-events";
import type { AuditEventListInput } from "../../../types/account-security";
import { MetricIcon } from "./AccountStatusCard";

/** 构造工作台最近七天重要审计请求，并在挂载周期内冻结时间窗。 */
function createRecentAuditInput(): AuditEventListInput {
  const occurredTo = new Date();
  const occurredFrom = new Date(occurredTo.getTime() - 7 * 24 * 60 * 60 * 1_000);

  return {
    occurredFrom: occurredFrom.toISOString(),
    occurredTo: occurredTo.toISOString(),
    includeRoutine: false,
    pageSize: 4,
  };
}

/** 渲染重要审计指标；只有 audit:read 页面分支才会挂载本组件。 */
export function RecentAuditMetricCard() {
  const [input] = useState(createRecentAuditInput);
  const query = useQuery(auditEventListQueryOptions(input));

  return (
    <Card>
      <CardContent sx={{ minHeight: 152 }}>
        <Stack direction="row" justifyContent="space-between">
          <Typography variant="subtitle2" color="text.secondary">
            重要审计
          </Typography>
          <MetricIcon>
            <AssignmentOutlinedIcon fontSize="small" />
          </MetricIcon>
        </Stack>
        {query.isPending ? <Skeleton variant="text" width="60%" height={48} /> : null}
        {query.isError && query.data === undefined ? (
          <Alert severity="warning" sx={{ mt: 1 }}>
            审计摘要暂不可用
          </Alert>
        ) : null}
        {query.data === undefined ? null : (
          <>
            <Typography variant="h3" sx={{ mt: 1 }}>
              {query.data.items.length}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              最近 7 天 · 不含例行 Token 轮换
            </Typography>
          </>
        )}
      </CardContent>
    </Card>
  );
}

/** 渲染最近重要审计列表，并把失败限制在本卡片。 */
export function RecentAuditEventsCard() {
  const [input] = useState(createRecentAuditInput);
  const query = useQuery(auditEventListQueryOptions(input));

  return (
    <Card component="section" aria-labelledby="recent-audit-title">
      <CardContent>
        <Stack direction="row" justifyContent="space-between" alignItems="center">
          <div>
            <Typography id="recent-audit-title" component="h2" variant="h5">
              近期重要操作
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
              SUPER_ADMIN 可见的脱敏审计摘要。
            </Typography>
          </div>
          <Button component={RouterLink} to="/security/audit">
            查看全部
          </Button>
        </Stack>
        {query.isPending ? (
          <Stack spacing={1.5} sx={{ mt: 2 }} aria-label="正在加载近期审计">
            <Skeleton variant="rounded" height={52} />
            <Skeleton variant="rounded" height={52} />
            <Skeleton variant="rounded" height={52} />
          </Stack>
        ) : null}
        {query.isError && query.data === undefined ? (
          <Alert severity="warning" sx={{ mt: 2 }}>
            近期审计暂时不可用，其他工作台区块保持可用。
          </Alert>
        ) : null}
        {query.data?.items.length === 0 ? (
          <Alert severity="success" sx={{ mt: 2 }}>
            最近七天没有重要审计事件。
          </Alert>
        ) : null}
        <Stack divider={<div />} sx={{ mt: query.data === undefined ? 0 : 1 }}>
          {query.data?.items.slice(0, 4).map((event) => (
            <Stack
              key={event.id}
              direction="row"
              justifyContent="space-between"
              alignItems="center"
              sx={{ py: 1.5, borderBottom: 1, borderColor: "divider" }}
            >
              <div>
                <Typography variant="subtitle2">{event.summary}</Typography>
                <Typography variant="caption" color="text.secondary">
                  {event.actor?.displayName ?? "系统"} ·{" "}
                  {new Intl.DateTimeFormat("zh-CN", {
                    month: "2-digit",
                    day: "2-digit",
                    hour: "2-digit",
                    minute: "2-digit",
                  }).format(new Date(event.occurredAt))}
                </Typography>
              </div>
              <Typography variant="caption" color="primary.dark">
                {event.category}
              </Typography>
            </Stack>
          ))}
        </Stack>
      </CardContent>
    </Card>
  );
}
