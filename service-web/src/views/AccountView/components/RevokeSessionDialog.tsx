import {
  Alert,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Typography,
} from "@mui/material";

import type { useAccount } from "../hooks/useAccount";

/** 描述 Session 撤销确认 Dialog 消费的页面模型。 */
interface RevokeSessionDialogProps {
  model: ReturnType<typeof useAccount>;
}

/** 渲染单个或全部其他 Session family 的明确确认范围。 */
export function RevokeSessionDialog({ model }: RevokeSessionDialogProps) {
  const state = model.dialogState;

  if (state?.kind !== "revoke-session" && state?.kind !== "revoke-others") {
    return null;
  }

  const family =
    state.kind === "revoke-session"
      ? model.sessionQuery.data?.items.find((item) => item.familyId === state.familyId)
      : undefined;
  const isSubmitting =
    model.revokeSessionMutation.isPending || model.revokeOthersMutation.isPending;
  const cannotResolveFamily = state.kind === "revoke-session" && family === undefined;

  /** 提交当前 URL 指向的撤销动作。 */
  function handleConfirm(): void {
    if (state?.kind === "revoke-others") {
      model.revokeOthersMutation.mutate();
      return;
    }
    if (state?.kind === "revoke-session" && family !== undefined) {
      model.revokeSessionMutation.mutate(family);
    }
  }

  return (
    <Dialog open onClose={model.closeDialog} aria-labelledby="revoke-session-title">
      <DialogTitle id="revoke-session-title">
        {state.kind === "revoke-others" ? "退出其他会话？" : "退出这个会话？"}
      </DialogTitle>
      <DialogContent>
        {cannotResolveFamily ? (
          <Alert severity="warning">该会话已不在当前活动列表中，请刷新后重试。</Alert>
        ) : (
          <Typography color="text.secondary">
            {state.kind === "revoke-others"
              ? "将保留当前会话，并退出本账号全部其他活动 Session family。"
              : family?.current
                ? `将退出当前会话 ${family.familyId.slice(-4).toUpperCase()}，完成后需要重新登录。`
                : `将退出会话 ${family?.familyId.slice(-4).toUpperCase()}，此操作不会影响当前会话。`}
          </Typography>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={model.closeDialog}>取消</Button>
        <Button
          variant="contained"
          color="error"
          onClick={handleConfirm}
          disabled={isSubmitting || cannotResolveFamily}
        >
          {isSubmitting ? "正在退出" : "确认退出"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
