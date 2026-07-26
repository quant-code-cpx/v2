import { CssBaseline, ThemeProvider } from "@mui/material";
import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "react-router-dom";

import { queryClient } from "./api/query-client";
import { AuthProvider } from "./components/AuthProvider";
import { FeedbackProvider } from "./components/FeedbackProvider";
import { createAppRouter } from "./router";
import { createAppTheme } from "./styles/theme";

const router = createAppRouter();
const theme = createAppTheme();

/** 组合固定浅色桌面主题、查询、鉴权、反馈与路由运行时。 */
export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider theme={theme}>
        <CssBaseline enableColorScheme />
        <AuthProvider>
          <FeedbackProvider>
            <RouterProvider router={router} />
          </FeedbackProvider>
        </AuthProvider>
      </ThemeProvider>
    </QueryClientProvider>
  );
}
