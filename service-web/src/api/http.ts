import { environment } from "../config/env";

/** 携带安全 HTTP 失败元数据，不保留服务端 Problem detail。 */
export class ApiError extends Error {
  /** 构造只包含状态、稳定 code 与可选重试时间的错误。 */
  public constructor(
    public readonly status: number,
    public readonly code?: string,
    public readonly retryAfterSeconds?: number,
  ) {
    super("API request failed");
    this.name = "ApiError";
  }
}

/** 即使生产 chunk 持有不同类副本，也能识别安全 API 失败元数据。 */
export function isApiError(error: unknown): error is ApiError {
  if (error instanceof ApiError) {
    return true;
  }

  return (
    typeof error === "object" &&
    error !== null &&
    "status" in error &&
    typeof error.status === "number"
  );
}

/** 描述通过 `service-api` 边界发送的 JSON 请求数据；方法由传输层固定为 POST。 */
export interface JsonRequestOptions {
  body?: unknown;
  headers?: HeadersInit;
  signal?: AbortSignal;
}

/** 描述传输输入，使单元测试可提供符合合同的响应。 */
export interface HttpTransportRequest {
  url: string;
  init: RequestInit;
}

/** 抽象浏览器传输，仅供确定性单元测试替换。 */
export type HttpTransport = (request: HttpTransportRequest) => Promise<Response>;

/** 所有生产请求使用浏览器 fetch。 */
const browserTransport: HttpTransport = async ({ url, init }) => fetch(url, init);

/** 保存当前传输；生产环境始终使用浏览器实现。 */
let activeTransport: HttpTransport = browserTransport;

/** 使用公开构建期 API origin 构造版本化 `service-api` URL。 */
export function apiUrl(path: string): string {
  const configuredBaseUrl = environment.VITE_API_BASE_URL;

  return configuredBaseUrl === undefined ? path : new URL(path, configuredBaseUrl).toString();
}

/** 仅在 Vitest 执行时安装临时传输。 */
export function setHttpTransportForTests(transport?: HttpTransport): void {
  if (import.meta.env.MODE !== "test") {
    throw new Error("Test transport is unavailable outside Vitest.");
  }

  activeTransport = transport ?? browserTransport;
}

/** 从 Problem 响应读取稳定错误 code，不公开 detail 文本。 */
function readProblemCode(value: unknown): string | undefined {
  if (typeof value !== "object" || value === null || !("code" in value)) {
    return undefined;
  }

  const code = value.code;
  return typeof code === "string" ? code : undefined;
}

/** 将可选 Retry-After header 解析为有界正秒数。 */
function readRetryAfterSeconds(headers: Headers): number | undefined {
  const rawValue = headers.get("Retry-After");
  if (rawValue === null) {
    return undefined;
  }

  const parsedValue = Number.parseInt(rawValue, 10);
  return Number.isSafeInteger(parsedValue) && parsedValue > 0 ? parsedValue : undefined;
}

/** 仅在端点返回实体 Body 时解析 JSON。 */
async function readJsonBody(response: Response): Promise<unknown> {
  if (response.status === 204 || response.headers.get("content-length") === "0") {
    return undefined;
  }

  const contentType = response.headers.get("content-type") ?? "";
  return contentType.includes("json") ? response.json() : undefined;
}

/** 描述可能由条件请求返回 204 的完整 HTTP 读取结果。 */
export interface JsonHttpResponse<T> {
  data: T | undefined;
  headers: Headers;
  status: number;
}

/** 发送携带 cookie 的 POST JSON 请求，并保留 204 与实体响应的区别。 */
export async function requestJsonResponse<T>(
  path: string,
  options: JsonRequestOptions,
): Promise<JsonHttpResponse<T>> {
  const headers = new Headers(options.headers);
  headers.set("Accept", "application/json");

  const init: RequestInit = {
    method: "POST",
    headers,
    credentials: "include",
    signal: options.signal,
  };

  if (options.body !== undefined) {
    headers.set("Content-Type", "application/json");
    init.body = JSON.stringify(options.body);
  }

  const response = await activeTransport({ url: apiUrl(path), init });
  const payload = await readJsonBody(response);

  if (!response.ok) {
    throw new ApiError(
      response.status,
      readProblemCode(payload),
      readRetryAfterSeconds(response.headers),
    );
  }

  return { data: payload as T | undefined, headers: response.headers, status: response.status };
}

/** 发送携带 cookie 的 POST JSON 请求，并返回必须存在的载荷及响应元数据。 */
export async function requestJsonWithMetadata<T>(
  path: string,
  options: JsonRequestOptions,
): Promise<{ data: T; headers: Headers }> {
  const response = await requestJsonResponse<T>(path, options);

  return { data: response.data as T, headers: response.headers };
}

/** 发送一个 POST JSON 请求，并仅返回成功载荷。 */
export async function requestJson<T>(path: string, options: JsonRequestOptions): Promise<T> {
  const response = await requestJsonWithMetadata<T>(path, options);
  return response.data;
}
