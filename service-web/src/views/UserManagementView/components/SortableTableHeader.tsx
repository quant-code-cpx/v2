import { TableCell, TableSortLabel } from "@mui/material";
import { useCallback } from "react";

import type { UserListFilters } from "../../../types/access";

/** 描述与 URL 排序状态绑定的表头。 */
interface SortableTableHeaderProps {
  label: string;
  sort: NonNullable<UserListFilters["sort"]>;
  activeSort: UserListFilters["sort"];
  order: UserListFilters["order"];
  onSort: (sort: NonNullable<UserListFilters["sort"]>) => void;
  width: string;
}

/** 渲染标准表头排序控件，并保留 URL 所有权。 */
export function SortableTableHeader({
  label,
  sort,
  activeSort,
  order,
  onSort,
  width,
}: SortableTableHeaderProps) {
  const isActive = activeSort === sort;
  const currentOrderLabel = order === "asc" ? "升序" : "降序";

  /** 将当前字段传给 URL 排序动作。 */
  const handleSort = useCallback(() => {
    onSort(sort);
  }, [onSort, sort]);

  return (
    <TableCell
      width={width}
      sortDirection={isActive ? order : false}
      sx={{ px: 1.5, whiteSpace: "nowrap" }}
    >
      <TableSortLabel
        active={isActive}
        direction={isActive ? order : "asc"}
        onClick={handleSort}
        aria-label={`按${label}排序，${isActive ? `当前${currentOrderLabel}` : "当前未排序"}`}
      >
        {label}
      </TableSortLabel>
    </TableCell>
  );
}
