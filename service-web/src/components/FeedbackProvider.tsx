import { Alert, Snackbar } from "@mui/material";
import { createContext, useCallback, useContext, useMemo, useState } from "react";
import type { AlertColor } from "@mui/material";
import type { PropsWithChildren, SyntheticEvent } from "react";

/** 描述用于全局展示的短时、已净化反馈消息。 */
interface FeedbackEntry {
  severity: AlertColor;
  message: string;
}

/** 描述应用页面可用的安全反馈动作。 */
interface FeedbackContextValue {
  success: (message: string) => void;
  error: (message: string) => void;
  info: (message: string) => void;
}

/** 应用级 Provider 外不提供反馈 API。 */
const FeedbackContext = createContext<FeedbackContextValue | undefined>(undefined);

/** 为非敏感 UI 结果提供可访问的应用级短时反馈区域。 */
export function FeedbackProvider({ children }: PropsWithChildren) {
  const [entry, setEntry] = useState<FeedbackEntry | null>(null);

  /** 用已知安全的消息与级别替换上一条反馈。 */
  const show = useCallback((severity: AlertColor, message: string) => {
    setEntry({ severity, message });
  }, []);

  /** 发布成功消息，不暴露响应数据。 */
  const success = useCallback(
    (message: string) => {
      show("success", message);
    },
    [show],
  );

  /** 发布通用错误消息，不保留服务端 Problem detail。 */
  const error = useCallback(
    (message: string) => {
      show("error", message);
    },
    [show],
  );

  /** 发布中性状态消息。 */
  const info = useCallback(
    (message: string) => {
      show("info", message);
    },
    [show],
  );

  /** 超时或显式关闭时移除反馈。 */
  const handleClose = useCallback((_event?: SyntheticEvent | Event, reason?: string) => {
    if (reason === "clickaway") {
      return;
    }

    setEntry(null);
  }, []);

  /** 发布回调未变化时保持上下文引用稳定。 */
  const value = useMemo<FeedbackContextValue>(
    () => ({ success, error, info }),
    [error, info, success],
  );

  return (
    <FeedbackContext.Provider value={value}>
      {children}
      <Snackbar
        open={entry !== null}
        autoHideDuration={4_000}
        onClose={handleClose}
        anchorOrigin={{ vertical: "bottom", horizontal: "right" }}
      >
        <Alert severity={entry?.severity ?? "info"} variant="filled" onClose={handleClose}>
          {entry?.message}
        </Alert>
      </Snackbar>
    </FeedbackContext.Provider>
  );
}

/** 读取全局反馈动作，并强制要求应用级 Provider。 */
export function useFeedback(): FeedbackContextValue {
  const context = useContext(FeedbackContext);

  if (context === undefined) {
    throw new Error("useFeedback must be used inside FeedbackProvider.");
  }

  return context;
}
