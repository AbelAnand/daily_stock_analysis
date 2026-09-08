export type PaperTradingStatus = {
  enabled: boolean;
  dryRun: boolean;
  killSwitchActive: boolean;
  allowShort: boolean;
  marketOpen: boolean;
  nextOpen: string | null;
  nextClose: string | null;
  maxPositions: number;
};

export type PaperAccount = {
  label: string;
  accountNumber: string;
  equity: number;
  lastEquity: number;
  cash: number;
  buyingPower: number;
  portfolioValue: number;
  longMarketValue: number;
  shortMarketValue: number;
  createdAt: string | null;
};

export type PaperPnlBasis = 'account_equity_since_first_trade' | 'realized_plus_unrealized';

export type PaperPnl = {
  totalPnl: number;
  totalPnlPct: number;
  totalPnlBasis: PaperPnlBasis;
  since: string | null;
  baselineEquity: number | null;
  dayPnl: number;
  dayPnlPct: number;
  unrealizedPnl: number;
  realizedPnl: number;
  closedTrades: number;
  wins: number;
  losses: number;
};

export type PaperPnlPoint = {
  date: string;
  equity: number;
  pnl: number;
};

export type PaperExposure = {
  openPositions: number;
  grossExposure: number;
  grossExposurePct: number;
  longMarketValue: number;
  shortMarketValue: number;
};

export type PaperPositionSide = 'long' | 'short';

export type PaperPosition = {
  symbol: string;
  side: PaperPositionSide;
  qty: number;
  avgEntry: number;
  currentPrice: number | null;
  marketValue: number;
  costBasis: number;
  unrealizedPl: number;
  unrealizedPlpc: number;
  unrealizedIntradayPl: number;
  changeToday: number;
  stopPrice: number | null;
  targetPrice: number | null;
  initialStop: number | null;
  rMultiple: number | null;
  protected: boolean;
};

export type PaperOpenOrder = {
  id: string;
  symbol: string;
  side: string;
  qty: number;
  orderClass: string;
  orderType: string;
  limitPrice: number | null;
  stopPrice: number | null;
  status: string;
  submittedAt: string | null;
};

export type PaperTradeActivity = {
  id: number;
  createdAt: string | null;
  symbol: string;
  action: string | null;
  side: string | null;
  qty: number | null;
  limitPrice: number | null;
  stopPrice: number | null;
  targetPrice: number | null;
  status: string;
  reason: string | null;
  orderId: string | null;
  dryRun: boolean;
};

export type PaperDashboard = {
  generatedAt: string;
  status: PaperTradingStatus;
  account: PaperAccount;
  pnl: PaperPnl;
  pnlHistory: PaperPnlPoint[];
  exposure: PaperExposure;
  positions: PaperPosition[];
  openOrders: PaperOpenOrder[];
  recentTrades: PaperTradeActivity[];
};

export type PaperCloseStatus = 'submitted' | 'dry_run' | 'error';

export type PaperCloseResult = {
  symbol: string;
  side: string;
  qty: number;
  status: PaperCloseStatus;
  orderId: string | null;
  message: string;
};

export type PaperCloseAllResult = {
  requested: number;
  closed: number;
  failed: number;
  results: PaperCloseResult[];
};
