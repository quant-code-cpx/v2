import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useCallback, useMemo, useRef, useState } from "react";

import {
  createDataOperationIdempotencyKey,
  preflightDataSync,
  submitDataSync,
} from "../../../api/data-operations";
import { isApiError } from "../../../api/http";
import type {
  DatasetSummary,
  SubmissionReceipt,
  SyncMode,
  SyncPreflight,
  SyncPreflightTarget,
  TargetSelector,
} from "../../../types/data-operations";
import {
  createDefaultTargetSelector,
  isTargetSelectorStructurallyReady,
} from "../utils/target-selector";

/** 根据模式创建不携带无关日期字段的同步 target 草稿。 */
function targetForMode(
  datasetCode: string,
  mode: SyncMode,
  selector: TargetSelector,
): SyncPreflightTarget {
  if (mode === "DATE_RANGE") {
    return {
      datasetCode,
      mode,
      selector,
      dateFrom: null,
      dateTo: null,
      observationDate: null,
    };
  }
  if (mode === "OBSERVATION_DATE") {
    return {
      datasetCode,
      mode,
      selector,
      dateFrom: null,
      dateTo: null,
      observationDate: null,
    };
  }

  return {
    datasetCode,
    mode,
    selector,
    dateFrom: null,
    dateTo: null,
    observationDate: null,
  };
}

/** 从服务端 capability 构造一个数据集的首个可用人工同步 target。 */
function initialTarget(dataset: DatasetSummary): SyncPreflightTarget | undefined {
  const mode = dataset.capability.supportedModes[0];
  const selector = createDefaultTargetSelector(
    dataset.capability.selectorKinds,
    dataset.datasetCode,
  );
  return mode === undefined || selector === undefined
    ? undefined
    : targetForMode(dataset.datasetCode, mode, selector);
}

/** 验证前端表单的基础结构，真正能力与日期边界仍以预检为权威。 */
function isTargetStructurallyReady(target: SyncPreflightTarget): boolean {
  if (!isTargetSelectorStructurallyReady(target.selector)) return false;
  if (target.mode === "DATE_RANGE") {
    return target.dateFrom !== null && target.dateTo !== null && target.dateFrom <= target.dateTo;
  }
  if (target.mode === "OBSERVATION_DATE") {
    return target.observationDate !== null;
  }

  return true;
}

/** 描述同步 Dialog Hook 的输入边界。 */
interface UseSyncCommandDialogInput {
  datasets: DatasetSummary[];
  onSubmission: (receipt: SubmissionReceipt) => void;
}

/** 管理 capability-aware target 草稿、预检快照和稳定幂等提交。 */
export function useSyncCommandDialog({ datasets, onSubmission }: UseSyncCommandDialogInput) {
  const queryClient = useQueryClient();
  const [targets, setTargets] = useState<SyncPreflightTarget[]>(() =>
    datasets.flatMap((dataset) => {
      const target = initialTarget(dataset);
      return target === undefined ? [] : [target];
    }),
  );
  const [reason, setReason] = useState("");
  const [preflight, setPreflight] = useState<SyncPreflight | undefined>();
  const idempotencyKeyRef = useRef<string | undefined>(undefined);

  /** 用数据集 capability 判断当前草稿是否可请求预检。 */
  const canPreflight = useMemo(
    () =>
      targets.length === datasets.length &&
      targets.length > 0 &&
      targets.every((target) => isTargetStructurallyReady(target)),
    [datasets.length, targets],
  );

  /** 请求无副作用预检，并冻结本次可提交的服务端 target 顺序。 */
  const preflightMutation = useMutation({
    mutationFn: preflightDataSync,
    /** 只在服务端接受预检后保留可提交快照。 */
    onSuccess: (result) => {
      setPreflight(result);
      idempotencyKeyRef.current = undefined;
    },
  });

  /** 以同一稳定键提交预检冻结的 target，首次结果仍只表示 PENDING。 */
  const submitMutation = useMutation({
    mutationFn: async (): Promise<SubmissionReceipt> => {
      if (preflight === undefined) {
        throw new Error("同步预检尚未完成。");
      }
      const idempotencyKey = idempotencyKeyRef.current ?? createDataOperationIdempotencyKey();
      idempotencyKeyRef.current = idempotencyKey;

      return submitDataSync(
        {
          preflightId: preflight.preflightId,
          requestHash: preflight.requestHash,
          // 必须复用预检的 target 与顺序，禁止用页面当前草稿重新排序。
          targets: preflight.targets.map((result) => result.target),
          reason,
        },
        { idempotencyKey },
      );
    },
    /** 提交持久化后刷新只读投影，并把回执交给 submission 跟踪视图。 */
    onSuccess: (receipt) => {
      void queryClient.invalidateQueries({ queryKey: ["dataOperations"] });
      onSubmission(receipt);
    },
  });

  /** 修改一个 target 的模式，立即清除过期预检与旧幂等键。 */
  const setTargetMode = useCallback((datasetCode: string, mode: SyncMode) => {
    setTargets((current) =>
      current.map((target) =>
        target.datasetCode === datasetCode
          ? targetForMode(datasetCode, mode, target.selector)
          : target,
      ),
    );
    setPreflight(undefined);
    idempotencyKeyRef.current = undefined;
  }, []);

  /** 修改一个 target 的受限业务 selector，立即使此前预检与幂等键失效。 */
  const setTargetSelector = useCallback((datasetCode: string, selector: TargetSelector) => {
    setTargets((current) =>
      current.map((target) =>
        target.datasetCode === datasetCode ? { ...target, selector } : target,
      ),
    );
    setPreflight(undefined);
    idempotencyKeyRef.current = undefined;
  }, []);

  /** 修改日期范围起点，立即使此前预检失效。 */
  const setDateFrom = useCallback((datasetCode: string, dateFrom: string | null) => {
    setTargets((current) =>
      current.map((target) =>
        target.datasetCode === datasetCode ? { ...target, dateFrom } : target,
      ),
    );
    setPreflight(undefined);
    idempotencyKeyRef.current = undefined;
  }, []);

  /** 修改日期范围终点，立即使此前预检失效。 */
  const setDateTo = useCallback((datasetCode: string, dateTo: string | null) => {
    setTargets((current) =>
      current.map((target) =>
        target.datasetCode === datasetCode ? { ...target, dateTo } : target,
      ),
    );
    setPreflight(undefined);
    idempotencyKeyRef.current = undefined;
  }, []);

  /** 修改观察日，立即使此前预检失效。 */
  const setObservationDate = useCallback((datasetCode: string, observationDate: string | null) => {
    setTargets((current) =>
      current.map((target) =>
        target.datasetCode === datasetCode ? { ...target, observationDate } : target,
      ),
    );
    setPreflight(undefined);
    idempotencyKeyRef.current = undefined;
  }, []);

  /** 更新操作原因；原因是公开审计的一部分，不进入 URL。 */
  const setOperationReason = useCallback((nextReason: string) => {
    setReason(nextReason);
  }, []);

  /** 仅在草稿完成后请求新的服务端预检。 */
  const requestPreflight = useCallback(() => {
    if (canPreflight) {
      preflightMutation.mutate({ targets });
    }
  }, [canPreflight, preflightMutation, targets]);

  /** 仅在接受预检和填写原因后提交授权意图。 */
  const submit = useCallback(() => {
    if (preflight?.accepted === true && reason.trim().length >= 2) {
      submitMutation.mutate();
    }
  }, [preflight?.accepted, reason, submitMutation]);

  /** 只提取安全 API code，不渲染原始错误正文。 */
  const preflightErrorCode = isApiError(preflightMutation.error)
    ? preflightMutation.error.code
    : undefined;
  /** 只提取安全 API code，不渲染原始错误正文。 */
  const submitErrorCode = isApiError(submitMutation.error) ? submitMutation.error.code : undefined;

  return {
    targets,
    reason,
    preflight,
    canPreflight,
    canSubmit: preflight?.accepted === true && reason.trim().length >= 2,
    isPreflighting: preflightMutation.isPending,
    isSubmitting: submitMutation.isPending,
    preflightErrorCode,
    submitErrorCode,
    setTargetMode,
    setTargetSelector,
    setDateFrom,
    setDateTo,
    setObservationDate,
    setOperationReason,
    requestPreflight,
    submit,
  };
}
