import {
  AccountBalanceOutlined as AccountBalanceOutlinedIcon,
  ArrowForwardRounded as ArrowForwardRoundedIcon,
  CurrencyExchangeOutlined as CurrencyExchangeOutlinedIcon,
  DomainOutlined as DomainOutlinedIcon,
  HomeWorkOutlined as HomeWorkOutlinedIcon,
  PaidOutlined as PaidOutlinedIcon,
  ShowChartOutlined as ShowChartOutlinedIcon,
} from "@mui/icons-material";
import { Alert, Box, Button, Card, CardContent, Chip, Stack, Typography } from "@mui/material";
import type { ReactNode } from "react";
import { Link as RouterLink } from "react-router-dom";

/** 基金中心一个明确产品类型的能力卡。 */
interface FundTypeCard {
  title: string;
  description: string;
  icon: ReactNode;
  phase: "ETF_AVAILABLE" | "NOT_CONNECTED";
}

/** 基金类型入口；禁用项不带任何估算数量或伪造数据。 */
const fundTypes: readonly FundTypeCard[] = [
  {
    title: "交易所上市 ETF",
    description:
      "产品目录与日线；普通 ETF 展示来源单位净值，收益型货币 ETF 的 NAV 明确不支持；申购、赎回等状态按来源实际维度独立展示。",
    icon: <ShowChartOutlinedIcon />,
    phase: "ETF_AVAILABLE",
  },
  {
    title: "场外公募基金",
    description: "尚未接入产品主数据、份额类别、NAV 日历与公开读取合同。",
    icon: <AccountBalanceOutlinedIcon />,
    phase: "NOT_CONNECTED",
  },
  {
    title: "LOF",
    description: "尚未接入明确产品分类和场内、场外双重价格/NAV 口径。",
    icon: <CurrencyExchangeOutlinedIcon />,
    phase: "NOT_CONNECTED",
  },
  {
    title: "REITs",
    description: "尚未接入独立产品分类、资产与分派事件合同。",
    icon: <HomeWorkOutlinedIcon />,
    phase: "NOT_CONNECTED",
  },
  {
    title: "货币基金",
    description: "尚未接入万份收益、七日年化及其口径和披露日历。",
    icon: <PaidOutlinedIcon />,
    phase: "NOT_CONNECTED",
  },
  {
    title: "其他交易所基金",
    description: "尚未接入来源明确的产品分类目录，不能按代码前缀归类。",
    icon: <DomainOutlinedIcon />,
    phase: "NOT_CONNECTED",
  },
];

/** 渲染基金类型分类入口，并明确第一阶段只开放真实 ETF 数据链路。 */
export function FundCenterView() {
  return (
    <Stack spacing={3}>
      <Box>
        <Typography component="h1" variant="h4">
          基金与 ETF 中心
        </Typography>
        <Typography color="text.secondary" sx={{ mt: 0.5 }}>
          按产品类型进入独立数据能力；当前第一阶段只开放交易所上市 ETF。
        </Typography>
      </Box>

      <Alert severity="info" variant="outlined">
        “基金”是分类入口，不代表所有基金已有数据。场外公募基金、LOF、REITs
        和普通货币基金均需新增产品目录与公开读取合同；交易所货币 ETF 仍从 ETF 入口进入，但收益型 NAV
        不会映射为单位净值。
      </Alert>

      <Box
        component="section"
        aria-label="基金类型能力"
        sx={{
          display: "grid",
          gridTemplateColumns: "repeat(3, minmax(0, 1fr))",
          gap: 3,
        }}
      >
        {/* 基金类型保持固定业务顺序，禁用类型只说明缺口。 */}
        {fundTypes.map((fundType) => (
          <Card key={fundType.title} sx={{ minHeight: 224 }}>
            <CardContent sx={{ height: "100%", display: "flex", flexDirection: "column", p: 3 }}>
              <Box
                sx={{
                  width: 48,
                  height: 48,
                  display: "grid",
                  placeItems: "center",
                  borderRadius: 1.5,
                  bgcolor:
                    fundType.phase === "ETF_AVAILABLE" ? "primary.lighter" : "action.selected",
                  color: fundType.phase === "ETF_AVAILABLE" ? "primary.main" : "text.secondary",
                }}
              >
                {fundType.icon}
              </Box>
              <Stack direction="row" alignItems="center" spacing={1} sx={{ mt: 2 }}>
                <Typography component="h2" variant="h6">
                  {fundType.title}
                </Typography>
                <Chip
                  size="small"
                  color={fundType.phase === "ETF_AVAILABLE" ? "primary" : "default"}
                  label={fundType.phase === "ETF_AVAILABLE" ? "第一阶段" : "尚未接入"}
                  variant={fundType.phase === "ETF_AVAILABLE" ? "filled" : "outlined"}
                />
              </Stack>
              <Typography variant="body2" color="text.secondary" sx={{ mt: 1, flex: 1 }}>
                {fundType.description}
              </Typography>
              {fundType.phase === "ETF_AVAILABLE" ? (
                <Button
                  component={RouterLink}
                  to="/market/etfs"
                  endIcon={<ArrowForwardRoundedIcon />}
                  sx={{ mt: 2, alignSelf: "flex-start" }}
                >
                  进入 ETF 目录
                </Button>
              ) : (
                <Typography variant="caption" color="text.disabled" sx={{ mt: 2 }}>
                  不使用代码前缀或目录差集补齐分类
                </Typography>
              )}
            </CardContent>
          </Card>
        ))}
      </Box>
    </Stack>
  );
}
