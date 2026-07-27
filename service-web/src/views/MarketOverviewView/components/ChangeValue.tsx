import {
  ArrowDownwardRounded as ArrowDownwardRoundedIcon,
  ArrowUpwardRounded as ArrowUpwardRoundedIcon,
} from "@mui/icons-material";
import { Stack, Typography } from "@mui/material";

/** 用显式图标与中国市场语义色复用有方向的百分比。 */
export function ChangeValue({ value }: { value: number }) {
  const positive = value >= 0;

  return (
    <Stack
      direction="row"
      spacing={0.25}
      alignItems="center"
      color={positive ? "error.main" : "success.main"}
    >
      {positive ? (
        <ArrowUpwardRoundedIcon fontSize="inherit" />
      ) : (
        <ArrowDownwardRoundedIcon fontSize="inherit" />
      )}
      <Typography component="span" fontWeight={700}>
        {`${positive ? "+" : ""}${value.toFixed(2)}%`}
      </Typography>
    </Stack>
  );
}
