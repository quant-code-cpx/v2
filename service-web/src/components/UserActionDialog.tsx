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
import { useCallback, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { FocusEvent, FormEvent, ChangeEvent } from "react";

import { deleteUser, resetUserPassword, userDetailQueryOptions } from "../api/users";
import { isApiError } from "../api/http";
import { useFeedback } from "./FeedbackProvider";
import { validateManagedPassword } from "../utils/user-form-validation";
import { userRoleLabel, userStatusLabel } from "../utils/user-presentation";

/** Describe a focused destructive or password-reset dialog action. */
interface UserActionDialogProps {
  kind: "delete" | "reset-password";
  userId: string;
  onClose: () => void;
}

/** Render an ETag-aware delete or reset confirmation as a Dialog, never a Drawer. */
export function UserActionDialog({ kind, userId, onClose }: UserActionDialogProps) {
  const queryClient = useQueryClient();
  const { error: showError, success } = useFeedback();
  const detailQuery = useQuery(userDetailQueryOptions(userId));
  const [password, setPassword] = useState("");
  const [passwordError, setPasswordError] = useState<string | undefined>(undefined);
  const [formError, setFormError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  /** Erase a reset password from component state on every terminal interaction. */
  const clearPassword = useCallback(() => {
    setPassword("");
    setPasswordError(undefined);
  }, []);

  /** Close this URL-owned action dialog after clearing its transient password. */
  const handleClose = useCallback(() => {
    clearPassword();
    setFormError(null);
    onClose();
  }, [clearPassword, onClose]);

  /** Clear password when keyboard focus leaves the complete confirmation form. */
  const handleFormBlur = useCallback(
    (event: FocusEvent<HTMLFormElement>) => {
      if (!event.currentTarget.contains(event.relatedTarget)) {
        clearPassword();
      }
    },
    [clearPassword],
  );

  /** Update reset password only in local component state. */
  const handlePasswordChange = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    setPassword(event.target.value);
    setPasswordError(undefined);
    setFormError(null);
  }, []);

  /** Invalidate list/detail cache after a successful terminal action. */
  const invalidateUsers = useCallback(async () => {
    await queryClient.invalidateQueries({ queryKey: ["users", "list"] });
    queryClient.removeQueries({ queryKey: ["users", "detail", userId] });
  }, [queryClient, userId]);

  /** Map concurrent or inaccessible action failures to safe recovery feedback. */
  const handleActionError = useCallback(
    async (error: unknown) => {
      if (isApiError(error) && error.status === 412) {
        setFormError("用户信息已被更新，请重新确认后操作。");
        await queryClient.invalidateQueries({ queryKey: ["users", "detail", userId] });
        return;
      }
      if (isApiError(error) && (error.status === 403 || error.status === 404)) {
        showError("权限或用户状态已变化。");
        handleClose();
        return;
      }

      setFormError(kind === "delete" ? "删除失败，请稍后重试。" : "重置失败，请稍后重试。");
    },
    [handleClose, kind, queryClient, showError, userId],
  );

  /** Execute one ETag-protected action and remove sensitive password state in every outcome. */
  const handleSubmit = useCallback(
    async (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      const detail = detailQuery.data;

      if (detail === undefined) {
        return;
      }

      if (kind === "reset-password") {
        const validationError = validateManagedPassword(password);
        if (validationError !== undefined) {
          clearPassword();
          setPasswordError(validationError);
          return;
        }
      }

      setIsSubmitting(true);
      setFormError(null);
      try {
        if (kind === "delete") {
          await deleteUser(userId, detail.etag);
        } else {
          await resetUserPassword(userId, password, detail.etag);
        }
        await invalidateUsers();
        success(kind === "delete" ? "用户已删除。" : "密码已重置。");
        handleClose();
      } catch (error: unknown) {
        await handleActionError(error);
      } finally {
        clearPassword();
        setIsSubmitting(false);
      }
    },
    [
      clearPassword,
      detailQuery.data,
      handleActionError,
      handleClose,
      invalidateUsers,
      kind,
      password,
      success,
      userId,
    ],
  );

  const isLoading = detailQuery.isPending;
  const cannotLoad = detailQuery.isError;
  const target = detailQuery.data?.user;
  const targetName = target?.displayName;

  return (
    <Dialog
      open
      onClose={handleClose}
      fullWidth
      maxWidth={false}
      PaperProps={{ sx: { width: 720 } }}
      aria-labelledby="user-action-title"
    >
      <Box component="form" noValidate onSubmit={handleSubmit} onBlur={handleFormBlur}>
        <DialogTitle id="user-action-title">
          {kind === "delete"
            ? targetName === undefined
              ? "删除用户"
              : `删除用户“${targetName}”？`
            : "重置密码"}
        </DialogTitle>
        <DialogContent>
          {isLoading ? <ActionSkeleton /> : null}
          {cannotLoad ? <Alert severity="error">用户信息暂时不可用，请关闭后重试。</Alert> : null}
          {!isLoading && !cannotLoad ? (
            <Stack spacing={2.5} sx={{ pt: 0.5 }}>
              {formError === null ? null : <Alert severity="error">{formError}</Alert>}
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
                      {target?.account ?? "—"}
                    </Typography>
                    <Typography variant="body2" color="inherit" sx={{ mt: 0.5 }}>
                      {target === undefined
                        ? "正在确认账号状态"
                        : `${userRoleLabel(target.role)} · 当前${userStatusLabel(target.status)}`}
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
                    value={password}
                    onChange={handlePasswordChange}
                    autoComplete="new-password"
                    required
                    fullWidth
                    error={passwordError !== undefined}
                    helperText={passwordError ?? "至少 12 位，且需包含数字。"}
                    slotProps={{ htmlInput: { maxLength: 512 } }}
                  />
                </>
              )}
            </Stack>
          ) : null}
        </DialogContent>
        <DialogActions>
          <Button onClick={handleClose}>取消</Button>
          <Button
            type="submit"
            variant="contained"
            color={kind === "delete" ? "error" : "primary"}
            disabled={isSubmitting || isLoading || cannotLoad}
          >
            {isSubmitting ? "正在提交" : kind === "delete" ? "确认删除" : "确认重置"}
          </Button>
        </DialogActions>
      </Box>
    </Dialog>
  );
}

/** Reserve confirmation content geometry while detail and ETag are loading. */
function ActionSkeleton() {
  return (
    <Stack spacing={2} sx={{ pt: 0.5 }} aria-label="正在加载用户信息">
      <Skeleton variant="text" width="72%" />
      <Skeleton variant="rounded" height={56} />
    </Stack>
  );
}
