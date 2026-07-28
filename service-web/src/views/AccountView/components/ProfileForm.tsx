import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  Skeleton,
  Stack,
  TextField,
  Typography,
} from "@mui/material";

import { userRoleLabel } from "../../../utils/user-presentation";
import type { useAccount } from "../hooks/useAccount";

/** 描述个人资料表单消费的页面模型。 */
interface ProfileFormProps {
  model: ReturnType<typeof useAccount>;
}

/** 渲染只读身份字段与受 `ETag` 保护的显示名称编辑。 */
export function ProfileForm({ model }: ProfileFormProps) {
  const profile = model.profileQuery.data?.user;

  return (
    <Card component="section" aria-labelledby="profile-title">
      <CardContent>
        <Stack
          direction="row"
          justifyContent="space-between"
          alignItems="flex-start"
          sx={{ mb: 3 }}
        >
          <Box>
            <Typography id="profile-title" component="h2" variant="h5">
              个人资料
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
              账号和角色由系统管理，显示名称可以修改。
            </Typography>
          </Box>
          <Chip
            color="success"
            label={profile?.status === "ACTIVE" ? "● 账号启用" : "账号不可用"}
          />
        </Stack>

        {model.profileQuery.isPending ? (
          <Stack spacing={2} aria-label="正在加载个人资料">
            <Stack direction="row" spacing={2}>
              <Skeleton variant="rounded" height={78} sx={{ flex: 1 }} />
              <Skeleton variant="rounded" height={78} sx={{ flex: 1 }} />
            </Stack>
            <Skeleton variant="rounded" height={78} />
          </Stack>
        ) : null}
        {model.profileQuery.isError && profile === undefined ? (
          <Alert
            severity="error"
            action={
              <Button
                color="inherit"
                size="small"
                onClick={() => void model.profileQuery.refetch()}
              >
                重试
              </Button>
            }
          >
            个人资料暂时不可用。
          </Alert>
        ) : null}
        {profile === undefined ? null : (
          <Box component="form" noValidate onSubmit={model.handleProfileSubmit}>
            <Stack direction="row" spacing={2}>
              <TextField
                label="账号"
                value={profile.account}
                fullWidth
                slotProps={{ htmlInput: { readOnly: true } }}
                helperText="登录标识创建后不可修改。"
              />
              <TextField
                label="角色"
                value={userRoleLabel(profile.role)}
                fullWidth
                slotProps={{ htmlInput: { readOnly: true } }}
                helperText="权限由服务端角色策略计算。"
              />
            </Stack>
            <TextField
              label="显示名称"
              value={model.displayName}
              onChange={model.handleDisplayNameChange}
              fullWidth
              required
              error={model.profileError !== null}
              helperText={model.profileError ?? "用于页面身份和审计 Actor 展示，最多 120 字。"}
              slotProps={{ htmlInput: { maxLength: 120 } }}
              sx={{ mt: 2 }}
            />
            <Stack direction="row" justifyContent="flex-end" spacing={1.5} sx={{ mt: 3 }}>
              {model.profileConflict ? (
                <Button onClick={() => void model.handleReloadProfile()}>重新加载</Button>
              ) : null}
              <Button
                type="submit"
                variant="contained"
                disabled={
                  model.isSavingProfile ||
                  model.loadedEtag === null ||
                  model.displayName.trim() === profile.displayName
                }
              >
                {model.isSavingProfile ? "正在保存" : "保存资料"}
              </Button>
            </Stack>
          </Box>
        )}
      </CardContent>
    </Card>
  );
}
