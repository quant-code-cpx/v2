import { useState } from "react";
import type { SyntheticEvent } from "react";
import { Box, Card, CardContent, Divider, Stack, Tab, Tabs, Typography } from "@mui/material";
import { Link as RouterLink } from "react-router-dom";

import { MarketDirectionalValue } from "../../../components/MarketDirectionalValue";
import type { MarketOverview, MarketSectorScheme } from "../../../types/market";

type SectorFamily = "eastmoneyIndustry" | "eastmoneyConcept";
type SectorDirection = "strongest" | "weakest";

/** 返回首页板块体系对应的 canonical 路由身份。 */
function schemeForFamily(family: SectorFamily): MarketSectorScheme {
  return family === "eastmoneyIndustry" ? "eastmoney.industry" : "eastmoney.concept";
}

/** 渲染一条同体系、同 publication 的板块强弱摘要。 */
function SectorRankRow({
  item,
  scheme,
}: {
  item: MarketOverview["sectorRankings"]["eastmoneyIndustry"]["strongest"][number];
  scheme: MarketSectorScheme;
}) {
  return (
    <Stack
      component={RouterLink}
      to={`/market/sectors/${scheme}/${encodeURIComponent(item.sectorCode)}`}
      direction="row"
      justifyContent="space-between"
      alignItems="center"
      spacing={2}
      sx={{
        py: 1,
        color: "inherit",
        textDecoration: "none",
        "&:hover": { color: "primary.main" },
        "&:focus-visible": { outline: 2, outlineColor: "primary.main", outlineOffset: 2 },
      }}
    >
      <Box minWidth={0}>
        <Typography variant="body2" fontWeight={700} noWrap>
          {item.rank}. {item.name}
        </Typography>
        <Typography variant="caption" color="text.secondary" noWrap>
          {item.leadingEquity === null
            ? `有效样本 ${item.validSamples}`
            : `领涨 ${item.leadingEquity.name} · 有效样本 ${item.validSamples}`}
        </Typography>
      </Box>
      <MarketDirectionalValue value={item.changePercent} variant="compact" />
    </Stack>
  );
}

/** 渲染东财行业或概念的最强、最弱摘要，禁止跨体系混排。 */
export function MarketSectorRankingCard({ overview }: { overview: MarketOverview }) {
  const [family, setFamily] = useState<SectorFamily>("eastmoneyIndustry");
  const [direction, setDirection] = useState<SectorDirection>("strongest");

  /** 切换东财行业和概念两个语义独立的目录。 */
  function handleFamilyChange(_event: SyntheticEvent, nextFamily: SectorFamily): void {
    setFamily(nextFamily);
  }

  /** 切换同一体系 publication 内的最强和最弱列表。 */
  function handleDirectionChange(_event: SyntheticEvent, nextDirection: SectorDirection): void {
    setDirection(nextDirection);
  }

  const items = overview.sectorRankings[family][direction].slice(0, 6);
  const scheme = schemeForFamily(family);

  return (
    <Card component="section" aria-label="行业与概念强弱摘要">
      <CardContent>
        <Stack direction="row" justifyContent="space-between" alignItems="baseline">
          <Typography variant="h6">行业与概念</Typography>
          <Typography
            component={RouterLink}
            to="/market/sectors"
            variant="body2"
            color="primary.main"
            sx={{ textDecoration: "none" }}
          >
            进入板块中心
          </Typography>
        </Stack>
        <Tabs
          value={family}
          onChange={handleFamilyChange}
          aria-label="板块分类体系"
          sx={{ mt: 0.5 }}
        >
          <Tab value="eastmoneyIndustry" label="东财行业" />
          <Tab value="eastmoneyConcept" label="东财概念" />
        </Tabs>
        <Tabs
          value={direction}
          onChange={handleDirectionChange}
          aria-label="板块强弱方向"
          sx={{ minHeight: 36 }}
        >
          <Tab value="strongest" label="最强" sx={{ minHeight: 36 }} />
          <Tab value="weakest" label="最弱" sx={{ minHeight: 36 }} />
        </Tabs>
        {items.length === 0 ? (
          <Typography variant="body2" color="text.secondary" sx={{ py: 3 }}>
            当前 publication 未包含该方向板块。
          </Typography>
        ) : (
          <Stack divider={<Divider flexItem />}>
            {items.map(
              /** 板块身份由体系与供应商代码共同确定。 */
              (item) => (
                <SectorRankRow key={`${scheme}:${item.sectorCode}`} item={item} scheme={scheme} />
              ),
            )}
          </Stack>
        )}
      </CardContent>
    </Card>
  );
}
