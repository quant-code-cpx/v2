import {
  Alert,
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  MenuItem,
  Skeleton,
  Stack,
  TextField,
} from "@mui/material";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { ChangeEvent, FocusEvent, FormEvent } from "react";

import { createUser, updateUser, userDetailQueryOptions } from "../api/users";
import { isApiError } from "../api/http";
import { useFeedback } from "./FeedbackProvider";
import type { CurrentUser, UserRole, UserStatus } from "../types/access";
import {
  hasCreateUserErrors,
  normalizeAccount,
  validateCreateUserInput,
} from "../utils/user-form-validation";
import { userRoleLabel, userStatusLabel } from "../utils/user-presentation";

/** Describe editable local dialog fields, including a transient create-only password. */
interface UserEditorValues {
  account: string;
  displayName: string;
  password: string;
  role: Extract<UserRole, "USER" | "ADMIN">;
  status: Extract<UserStatus, "ACTIVE" | "DISABLED">;
}

/** Describe one creation or existing-user editing dialog request. */
interface UserEditorDialogProps {
  mode: "create" | "edit";
  userId?: string;
  actor: CurrentUser;
  onClose: () => void;
}

/** Create an empty create-user form using conservative role and status defaults. */
function createEmptyValues(): UserEditorValues {
  return {
    account: "",
    displayName: "",
    password: "",
    role: "USER",
    status: "ACTIVE",
  };
}

/** Map a returned user resource into edit-safe local form values without any password. */
function userToEditorValues(user: {
  account: string;
  displayName: string;
  role: UserRole;
  status: UserStatus;
}): UserEditorValues {
  return {
    account: user.account,
    displayName: user.displayName,
    password: "",
    role: user.role === "ADMIN" ? "ADMIN" : "USER",
    status: user.status === "DISABLED" ? "DISABLED" : "ACTIVE",
  };
}

/** Render the required 720px create/edit Dialog; passwords exist only for creation. */
export function UserEditorDialog({ mode, userId, actor, onClose }: UserEditorDialogProps) {
  const queryClient = useQueryClient();
  const { error: showError, success } = useFeedback();
  const [values, setValues] = useState<UserEditorValues>(createEmptyValues);
  const [fieldErrors, setFieldErrors] = useState<{
    account?: string;
    displayName?: string;
    password?: string;
  }>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const seededTargetId = useRef<string | undefined>(undefined);
  const detailQuery = useQuery({
    ...userDetailQueryOptions(userId ?? ""),
    enabled: mode === "edit" && userId !== undefined,
  });
  const canManageAdmins =
    actor.permissions.includes("admins:manage") || actor.permissions.includes("admins:create");

  /** Seed dialog values only when a new create/edit target first becomes available. */
  useEffect(() => {
    if (mode === "create") {
      if (seededTargetId.current !== "create") {
        seededTargetId.current = "create";
        setValues(createEmptyValues());
        setFieldErrors({});
        setFormError(null);
      }
      return;
    }

    if (detailQuery.data !== undefined && seededTargetId.current !== detailQuery.data.user.id) {
      seededTargetId.current = detailQuery.data.user.id;
      setValues(userToEditorValues(detailQuery.data.user));
      setFieldErrors({});
      setFormError(null);
    }
  }, [detailQuery.data, mode]);

  /** Drop local password state whenever the dialog closes, submits, or loses external focus. */
  const clearPassword = useCallback(() => {
    setValues((currentValues) => ({ ...currentValues, password: "" }));
  }, []);

  /** Close the URL-owned dialog after clearing all transient sensitive state. */
  const handleClose = useCallback(() => {
    clearPassword();
    setFormError(null);
    onClose();
  }, [clearPassword, onClose]);

  /** Clear a password when focus exits the complete dialog form, not when moving inside it. */
  const handleFormBlur = useCallback(
    (event: FocusEvent<HTMLFormElement>) => {
      if (!event.currentTarget.contains(event.relatedTarget)) {
        clearPassword();
      }
    },
    [clearPassword],
  );

  /** Update one editable text field without keeping server errors after user correction. */
  const handleTextChange = useCallback(
    (field: "account" | "displayName" | "password") => (event: ChangeEvent<HTMLInputElement>) => {
      const value = event.target.value;
      setValues((currentValues) => ({ ...currentValues, [field]: value }));
      setFieldErrors((currentErrors) => ({ ...currentErrors, [field]: undefined }));
      setFormError(null);
    },
    [],
  );

  /** Update one role or status selection without making a client-side authorization decision. */
  const handleSelectChange = useCallback(
    (field: "role" | "status") => (event: ChangeEvent<HTMLInputElement>) => {
      const value = event.target.value;
      setValues((currentValues) =>
        field === "role"
          ? { ...currentValues, role: value === "ADMIN" ? "ADMIN" : "USER" }
          : { ...currentValues, status: value === "DISABLED" ? "DISABLED" : "ACTIVE" },
      );
      setFormError(null);
    },
    [],
  );

  /** Refresh affected list and detail caches after one successful write. */
  const invalidateUsers = useCallback(async () => {
    await queryClient.invalidateQueries({ queryKey: ["users", "list"] });
    if (userId !== undefined) {
      await queryClient.invalidateQueries({ queryKey: ["users", "detail", userId] });
    }
  }, [queryClient, userId]);

  /** Handle conflict and stale-write errors without displaying raw response detail. */
  const handleSaveError = useCallback(
    async (error: unknown) => {
      if (isApiError(error) && error.status === 409 && mode === "create") {
        setFieldErrors({ account: "账号已被使用。" });
        return;
      }
      if (isApiError(error) && error.status === 412) {
        setFormError("用户信息已被更新，请重新确认后保存。");
        if (userId !== undefined) {
          await queryClient.invalidateQueries({ queryKey: ["users", "detail", userId] });
        }
        return;
      }
      if (isApiError(error) && (error.status === 403 || error.status === 404)) {
        showError("权限或用户状态已变化。");
        handleClose();
        return;
      }

      setFormError("保存失败，请稍后重试。");
    },
    [handleClose, mode, queryClient, showError, userId],
  );

  /** Submit creation or ETag-protected edit while clearing initial passwords in all outcomes. */
  const handleSubmit = useCallback(
    async (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      setFormError(null);

      if (mode === "create") {
        const createInput = {
          account: normalizeAccount(values.account),
          displayName: values.displayName.trim(),
          password: values.password,
          role: values.role,
          status: values.status,
        };
        const nextErrors = validateCreateUserInput(createInput);

        if (hasCreateUserErrors(nextErrors)) {
          setFieldErrors(nextErrors);
          clearPassword();
          return;
        }

        setIsSubmitting(true);
        try {
          await createUser(createInput);
          await invalidateUsers();
          success("用户已创建。");
          handleClose();
        } catch (error: unknown) {
          await handleSaveError(error);
        } finally {
          clearPassword();
          setIsSubmitting(false);
        }
        return;
      }

      if (userId === undefined || detailQuery.data === undefined) {
        return;
      }

      const displayName = values.displayName.trim();
      if (displayName.length < 1 || displayName.length > 120) {
        setFieldErrors({ displayName: "姓名需为 1–120 个字符。" });
        return;
      }

      setIsSubmitting(true);
      try {
        await updateUser(
          userId,
          { displayName, role: values.role, status: values.status },
          detailQuery.data.etag,
        );
        await invalidateUsers();
        success("用户信息已保存。");
        handleClose();
      } catch (error: unknown) {
        await handleSaveError(error);
      } finally {
        clearPassword();
        setIsSubmitting(false);
      }
    },
    [
      clearPassword,
      detailQuery.data,
      handleClose,
      handleSaveError,
      invalidateUsers,
      mode,
      success,
      userId,
      values,
    ],
  );

  /** Return role choices limited to current server-granted administrator capability. */
  const selectableRoles = useMemo<Extract<UserRole, "USER" | "ADMIN">[]>(
    () => (canManageAdmins ? ["USER", "ADMIN"] : ["USER"]),
    [canManageAdmins],
  );

  const isLoadingEdit = mode === "edit" && detailQuery.isPending;
  const cannotLoadEdit = mode === "edit" && detailQuery.isError;

  return (
    <Dialog
      open
      onClose={handleClose}
      fullWidth
      maxWidth={false}
      PaperProps={{ sx: { width: 720 } }}
      aria-labelledby="user-editor-title"
    >
      <Box component="form" noValidate onSubmit={handleSubmit} onBlur={handleFormBlur}>
        <DialogTitle id="user-editor-title">
          {mode === "create" ? "新建用户" : "编辑用户"}
        </DialogTitle>
        <DialogContent>
          {isLoadingEdit ? <EditorSkeleton /> : null}
          {cannotLoadEdit ? (
            <Alert severity="error">用户信息暂时不可用，请关闭后重试。</Alert>
          ) : null}
          {!isLoadingEdit && !cannotLoadEdit ? (
            <Stack spacing={2.5} sx={{ pt: 1.5 }}>
              {formError === null ? null : <Alert severity="error">{formError}</Alert>}
              <TextField
                label="账号"
                value={values.account}
                onChange={handleTextChange("account")}
                required={mode === "create"}
                fullWidth
                error={fieldErrors.account !== undefined}
                helperText={
                  mode === "create"
                    ? (fieldErrors.account ?? "5–32 位小写字母、数字、点、下划线或连字符。")
                    : "账号创建后不可修改。"
                }
                slotProps={{
                  input: { readOnly: mode === "edit" },
                  htmlInput: mode === "create" ? { maxLength: 32 } : undefined,
                }}
                sx={
                  mode === "edit"
                    ? {
                        "& .MuiOutlinedInput-root": { bgcolor: "grey.50" },
                        "& .MuiInputBase-input": { color: "text.secondary", cursor: "default" },
                      }
                    : undefined
                }
              />
              <TextField
                label="姓名"
                value={values.displayName}
                onChange={handleTextChange("displayName")}
                required
                fullWidth
                error={fieldErrors.displayName !== undefined}
                helperText={fieldErrors.displayName}
                slotProps={{ htmlInput: { maxLength: 120 } }}
              />
              {mode === "create" ? (
                <TextField
                  label="初始密码"
                  type="password"
                  value={values.password}
                  onChange={handleTextChange("password")}
                  autoComplete="new-password"
                  required
                  fullWidth
                  error={fieldErrors.password !== undefined}
                  helperText={fieldErrors.password ?? "至少 12 位，且需包含数字。"}
                  slotProps={{ htmlInput: { maxLength: 512 } }}
                />
              ) : null}
              <TextField
                select
                label="角色"
                value={values.role}
                onChange={handleSelectChange("role")}
                fullWidth
              >
                {/* Render only role grants allowed by current server-calculated capability. */}
                {selectableRoles.map((role) => (
                  <MenuItem key={role} value={role}>
                    {userRoleLabel(role)}
                  </MenuItem>
                ))}
              </TextField>
              <TextField
                select
                label="状态"
                value={values.status}
                onChange={handleSelectChange("status")}
                fullWidth
              >
                <MenuItem value="ACTIVE">{userStatusLabel("ACTIVE")}</MenuItem>
                <MenuItem value="DISABLED">{userStatusLabel("DISABLED")}</MenuItem>
              </TextField>
            </Stack>
          ) : null}
        </DialogContent>
        <DialogActions>
          <Button onClick={handleClose}>取消</Button>
          <Button
            type="submit"
            variant="contained"
            disabled={isSubmitting || isLoadingEdit || cannotLoadEdit}
          >
            {isSubmitting ? "正在保存" : "保存"}
          </Button>
        </DialogActions>
      </Box>
    </Dialog>
  );
}

/** Reserve user form geometry while the ETag-protected edit detail is loading. */
function EditorSkeleton() {
  return (
    <Stack spacing={2.5} sx={{ pt: 0.5 }} aria-label="正在加载用户信息">
      <Skeleton variant="rounded" height={56} />
      <Skeleton variant="rounded" height={56} />
      <Skeleton variant="rounded" height={56} />
      <Skeleton variant="rounded" height={56} />
    </Stack>
  );
}
