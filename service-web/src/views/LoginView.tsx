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
import { useCallback, useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import type { ChangeEvent, FocusEvent, FormEvent, MouseEvent } from "react";

import { createLoginCaptcha } from "../api/auth";
import { isApiError } from "../api/http";
import { useAuth } from "../components/AuthProvider";
import { shadows } from "../styles/design-tokens";
import type { CaptchaChallenge } from "../types/access";
import { safeReturnTo } from "../utils/return-to";

/** 将稳定 API 状态转换为不泄露账号存在性的登录反馈。 */
function loginErrorMessage(error: unknown): string {
  if (isApiError(error) && error.status === 422) {
    return "验证码已失效或不正确，请重新输入。";
  }
  if (isApiError(error) && error.status === 429) {
    return "操作频繁，请稍后重试。";
  }

  return "登录失败，请检查账号、密码和验证码。";
}

/** 渲染唯一匿名入口，提供账号、密码与常驻 PNG 验证码登录。 */
export function LoginView() {
  const { login } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const sessionExpired = new URLSearchParams(location.search).get("reason") === "session-expired";
  const [account, setAccount] = useState("");
  const [password, setPassword] = useState("");
  const [captchaAnswer, setCaptchaAnswer] = useState("");
  const [captcha, setCaptcha] = useState<CaptchaChallenge | null>(null);
  const [isCaptchaLoading, setIsCaptchaLoading] = useState(true);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isPasswordVisible, setIsPasswordVisible] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  /** 请求服务端渲染的验证码图片，客户端不生成或缓存答案。 */
  const refreshCaptcha = useCallback(async (clearFeedback = true) => {
    setIsCaptchaLoading(true);
    if (clearFeedback) {
      setErrorMessage(null);
    }

    try {
      const challenge = await createLoginCaptcha();
      setCaptcha(challenge);
    } catch {
      setCaptcha(null);
      setErrorMessage("验证码暂时不可用，请刷新后重试。");
    } finally {
      setIsCaptchaLoading(false);
    }
  }, []);

  /** 响应用户刷新操作，请求新的验证码挑战。 */
  const handleCaptchaRefresh = useCallback(() => {
    void refreshCaptcha();
  }, [refreshCaptcha]);

  /** 登录页挂载后立即请求必填验证码。 */
  useEffect(() => {
    void refreshCaptcha();
  }, [refreshCaptcha]);

  /** 焦点离开完整表单时清除内存中的敏感密码状态。 */
  const handleFormBlur = useCallback((event: FocusEvent<HTMLFormElement>) => {
    if (!event.currentTarget.contains(event.relatedTarget)) {
      setPassword("");
      setIsPasswordVisible(false);
    }
  }, []);

  /** 提交绑定验证码的登录请求，并在每次尝试后替换已消耗的图片。 */
  const handleSubmit = useCallback(
    async (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      const challengeId = captcha?.challengeId;

      if (challengeId === undefined || isCaptchaLoading) {
        setErrorMessage("验证码暂时不可用，请刷新后重试。");
        return;
      }

      setIsSubmitting(true);
      setErrorMessage(null);

      try {
        await login({ account, password, captchaId: challengeId, captchaAnswer });
        const search = new URLSearchParams(location.search);
        void navigate(safeReturnTo(search.get("returnTo")), { replace: true });
      } catch (error: unknown) {
        setErrorMessage(loginErrorMessage(error));
      } finally {
        // 每次尝试都会消耗挑战，密码和验证码不得跨提交结果留存。
        setPassword("");
        setCaptchaAnswer("");
        setIsPasswordVisible(false);
        setIsSubmitting(false);
        await refreshCaptcha(false);
      }
    },
    [
      account,
      captcha?.challengeId,
      captchaAnswer,
      isCaptchaLoading,
      location.search,
      login,
      navigate,
      password,
      refreshCaptcha,
    ],
  );

  /** 仅在用户主动操作期间切换本地密码可见状态。 */
  const handlePasswordVisibility = useCallback(() => {
    setIsPasswordVisible((visible) => !visible);
  }, []);

  /** 更新登录账号，不套用创建用户时的长度或字符校验。 */
  const handleAccountChange = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    setAccount(event.target.value);
  }, []);

  /** 仅在组件内存中更新密码。 */
  const handlePasswordChange = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    setPassword(event.target.value);
  }, []);

  /** 仅在本次提交完成前保留验证码答案。 */
  const handleCaptchaAnswerChange = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    setCaptchaAnswer(event.target.value);
  }, []);

  return (
    <Box
      component="main"
      sx={{
        minHeight: "100vh",
        display: "grid",
        gridTemplateColumns: "42fr 58fr",
        bgcolor: "background.default",
      }}
    >
      <Box
        sx={{
          px: 12,
          py: 10,
          display: "grid",
          gridTemplateRows: "auto 1fr",
          bgcolor: "grey.100",
        }}
      >
        <Stack direction="row" spacing={1.5} alignItems="center">
          <Box
            aria-hidden="true"
            sx={{
              position: "relative",
              width: 28,
              height: 28,
              flex: "0 0 auto",
            }}
          >
            <Box
              sx={{
                position: "absolute",
                top: 2,
                left: 3,
                width: 13,
                height: 13,
                borderRadius: "50%",
                bgcolor: "primary.main",
              }}
            />
            <Box
              sx={{
                position: "absolute",
                right: 3,
                bottom: 2,
                width: 14,
                height: 14,
                borderRadius: "50%",
                bgcolor: "secondary.main",
              }}
            />
          </Box>
          <Typography variant="h6" fontWeight={800}>
            Apex数据智能分析平台
          </Typography>
        </Stack>

        <Box sx={{ maxWidth: 480, alignSelf: "center" }}>
          <Typography component="h1" variant="h1">
            看见数据，
            <br />
            读懂市场。
          </Typography>
          <Typography color="text.secondary" sx={{ mt: 3 }}>
            登录后进入股票与市场数据分析平台。后续 AI 研究助手将帮助发现线索，判断仍由你完成。
          </Typography>
        </Box>
      </Box>

      <Box
        sx={{
          px: 10,
          py: 10,
          display: "grid",
          placeItems: "center",
          bgcolor: "background.paper",
          borderLeft: 1,
          borderColor: "divider",
        }}
      >
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
            onSubmit={handleSubmit}
            onBlur={handleFormBlur}
            sx={{ mt: 4 }}
          >
            <Stack spacing={2}>
              {sessionExpired ? (
                <Alert severity="info">登录状态已失效，请重新登录。验证后将返回原页面。</Alert>
              ) : null}
              {errorMessage === null ? null : <Alert severity="error">{errorMessage}</Alert>}
              <TextField
                label="账号"
                placeholder="请输入账号"
                value={account}
                onChange={handleAccountChange}
                autoComplete="username"
                required
                fullWidth
                slotProps={{ inputLabel: { shrink: true } }}
              />
              <TextField
                label="密码"
                type={isPasswordVisible ? "text" : "password"}
                value={password}
                onChange={handlePasswordChange}
                autoComplete="current-password"
                placeholder="请输入密码"
                required
                fullWidth
                slotProps={{
                  inputLabel: { shrink: true },
                  input: {
                    endAdornment: (
                      <InputAdornment position="end">
                        <Tooltip title={isPasswordVisible ? "隐藏密码" : "显示密码"}>
                          <IconButton
                            aria-label={isPasswordVisible ? "隐藏密码" : "显示密码"}
                            edge="end"
                            onClick={handlePasswordVisibility}
                            onMouseDown={preventInputBlur}
                          >
                            {isPasswordVisible ? (
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
                value={captchaAnswer}
                onChange={handleCaptchaAnswerChange}
                autoComplete="off"
                inputMode="text"
                required
                fullWidth
                slotProps={{
                  input: {
                    endAdornment: (
                      <InputAdornment position="end" sx={{ gap: 0.5 }}>
                        {isCaptchaLoading ? (
                          <CircularProgress size={20} aria-label="正在加载验证码" />
                        ) : captcha !== null ? (
                          <Box
                            component="img"
                            src={captcha.imageDataUrl}
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
                              onClick={handleCaptchaRefresh}
                              disabled={isCaptchaLoading || isSubmitting}
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
                disabled={isSubmitting || isCaptchaLoading || captcha === null}
              >
                {isSubmitting ? "正在登录" : "登录"}
              </Button>
            </Stack>
          </Box>
        </Paper>
      </Box>
    </Box>
  );
}

/** 密码显隐按钮仅改变本地状态时保持输入框焦点。 */
function preventInputBlur(event: MouseEvent<HTMLButtonElement>): void {
  event.preventDefault();
}
