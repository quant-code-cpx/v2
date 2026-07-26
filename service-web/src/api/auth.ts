import { requestJson } from "./http";
import type {
  AccessTokenResponse,
  CaptchaChallenge,
  CurrentUser,
  LoginInput,
} from "../types/access";

/** Request a new backend-rendered CAPTCHA without a client-generated challenge. */
export async function createLoginCaptcha(): Promise<CaptchaChallenge> {
  return requestJson<CaptchaChallenge>("/api/v1/auth/captcha", { method: "POST" });
}

/** Exchange account, password, and one CAPTCHA answer for a short-lived access token. */
export async function loginWithCaptcha(input: LoginInput): Promise<AccessTokenResponse> {
  return requestJson<AccessTokenResponse>("/api/v1/auth/login", { method: "POST", body: input });
}

/** Rotate the HttpOnly refresh cookie into a new short-lived access token. */
export async function refreshAccessToken(): Promise<AccessTokenResponse> {
  return requestJson<AccessTokenResponse>("/api/v1/auth/refresh", { method: "POST" });
}

/** Clear the server session cookie through its idempotent same-origin endpoint. */
export async function logoutCurrentSession(): Promise<void> {
  await requestJson<void>("/api/v1/auth/logout", { method: "POST" });
}

/** Read current identity and permissions using an in-memory Bearer token. */
export async function getCurrentUser(accessToken: string): Promise<CurrentUser> {
  return requestJson<CurrentUser>("/api/v1/users/me", {
    method: "GET",
    headers: { Authorization: `Bearer ${accessToken}` },
  });
}
