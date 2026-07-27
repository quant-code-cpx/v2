import { useCallback, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { ChangeEvent, FocusEvent, FormEvent } from "react";

import { isApiError } from "../../../api/http";
import { deleteUser, resetUserPassword, userDetailQueryOptions } from "../../../api/users";
import { useFeedback } from "../../../components/FeedbackProvider";
import { validateManagedPassword } from "../../../utils/user-form-validation";

/** 描述删除或密码重置 Hook 的输入。 */
interface UseUserActionDialogInput {
  kind: "delete" | "reset-password";
  userId: string;
  onClose: () => void;
}

/** 管理 ETag 保护的删除/密码重置状态、提交与敏感信息清理。 */
export function useUserActionDialog({ kind, userId, onClose }: UseUserActionDialogInput) {
  const queryClient = useQueryClient();
  const { error: showError, success } = useFeedback();
  const detailQuery = useQuery(userDetailQueryOptions(userId));
  const [password, setPassword] = useState("");
  const [passwordError, setPasswordError] = useState<string | undefined>(undefined);
  const [formError, setFormError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  /** 清除组件内存中的重置密码与字段错误。 */
  const clearPassword = useCallback(() => {
    setPassword("");
    setPasswordError(undefined);
  }, []);

  /** 清除短期密码后关闭 URL 所有的动作 Dialog。 */
  const handleClose = useCallback(() => {
    clearPassword();
    setFormError(null);
    onClose();
  }, [clearPassword, onClose]);

  /** 焦点离开完整确认表单时清除密码。 */
  const handleFormBlur = useCallback(
    (event: FocusEvent<HTMLFormElement>) => {
      if (!event.currentTarget.contains(event.relatedTarget)) {
        clearPassword();
      }
    },
    [clearPassword],
  );

  /** 仅在组件内存中更新重置密码。 */
  const handlePasswordChange = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    setPassword(event.target.value);
    setPasswordError(undefined);
    setFormError(null);
  }, []);

  /** 成功动作后刷新列表并移除目标详情缓存。 */
  const invalidateUsers = useCallback(async () => {
    await queryClient.invalidateQueries({ queryKey: ["users", "list"] });
    queryClient.removeQueries({ queryKey: ["users", "detail", userId] });
  }, [queryClient, userId]);

  /** 将并发冲突或目标不可访问映射为安全恢复反馈。 */
  const handleActionError = useCallback(
    async (error: unknown) => {
      if (isApiError(error) && error.status === 412) {
        setFormError("用户信息已被更新，请重新确认后操作。");
        await queryClient.invalidateQueries({ queryKey: ["users", "detail", userId] });
        return;
      }
      if (isApiError(error) && (error.status === 403 || error.status === 404)) {
        showError("权限或用户状态已变化。");
        handleClose();
        return;
      }

      setFormError(kind === "delete" ? "删除失败，请稍后重试。" : "重置失败，请稍后重试。");
    },
    [handleClose, kind, queryClient, showError, userId],
  );

  /** 执行 ETag 保护动作，并在所有结果中移除敏感密码。 */
  const handleSubmit = useCallback(
    async (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      const detail = detailQuery.data;

      if (detail === undefined) {
        return;
      }

      if (kind === "reset-password") {
        const validationError = validateManagedPassword(password);
        if (validationError !== undefined) {
          clearPassword();
          setPasswordError(validationError);
          return;
        }
      }

      setIsSubmitting(true);
      setFormError(null);
      try {
        if (kind === "delete") {
          await deleteUser(userId, detail.etag);
        } else {
          await resetUserPassword(userId, password, detail.etag);
        }
        await invalidateUsers();
        success(kind === "delete" ? "用户已删除。" : "密码已重置。");
        handleClose();
      } catch (error: unknown) {
        await handleActionError(error);
      } finally {
        clearPassword();
        setIsSubmitting(false);
      }
    },
    [
      clearPassword,
      detailQuery.data,
      handleActionError,
      handleClose,
      invalidateUsers,
      kind,
      password,
      success,
      userId,
    ],
  );

  return {
    password,
    passwordError,
    formError,
    isSubmitting,
    isLoading: detailQuery.isPending,
    cannotLoad: detailQuery.isError,
    target: detailQuery.data?.user,
    handlePasswordChange,
    handleFormBlur,
    handleSubmit,
    handleClose,
  };
}
