import type { CreateUserInput } from "../types/access";

/** Match the frozen administrator-only account creation rule. */
const accountPattern = /^[a-z0-9][a-z0-9._-]{4,31}$/;

/** Require a long password with at least one numeric character. */
const passwordPattern = /\d/;

/** Describe field-specific validation feedback for the create-user dialog. */
export interface CreateUserFieldErrors {
  account?: string;
  displayName?: string;
  password?: string;
}

/** Normalize account casing before local validation and contract submission. */
export function normalizeAccount(account: string): string {
  return account.trim().toLowerCase();
}

/** Check creation-only account, display-name, and initial-password constraints. */
export function validateCreateUserInput(input: CreateUserInput): CreateUserFieldErrors {
  const errors: CreateUserFieldErrors = {};
  const normalizedAccount = normalizeAccount(input.account);
  const normalizedDisplayName = input.displayName.trim();

  if (!accountPattern.test(normalizedAccount)) {
    errors.account = "账号需为 5–32 位小写字母、数字、点、下划线或连字符。";
  }
  if (normalizedDisplayName.length < 1 || normalizedDisplayName.length > 120) {
    errors.displayName = "姓名需为 1–120 个字符。";
  }
  if (
    input.password.length < 12 ||
    input.password.length > 512 ||
    !passwordPattern.test(input.password)
  ) {
    errors.password = "密码至少 12 位，且需包含数字。";
  }

  return errors;
}

/** Validate a managed-password value used by an administrator reset confirmation. */
export function validateManagedPassword(password: string): string | undefined {
  if (password.length < 12 || password.length > 512 || !passwordPattern.test(password)) {
    return "密码至少 12 位，且需包含数字。";
  }

  return undefined;
}

/** Return whether validation produced no field-level error. */
export function hasCreateUserErrors(errors: CreateUserFieldErrors): boolean {
  return Object.keys(errors).length > 0;
}
