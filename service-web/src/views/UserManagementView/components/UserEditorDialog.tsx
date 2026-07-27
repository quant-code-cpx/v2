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

import type { CurrentUser } from "../../../types/access";
import { userRoleLabel, userStatusLabel } from "../../../utils/user-presentation";
import { useUserEditorDialog } from "../hooks/useUserEditorDialog";
import type { UserEditorDialogModel } from "../hooks/useUserEditorDialog";

/** 描述创建或编辑用户 Dialog 请求。 */
interface UserEditorDialogProps {
  mode: "create" | "edit";
  userId?: string;
  actor: CurrentUser;
  onClose: () => void;
}

/** 渲染 720px 创建/编辑 Dialog，状态与副作用交给独立 Hook。 */
export function UserEditorDialog({ mode, userId, actor, onClose }: UserEditorDialogProps) {
  const model = useUserEditorDialog({ mode, userId, actor, onClose });

  return (
    <Dialog
      open
      onClose={model.handleClose}
      fullWidth
      maxWidth={false}
      PaperProps={{ sx: { width: 720 } }}
      aria-labelledby="user-editor-title"
    >
      <Box component="form" noValidate onSubmit={model.handleSubmit} onBlur={model.handleFormBlur}>
        <DialogTitle id="user-editor-title">
          {mode === "create" ? "新建用户" : "编辑用户"}
        </DialogTitle>
        <DialogContent>
          {model.isLoadingEdit ? <EditorSkeleton /> : null}
          {model.cannotLoadEdit ? (
            <Alert severity="error">用户信息暂时不可用，请关闭后重试。</Alert>
          ) : null}
          {!model.isLoadingEdit && !model.cannotLoadEdit ? (
            <UserEditorFields model={model} />
          ) : null}
        </DialogContent>
        <DialogActions>
          <Button onClick={model.handleClose}>取消</Button>
          <Button
            type="submit"
            variant="contained"
            disabled={model.isSubmitting || model.isLoadingEdit || model.cannotLoadEdit}
          >
            {model.isSubmitting ? "正在保存" : "保存"}
          </Button>
        </DialogActions>
      </Box>
    </Dialog>
  );
}

/** ETag 保护的编辑详情加载时保留表单几何。 */
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

/** 渲染创建/编辑用户字段，不持有远程或提交状态。 */
function UserEditorFields({ model }: { model: UserEditorDialogModel }) {
  return (
    <Stack spacing={2.5} sx={{ pt: 1.5 }}>
      {model.formError === null ? null : <Alert severity="error">{model.formError}</Alert>}
      <TextField
        label="账号"
        value={model.values.account}
        onChange={model.handleTextChange("account")}
        required={model.mode === "create"}
        fullWidth
        error={model.fieldErrors.account !== undefined}
        helperText={
          model.mode === "create"
            ? (model.fieldErrors.account ?? "5–32 位小写字母、数字、点、下划线或连字符。")
            : "账号创建后不可修改。"
        }
        slotProps={{
          input: { readOnly: model.mode === "edit" },
          htmlInput: model.mode === "create" ? { maxLength: 32 } : undefined,
        }}
        sx={
          model.mode === "edit"
            ? {
                "& .MuiOutlinedInput-root": { bgcolor: "grey.50" },
                "& .MuiInputBase-input": { color: "text.secondary", cursor: "default" },
              }
            : undefined
        }
      />
      <TextField
        label="姓名"
        value={model.values.displayName}
        onChange={model.handleTextChange("displayName")}
        required
        fullWidth
        error={model.fieldErrors.displayName !== undefined}
        helperText={model.fieldErrors.displayName}
        slotProps={{ htmlInput: { maxLength: 120 } }}
      />
      {model.mode === "create" ? (
        <TextField
          label="初始密码"
          type="password"
          value={model.values.password}
          onChange={model.handleTextChange("password")}
          autoComplete="new-password"
          required
          fullWidth
          error={model.fieldErrors.password !== undefined}
          helperText={model.fieldErrors.password ?? "至少 12 位，且需包含数字。"}
          slotProps={{ htmlInput: { maxLength: 512 } }}
        />
      ) : null}
      <TextField
        select
        label="角色"
        value={model.values.role}
        onChange={model.handleSelectChange("role")}
        fullWidth
      >
        {/* 只渲染当前服务端权限允许授予的角色。 */}
        {model.selectableRoles.map((role) => (
          <MenuItem key={role} value={role}>
            {userRoleLabel(role)}
          </MenuItem>
        ))}
      </TextField>
      <TextField
        select
        label="状态"
        value={model.values.status}
        onChange={model.handleSelectChange("status")}
        fullWidth
      >
        <MenuItem value="ACTIVE">{userStatusLabel("ACTIVE")}</MenuItem>
        <MenuItem value="DISABLED">{userStatusLabel("DISABLED")}</MenuItem>
      </TextField>
    </Stack>
  );
}
