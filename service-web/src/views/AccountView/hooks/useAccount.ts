import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import type { ChangeEvent, FormEvent } from "react";

import {
  revokeOtherSessionFamilies,
  revokeSessionFamily,
  sessionFamiliesQueryOptions,
} from "../../../api/account-security";
import {
  changeCurrentPassword,
  currentProfileQueryOptions,
  updateCurrentProfile,
} from "../../../api/account";
import { authMeQueryKey, authSession } from "../../../api/auth-session";
import { isApiError } from "../../../api/http";
import { useAuth } from "../../../components/AuthProvider";
import { useFeedback } from "../../../components/FeedbackProvider";
import type { SessionFamily } from "../../../types/account-security";
import { parseAccountDialogState, serializeAccountDialogState } from "../utils/account-url";
import type { AccountDialogState } from "../utils/account-url";

/** 管理个人资料、Session family、URL Dialog 与敏感动作生命周期。 */
export function useAccount() {
  const { hasPermission } = useAuth();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [searchParameters, setSearchParameters] = useSearchParams();
  const { error: showError, success } = useFeedback();
  const canReadSessions = hasPermission("sessions:read");
  const canRevokeSessions = hasPermission("sessions:revoke");
  const profileQuery = useQuery(currentProfileQueryOptions());
  const sessionQuery = useQuery({
    ...sessionFamiliesQueryOptions({ pageSize: 50 }),
    enabled: canReadSessions,
  });
  const dialogState = useMemo(() => parseAccountDialogState(searchParameters), [searchParameters]);
  const [displayName, setDisplayName] = useState("");
  const [loadedEtag, setLoadedEtag] = useState<string | null>(null);
  const [profileError, setProfileError] = useState<string | null>(null);
  const [profileConflict, setProfileConflict] = useState(false);

  /** 首次资料加载时建立可编辑草稿；后台刷新不得覆盖未提交输入。 */
  useEffect(() => {
    if (loadedEtag === null && profileQuery.data !== undefined) {
      setDisplayName(profileQuery.data.user.displayName);
      setLoadedEtag(profileQuery.data.etag);
    }
  }, [loadedEtag, profileQuery.data]);

  const profileMutation = useMutation({
    /** 提交显示名称与当前已加载的强 `ETag`。 */
    mutationFn: async () => {
      if (loadedEtag === null) {
        throw new Error("Profile ETag is unavailable.");
      }

      return updateCurrentProfile({ displayName: displayName.trim() }, loadedEtag);
    },
    /** 同步资料与身份缓存，并使工作台审计摘要重新获取。 */
    onSuccess: async (nextProfile) => {
      queryClient.setQueryData(["account", "profile"], nextProfile);
      queryClient.setQueryData(authMeQueryKey, nextProfile.user);
      setDisplayName(nextProfile.user.displayName);
      setLoadedEtag(nextProfile.etag);
      setProfileConflict(false);
      setProfileError(null);
      await queryClient.invalidateQueries({ queryKey: ["audit-events"] });
      success("个人资料已保存。");
    },
    /** 保留 412 草稿；其他错误只显示安全的本地恢复文案。 */
    onError: (error: unknown) => {
      if (isApiError(error) && error.status === 412) {
        setProfileConflict(true);
        setProfileError("资料已在其他窗口更新。当前草稿仍保留，请重新加载后再确认。");
        return;
      }

      setProfileError("资料保存失败，请稍后重试。");
    },
  });

  const passwordMutation = useMutation({
    mutationFn: changeCurrentPassword,
    /** 改密会使全部服务端 Session 失效，必须同步清除浏览器内存状态。 */
    onSuccess: () => {
      authSession.clear(false, "password-changed");
      void navigate("/login?reason=password-changed", { replace: true });
    },
  });

  const revokeSessionMutation = useMutation({
    mutationFn: async (family: SessionFamily) => {
      await revokeSessionFamily(family.familyId);
      return family;
    },
    /** 当前 family 撤销后立刻退出；其他 family 仅刷新安全查询。 */
    onSuccess: async (family) => {
      closeDialog();
      if (family.current) {
        authSession.clear(false, "session-revoked");
        void navigate("/login?reason=session-revoked", { replace: true });
        return;
      }

      await invalidateSessionState();
      success("会话已退出。");
    },
    onError: () => {
      showError("退出会话失败，请稍后重试。");
    },
  });

  const revokeOthersMutation = useMutation({
    mutationFn: revokeOtherSessionFamilies,
    onSuccess: async (result) => {
      closeDialog();
      await invalidateSessionState();
      success(
        result.revokedFamilyCount === 0
          ? "当前没有其他活动会话。"
          : `已退出 ${result.revokedFamilyCount} 个其他会话。`,
      );
    },
    onError: () => {
      showError("退出其他会话失败，请稍后重试。");
    },
  });

  /** 关闭 URL 所有的 Dialog，并让 React Router 恢复触发按钮焦点。 */
  const closeDialog = useCallback(() => {
    setSearchParameters(serializeAccountDialogState(undefined), { replace: true });
  }, [setSearchParameters]);

  /** 更新 URL 所有的 Dialog，不把任何敏感输入放入地址。 */
  const openDialog = useCallback(
    (state: AccountDialogState) => {
      setSearchParameters(serializeAccountDialogState(state));
    },
    [setSearchParameters],
  );

  /** 使个人中心与工作台共享的 Session 查询失效。 */
  const invalidateSessionState = useCallback(async () => {
    await queryClient.invalidateQueries({ queryKey: ["auth", "session-families"] });
  }, [queryClient]);

  /** 更新显示名称草稿并清除上一次表单错误。 */
  const handleDisplayNameChange = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    setDisplayName(event.target.value);
    setProfileError(null);
    setProfileConflict(false);
  }, []);

  /** 校验并提交本人资料。 */
  const handleProfileSubmit = useCallback(
    (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      const normalizedName = displayName.trim();

      if (normalizedName.length < 1 || normalizedName.length > 120) {
        setProfileError("显示名称需为 1–120 个字符。");
        return;
      }

      profileMutation.mutate();
    },
    [displayName, profileMutation],
  );

  /** 用户确认冲突恢复后加载权威资料，并丢弃旧草稿。 */
  const handleReloadProfile = useCallback(async () => {
    const result = await profileQuery.refetch();

    if (result.data !== undefined) {
      setDisplayName(result.data.user.displayName);
      setLoadedEtag(result.data.etag);
      setProfileConflict(false);
      setProfileError(null);
    }
  }, [profileQuery]);

  /** 手动刷新 Session 列表时保留已加载行。 */
  const refreshSessions = useCallback(async () => {
    await sessionQuery.refetch();
  }, [sessionQuery]);

  return {
    canReadSessions,
    canRevokeSessions,
    profileQuery,
    sessionQuery,
    dialogState,
    displayName,
    loadedEtag,
    profileError,
    profileConflict,
    isSavingProfile: profileMutation.isPending,
    passwordMutation,
    revokeSessionMutation,
    revokeOthersMutation,
    handleDisplayNameChange,
    handleProfileSubmit,
    handleReloadProfile,
    refreshSessions,
    openDialog,
    closeDialog,
  };
}
