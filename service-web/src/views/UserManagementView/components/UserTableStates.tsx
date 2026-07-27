import { SearchOffOutlined as SearchOffOutlinedIcon } from "@mui/icons-material";
import {
  Box,
  Button,
  Skeleton,
  Stack,
  TableBody,
  TableCell,
  TableRow,
  Typography,
} from "@mui/material";

import { brandColors } from "../../../styles/design-tokens";

/** 列表加载时渲染与正式表格相同的行几何。 */
export function UserTableSkeleton() {
  const skeletonRows = [0, 1, 2, 3] as const;

  return (
    <TableBody>
      {/* 为预期首屏结果渲染固定数量的稳定骨架行。 */}
      {skeletonRows.map((index) => (
        <TableRow key={index}>
          <TableCell>
            <Skeleton variant="text" width="72%" />
          </TableCell>
          <TableCell>
            <Skeleton variant="text" width="60%" />
          </TableCell>
          <TableCell>
            <Skeleton variant="rounded" width={64} height={24} />
          </TableCell>
          <TableCell>
            <Skeleton variant="text" width="84%" />
          </TableCell>
          <TableCell>
            <Skeleton variant="text" width="84%" />
          </TableCell>
          <TableCell>
            <Skeleton variant="text" width="84%" />
          </TableCell>
          <TableCell>
            <Skeleton variant="rounded" width={104} height={32} sx={{ ml: "auto" }} />
          </TableCell>
        </TableRow>
      ))}
    </TableBody>
  );
}

/** 描述空列表状态提供的恢复动作。 */
interface UserTableEmptyProps {
  onReset: () => void;
}

/** 渲染没有匹配用户时的可恢复空状态。 */
export function UserTableEmpty({ onReset }: UserTableEmptyProps) {
  return (
    <TableBody>
      <TableRow>
        <TableCell colSpan={7} align="center" sx={{ py: 8 }}>
          <Stack spacing={1.5} alignItems="center">
            <Box
              sx={{
                width: 48,
                height: 48,
                display: "grid",
                placeItems: "center",
                borderRadius: 1.5,
                bgcolor: brandColors.primaryLighter,
                color: "primary.main",
              }}
            >
              <SearchOffOutlinedIcon />
            </Box>
            <Typography fontWeight={700}>没有匹配用户</Typography>
            <Typography variant="body2" color="text.secondary">
              调整筛选条件后重试。
            </Typography>
            <Button size="small" onClick={onReset}>
              重置筛选
            </Button>
          </Stack>
        </TableCell>
      </TableRow>
    </TableBody>
  );
}
