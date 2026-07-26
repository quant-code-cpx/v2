import { Alert, Snackbar } from "@mui/material";
import { createContext, useCallback, useContext, useMemo, useState } from "react";
import type { AlertColor } from "@mui/material";
import type { PropsWithChildren, SyntheticEvent } from "react";

/** Describe a short, pre-sanitized feedback message for global presentation. */
interface FeedbackEntry {
  severity: AlertColor;
  message: string;
}

/** Describe safe feedback operations available to application pages. */
interface FeedbackContextValue {
  success: (message: string) => void;
  error: (message: string) => void;
  info: (message: string) => void;
}

/** Keep no feedback API available outside its application-level provider. */
const FeedbackContext = createContext<FeedbackContextValue | undefined>(undefined);

/** Render one accessible, transient feedback region for non-sensitive UI outcomes. */
export function FeedbackProvider({ children }: PropsWithChildren) {
  const [entry, setEntry] = useState<FeedbackEntry | null>(null);

  /** Replace any prior feedback with a known-safe message and severity. */
  const show = useCallback((severity: AlertColor, message: string) => {
    setEntry({ severity, message });
  }, []);

  /** Publish a success message without exposing response data. */
  const success = useCallback(
    (message: string) => {
      show("success", message);
    },
    [show],
  );

  /** Publish a generic error message without retaining server Problem detail. */
  const error = useCallback(
    (message: string) => {
      show("error", message);
    },
    [show],
  );

  /** Publish a neutral status message. */
  const info = useCallback(
    (message: string) => {
      show("info", message);
    },
    [show],
  );

  /** Dismiss feedback after timeout or an explicit close action. */
  const handleClose = useCallback((_event?: SyntheticEvent | Event, reason?: string) => {
    if (reason === "clickaway") {
      return;
    }

    setEntry(null);
  }, []);

  /** Keep context identity stable until a publishing callback changes. */
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

/** Read global feedback actions and require the application provider. */
export function useFeedback(): FeedbackContextValue {
  const context = useContext(FeedbackContext);

  if (context === undefined) {
    throw new Error("useFeedback must be used inside FeedbackProvider.");
  }

  return context;
}
