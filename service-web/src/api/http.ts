import { environment } from "../config/env";

/** Carry safe HTTP failure metadata without retaining a server Problem detail. */
export class ApiError extends Error {
  /** Construct an error limited to status, stable code, and optional retry timing. */
  public constructor(
    public readonly status: number,
    public readonly code?: string,
    public readonly retryAfterSeconds?: number,
  ) {
    super("API request failed");
    this.name = "ApiError";
  }
}

/** Recognize safe API failure metadata even when production chunks hold separate class copies. */
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

/** Describe JSON-only request data sent through the service-api boundary. */
export interface JsonRequestOptions {
  method: "GET" | "POST" | "PATCH" | "DELETE";
  body?: unknown;
  headers?: HeadersInit;
  signal?: AbortSignal;
}

/** Describe transport input so unit tests can provide contract-shaped responses. */
export interface HttpTransportRequest {
  url: string;
  init: RequestInit;
}

/** Abstract browser transport exclusively for deterministic unit tests. */
export type HttpTransport = (request: HttpTransportRequest) => Promise<Response>;

/** Use browser fetch for every production request. */
const browserTransport: HttpTransport = async ({ url, init }) => fetch(url, init);

/** Hold the active transport; production always keeps the browser implementation. */
let activeTransport: HttpTransport = browserTransport;

/** Build a versioned service-api URL from the public build-time API origin. */
export function apiUrl(path: string): string {
  const configuredBaseUrl = environment.VITE_API_BASE_URL;

  return configuredBaseUrl === undefined ? path : new URL(path, configuredBaseUrl).toString();
}

/** Install a transient transport only when Vitest is executing. */
export function setHttpTransportForTests(transport?: HttpTransport): void {
  if (import.meta.env.MODE !== "test") {
    throw new Error("Test transport is unavailable outside Vitest.");
  }

  activeTransport = transport ?? browserTransport;
}

/** Read a stable error code from a Problem response without exposing its detail text. */
function readProblemCode(value: unknown): string | undefined {
  if (typeof value !== "object" || value === null || !("code" in value)) {
    return undefined;
  }

  const code = value.code;
  return typeof code === "string" ? code : undefined;
}

/** Parse an optional Retry-After header as bounded positive seconds. */
function readRetryAfterSeconds(headers: Headers): number | undefined {
  const rawValue = headers.get("Retry-After");
  if (rawValue === null) {
    return undefined;
  }

  const parsedValue = Number.parseInt(rawValue, 10);
  return Number.isSafeInteger(parsedValue) && parsedValue > 0 ? parsedValue : undefined;
}

/** Decode JSON only when the endpoint returned an entity body. */
async function readJsonBody(response: Response): Promise<unknown> {
  if (response.status === 204 || response.headers.get("content-length") === "0") {
    return undefined;
  }

  const contentType = response.headers.get("content-type") ?? "";
  return contentType.includes("json") ? response.json() : undefined;
}

/** Send one JSON request with cookies included and return payload plus response metadata. */
export async function requestJsonWithMetadata<T>(
  path: string,
  options: JsonRequestOptions,
): Promise<{ data: T; headers: Headers }> {
  const headers = new Headers(options.headers);
  headers.set("Accept", "application/json");

  const init: RequestInit = {
    method: options.method,
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

  return { data: payload as T, headers: response.headers };
}

/** Send one JSON request and return only its success payload. */
export async function requestJson<T>(path: string, options: JsonRequestOptions): Promise<T> {
  const response = await requestJsonWithMetadata<T>(path, options);
  return response.data;
}
