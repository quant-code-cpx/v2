import { requestJson } from "./http";
import type {
  AccessTokenResponse,
  CaptchaChallenge,
  CurrentUser,
  LoginInput,
} from "../types/access";

/** 请求后端渲染的新验证码，不由客户端生成 challenge。 */
export async function createLoginCaptcha(): Promise<CaptchaChallenge> {
  return requestJson<CaptchaChallenge>("/api/v1/auth/captcha", {});
}

/** 使用账号、密码和一次验证码答案换取短期 access token。 */
export async function loginWithCaptcha(input: LoginInput): Promise<AccessTokenResponse> {
  return requestJson<AccessTokenResponse>("/api/v1/auth/login", { body: input });
}

/** 轮换 HttpOnly refresh cookie 并取得新的短期 access token。 */
export async function refreshAccessToken(): Promise<AccessTokenResponse> {
  return requestJson<AccessTokenResponse>("/api/v1/auth/refresh", {});
}

/** 通过幂等同源端点清除服务端会话 cookie。 */
export async function logoutCurrentSession(): Promise<void> {
  await requestJson<void>("/api/v1/auth/logout", {});
}

/** 使用内存中的 Bearer token 读取当前身份和权限。 */
export async function getCurrentUser(accessToken: string): Promise<CurrentUser> {
  return requestJson<CurrentUser>("/api/v1/users/me", {
    headers: { Authorization: `Bearer ${accessToken}` },
  });
}
