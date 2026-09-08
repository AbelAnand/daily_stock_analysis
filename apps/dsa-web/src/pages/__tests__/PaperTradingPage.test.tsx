import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createApiError } from '../../api/error';
import { UiLanguageProvider } from '../../contexts/UiLanguageContext';
import { UI_LANGUAGE_STORAGE_KEY } from '../../utils/uiLanguage';
import PaperTradingPage from '../PaperTradingPage';

const { getDashboard, closePosition, closeAllPositions } = vi.hoisted(() => ({
  getDashboard: vi.fn(),
  closePosition: vi.fn(),
  closeAllPositions: vi.fn(),
}));

vi.mock('../../api/paperTrading', () => ({
  paperTradingApi: { getDashboard, closePosition, closeAllPositions },
}));

function position(symbol: string, overrides: Record<string, unknown> = {}) {
  return {
    symbol,
    side: 'long',
    qty: 17,
    avgEntry: 343.61,
    currentPrice: 350,
    marketValue: 5950,
    costBasis: 5841.37,
    unrealizedPl: 108.63,
    unrealizedPlpc: 0.0186,
    unrealizedIntradayPl: 19.55,
    changeToday: 0.0034,
    stopPrice: 340,
    targetPrice: 380,
    initialStop: 330,
    rMultiple: 0.47,
    protected: true,
    ...overrides,
  };
}

function dashboard(overrides: Record<string, unknown> = {}) {
  return {
    generatedAt: '2026-09-04T14:00:00+00:00',
    status: {
      enabled: true, dryRun: false, killSwitchActive: false, allowShort: true,
      marketOpen: true, nextOpen: null, nextClose: '2026-09-04T20:00:00+00:00', maxPositions: 10,
    },
    account: {
      label: 'B', accountNumber: 'PA_TEST', equity: 31000, lastEquity: 30500, cash: 20000, buyingPower: 80000,
      portfolioValue: 31000, longMarketValue: 11000, shortMarketValue: 0, createdAt: null,
    },
    pnl: {
      totalPnl: 1000, totalPnlPct: 3.33, totalPnlBasis: 'account_equity_since_first_trade', since: '2026-08-22',
      baselineEquity: 30000, dayPnl: -500, dayPnlPct: -1.64, unrealizedPnl: 108.63, realizedPnl: -44.22,
      closedTrades: 2, wins: 0, losses: 1,
    },
    pnlHistory: [
      { date: '2026-08-22', equity: 30000, pnl: 0 },
      { date: '2026-08-25', equity: 30120, pnl: 120 },
      { date: '2026-09-04', equity: 31000, pnl: 1000 },
    ],
    exposure: { openPositions: 2, grossExposure: 11000, grossExposurePct: 35.48, longMarketValue: 11000, shortMarketValue: 0 },
    positions: [position('GOOGL'), position('NIO', { side: 'short', qty: 50, stopPrice: null, targetPrice: null, protected: false, unrealizedPl: -12.5 })],
    openOrders: [{
      id: 'o1', symbol: 'AAPL', side: 'buy', qty: 5, orderClass: 'bracket', orderType: 'limit',
      limitPrice: 180, stopPrice: null, status: 'new', submittedAt: '2026-09-04T13:35:00+00:00',
    }],
    recentTrades: [{
      id: 1, createdAt: '2026-09-03T12:11:59', symbol: 'GOOGL', action: 'buy', side: 'buy', qty: 17,
      limitPrice: 343.61, stopPrice: 330, targetPrice: 370, status: 'submitted', reason: 'chase', orderId: 'abc', dryRun: false,
    }],
    ...overrides,
  };
}

function renderPage() {
  return render(
    <UiLanguageProvider>
      <PaperTradingPage />
    </UiLanguageProvider>,
  );
}

describe('PaperTradingPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.setItem(UI_LANGUAGE_STORAGE_KEY, 'en');
    getDashboard.mockResolvedValue(dashboard());
  });

  it('renders P&L tiles, positions, orders and activity', async () => {
    renderPage();

    expect(await screen.findByText('+$1,000.00')).toBeInTheDocument();
    expect(within(screen.getByTestId('kpi-total-pnl')).getByText('+3.33%')).toBeInTheDocument();
    expect(within(screen.getByTestId('kpi-day-pnl')).getByText('-$500.00')).toBeInTheDocument();
    expect(screen.getByText('-$44.22')).toBeInTheDocument();
    expect(screen.getByText('2 closed · 0W 1L')).toBeInTheDocument();
    expect(screen.getByText('Open')).toBeInTheDocument();
    expect(screen.queryByText('Bot off')).not.toBeInTheDocument();
    expect(screen.getByText('Bot P&L')).toBeInTheDocument();
    expect(screen.getByTestId('pnl-chart')).toBeInTheDocument();

    const googl = screen.getByTestId('position-row-GOOGL');
    expect(within(googl).getByText('LONG')).toBeInTheDocument();
    expect(within(googl).getByText('+$108.63')).toBeInTheDocument();
    expect(within(googl).getByText('340.00')).toBeInTheDocument();
    expect(within(googl).getByText('+0.47R')).toBeInTheDocument();

    const nio = screen.getByTestId('position-row-NIO');
    expect(within(nio).getByText('SHORT')).toBeInTheDocument();
    expect(within(nio).getByText('No stop')).toBeInTheDocument();

    expect(screen.getByText('AAPL')).toBeInTheDocument();
    expect(screen.getByText('chase')).toBeInTheDocument();
  });

  it('closes one position after confirmation and refreshes', async () => {
    closePosition.mockResolvedValue({ symbol: 'GOOGL', side: 'sell', qty: 17, status: 'submitted', orderId: 'c1', message: 'ok' });
    renderPage();
    await screen.findByTestId('position-row-GOOGL');

    fireEvent.click(screen.getByRole('button', { name: 'Close GOOGL' }));
    expect(screen.getByText('Close GOOGL?')).toBeInTheDocument();
    expect(screen.getByText(/Market order to sell 17 GOOGL/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Close position' }));

    await waitFor(() => expect(closePosition).toHaveBeenCalledWith('GOOGL'));
    expect(await screen.findByText('GOOGL: close order submitted (sell 17)')).toBeInTheDocument();
    await waitFor(() => expect(getDashboard.mock.calls.length).toBeGreaterThanOrEqual(2));
  });

  it('requires the typed phrase before flattening everything', async () => {
    closeAllPositions.mockResolvedValue({
      requested: 2, closed: 1, failed: 1,
      results: [
        { symbol: 'GOOGL', side: 'sell', qty: 17, status: 'submitted', orderId: 'c1', message: 'ok' },
        { symbol: 'NIO', side: 'buy_to_cover', qty: 50, status: 'error', orderId: null, message: 'insufficient qty' },
      ],
    });
    renderPage();
    await screen.findByTestId('position-row-GOOGL');

    fireEvent.click(screen.getByRole('button', { name: /Close all positions/ }));
    const dialog = screen.getByRole('dialog');
    const confirm = within(dialog).getByRole('button', { name: 'Flatten everything' });
    expect(confirm).toBeDisabled();

    fireEvent.change(within(dialog).getByPlaceholderText('CLOSE ALL'), { target: { value: 'close all' } });
    expect(confirm).toBeEnabled();
    fireEvent.click(confirm);

    await waitFor(() => expect(closeAllPositions).toHaveBeenCalledTimes(1));
    expect(await screen.findByText('Close all: 1 of 2 submitted, 1 failed')).toBeInTheDocument();
    expect(screen.getByText(/NIO: close failed — insufficient qty/)).toBeInTheDocument();
  });

  it('shows a setup state when no paper account is reachable', async () => {
    getDashboard.mockRejectedValueOnce(createApiError({
      title: 'Service unavailable',
      message: 'No reachable Alpaca paper account',
      rawMessage: 'No reachable Alpaca paper account',
      status: 503,
      category: 'http_error',
    }));
    renderPage();

    expect(await screen.findByText('Paper trading is not connected')).toBeInTheDocument();
    expect(screen.queryByText('Total P&L')).not.toBeInTheDocument();
    expect(screen.queryByTestId('pnl-chart')).not.toBeInTheDocument();
  });

  it('disables close-all when there are no positions', async () => {
    getDashboard.mockResolvedValue(dashboard({ positions: [], pnlHistory: [], exposure: { openPositions: 0, grossExposure: 0, grossExposurePct: 0, longMarketValue: 0, shortMarketValue: 0 } }));
    renderPage();

    expect(await screen.findByText('No open positions')).toBeInTheDocument();
    expect(screen.getByText('No P&L history yet')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Close all positions/ })).toBeDisabled();
  });

  it('polls while auto-refresh is on and stops when paused', async () => {
    vi.useFakeTimers();
    try {
      renderPage();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(getDashboard).toHaveBeenCalledTimes(1);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(15_000);
      });
      expect(getDashboard).toHaveBeenCalledTimes(2);

      fireEvent.click(screen.getByRole('button', { name: /Auto 15s/ }));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000);
      });
      expect(getDashboard).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });
});
