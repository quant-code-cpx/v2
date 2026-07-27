import { Box } from "@mui/material";

import { LoginBrandPanel } from "./components/LoginBrandPanel";
import { LoginFormCard } from "./components/LoginFormCard";
import { useLoginForm } from "./hooks/useLoginForm";

/** 渲染唯一匿名路由，并组合相互隔离的品牌区与登录表单。 */
export function LoginView() {
  const loginForm = useLoginForm();

  return (
    <Box
      component="main"
      sx={{
        minHeight: "100vh",
        display: "grid",
        gridTemplateColumns: "42fr 58fr",
        bgcolor: "background.default",
      }}
    >
      <LoginBrandPanel />
      <Box
        sx={{
          px: 10,
          py: 10,
          display: "grid",
          placeItems: "center",
          bgcolor: "background.paper",
          borderLeft: 1,
          borderColor: "divider",
        }}
      >
        <LoginFormCard model={loginForm} />
      </Box>
    </Box>
  );
}
