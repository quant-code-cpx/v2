import { SearchOffOutlined as SearchOffOutlinedIcon } from "@mui/icons-material";
import { Box, Stack, TableCell, TableRow, Typography } from "@mui/material";

/** 描述数据运维表格空态需要占据的列数和业务文案。 */
interface DataOperationsTableEmptyStateProps {
  colSpan: number;
  title: string;
  description: string;
}

/** 在表格主体内呈现可辨认空态，避免只留下表头造成加载失败的错觉。 */
export function DataOperationsTableEmptyState({
  colSpan,
  title,
  description,
}: DataOperationsTableEmptyStateProps) {
  return (
    <TableRow>
      <TableCell colSpan={colSpan} align="center" sx={{ py: 8 }}>
        <Stack spacing={1.5} alignItems="center">
          <Box
            sx={{
              width: 48,
              height: 48,
              display: "grid",
              placeItems: "center",
              borderRadius: 1.5,
              bgcolor: "action.selected",
              color: "primary.main",
            }}
          >
            <SearchOffOutlinedIcon />
          </Box>
          <Typography fontWeight={700}>{title}</Typography>
          <Typography variant="body2" color="text.secondary">
            {description}
          </Typography>
        </Stack>
      </TableCell>
    </TableRow>
  );
}
