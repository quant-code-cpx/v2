import { FinancialDataClient } from '../../data-sync/clients/financial-data.client.js';
import { FinancialDataController } from './financial-data.controller.js';
import { FinancialDataService } from './financial-data.service.js';

/** 提供给共享 `StockModule` 的财务 controller 接线点。 */
export const FINANCIAL_DATA_CONTROLLERS = [FinancialDataController] as const;

/** 提供给共享 `StockModule` 的财务应用服务接线点。 */
export const FINANCIAL_DATA_PROVIDERS = [FinancialDataService] as const;

/** 提供给共享 `DataSyncModule` 的内部 HTTP client 接线点。 */
export const FINANCIAL_DATA_CLIENT_PROVIDERS = [FinancialDataClient] as const;
