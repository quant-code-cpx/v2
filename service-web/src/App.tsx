import { CssBaseline, ThemeProvider } from "@mui/material";
import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "react-router-dom";

import { queryClient } from "./api/query-client";
import { createAppRouter } from "./router";
import { ColorModeProvider, useAppTheme } from "./styles/theme";

const router = createAppRouter(queryClient);

function RoutedApplication() {
  const theme = useAppTheme();

  return (
    <ThemeProvider theme={theme}>
      <CssBaseline enableColorScheme />
      <RouterProvider router={router} />
    </ThemeProvider>
  );
}

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <ColorModeProvider>
        <RoutedApplication />
      </ColorModeProvider>
    </QueryClientProvider>
  );
}
