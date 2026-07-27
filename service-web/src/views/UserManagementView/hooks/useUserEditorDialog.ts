import { useCallback, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { FormEvent } from "react";

import { isApiError } from "../../../api/http";
import { createUser, updateUser, userDetailQueryOptions } from "../../../api/users";
import { useFeedback } from "../../../components/FeedbackProvider";
import type { CurrentUser } from "../../../types/access";
import {
  hasCreateUserErrors,
  normalizeAccount,
  validateCreateUserInput,
} from "../../../utils/user-form-validation";
import { useUserEditorForm } from "./useUserEditorForm";

/** 描述用户编辑 Hook 的输入。 */
interface UseUserEditorDialogInput {
  mode: "create" | "edit";
  userId?: string;
  actor: CurrentUser;
  onClose: () => void;
}

/** 管理创建/编辑用户 Dialog 的 ETag 查询、提交与反馈。 */
export function useUserEditorDialog({ mode, userId, actor, onClose }: UseUserEditorDialogInput) {
  const queryClient = useQueryClient();
  const { error: showError, success } = useFeedback();
  const [isSubmitting, setIsSubmitting] = useState(false);
  const detailQuery = useQuery({
    ...userDetailQueryOptions(userId ?? ""),
    enabled: mode === "edit" && userId !== undefined,
  });
  const canManageAdmins =
    actor.permissions.includes("admins:manage") || actor.permissions.includes("admins:create");
  const form = useUserEditorForm({
    mode,
    target: detailQuery.data?.user,
    canManageAdmins,
  });

  /** 清除短期敏感状态后关闭 URL 所有的 Dialog。 */
  const handleClose = useCallback(() => {
    form.clearPassword();
    form.setFormError(null);
    onClose();
  }, [form.clearPassword, form.setFormError, onClose]);

  /** 成功写入后刷新受影响的列表与详情缓存。 */
  const invalidateUsers = useCallback(async () => {
    await queryClient.invalidateQueries({ queryKey: ["users", "list"] });
    if (userId !== undefined) {
      await queryClient.invalidateQueries({ queryKey: ["users", "detail", userId] });
    }
  }, [queryClient, userId]);

  /** 将冲突、过期写与权限变化映射为安全恢复反馈。 */
  const handleSaveError = useCallback(
    async (error: unknown) => {
      if (isApiError(error) && error.status === 409 && mode === "create") {
        form.setFieldErrors({ account: "账号已被使用。" });
        return;
      }
      if (isApiError(error) && error.status === 412) {
        form.setFormError("用户信息已被更新，请重新确认后保存。");
        if (userId !== undefined) {
          await queryClient.invalidateQueries({ queryKey: ["users", "detail", userId] });
        }
        return;
      }
      if (isApiError(error) && (error.status === 403 || error.status === 404)) {
        showError("权限或用户状态已变化。");
        handleClose();
        return;
      }

      form.setFormError("保存失败，请稍后重试。");
    },
    [form.setFieldErrors, form.setFormError, handleClose, mode, queryClient, showError, userId],
  );

  /** 提交创建或 ETag 保护的编辑，并在所有结果中清除初始密码。 */
  const handleSubmit = useCallback(
    async (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      form.setFormError(null);

      if (mode === "create") {
        const createInput = {
          account: normalizeAccount(form.values.account),
          displayName: form.values.displayName.trim(),
          password: form.values.password,
          role: form.values.role,
          status: form.values.status,
        };
        const nextErrors = validateCreateUserInput(createInput);

        if (hasCreateUserErrors(nextErrors)) {
          form.setFieldErrors(nextErrors);
          form.clearPassword();
          return;
        }

        setIsSubmitting(true);
        try {
          await createUser(createInput);
          await invalidateUsers();
          success("用户已创建。");
          handleClose();
        } catch (error: unknown) {
          await handleSaveError(error);
        } finally {
          form.clearPassword();
          setIsSubmitting(false);
        }
        return;
      }

      if (userId === undefined || detailQuery.data === undefined) {
        return;
      }

      const displayName = form.values.displayName.trim();
      if (displayName.length < 1 || displayName.length > 120) {
        form.setFieldErrors({ displayName: "姓名需为 1–120 个字符。" });
        return;
      }

      setIsSubmitting(true);
      try {
        await updateUser(
          userId,
          { displayName, role: form.values.role, status: form.values.status },
          detailQuery.data.etag,
        );
        await invalidateUsers();
        success("用户信息已保存。");
        handleClose();
      } catch (error: unknown) {
        await handleSaveError(error);
      } finally {
        form.clearPassword();
        setIsSubmitting(false);
      }
    },
    [detailQuery.data, form, handleClose, handleSaveError, invalidateUsers, mode, success, userId],
  );

  return {
    mode,
    values: form.values,
    fieldErrors: form.fieldErrors,
    formError: form.formError,
    selectableRoles: form.selectableRoles,
    isSubmitting,
    isLoadingEdit: mode === "edit" && detailQuery.isPending,
    cannotLoadEdit: mode === "edit" && detailQuery.isError,
    handleTextChange: form.handleTextChange,
    handleSelectChange: form.handleSelectChange,
    handleFormBlur: form.handleFormBlur,
    handleSubmit,
    handleClose,
  };
}

/** 暴露页面私有编辑字段可复用的 Hook 返回类型。 */
export type UserEditorDialogModel = ReturnType<typeof useUserEditorDialog>;
