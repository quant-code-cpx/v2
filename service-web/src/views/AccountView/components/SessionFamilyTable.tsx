import { MonitorOutlined as MonitorOutlinedIcon } from "@mui/icons-material";
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  Skeleton,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Typography,
} from "@mui/material";

import type { SessionFamily } from "../../../types/account-security";
import type { useAccount } from "../hooks/useAccount";

/** 格式化 Session 活动与过期时间。 */
const sessionTimeFormatter = new Intl.DateTimeFormat("zh-CN", {
  dateStyle: "medium",
  timeStyle: "short",
});

/** 描述 Session family 表格消费的页面模型。 */
interface SessionFamilyTableProps {
  model: ReturnType<typeof useAccount>;
}

/** 渲染本人活动 Session family，不展示 IP、设备、UA 或位置。 */
export function SessionFamilyTable({ model }: SessionFamilyTableProps) {
  const sessionPage = model.sessionQuery.data;

  return (
    <Card component="section" aria-labelledby="session-family-title">
      <CardContent>
        <Stack direction="row" justifyContent="space-between" alignItems="flex-start">
          <Box>
            <Typography id="session-family-title" component="h2" variant="h5">
              活动会话
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
              不保存设备、IP 或位置；以最近活动时间和随机会话标识区分。
            </Typography>
          </Box>
          <Stack direction="row" spacing={1}>
            <Button
              onClick={() => void model.refreshSessions()}
              disabled={model.sessionQuery.isFetching}
            >
              刷新
            </Button>
            <Button
              variant="outlined"
              disabled={
                !model.canRevokeSessions ||
                sessionPage === undefined ||
                sessionPage.items.every((family) => family.current)
              }
              onClick={() => model.openDialog({ kind: "revoke-others" })}
            >
              退出其他会话
            </Button>
          </Stack>
        </Stack>
      </CardContent>

      {!model.canReadSessions ? (
        <Alert severity="warning" sx={{ mx: 3, mb: 3 }}>
          当前身份缺少 sessions:read 权限，未请求会话数据。
        </Alert>
      ) : null}
      {model.sessionQuery.isPending ? <SessionTableSkeleton /> : null}
      {model.sessionQuery.isError && sessionPage === undefined ? (
        <Alert
          severity="error"
          sx={{ mx: 3, mb: 3 }}
          action={
            <Button color="inherit" size="small" onClick={() => void model.refreshSessions()}>
              重试
            </Button>
          }
        >
          活动会话暂时不可用。
        </Alert>
      ) : null}
      {sessionPage !== undefined &&
      sessionPage.items.length === 1 &&
      sessionPage.items[0]?.current ? (
        <Alert severity="success" sx={{ mx: 3, mb: 2 }}>
          当前只有这个会话，无需退出其他会话。
        </Alert>
      ) : null}
      {sessionPage === undefined ? null : (
        <TableContainer tabIndex={0} aria-label="活动会话表格，可横向滚动">
          <Table aria-label="活动会话">
            <TableHead>
              <TableRow>
                <TableCell scope="col">会话</TableCell>
                <TableCell scope="col">最近活动</TableCell>
                <TableCell scope="col">绝对过期</TableCell>
                <TableCell scope="col">状态</TableCell>
                <TableCell scope="col" align="right">
                  操作
                </TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {sessionPage.items.map((family) => (
                <SessionFamilyRow key={family.familyId} family={family} model={model} />
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}
    </Card>
  );
}

/** 描述一个 Session family 表格行。 */
interface SessionFamilyRowProps {
  family: SessionFamily;
  model: ReturnType<typeof useAccount>;
}

/** 渲染随机 family 后四位与允许动作。 */
function SessionFamilyRow({ family, model }: SessionFamilyRowProps) {
  return (
    <TableRow hover>
      <TableCell>
        <Stack direction="row" spacing={1.25} alignItems="center">
          <MonitorOutlinedIcon color="primary" fontSize="small" />
          <Box>
            <Typography variant="subtitle2">
              会话 {family.familyId.slice(-4).toUpperCase()}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              随机 family 标识
            </Typography>
          </Box>
        </Stack>
      </TableCell>
      <TableCell>{sessionTimeFormatter.format(new Date(family.lastActiveAt))}</TableCell>
      <TableCell>{sessionTimeFormatter.format(new Date(family.absoluteExpiresAt))}</TableCell>
      <TableCell>
        <Chip
          color={family.current ? "success" : "info"}
          label={family.current ? "● 当前会话" : "活动"}
        />
      </TableCell>
      <TableCell align="right">
        {family.current ? (
          <Button disabled>当前使用中</Button>
        ) : (
          <Button
            variant="outlined"
            disabled={!model.canRevokeSessions}
            onClick={() => model.openDialog({ kind: "revoke-session", familyId: family.familyId })}
          >
            退出会话
          </Button>
        )}
      </TableCell>
    </TableRow>
  );
}

/** Session 初次加载时保留表头与三行几何。 */
function SessionTableSkeleton() {
  return (
    <Stack spacing={1.5} sx={{ px: 3, pb: 3 }} aria-label="正在加载活动会话">
      <Skeleton variant="rounded" height={56} />
      <Skeleton variant="rounded" height={76} />
      <Skeleton variant="rounded" height={76} />
    </Stack>
  );
}
