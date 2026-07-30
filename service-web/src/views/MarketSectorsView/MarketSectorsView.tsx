import { useEffect } from "react";
import {
  Alert,
  Box,
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
  Typography,
} from "@mui/material";
import type { SelectChangeEvent } from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { Link as RouterLink, useSearchParams } from "react-router-dom";

import {
  marketSectorDirectoryQueryOptions,
  marketSectorEodQueryOptions,
  marketSectorMoneyFlowQueryOptions,
  marketSectorStrengthQueryOptions,
} from "../../api/market";
import { MarketDataState } from "../../components/MarketDataState";
import { MarketDirectionalValue } from "../../components/MarketDirectionalValue";
import { MarketMoneyFlowChart } from "../../components/MarketMoneyFlowChart";
import { MarketPageHeader } from "../../components/MarketPageHeader";
import { MarketStrengthChart } from "../../components/MarketStrengthChart";
import { marketSectorEodSorts, marketSectorSchemes } from "../../types/market";
import type { MarketSectorEodSort, MarketSectorScheme } from "../../types/market";
import {
  formatCoverageRatio,
  formatCnyYi,
  formatMarketDateTime,
  formatSourceDecimal,
} from "../../utils/market-formatters";

const strengthWindows = [1, 5, 20] as const;

/** 从可分享 URL 读取受支持板块体系，未知值回落至东财行业。 */
function parseScheme(value: string | null): MarketSectorScheme {
  return marketSectorSchemes.includes(value as MarketSectorScheme)
    ? (value as MarketSectorScheme)
    : "eastmoney.industry";
}

/** 从可分享 URL 读取受支持横截面排序。 */
function parseSort(value: string | null): MarketSectorEodSort {
  return marketSectorEodSorts.includes(value as MarketSectorEodSort)
    ? (value as MarketSectorEodSort)
    : "changePercent";
}

/** 从可分享 URL 读取强弱窗口，拒绝任意数字。 */
function parseWindow(value: string | null): 1 | 5 | 20 {
  const numeric = Number(value);
  return strengthWindows.includes(numeric as 1 | 5 | 20) ? (numeric as 1 | 5 | 20) : 5;
}

/** 返回板块体系的页面短标签，文案不暗示东财与申万可等同。 */
function schemeLabel(scheme: MarketSectorScheme): string {
  return scheme === "eastmoney.industry" ? "东财行业" : "东财概念";
}

/** 渲染东财行业与概念的 EOD 横截面、目录和已发布强弱分析。 */
export function MarketSectorsView() {
  const [searchParams, setSearchParams] = useSearchParams();
  const scheme = parseScheme(searchParams.get("scheme"));
  const sort = parseSort(searchParams.get("sort"));
  const order = searchParams.get("order") === "asc" ? "asc" : "desc";
  const flowOrder = searchParams.get("flowOrder") === "asc" ? "asc" : "desc";
  const window = parseWindow(searchParams.get("window"));
  const asOf = searchParams.get("asOf") ?? undefined;
  const cursor = searchParams.get("cursor") ?? undefined;

  const directoryQuery = useQuery(marketSectorDirectoryQueryOptions(scheme));
  const eodQuery = useQuery(
    marketSectorEodQueryOptions({
      scheme,
      asOf,
      sort,
      order,
      cursor,
      limit: 50,
    }),
  );
  const strengthQuery = useQuery(
    marketSectorStrengthQueryOptions({
      scheme,
      asOf,
      window,
      order: "desc",
      limit: 20,
    }),
  );
  const moneyFlowQuery = useQuery(
    marketSectorMoneyFlowQueryOptions({
      scheme,
      asOf,
      order: flowOrder,
      limit: 20,
    }),
  );

  /** 把非法或冗余 URL 值规范化，保证复制链接可复现相同查询。 */
  useEffect(() => {
    const canonical = new URLSearchParams();
    if (scheme !== "eastmoney.industry") canonical.set("scheme", scheme);
    if (sort !== "changePercent") canonical.set("sort", sort);
    if (order !== "desc") canonical.set("order", order);
    if (flowOrder !== "desc") canonical.set("flowOrder", flowOrder);
    if (window !== 5) canonical.set("window", String(window));
    if (asOf !== undefined) canonical.set("asOf", asOf);
    if (cursor !== undefined) canonical.set("cursor", cursor);
    if (canonical.toString() !== searchParams.toString()) {
      setSearchParams(canonical, { replace: true });
    }
  }, [asOf, cursor, flowOrder, order, scheme, searchParams, setSearchParams, sort, window]);

  /** 合并筛选到 URL，并在口径变化时清除上一 publication 游标。 */
  function updateUrl(patch: Record<string, string | undefined>, preserveCursor = false): void {
    const next = new URLSearchParams(searchParams);
    Object.entries(patch).forEach(
      /** 空值删除参数，其余值原样交给规范化 effect。 */
      ([key, value]) => {
        if (value === undefined) next.delete(key);
        else next.set(key, value);
      },
    );
    if (!preserveCursor) next.delete("cursor");
    setSearchParams(next);
  }

  /** 切换东财目录体系并回到横截面第一页。 */
  function handleSchemeChange(event: SelectChangeEvent): void {
    updateUrl({ scheme: event.target.value });
  }

  /** 切换服务端横截面排序字段。 */
  function handleSortChange(event: SelectChangeEvent): void {
    updateUrl({ sort: event.target.value });
  }

  /** 切换横截面排序方向。 */
  function handleOrderChange(event: SelectChangeEvent): void {
    updateUrl({ order: event.target.value });
  }

  /** 切换已发布持续性窗口。 */
  function handleWindowChange(event: SelectChangeEvent): void {
    updateUrl({ window: event.target.value });
  }

  /** 切换来源资金流的净流入或净流出优先方向。 */
  function handleFlowOrderChange(event: SelectChangeEvent): void {
    updateUrl({ flowOrder: event.target.value });
  }

  /** 并行条件刷新四个独立 publication。 */
  function handleRefresh(): void {
    void Promise.all([
      directoryQuery.refetch(),
      eodQuery.refetch(),
      strengthQuery.refetch(),
      moneyFlowQuery.refetch(),
    ]);
  }

  /** 返回横截面第一页。 */
  function handleFirstPage(): void {
    updateUrl({ cursor: undefined }, true);
  }

  /** 写入服务端返回的不透明下一游标。 */
  function handleNextPage(): void {
    const nextCursor = eodQuery.data?.payload.nextCursor;
    if (nextCursor !== null && nextCursor !== undefined) {
      updateUrl({ cursor: nextCursor }, true);
    }
  }

  return (
    <Stack spacing={3}>
      <MarketPageHeader
        title="行业与板块"
        subtitle="东财行业与概念各自排名；名称相近不代表与申万 taxonomy 等价。"
        status={
          <Stack direction="row" spacing={1} alignItems="center">
            <Chip size="small" variant="outlined" label={schemeLabel(scheme)} />
            <Button
              component={RouterLink}
              to="/market/industries/sw"
              size="small"
              variant="outlined"
            >
              申万行业体系
            </Button>
          </Stack>
        }
        onRefresh={handleRefresh}
        refreshing={
          directoryQuery.isFetching ||
          eodQuery.isFetching ||
          strengthQuery.isFetching ||
          moneyFlowQuery.isFetching
        }
      />
      <Card>
        <CardContent>
          <Stack direction="row" spacing={2} alignItems="center">
            <FormControl size="small" sx={{ minWidth: 150 }}>
              <InputLabel id="sector-scheme-label">分类体系</InputLabel>
              <Select
                labelId="sector-scheme-label"
                value={scheme}
                label="分类体系"
                onChange={handleSchemeChange}
              >
                <MenuItem value="eastmoney.industry">东财行业</MenuItem>
                <MenuItem value="eastmoney.concept">东财概念</MenuItem>
              </Select>
            </FormControl>
            <FormControl size="small" sx={{ minWidth: 150 }}>
              <InputLabel id="sector-sort-label">横截面排序</InputLabel>
              <Select
                labelId="sector-sort-label"
                value={sort}
                label="横截面排序"
                onChange={handleSortChange}
              >
                <MenuItem value="changePercent">涨跌幅</MenuItem>
                <MenuItem value="turnoverPercent">换手率</MenuItem>
                <MenuItem value="marketValue">市值</MenuItem>
                <MenuItem value="latestValue">板块点位</MenuItem>
                <MenuItem value="leaderChangePercent">领涨股涨幅</MenuItem>
              </Select>
            </FormControl>
            <FormControl size="small" sx={{ minWidth: 120 }}>
              <InputLabel id="sector-order-label">排序方向</InputLabel>
              <Select
                labelId="sector-order-label"
                value={order}
                label="排序方向"
                onChange={handleOrderChange}
              >
                <MenuItem value="desc">从高到低</MenuItem>
                <MenuItem value="asc">从低到高</MenuItem>
              </Select>
            </FormControl>
            <Typography variant="body2" color="text.secondary" sx={{ ml: "auto" }}>
              {directoryQuery.data === undefined
                ? "目录 publication 暂不可用"
                : `当前目录页 ${directoryQuery.data.payload.items.length} 个板块`}
            </Typography>
          </Stack>
        </CardContent>
      </Card>
      {directoryQuery.isError ? (
        <Alert severity="warning">
          板块目录读取失败；横截面与强弱 publication 仍独立展示，不能据其反推完整目录。
        </Alert>
      ) : null}
      {moneyFlowQuery.isError ? (
        <Alert severity="warning">
          板块来源资金流 publication 读取失败；价格强弱与 EOD
          横截面仍独立展示，二者不会替代资金方向。
        </Alert>
      ) : null}
      <Box
        sx={{
          display: "grid",
          gridTemplateColumns: "minmax(0, 1.45fr) minmax(340px, 0.55fr)",
          gap: 2,
        }}
      >
        <Card component="section" aria-label="板块 EOD 横截面排行">
          <CardContent>
            <Stack direction="row" justifyContent="space-between" alignItems="baseline">
              <Typography variant="h6">板块 EOD 横截面</Typography>
              {eodQuery.data === undefined ? null : (
                <Typography variant="caption" color="text.secondary">
                  {eodQuery.data.payload.tradeDate} ·{" "}
                  {formatMarketDateTime(eodQuery.data.payload.publishedAt)} · 输入版本{" "}
                  {eodQuery.data.payload.inputDataVersions.length} 个
                </Typography>
              )}
            </Stack>
            {eodQuery.isPending ? (
              <Skeleton variant="rounded" height={520} sx={{ mt: 2 }} />
            ) : eodQuery.data === undefined ? (
              <MarketDataState
                variant="error"
                title="板块横截面不可用"
                message="真实 EOD publication 未成功载入；不会用板块目录或日 K 线临时计算排行。"
                onRetry={
                  /** 仅重试板块 EOD 横截面。 */
                  () => void eodQuery.refetch()
                }
                minHeight={520}
              />
            ) : eodQuery.data.payload.items.length === 0 ? (
              <MarketDataState
                variant="empty"
                title="当前横截面没有记录"
                message="publication 有效，但当前筛选和游标没有返回板块。"
                minHeight={520}
              />
            ) : (
              <>
                <TableContainer sx={{ mt: 1 }}>
                  <Table size="small" aria-label="板块 EOD 排行">
                    <TableHead>
                      <TableRow>
                        <TableCell>排名 / 板块</TableCell>
                        <TableCell align="right">点位</TableCell>
                        <TableCell align="right">涨跌幅</TableCell>
                        <TableCell align="right">换手率</TableCell>
                        <TableCell align="right">市值</TableCell>
                        <TableCell align="right">上涨 / 下跌</TableCell>
                        <TableCell>领涨股</TableCell>
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {eodQuery.data.payload.items.map(
                        /** 每行保持 scheme 与 sectorCode 联合身份并深链详情。 */
                        (item) => (
                          <TableRow key={`${item.scheme}:${item.code}`} hover>
                            <TableCell>
                              <Typography
                                component={RouterLink}
                                to={`/market/sectors/${item.scheme}/${encodeURIComponent(item.code)}`}
                                variant="body2"
                                fontWeight={700}
                                color="primary.main"
                                sx={{ textDecoration: "none" }}
                              >
                                {item.rank ?? item.position}. {item.name}
                              </Typography>
                              <Typography variant="caption" color="text.secondary">
                                {item.code}
                              </Typography>
                            </TableCell>
                            <TableCell align="right">
                              {formatSourceDecimal(item.latestValue)}
                            </TableCell>
                            <TableCell align="right">
                              <MarketDirectionalValue
                                value={item.changePercent}
                                variant="compact"
                              />
                            </TableCell>
                            <TableCell align="right">
                              {formatSourceDecimal(item.turnoverPercent, "%")}
                            </TableCell>
                            <TableCell align="right">
                              {item.marketValue === null
                                ? "来源未报告"
                                : formatCnyYi(item.marketValue)}
                            </TableCell>
                            <TableCell align="right">
                              {item.advancers === null || item.decliners === null
                                ? "来源未报告"
                                : `${item.advancers} / ${item.decliners}`}
                            </TableCell>
                            <TableCell>
                              <Typography variant="body2">
                                {item.leaderName ?? "来源未报告"}
                              </Typography>
                              <MarketDirectionalValue
                                value={item.leaderChangePercent}
                                variant="compact"
                              />
                            </TableCell>
                          </TableRow>
                        ),
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
                    disabled={eodQuery.data.payload.nextCursor === null}
                    onClick={handleNextPage}
                  >
                    下一页
                  </Button>
                </Stack>
              </>
            )}
          </CardContent>
        </Card>
        <Card component="section" aria-label="板块强弱持续性">
          <CardContent>
            <Stack direction="row" justifyContent="space-between" alignItems="center">
              <Typography variant="h6">强弱与持续性</Typography>
              <FormControl size="small" sx={{ minWidth: 112 }}>
                <InputLabel id="strength-window-label">窗口</InputLabel>
                <Select
                  labelId="strength-window-label"
                  value={String(window)}
                  label="窗口"
                  onChange={handleWindowChange}
                >
                  <MenuItem value="1">1 日</MenuItem>
                  <MenuItem value="5">5 日</MenuItem>
                  <MenuItem value="20">20 日</MenuItem>
                </Select>
              </FormControl>
            </Stack>
            <Typography variant="caption" color="text.secondary">
              服务端已发布方法学内比较；浏览器不按不完整 K 线重算排名。
            </Typography>
            {strengthQuery.isPending ? (
              <Skeleton variant="rounded" height={360} sx={{ mt: 2 }} />
            ) : strengthQuery.data === undefined ? (
              <MarketDataState
                variant="error"
                title="强弱分析不可用"
                message="该派生 publication 失败，不影响左侧 EOD 横截面。"
                onRetry={
                  /** 仅重试强弱 publication。 */
                  () => void strengthQuery.refetch()
                }
                minHeight={360}
              />
            ) : strengthQuery.data.payload.items.length === 0 ? (
              <MarketDataState
                variant="empty"
                title="窗口内没有有效样本"
                message="服务端没有发布满足覆盖质量的强弱记录。"
                minHeight={360}
              />
            ) : (
              <>
                <MarketStrengthChart points={strengthQuery.data.payload.items.slice(0, 12)} />
                <Typography variant="caption" color="text.secondary">
                  方法学版本 {strengthQuery.data.payload.methodologyVersion} ·{" "}
                  {strengthQuery.data.payload.tradeDate}
                </Typography>
              </>
            )}
          </CardContent>
        </Card>
      </Box>
      <Card component="section" aria-label="板块来源资金流排行">
        <CardContent>
          <Stack direction="row" spacing={2} alignItems="center">
            <Typography variant="h6">板块资金流排行</Typography>
            <FormControl size="small" sx={{ minWidth: 144, ml: "auto" }}>
              <InputLabel id="sector-flow-order-label">资金方向</InputLabel>
              <Select
                labelId="sector-flow-order-label"
                value={flowOrder}
                label="资金方向"
                onChange={handleFlowOrderChange}
              >
                <MenuItem value="desc">净流入优先</MenuItem>
                <MenuItem value="asc">净流出优先</MenuItem>
              </Select>
            </FormControl>
          </Stack>
          <Alert severity="info" sx={{ mt: 1 }}>
            仅在 Eastmoney trade_direction_flow
            来源方法学内比较，不描述为统一市场资金事实，也不以涨跌幅推断流向。
          </Alert>
          {moneyFlowQuery.isPending ? (
            <Skeleton variant="rounded" height={380} sx={{ mt: 2 }} />
          ) : moneyFlowQuery.data === undefined ? (
            <MarketDataState
              variant="error"
              title="板块资金流不可用"
              message="来源净额 publication 未载入；页面不会使用板块涨跌排行代替。"
              onRetry={
                /** 仅重试板块资金流 publication。 */
                () => void moneyFlowQuery.refetch()
              }
              minHeight={380}
            />
          ) : moneyFlowQuery.data.payload.items.length === 0 ? (
            <MarketDataState
              variant="empty"
              title="当前没有资金流排行记录"
              message="publication 有效，但该体系和日期未返回满足质量门的板块。"
              minHeight={380}
            />
          ) : (
            <Box
              sx={{
                display: "grid",
                gridTemplateColumns: "minmax(0, 1.35fr) minmax(360px, 0.65fr)",
                gap: 3,
                mt: 1,
              }}
            >
              <MarketMoneyFlowChart points={moneyFlowQuery.data.payload.items.slice(0, 12)} />
              <Stack spacing={1}>
                {moneyFlowQuery.data.payload.items.slice(0, 8).map(
                  /** 每条资金流排名深链同体系板块详情。 */
                  (item) => (
                    <Stack
                      key={`${scheme}:${item.sectorCode}`}
                      component={RouterLink}
                      to={`/market/sectors/${scheme}/${encodeURIComponent(item.sectorCode)}`}
                      direction="row"
                      justifyContent="space-between"
                      alignItems="center"
                      sx={{ color: "inherit", textDecoration: "none", py: 0.75 }}
                    >
                      <Box minWidth={0}>
                        <Typography variant="body2" fontWeight={700} noWrap>
                          {item.rank}. {item.name}
                        </Typography>
                        <MarketDirectionalValue value={item.changePercent} variant="compact" />
                      </Box>
                      <Typography
                        variant="body2"
                        fontWeight={700}
                        color={
                          Number(item.netAmountCny) > 0
                            ? "error.main"
                            : Number(item.netAmountCny) < 0
                              ? "success.main"
                              : "text.secondary"
                        }
                      >
                        {Number(item.netAmountCny) > 0 ? "+" : ""}
                        {formatCnyYi(item.netAmountCny)}
                      </Typography>
                    </Stack>
                  ),
                )}
                <Typography variant="caption" color="text.secondary">
                  覆盖 {formatCoverageRatio(moneyFlowQuery.data.payload.coverage)} ·{" "}
                  {moneyFlowQuery.data.payload.methodology.id} ·{" "}
                  {moneyFlowQuery.data.payload.tradeDate}
                </Typography>
              </Stack>
            </Box>
          )}
        </CardContent>
      </Card>
    </Stack>
  );
}
