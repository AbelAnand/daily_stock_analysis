import apiClient from './index';
import { toCamelCase } from './utils';
import type { PaperCloseAllResult, PaperCloseResult, PaperDashboard } from '../types/paperTrading';

export const paperTradingApi = {
  getDashboard: async (): Promise<PaperDashboard> => {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/paper-trading/dashboard');
    return toCamelCase<PaperDashboard>(response.data);
  },

  closePosition: async (symbol: string): Promise<PaperCloseResult> => {
    const response = await apiClient.post<Record<string, unknown>>(
      `/api/v1/paper-trading/positions/${encodeURIComponent(symbol.toUpperCase())}/close`,
    );
    return toCamelCase<PaperCloseResult>(response.data);
  },

  closeAllPositions: async (): Promise<PaperCloseAllResult> => {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/paper-trading/positions/close-all');
    return toCamelCase<PaperCloseAllResult>(response.data);
  },
};
