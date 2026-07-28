import {
  Button,
  Card,
  CardContent,
  Checkbox,
  FormControlLabel,
  MenuItem,
  Stack,
  TextField,
} from "@mui/material";
import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import { auditCategories } from "../../../types/account-security";
import type { AuditCategory } from "../../../types/account-security";
import { auditCategoryLabel } from "../utils/audit-event-presentation";
import { auditRanges, auditRangeLabel, isUuid } from "../utils/audit-event-url";
import type { AuditRange, AuditUrlState } from "../utils/audit-event-url";

/** 描述审计筛选区的 URL 状态与动作。 */
interface AuditFiltersProps {
  state: AuditUrlState;
  isFetching: boolean;
  onApply: (
    filters: Pick<AuditUrlState, "category" | "range" | "actorId" | "targetId" | "includeRoutine">,
  ) => void;
  onReset: () => void;
}

/** 渲染可应用、可重置且最终进入 URL 的审计筛选表单。 */
export function AuditFilters({ state, isFetching, onApply, onReset }: AuditFiltersProps) {
  const [category, setCategory] = useState<AuditCategory | "">(state.category ?? "");
  const [range, setRange] = useState<AuditRange>(state.range);
  const [actorId, setActorId] = useState(state.actorId ?? "");
  const [targetId, setTargetId] = useState(state.targetId ?? "");
  const [includeRoutine, setIncludeRoutine] = useState(state.includeRoutine);
  const actorIdInvalid = actorId.length > 0 && !isUuid(actorId);
  const targetIdInvalid = targetId.length > 0 && !isUuid(targetId);

  /** 浏览器返回或外部 URL 变化时同步非敏感筛选草稿。 */
  useEffect(() => {
    setCategory(state.category ?? "");
    setRange(state.range);
    setActorId(state.actorId ?? "");
    setTargetId(state.targetId ?? "");
    setIncludeRoutine(state.includeRoutine);
  }, [state]);

  /** 校验 UUID 后把筛选整体提交给页面 URL。 */
  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (actorIdInvalid || targetIdInvalid) {
      return;
    }

    onApply({
      range,
      includeRoutine,
      ...(category === "" ? {} : { category }),
      ...(actorId.trim().length === 0 ? {} : { actorId: actorId.trim() }),
      ...(targetId.trim().length === 0 ? {} : { targetId: targetId.trim() }),
    });
  }

  return (
    <Card component="section" aria-label="审计筛选">
      <CardContent component="form" noValidate onSubmit={handleSubmit}>
        <Stack direction="row" spacing={1.5} alignItems="flex-start">
          <TextField
            select
            label="分类"
            value={category}
            onChange={(event) => setCategory(event.target.value as AuditCategory | "")}
            sx={{ width: 150 }}
          >
            <MenuItem value="">全部分类</MenuItem>
            {auditCategories.map((item) => (
              <MenuItem key={item} value={item}>
                {auditCategoryLabel(item)}
              </MenuItem>
            ))}
          </TextField>
          <TextField
            select
            label="时间范围"
            value={range}
            onChange={(event) => setRange(event.target.value as AuditRange)}
            sx={{ width: 155 }}
          >
            {auditRanges.map((item) => (
              <MenuItem key={item} value={item}>
                {auditRangeLabel(item)}
              </MenuItem>
            ))}
          </TextField>
          <TextField
            label="Actor ID"
            value={actorId}
            onChange={(event) => setActorId(event.target.value)}
            error={actorIdInvalid}
            helperText={actorIdInvalid ? "请输入有效 UUID。" : " "}
            sx={{ flex: 1 }}
          />
          <TextField
            label="目标 ID"
            value={targetId}
            onChange={(event) => setTargetId(event.target.value)}
            error={targetIdInvalid}
            helperText={targetIdInvalid ? "请输入有效 UUID。" : " "}
            sx={{ flex: 1 }}
          />
          <FormControlLabel
            control={
              <Checkbox
                checked={includeRoutine}
                onChange={(event) => setIncludeRoutine(event.target.checked)}
              />
            }
            label="含例行轮换"
            sx={{ minHeight: 56, mx: 0 }}
          />
          <Button
            type="submit"
            variant="contained"
            size="large"
            disabled={actorIdInvalid || targetIdInvalid || isFetching}
          >
            应用筛选
          </Button>
          <Button type="button" size="large" onClick={onReset}>
            重置
          </Button>
        </Stack>
      </CardContent>
    </Card>
  );
}
