import { CloseOutlined as CloseOutlinedIcon } from "@mui/icons-material";
import {
  Alert,
  Box,
  Chip,
  Divider,
  Drawer,
  IconButton,
  Skeleton,
  Stack,
  Typography,
} from "@mui/material";

import type { useAuditEvents } from "../hooks/useAuditEvents";
import { auditCategoryLabel, auditDetailEntries } from "../utils/audit-event-presentation";

/** 格式化审计详情发生时间。 */
const detailTimeFormatter = new Intl.DateTimeFormat("zh-CN", {
  dateStyle: "medium",
  timeStyle: "medium",
});

/** 描述详情 Drawer 消费的页面模型。 */
interface AuditEventDrawerProps {
  model: ReturnType<typeof useAuditEvents>;
}

/** 渲染仅含合同 allowlist 字段的审计详情 Drawer。 */
export function AuditEventDrawer({ model }: AuditEventDrawerProps) {
  const open = model.urlState.eventId !== undefined;
  const detail = model.detailQuery.data;

  return (
    <Drawer
      anchor="right"
      open={open}
      onClose={model.closeEvent}
      ModalProps={{ keepMounted: false }}
      PaperProps={{ "aria-labelledby": "audit-detail-title" }}
    >
      <Stack direction="row" justifyContent="space-between" alignItems="flex-start" sx={{ p: 3 }}>
        <Box>
          <Typography id="audit-detail-title" component="h2" variant="h5">
            审计事件详情
          </Typography>
          {detail === undefined ? null : (
            <Chip label={auditCategoryLabel(detail.category)} color="warning" sx={{ mt: 1 }} />
          )}
        </Box>
        <IconButton aria-label="关闭审计详情" onClick={model.closeEvent}>
          <CloseOutlinedIcon />
        </IconButton>
      </Stack>
      <Divider />
      <Box sx={{ p: 3, overflowY: "auto" }}>
        {model.detailQuery.isPending ? (
          <Stack spacing={2} aria-label="正在加载审计详情">
            <Skeleton variant="text" width="72%" />
            <Skeleton variant="rounded" height={64} />
            <Skeleton variant="rounded" height={64} />
          </Stack>
        ) : null}
        {model.detailQuery.isError ? (
          <Alert severity="error">审计详情暂时不可用，请关闭后重试。</Alert>
        ) : null}
        {detail === undefined ? null : (
          <Stack component="dl" spacing={0} sx={{ m: 0 }}>
            <DetailEntry label="事件" value={detail.summary} />
            <DetailEntry label="Action" value={detail.action} mono />
            <DetailEntry
              label="时间"
              value={detailTimeFormatter.format(new Date(detail.occurredAt))}
            />
            <DetailEntry
              label="Actor"
              value={detail.actor === null ? "系统" : detail.actor.displayName}
            />
            <DetailEntry
              label="目标"
              value={
                detail.target.id === null
                  ? detail.target.type
                  : `${detail.target.type} · ${maskIdentifier(detail.target.id)}`
              }
            />
            <DetailEntry
              label="Request ID"
              value={detail.requestId === null ? "—" : maskIdentifier(detail.requestId)}
              mono
            />
            {auditDetailEntries(detail).map(([label, value]) => (
              <DetailEntry key={label} label={label} value={value} />
            ))}
          </Stack>
        )}
      </Box>
    </Drawer>
  );
}

/** 描述 Drawer 内一个净化后的键值。 */
interface DetailEntryProps {
  label: string;
  value: string;
  mono?: boolean;
}

/** 渲染详情定义列表的一行。 */
function DetailEntry({ label, value, mono = false }: DetailEntryProps) {
  return (
    <Box sx={{ py: 2, borderBottom: 1, borderColor: "divider" }}>
      <Typography component="dt" variant="caption" color="text.secondary">
        {label}
      </Typography>
      <Typography
        component="dd"
        variant="body2"
        fontWeight={600}
        sx={{
          m: 0,
          mt: 0.5,
          overflowWrap: "anywhere",
          ...(mono ? { fontFamily: "monospace" } : {}),
        }}
      >
        {value}
      </Typography>
    </Box>
  );
}

/** 只显示标识开头与末尾，减少截图或肩窥泄露。 */
function maskIdentifier(identifier: string): string {
  return identifier.length <= 12 ? identifier : `${identifier.slice(0, 6)}…${identifier.slice(-4)}`;
}
