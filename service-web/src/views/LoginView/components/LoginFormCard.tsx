import {
  RefreshOutlined as RefreshOutlinedIcon,
  VisibilityOffOutlined as VisibilityOffOutlinedIcon,
  VisibilityOutlined as VisibilityOutlinedIcon,
} from "@mui/icons-material";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  IconButton,
  InputAdornment,
  Paper,
  Stack,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import type { MouseEvent } from "react";

import { shadows } from "../../../styles/design-tokens";
import type { LoginFormModel } from "../hooks/useLoginForm";

/** 描述登录表单卡片唯一需要的页面模型。 */
interface LoginFormCardProps {
  model: LoginFormModel;
}

/** 渲染登录字段与反馈，业务状态全部由页面 Hook 持有。 */
export function LoginFormCard({ model }: LoginFormCardProps) {
  return (
    <Paper
      component="section"
      aria-labelledby="login-title"
      elevation={0}
      sx={{
        width: "100%",
        maxWidth: 380,
        p: 5,
        border: 1,
        borderColor: "divider",
        boxShadow: shadows.card,
      }}
    >
      <Typography id="login-title" variant="h4">
        登录
      </Typography>
      <Box
        component="form"
        noValidate
        onSubmit={model.handleSubmit}
        onBlur={model.handleFormBlur}
        sx={{ mt: 4 }}
      >
        <Stack spacing={2}>
          {model.sessionExpired ? (
            <Alert severity="info">登录状态已失效，请重新登录。验证后将返回原页面。</Alert>
          ) : null}
          {model.errorMessage === null ? null : (
            <Alert severity="error">{model.errorMessage}</Alert>
          )}
          <TextField
            label="账号"
            placeholder="请输入账号"
            value={model.account}
            onChange={model.handleAccountChange}
            autoComplete="username"
            required
            fullWidth
            slotProps={{ inputLabel: { shrink: true } }}
          />
          <TextField
            label="密码"
            type={model.isPasswordVisible ? "text" : "password"}
            value={model.password}
            onChange={model.handlePasswordChange}
            autoComplete="current-password"
            placeholder="请输入密码"
            required
            fullWidth
            slotProps={{
              inputLabel: { shrink: true },
              input: {
                endAdornment: (
                  <InputAdornment position="end">
                    <Tooltip title={model.isPasswordVisible ? "隐藏密码" : "显示密码"}>
                      <IconButton
                        aria-label={model.isPasswordVisible ? "隐藏密码" : "显示密码"}
                        edge="end"
                        onClick={model.handlePasswordVisibility}
                        onMouseDown={preventInputBlur}
                      >
                        {model.isPasswordVisible ? (
                          <VisibilityOffOutlinedIcon />
                        ) : (
                          <VisibilityOutlinedIcon />
                        )}
                      </IconButton>
                    </Tooltip>
                  </InputAdornment>
                ),
              },
            }}
          />
          <TextField
            label="验证码"
            placeholder="请输入验证码"
            value={model.captchaAnswer}
            onChange={model.handleCaptchaAnswerChange}
            autoComplete="off"
            inputMode="text"
            required
            fullWidth
            slotProps={{
              input: {
                endAdornment: (
                  <InputAdornment position="end" sx={{ gap: 0.5 }}>
                    {model.isCaptchaLoading ? (
                      <CircularProgress size={20} aria-label="正在加载验证码" />
                    ) : model.captcha !== null ? (
                      <Box
                        component="img"
                        src={model.captcha.imageDataUrl}
                        alt="图形验证码"
                        sx={{ width: 104, height: 36, objectFit: "contain", borderRadius: 1 }}
                      />
                    ) : (
                      <Box sx={{ width: 104, height: 36 }} />
                    )}
                    <Tooltip title="刷新验证码">
                      <span>
                        <IconButton
                          aria-label="刷新验证码"
                          onClick={model.handleCaptchaRefresh}
                          disabled={model.isCaptchaLoading || model.isSubmitting}
                        >
                          <RefreshOutlinedIcon />
                        </IconButton>
                      </span>
                    </Tooltip>
                  </InputAdornment>
                ),
              },
              inputLabel: { shrink: true },
            }}
          />
          <Button
            type="submit"
            variant="contained"
            size="large"
            fullWidth
            disabled={model.isSubmitting || model.isCaptchaLoading || model.captcha === null}
          >
            {model.isSubmitting ? "正在登录" : "登录"}
          </Button>
        </Stack>
      </Box>
    </Paper>
  );
}

/** 密码显隐按钮仅改变本地状态时保持输入框焦点。 */
function preventInputBlur(event: MouseEvent<HTMLButtonElement>): void {
  event.preventDefault();
}
