/** Return only a same-origin relative route suitable for post-login navigation. */
export function safeReturnTo(rawValue: string | null | undefined): string {
  if (
    rawValue === null ||
    rawValue === undefined ||
    !rawValue.startsWith("/") ||
    rawValue.startsWith("//")
  ) {
    return "/";
  }

  try {
    const parsed = new URL(rawValue, "https://apex.local");
    return parsed.origin === "https://apex.local"
      ? `${parsed.pathname}${parsed.search}${parsed.hash}`
      : "/";
  } catch {
    return "/";
  }
}
