import { useCallback, useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import type { ChangeEvent, FocusEvent, FormEvent } from "react";

import { createLoginCaptcha } from "../../../api/auth";
import { isApiError } from "../../../api/http";
import { useAuth } from "../../../components/AuthProvider";
import type { CaptchaChallenge } from "../../../types/access";
import { safeReturnTo } from "../../../utils/return-to";

/** 描述登录表单交给纯展示组件的状态与动作。 */
export interface LoginFormModel {
  account: string;
  password: string;
  captchaAnswer: string;
  captcha: CaptchaChallenge | null;
  isCaptchaLoading: boolean;
  isSubmitting: boolean;
  isPasswordVisible: boolean;
  sessionNotice: string | null;
  errorMessage: string | null;
  handleAccountChange: (event: ChangeEvent<HTMLInputElement>) => void;
  handlePasswordChange: (event: ChangeEvent<HTMLInputElement>) => void;
  handleCaptchaAnswerChange: (event: ChangeEvent<HTMLInputElement>) => void;
  handlePasswordVisibility: () => void;
  handleCaptchaRefresh: () => void;
  handleFormBlur: (event: FocusEvent<HTMLFormElement>) => void;
  handleSubmit: (event: FormEvent<HTMLFormElement>) => Promise<void>;
}

/** 管理登录页短期表单状态、验证码生命周期与成功后的安全返回。 */
export function useLoginForm(): LoginFormModel {
  const { login } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const sessionNotice = loginReasonNotice(new URLSearchParams(location.search).get("reason"));
  const [account, setAccount] = useState("");
  const [password, setPassword] = useState("");
  const [captchaAnswer, setCaptchaAnswer] = useState("");
  const [captcha, setCaptcha] = useState<CaptchaChallenge | null>(null);
  const [isCaptchaLoading, setIsCaptchaLoading] = useState(true);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isPasswordVisible, setIsPasswordVisible] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  /** 请求服务端渲染验证码；客户端不生成或缓存答案。 */
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

  /** 用户主动刷新时请求新验证码挑战。 */
  const handleCaptchaRefresh = useCallback(() => {
    void refreshCaptcha();
  }, [refreshCaptcha]);

  /** 登录页首次挂载后加载必填验证码。 */
  useEffect(() => {
    void refreshCaptcha();
  }, [refreshCaptcha]);

  /** 焦点离开完整表单时清除内存密码与显隐状态。 */
  const handleFormBlur = useCallback((event: FocusEvent<HTMLFormElement>) => {
    if (!event.currentTarget.contains(event.relatedTarget)) {
      setPassword("");
      setIsPasswordVisible(false);
    }
  }, []);

  /** 提交绑定验证码的登录请求，并在每次尝试后替换已消耗挑战。 */
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
        // 每次尝试都会消耗挑战，密码与验证码不得跨提交结果保留。
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

  /** 仅在用户主动操作时切换本地密码可见状态。 */
  const handlePasswordVisibility = useCallback(() => {
    setIsPasswordVisible((visible) => !visible);
  }, []);

  /** 更新登录账号，不套用创建用户时的字符规则。 */
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

  return {
    account,
    password,
    captchaAnswer,
    captcha,
    isCaptchaLoading,
    isSubmitting,
    isPasswordVisible,
    sessionNotice,
    errorMessage,
    handleAccountChange,
    handlePasswordChange,
    handleCaptchaAnswerChange,
    handlePasswordVisibility,
    handleCaptchaRefresh,
    handleFormBlur,
    handleSubmit,
  };
}

/** 将允许公开展示的退出原因转换为登录提示，不回显未知查询参数。 */
function loginReasonNotice(reason: string | null): string | null {
  if (reason === "session-expired") {
    return "登录状态已失效，请重新登录。验证后将返回原页面。";
  }
  if (reason === "password-changed") {
    return "密码已修改，请使用新密码重新登录。";
  }
  if (reason === "session-revoked") {
    return "当前会话已退出，请重新登录。";
  }

  return null;
}

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
