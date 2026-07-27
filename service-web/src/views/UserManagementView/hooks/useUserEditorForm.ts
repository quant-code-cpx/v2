import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ChangeEvent, FocusEvent } from "react";

import type { User, UserRole } from "../../../types/access";
import { createEmptyUserEditorValues, userToEditorValues } from "../utils/user-editor-values";
import type { UserEditorFieldErrors, UserEditorValues } from "../utils/user-editor-values";

/** 描述用户编辑本地表单 Hook 的输入。 */
interface UseUserEditorFormInput {
  mode: "create" | "edit";
  target: User | undefined;
  canManageAdmins: boolean;
}

/** 管理用户编辑字段、初始化边界、字段错误与密码清理。 */
export function useUserEditorForm({ mode, target, canManageAdmins }: UseUserEditorFormInput) {
  const [values, setValues] = useState<UserEditorValues>(createEmptyUserEditorValues);
  const [fieldErrors, setFieldErrors] = useState<UserEditorFieldErrors>({});
  const [formError, setFormError] = useState<string | null>(null);
  const seededTargetId = useRef<string | undefined>(undefined);

  /** 新目标首次可用时初始化表单，后续输入不被查询重渲染覆盖。 */
  useEffect(() => {
    if (mode === "create") {
      if (seededTargetId.current !== "create") {
        seededTargetId.current = "create";
        setValues(createEmptyUserEditorValues());
        setFieldErrors({});
        setFormError(null);
      }
      return;
    }

    if (target !== undefined && seededTargetId.current !== target.id) {
      seededTargetId.current = target.id;
      setValues(userToEditorValues(target));
      setFieldErrors({});
      setFormError(null);
    }
  }, [mode, target]);

  /** 清除组件内存中的创建密码。 */
  const clearPassword = useCallback(() => {
    setValues((currentValues) => ({ ...currentValues, password: "" }));
  }, []);

  /** 键盘焦点离开完整表单时清除密码。 */
  const handleFormBlur = useCallback(
    (event: FocusEvent<HTMLFormElement>) => {
      if (!event.currentTarget.contains(event.relatedTarget)) {
        clearPassword();
      }
    },
    [clearPassword],
  );

  /** 更新一个文本字段，并移除已被用户修正的错误。 */
  const handleTextChange = useCallback(
    (field: "account" | "displayName" | "password") => (event: ChangeEvent<HTMLInputElement>) => {
      const value = event.target.value;
      setValues((currentValues) => ({ ...currentValues, [field]: value }));
      setFieldErrors((currentErrors) => ({ ...currentErrors, [field]: undefined }));
      setFormError(null);
    },
    [],
  );

  /** 更新角色或状态，不在客户端作额外授权推断。 */
  const handleSelectChange = useCallback(
    (field: "role" | "status") => (event: ChangeEvent<HTMLInputElement>) => {
      const value = event.target.value;
      setValues((currentValues) =>
        field === "role"
          ? { ...currentValues, role: value === "ADMIN" ? "ADMIN" : "USER" }
          : { ...currentValues, status: value === "DISABLED" ? "DISABLED" : "ACTIVE" },
      );
      setFormError(null);
    },
    [],
  );

  /** 按当前服务端权限派生可选择角色。 */
  const selectableRoles = useMemo<Extract<UserRole, "USER" | "ADMIN">[]>(
    () => (canManageAdmins ? ["USER", "ADMIN"] : ["USER"]),
    [canManageAdmins],
  );

  return {
    values,
    fieldErrors,
    formError,
    selectableRoles,
    setFieldErrors,
    setFormError,
    clearPassword,
    handleTextChange,
    handleSelectChange,
    handleFormBlur,
  };
}
