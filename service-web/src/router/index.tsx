import type { QueryClient } from "@tanstack/react-query";
import { createBrowserRouter } from "react-router-dom";

import { marketOverviewQueryOptions } from "../api/market";
import { AppShell } from "../components/AppShell";
import { RouteErrorView } from "../views/RouteErrorView";

/** Create route tree with query prefetching and code-split analysis views. */
export function createAppRouter(queryClient: QueryClient) {
  return createBrowserRouter([
    {
      path: "/",
      Component: AppShell,
      errorElement: <RouteErrorView />,
      children: [
        {
          index: true,
          // Populate overview cache before view renders its query consumer.
          loader: () => queryClient.ensureQueryData(marketOverviewQueryOptions),
          /** Load overview route bundle only when root route is requested. */
          lazy: async () => {
            const { MarketOverviewView } = await import("../views/MarketOverviewView");

            return { Component: MarketOverviewView };
          },
        },
        {
          path: "instruments/:symbol",
          /** Load analysis route bundle only when a symbol route is requested. */
          lazy: async () => {
            const { InstrumentAnalysisView } = await import("../views/InstrumentAnalysisView");

            return { Component: InstrumentAnalysisView };
          },
        },
      ],
    },
  ]);
}
