import { createBrowserRouter } from "react-router-dom";

import { AppShell } from "../components/AppShell/AppShell";
import { RouteLoadingView } from "../components/RouteLoadingView";
import { ForbiddenView } from "../views/ForbiddenView/ForbiddenView";
import { RouteErrorView } from "../views/RouteErrorView/RouteErrorView";
import {
  redirectAuthenticatedLogin,
  requireDataOperationsAccess,
  requirePermission,
  requireSession,
} from "./guards";
import { requireStockConnectChannel, requireStockConnectSecurity } from "./stock-connect-loaders";

/** 创建匿名登录与默认拒绝的受保护路由树。 */
export function createAppRouter() {
  return createBrowserRouter([
    {
      path: "/login",
      loader: redirectAuthenticatedLogin,
      errorElement: <RouteErrorView />,
      HydrateFallback: RouteLoadingView,
      /** 加载不含图表依赖的隔离匿名登录路由。 */
      lazy: async () => {
        const { LoginView } = await import("../views/LoginView/LoginView");

        return { Component: LoginView };
      },
    },
    {
      path: "/",
      Component: AppShell,
      loader: requireSession,
      errorElement: <RouteErrorView />,
      HydrateFallback: RouteLoadingView,
      children: [
        {
          index: true,
          /** 按权限加载不含图表依赖的平台工作台。 */
          lazy: async () => {
            const { WorkspaceView } = await import("../views/WorkspaceView/WorkspaceView");

            return { Component: WorkspaceView };
          },
        },
        {
          path: "account",
          loader: requireSession,
          /** 会话验证后加载本人资料与安全管理页面。 */
          lazy: async () => {
            const { AccountView } = await import("../views/AccountView/AccountView");

            return { Component: AccountView };
          },
        },
        {
          path: "users",
          loader: requirePermission("users:read"),
          errorElement: <ForbiddenView />,
          /** 权限验证完成后才加载管理工作区。 */
          lazy: async () => {
            const { UserManagementView } =
              await import("../views/UserManagementView/UserManagementView");

            return { Component: UserManagementView };
          },
        },
        {
          path: "security/audit",
          loader: requirePermission("audit:read"),
          errorElement: <ForbiddenView />,
          /** 权限验证完成后才加载审计页面和查询代码。 */
          lazy: async () => {
            const { AuditEventsView } = await import("../views/AuditEventsView/AuditEventsView");

            return { Component: AuditEventsView };
          },
        },
        {
          path: "data-operations",
          loader: requireDataOperationsAccess,
          errorElement: <ForbiddenView />,
          /** 权限确认后才加载数据运维页面及其 19 路由适配器。 */
          lazy: async () => {
            const { DataOperationsView } =
              await import("../views/DataOperationsView/DataOperationsView");

            return { Component: DataOperationsView };
          },
        },
        {
          path: "market",
          loader: requireSession,
          /** 会话验证后加载真实市场 complete bundle 与指数趋势。 */
          lazy: async () => {
            const { MarketOverviewView } =
              await import("../views/MarketOverviewView/MarketOverviewView");

            return { Component: MarketOverviewView };
          },
        },
        {
          path: "market/sectors",
          loader: requireSession,
          /** 会话验证后加载东财行业和概念的独立横截面。 */
          lazy: async () => {
            const { MarketSectorsView } =
              await import("../views/MarketSectorsView/MarketSectorsView");

            return { Component: MarketSectorsView };
          },
        },
        {
          path: "market/sectors/:scheme/:sectorCode",
          loader: requireSession,
          /** 会话验证后加载单板块快照、原生周期 K 线和成分。 */
          lazy: async () => {
            const { MarketSectorDetailView } =
              await import("../views/MarketSectorDetailView/MarketSectorDetailView");

            return { Component: MarketSectorDetailView };
          },
        },
        {
          path: "market/industries/sw",
          loader: requireSession,
          /** 会话验证后加载申万独立 taxonomy 与估值 publication。 */
          lazy: async () => {
            const { SwIndustriesView } = await import("../views/SwIndustriesView/SwIndustriesView");

            return { Component: SwIndustriesView };
          },
        },
        {
          path: "market/industries/sw/:code",
          loader: requireSession,
          /** 会话验证后加载申万节点、估值、已物化 K 线和正式成分。 */
          lazy: async () => {
            const { SwIndustryDetailView } =
              await import("../views/SwIndustryDetailView/SwIndustryDetailView");

            return { Component: SwIndustryDetailView };
          },
        },
        {
          path: "market/equities",
          loader: requireSession,
          /** 会话验证后加载统一 A 股证券发现页面。 */
          lazy: async () => {
            const { EquityMarketView } = await import("../views/EquityMarketView/EquityMarketView");

            return { Component: EquityMarketView };
          },
        },
        {
          path: "market/equities/:exchange/:symbol",
          loader: requireSession,
          /** canonical 身份路径确认后加载个股多数据集工作区。 */
          lazy: async () => {
            const { EquityDetailView } = await import("../views/EquityDetailView/EquityDetailView");

            return { Component: EquityDetailView };
          },
        },
        {
          path: "market/funds",
          loader: requireSession,
          /** 会话验证后加载基金类型分类入口。 */
          lazy: async () => {
            const { FundCenterView } = await import("../views/FundCenterView/FundCenterView");

            return { Component: FundCenterView };
          },
        },
        {
          path: "market/etfs",
          loader: requireSession,
          /** 请求 ETF 产品目录时才加载 typed market-data 列表能力。 */
          lazy: async () => {
            const { EtfListView } = await import("../views/EtfListView/EtfListView");

            return { Component: EtfListView };
          },
        },
        {
          path: "market/etfs/:exchange/:symbol",
          loader: requireSession,
          /** 请求具体 ETF 时才加载 KLineChart、ECharts 与四数据集编排。 */
          lazy: async () => {
            const { EtfDetailView } = await import("../views/EtfDetailView/EtfDetailView");

            return { Component: EtfDetailView };
          },
        },
        {
          path: "market/stock-connect",
          loader: requireSession,
          /** 会话验证后才加载互联互通总览及其查询代码。 */
          lazy: async () => {
            const { StockConnectOverviewPage } =
              await import("../views/StockConnectView/StockConnectOverviewPage");

            return { Component: StockConnectOverviewPage };
          },
        },
        {
          path: "market/stock-connect/securities/:instrumentEntityRef",
          loader: requireStockConnectSecurity,
          /** 稳定身份校验后加载证券互联互通上下文。 */
          lazy: async () => {
            const { StockConnectSecurityPage } =
              await import("../views/StockConnectView/StockConnectSecurityPage");

            return { Component: StockConnectSecurityPage };
          },
        },
        {
          path: "market/stock-connect/:channel",
          loader: requireStockConnectChannel,
          /** 通道短名校验后加载单通道详情。 */
          lazy: async () => {
            const { StockConnectChannelPage } =
              await import("../views/StockConnectView/StockConnectChannelPage");

            return { Component: StockConnectChannelPage };
          },
        },
        {
          path: "instruments/:symbol",
          loader: requireSession,
          /** 旧 symbol-only 链接通过真实证券目录解析，不再加载已废弃分析页。 */
          lazy: async () => {
            const { LegacyInstrumentRedirectView } =
              await import("../views/LegacyInstrumentRedirectView/LegacyInstrumentRedirectView");

            return { Component: LegacyInstrumentRedirectView };
          },
        },
        {
          path: "*",
          Component: RouteErrorView,
        },
      ],
    },
  ]);
}
