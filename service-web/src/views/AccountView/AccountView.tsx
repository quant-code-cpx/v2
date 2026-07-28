import { Box, Breadcrumbs, Stack, Typography } from "@mui/material";

import { useAuth } from "../../components/AuthProvider";
import { AccountSecurityCard } from "./components/AccountSecurityCard";
import { ChangePasswordDialog } from "./components/ChangePasswordDialog";
import { ProfileForm } from "./components/ProfileForm";
import { RevokeSessionDialog } from "./components/RevokeSessionDialog";
import { SessionFamilyTable } from "./components/SessionFamilyTable";
import { useAccount } from "./hooks/useAccount";

/** 组合本人资料、账户安全摘要与 Session family 管理页面。 */
export function AccountView() {
  const { user } = useAuth();
  const model = useAccount();

  return (
    <Stack spacing={3}>
      <Box>
        <Breadcrumbs aria-label="当前位置" separator="/" sx={{ mb: 0.75 }}>
          <Typography variant="body2" color="text.secondary">
            个人
          </Typography>
          <Typography variant="body2" color="text.primary">
            我的账户
          </Typography>
        </Breadcrumbs>
        <Typography component="h1" variant="h3">
          我的账户
        </Typography>
        <Typography color="text.secondary" sx={{ mt: 0.5 }}>
          维护个人资料，检查并处置自己的登录会话。
        </Typography>
      </Box>
      <Box
        sx={{ display: "grid", gridTemplateColumns: "minmax(0, 3fr) minmax(320px, 2fr)", gap: 3 }}
      >
        <ProfileForm model={model} />
        <AccountSecurityCard user={user} model={model} />
      </Box>
      <SessionFamilyTable model={model} />
      {model.dialogState?.kind === "change-password" ? (
        <ChangePasswordDialog model={model} />
      ) : null}
      <RevokeSessionDialog model={model} />
    </Stack>
  );
}
