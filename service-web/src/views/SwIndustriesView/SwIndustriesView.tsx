import { useEffect, useMemo } from "react";
import type { ChangeEvent } from "react";
import {
  Alert,
  Button,
  Card,
  CardContent,
  Chip,
  FormControl,
  InputLabel,
  MenuItem,
  Select,
  Skeleton,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TextField,
  Typography,
} from "@mui/material";
import type { SelectChangeEvent } from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { Link as RouterLink, useSearchParams } from "react-router-dom";

import { swIndustryListQueryOptions, swIndustryValuationsQueryOptions } from "../../api/market";
import { MarketDataState } from "../../components/MarketDataState";
import { MarketPageHeader } from "../../components/MarketPageHeader";
import { formatMarketDateTime, formatSourceDecimal } from "../../utils/market-formatters";

/** 从 URL 读取申万层级，未知值表示全部层级。 */
function parseLevel(value: string | null): 1 | 2 | 3 | undefined {
  return value === "1" || value === "2" || value === "3" ? (Number(value) as 1 | 2 | 3) : undefined;
}

/** 渲染申万一级、二级、三级 taxonomy，并把估值 publication 保持为独立失败边界。 */
export function SwIndustriesView() {
  const [searchParams, setSearchParams] = useSearchParams();
  const level = parseLevel(searchParams.get("level"));
  const snapshotDate = searchParams.get("snapshotDate") ?? undefined;
  const parentCode = searchParams.get("parentCode")?.trim() || undefined;
  const cursor = searchParams.get("cursor") ?? undefined;

  const taxonomyQuery = useQuery(
    swIndustryListQueryOptions({
      level,
      parentCode,
      snapshotDate,
      cursor,
      limit: 100,
    }),
  );
  const valuationQuery = useQuery(
    swIndustryValuationsQueryOptions({
      level,
      snapshotDate,
      limit: 500,
    }),
  );
  const valuationsByCode = useMemo(
    /** 仅按同一申万稳定代码构造页面内估值索引。 */
    () =>
      new Map(
        (valuationQuery.data?.payload.items ?? []).map(
          /** 用申万稳定代码关联展示，不通过名称做语义映射。 */
          (valuation) => [valuation.code, valuation],
        ),
      ),
    [valuationQuery.data],
  );

  /** 规范 URL 参数，使未知层级不会产生不同但等价的链接。 */
  useEffect(() => {
    const canonical = new URLSearchParams();
    if (level !== undefined) canonical.set("level", String(level));
    if (snapshotDate !== undefined) canonical.set("snapshotDate", snapshotDate);
    if (parentCode !== undefined) canonical.set("parentCode", parentCode);
    if (cursor !== undefined) canonical.set("cursor", cursor);
    if (canonical.toString() !== searchParams.toString()) {
      setSearchParams(canonical, { replace: true });
    }
  }, [cursor, level, parentCode, searchParams, setSearchParams, snapshotDate]);

  /** 合并筛选并清除旧 taxonomy publication 的游标。 */
  function updateUrl(patch: Record<string, string | undefined>, preserveCursor = false): void {
    const next = new URLSearchParams(searchParams);
    Object.entries(patch).forEach(
      /** 空值删除参数，其余筛选原样写入。 */
      ([key, value]) => {
        if (value === undefined || value.length === 0) next.delete(key);
        else next.set(key, value);
      },
    );
    if (!preserveCursor) next.delete("cursor");
    setSearchParams(next);
  }

  /** 切换 taxonomy 层级。 */
  function handleLevelChange(event: SelectChangeEvent): void {
    updateUrl({ level: event.target.value || undefined });
  }

  /** 更新直接父级代码筛选。 */
  function handleParentChange(event: ChangeEvent<HTMLInputElement>): void {
    updateUrl({ parentCode: event.target.value || undefined });
  }

  /** 并行条件刷新 taxonomy 与估值两个 publication。 */
  function handleRefresh(): void {
    void Promise.all([taxonomyQuery.refetch(), valuationQuery.refetch()]);
  }

  /** 返回 taxonomy 第一页。 */
  function handleFirstPage(): void {
    updateUrl({ cursor: undefined }, true);
  }

  /** 写入 taxonomy 服务端不透明下一游标。 */
  function handleNextPage(): void {
    const nextCursor = taxonomyQuery.data?.payload.nextCursor;
    if (nextCursor !== null && nextCursor !== undefined) {
      updateUrl({ cursor: nextCursor }, true);
    }
  }

  return (
    <Stack spacing={3}>
      <MarketPageHeader
        title="申万行业"
        subtitle="展示申万独立 taxonomy 与来源估值；不把东财行业或概念按名称直接等同。"
        status={
          <Stack direction="row" spacing={1} alignItems="center">
            <Chip size="small" variant="outlined" label="sw.industry" />
            <Button component={RouterLink} to="/market/sectors" size="small" variant="outlined">
              东财行业与概念
            </Button>
          </Stack>
        }
        onRefresh={handleRefresh}
        refreshing={taxonomyQuery.isFetching || valuationQuery.isFetching}
      />
      <Card>
        <CardContent>
          <Stack direction="row" spacing={2} alignItems="center">
            <FormControl size="small" sx={{ minWidth: 144 }}>
              <InputLabel id="sw-level-label">taxonomy 层级</InputLabel>
              <Select
                labelId="sw-level-label"
                value={level === undefined ? "" : String(level)}
                label="taxonomy 层级"
                onChange={handleLevelChange}
              >
                <MenuItem value="">全部层级</MenuItem>
                <MenuItem value="1">申万一级</MenuItem>
                <MenuItem value="2">申万二级</MenuItem>
                <MenuItem value="3">申万三级</MenuItem>
              </Select>
            </FormControl>
            <TextField
              size="small"
              label="直接父级代码"
              value={parentCode ?? ""}
              onChange={handleParentChange}
              placeholder="例如 801010.SI"
              sx={{ width: 220 }}
            />
            <Typography variant="body2" color="text.secondary" sx={{ ml: "auto" }}>
              估值字段逐项保留来源可用性，不使用行业名称做跨体系连接。
            </Typography>
          </Stack>
        </CardContent>
      </Card>
      {valuationQuery.isError ? (
        <Alert severity="warning">
          估值 publication 读取失败；taxonomy 仍可浏览，所有估值单元格明确标记为当前不可用。
        </Alert>
      ) : null}
      <Card component="section" aria-label="申万 taxonomy 与估值">
        <CardContent>
          <Stack direction="row" justifyContent="space-between" alignItems="baseline">
            <Typography variant="h6">taxonomy 与估值</Typography>
            {taxonomyQuery.data === undefined ? null : (
              <Typography variant="caption" color="text.secondary">
                分类快照 {taxonomyQuery.data.payload.release.snapshotDate} ·{" "}
                {formatMarketDateTime(taxonomyQuery.data.payload.release.publishedAt)}
              </Typography>
            )}
          </Stack>
          <Alert severity="info" sx={{ mt: 1 }}>
            PE 与 PB 仅展示其自身来源字段；PE_TTM 和股息率在当前 `sw_daily`
            方法学中未报告，不能补零或借用其他供应商字段。
          </Alert>
          {taxonomyQuery.isPending ? (
            <Skeleton variant="rounded" height={560} sx={{ mt: 2 }} />
          ) : taxonomyQuery.data === undefined ? (
            <MarketDataState
              variant="error"
              title="申万 taxonomy 不可用"
              message="没有通过严格合同的分类 publication，页面不会用东财目录替代。"
              onRetry={
                /** 仅重试申万 taxonomy。 */
                () => void taxonomyQuery.refetch()
              }
              minHeight={520}
            />
          ) : taxonomyQuery.data.payload.items.length === 0 ? (
            <MarketDataState
              variant="empty"
              title="筛选下没有申万节点"
              message="请调整层级或父级代码；当前 publication 本身有效。"
              minHeight={420}
            />
          ) : (
            <>
              <TableContainer sx={{ mt: 1 }}>
                <Table size="small" aria-label="申万行业 taxonomy">
                  <TableHead>
                    <TableRow>
                      <TableCell>行业节点</TableCell>
                      <TableCell>层级</TableCell>
                      <TableCell>直接父级</TableCell>
                      <TableCell align="right">成分数</TableCell>
                      <TableCell align="right">PE</TableCell>
                      <TableCell align="right">PB</TableCell>
                      <TableCell align="right">PE_TTM</TableCell>
                      <TableCell align="right">股息率</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {taxonomyQuery.data.payload.items.map(
                      /** 按稳定申万代码关联独立估值页，缺页不伪装为来源数值空。 */
                      (industry) => {
                        const valuation = valuationsByCode.get(industry.code);
                        return (
                          <TableRow key={industry.code} hover>
                            <TableCell>
                              <Typography
                                component={RouterLink}
                                to={`/market/industries/sw/${encodeURIComponent(industry.code)}`}
                                variant="body2"
                                color="primary.main"
                                fontWeight={700}
                                sx={{ textDecoration: "none" }}
                              >
                                {industry.name}
                              </Typography>
                              <Typography variant="caption" color="text.secondary">
                                {industry.code}
                              </Typography>
                            </TableCell>
                            <TableCell>申万{industry.level}级</TableCell>
                            <TableCell>{industry.parentCode ?? "根节点"}</TableCell>
                            <TableCell align="right">{industry.componentCount}</TableCell>
                            <TableCell align="right">
                              {valuationQuery.data === undefined
                                ? "估值 publication 不可用"
                                : valuation === undefined
                                  ? "当前估值页未覆盖"
                                  : formatSourceDecimal(valuation.staticPe)}
                            </TableCell>
                            <TableCell align="right">
                              {valuationQuery.data === undefined
                                ? "估值 publication 不可用"
                                : valuation === undefined
                                  ? "当前估值页未覆盖"
                                  : formatSourceDecimal(valuation.pb)}
                            </TableCell>
                            <TableCell align="right">来源未报告</TableCell>
                            <TableCell align="right">来源未报告</TableCell>
                          </TableRow>
                        );
                      },
                    )}
                  </TableBody>
                </Table>
              </TableContainer>
              <Stack direction="row" justifyContent="space-between" sx={{ mt: 2 }}>
                <Button disabled={cursor === undefined} onClick={handleFirstPage}>
                  返回第一页
                </Button>
                <Button
                  variant="outlined"
                  disabled={taxonomyQuery.data.payload.nextCursor === null}
                  onClick={handleNextPage}
                >
                  下一页
                </Button>
              </Stack>
            </>
          )}
        </CardContent>
      </Card>
    </Stack>
  );
}
