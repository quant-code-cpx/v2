import {
  Alert,
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import { useCallback, useState } from "react";
import type { ChangeEvent, FocusEvent, FormEvent } from "react";

import { isApiError } from "../../../api/http";
import type { useAccount } from "../hooks/useAccount";

/** 描述修改密码 Dialog 消费的页面模型。 */
interface ChangePasswordDialogProps {
  model: ReturnType<typeof useAccount>;
}

/** 渲染仅在组件内存持有密码的阻塞式改密表单。 */
export function ChangePasswordDialog({ model }: ChangePasswordDialogProps) {
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [formError, setFormError] = useState<string | null>(null);

  /** 清除 Dialog 持有的全部密码值。 */
  const clearPasswords = useCallback(() => {
    setCurrentPassword("");
    setNewPassword("");
  }, []);

  /** 清除敏感输入后关闭 URL 所有的 Dialog。 */
  const handleClose = useCallback(() => {
    clearPasswords();
    model.closeDialog();
  }, [clearPasswords, model]);

  /** 焦点离开完整改密表单时清除密码，避免后台页面继续持有。 */
  const handleBlur = useCallback(
    (event: FocusEvent<HTMLFormElement>) => {
      if (!event.currentTarget.contains(event.relatedTarget)) {
        clearPasswords();
      }
    },
    [clearPasswords],
  );

  /** 更新当前密码并清除上次错误。 */
  const handleCurrentPasswordChange = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    setCurrentPassword(event.target.value);
    setFormError(null);
  }, []);

  /** 更新新密码并清除上次错误。 */
  const handleNewPasswordChange = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    setNewPassword(event.target.value);
    setFormError(null);
  }, []);

  /** 校验密码合同并提交；成功路径由页面 Hook 清理全局会话。 */
  const handleSubmit = useCallback(
    async (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();

      if (currentPassword.length < 1) {
        setFormError("请输入当前密码。");
        return;
      }
      if (newPassword.length < 12 || newPassword.length > 512 || !/\d/u.test(newPassword)) {
        setFormError("新密码至少 12 位，且需包含数字。");
        return;
      }

      try {
        await model.passwordMutation.mutateAsync({ currentPassword, newPassword });
      } catch (error: unknown) {
        setFormError(
          isApiError(error) && error.code === "current-password-invalid"
            ? "当前密码不正确。"
            : "密码修改失败，请稍后重试。",
        );
      } finally {
        clearPasswords();
      }
    },
    [clearPasswords, currentPassword, model.passwordMutation, newPassword],
  );

  return (
    <Dialog open onClose={handleClose} aria-labelledby="change-password-title">
      <Box component="form" noValidate onSubmit={handleSubmit} onBlur={handleBlur}>
        <DialogTitle id="change-password-title">修改登录密码</DialogTitle>
        <DialogContent>
          <Typography color="text.secondary" sx={{ mb: 2.5 }}>
            成功后全部会话失效，当前页面将返回登录。
          </Typography>
          {formError === null ? null : (
            <Alert severity="error" sx={{ mb: 2 }}>
              {formError}
            </Alert>
          )}
          <Stack direction="row" spacing={2}>
            <TextField
              label="当前密码"
              type="password"
              value={currentPassword}
              onChange={handleCurrentPasswordChange}
              autoComplete="current-password"
              required
              fullWidth
              slotProps={{ htmlInput: { maxLength: 512 } }}
            />
            <TextField
              label="新密码"
              type="password"
              value={newPassword}
              onChange={handleNewPasswordChange}
              autoComplete="new-password"
              required
              fullWidth
              helperText="至少 12 位，并包含数字；不会记录或回显。"
              slotProps={{ htmlInput: { maxLength: 512 } }}
            />
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={handleClose}>取消</Button>
          <Button type="submit" variant="contained" disabled={model.passwordMutation.isPending}>
            {model.passwordMutation.isPending ? "正在修改" : "确认修改"}
          </Button>
        </DialogActions>
      </Box>
    </Dialog>
  );
}
