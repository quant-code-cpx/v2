import {
  Alert,
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Skeleton,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import { alpha } from "@mui/material/styles";

import { userRoleLabel, userStatusLabel } from "../../../utils/user-presentation";
import { useUserActionDialog } from "../hooks/useUserActionDialog";

/** 描述一个聚焦的删除或密码重置动作。 */
interface UserActionDialogProps {
  kind: "delete" | "reset-password";
  userId: string;
  onClose: () => void;
}

/** 渲染 ETag 感知的删除或密码重置 Dialog。 */
export function UserActionDialog({ kind, userId, onClose }: UserActionDialogProps) {
  const model = useUserActionDialog({ kind, userId, onClose });
  const targetName = model.target?.displayName;

  return (
    <Dialog
      open
      onClose={model.handleClose}
      fullWidth
      maxWidth={false}
      PaperProps={{ sx: { width: 720 } }}
      aria-labelledby="user-action-title"
    >
      <Box component="form" noValidate onSubmit={model.handleSubmit} onBlur={model.handleFormBlur}>
        <DialogTitle id="user-action-title">
          {kind === "delete"
            ? targetName === undefined
              ? "删除用户"
              : `删除用户“${targetName}”？`
            : "重置密码"}
        </DialogTitle>
        <DialogContent>
          {model.isLoading ? <ActionSkeleton /> : null}
          {model.cannotLoad ? (
            <Alert severity="error">用户信息暂时不可用，请关闭后重试。</Alert>
          ) : null}
          {!model.isLoading && !model.cannotLoad ? (
            <Stack spacing={2.5} sx={{ pt: 0.5 }}>
              {model.formError === null ? null : <Alert severity="error">{model.formError}</Alert>}
              {kind === "delete" ? (
                <>
                  <Typography color="text.secondary">
                    管理员可删除普通用户；超级管理员可删除管理员和普通用户。删除后账号将立即无法登录，审计记录会保留。
                  </Typography>
                  <Box
                    sx={(theme) => ({
                      p: 2,
                      borderRadius: 1,
                      bgcolor: alpha(theme.palette.error.main, 0.08),
                      color: "error.dark",
                    })}
                  >
                    <Typography variant="subtitle2" color="inherit">
                      {model.target?.account ?? "—"}
                    </Typography>
                    <Typography variant="body2" color="inherit" sx={{ mt: 0.5 }}>
                      {model.target === undefined
                        ? "正在确认账号状态"
                        : `${userRoleLabel(model.target.role)} · 当前${userStatusLabel(
                            model.target.status,
                          )}`}
                    </Typography>
                  </Box>
                </>
              ) : (
                <>
                  <Typography color="text.secondary">
                    为“{targetName}”设置新密码。提交后请通过批准的站外渠道交付。
                  </Typography>
                  <TextField
                    label="新密码"
                    type="password"
                    value={model.password}
                    onChange={model.handlePasswordChange}
                    autoComplete="new-password"
                    required
                    fullWidth
                    error={model.passwordError !== undefined}
                    helperText={model.passwordError ?? "至少 12 位，且需包含数字。"}
                    slotProps={{ htmlInput: { maxLength: 512 } }}
                  />
                </>
              )}
            </Stack>
          ) : null}
        </DialogContent>
        <DialogActions>
          <Button onClick={model.handleClose}>取消</Button>
          <Button
            type="submit"
            variant="contained"
            color={kind === "delete" ? "error" : "primary"}
            disabled={model.isSubmitting || model.isLoading || model.cannotLoad}
          >
            {model.isSubmitting ? "正在提交" : kind === "delete" ? "确认删除" : "确认重置"}
          </Button>
        </DialogActions>
      </Box>
    </Dialog>
  );
}

/** 详情与 ETag 加载时保留确认内容几何。 */
function ActionSkeleton() {
  return (
    <Stack spacing={2} sx={{ pt: 0.5 }} aria-label="正在加载用户信息">
      <Skeleton variant="text" width="72%" />
      <Skeleton variant="rounded" height={56} />
    </Stack>
  );
}
