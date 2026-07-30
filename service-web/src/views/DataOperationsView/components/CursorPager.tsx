import { Button, Stack, Typography } from "@mui/material";

/** 描述一个服务端 cursor 页的当前定位和安全翻页回调。 */
interface CursorPagerProps {
  currentCursor?: string | null;
  nextCursor: string | null;
  isLoading: boolean;
  onPageChange: (cursor: string | undefined) => void;
}

/**
 * 只使用所属资源返回的 cursor 翻页，绝不将一个列表的 cursor 发送到其他资源端点。
 *
 * cursor 不可逆推上一页，因此“返回第一页”会清空该资源自己的 cursor，而不是伪造前页 cursor。
 */
export function CursorPager({
  currentCursor,
  nextCursor,
  isLoading,
  onPageChange,
}: CursorPagerProps) {
  if (currentCursor == null && nextCursor === null) return null;

  return (
    <Stack
      direction="row"
      spacing={1}
      alignItems="center"
      justifyContent="flex-end"
      sx={{ px: 3, py: 1.5, borderTop: 1, borderColor: "divider" }}
    >
      <Typography variant="caption" color="text.secondary">
        服务端 cursor 分页
      </Typography>
      {currentCursor != null ? (
        <Button size="small" onClick={() => onPageChange(undefined)} disabled={isLoading}>
          返回第一页
        </Button>
      ) : null}
      {nextCursor !== null ? (
        <Button size="small" onClick={() => onPageChange(nextCursor)} disabled={isLoading}>
          下一页
        </Button>
      ) : null}
    </Stack>
  );
}
