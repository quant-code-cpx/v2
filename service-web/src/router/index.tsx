import type { QueryClient } from "@tanstack/react-query";
import { createBrowserRouter } from "react-router-dom";

import { marketOverviewQueryOptions } from "../api/market";
import { AppShell } from "../components/AppShell";
import { RouteErrorView } from "../views/RouteErrorView";

export function createAppRouter(queryClient: QueryClient) {
  return createBrowserRouter([
    {
      path: "/",
      Component: AppShell,
      errorElement: <RouteErrorView />,
      children: [
        {
          index: true,
          loader: () => queryClient.ensureQueryData(marketOverviewQueryOptions),
          lazy: async () => {
            const { MarketOverviewView } = await import("../views/MarketOverviewView");

            return { Component: MarketOverviewView };
          },
        },
        {
          path: "instruments/:symbol",
          lazy: async () => {
            const { InstrumentAnalysisView } = await import("../views/InstrumentAnalysisView");

            return { Component: InstrumentAnalysisView };
          },
        },
      ],
    },
  ]);
}
