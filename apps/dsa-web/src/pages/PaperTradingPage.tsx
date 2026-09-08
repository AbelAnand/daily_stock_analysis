import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Pause, Play, RefreshCw } from 'lucide-react';
import { Area, AreaChart, CartesianGrid, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { getParsedApiError, type ParsedApiError } from '../api/error';
import { paperTradingApi } from '../api/paperTrading';
import { ApiErrorAlert, AppPage, Badge, Button, ConfirmDialog, EmptyState, InlineAlert, StatusDot } from '../components/common';
import { useUiLanguage } from '../contexts/UiLanguageContext';
import type { UiLanguage, UiTextKey, UiTextParams } from '../i18n/uiText';
import type {
  PaperCloseResult,
  PaperDashboard,
  PaperOpenOrder,
  PaperPnlPoint,
  PaperPosition,
  PaperTradeActivity,
} from '../types/paperTrading';
import { cn } from '../utils/cn';

type Translate = (key: UiTextKey, params?: UiTextParams) => string;

const AUTO_REFRESH_MS = 15_000;
const CLOSE_ALL_PHRASE = 'CLOSE ALL';

function getLocale(language: UiLanguage): string {
  return language === 'en' ? 'en-US' : 'zh-CN';
}

function formatMoney(value: number | null | undefined, language: UiLanguage, options: { signed?: boolean; compact?: boolean } = {}): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return '-';
  }
  const formatted = new Intl.NumberFormat(getLocale(language), {
    style: 'currency',
    currency: 'USD',
    ...(options.compact
      ? { notation: 'compact', minimumFractionDigits: 0, maximumFractionDigits: 1 }
      : { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
  }).format(Math.abs(value));
  if (value < 0) {
    return `-${formatted}`;
  }
  return options.signed && value > 0 ? `+${formatted}` : formatted;
}

function formatPrice(value: number | null | undefined, language: UiLanguage): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return '-';
  }
  return new Intl.NumberFormat(getLocale(language), { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value);
}

function formatPct(fraction: number | null | undefined, language: UiLanguage, options: { signed?: boolean; isPercent?: boolean } = {}): string {
  if (fraction === null || fraction === undefined || Number.isNaN(fraction)) {
    return '-';
  }
  const pct = options.isPercent ? fraction : fraction * 100;
  const formatted = new Intl.NumberFormat(getLocale(language), { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(Math.abs(pct));
  const sign = pct < 0 ? '-' : options.signed && pct > 0 ? '+' : '';
  return `${sign}${formatted}%`;
}

function formatQty(value: number | null | undefined, language: UiLanguage): string {
  if (value === null || value === undefined) {
    return '-';
  }
  return new Intl.NumberFormat(getLocale(language), { maximumFractionDigits: 4 }).format(value);
}

function formatTime(value: string | null | undefined, language: UiLanguage, withDate = true): string {
  if (!value) {
    return '-';
  }
  const date = new Date(value.endsWith('Z') || /[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat(getLocale(language), {
    ...(withDate ? { month: '2-digit', day: '2-digit' } : {}),
    hour: '2-digit',
    minute: '2-digit',
    second: withDate ? undefined : '2-digit',
  }).format(date);
}

function formatDay(isoDate: string, language: UiLanguage): string {
  const date = new Date(`${isoDate}T12:00:00Z`);
  if (Number.isNaN(date.getTime())) {
    return isoDate;
  }
  return new Intl.DateTimeFormat(getLocale(language), { month: '2-digit', day: '2-digit', timeZone: 'UTC' }).format(date);
}

function pnlClass(value: number | null | undefined): string {
  if (value === null || value === undefined || value === 0) return 'text-foreground';
  return value > 0 ? 'text-success' : 'text-danger';
}

function statusBadgeVariant(status: string): 'success' | 'warning' | 'danger' | 'info' | 'default' {
  switch (status) {
    case 'submitted':
      return 'success';
    case 'dry_run':
    case 'deferred':
      return 'info';
    case 'error':
      return 'danger';
    case 'skipped':
      return 'default';
    default:
      return 'warning';
  }
}

const Money: React.FC<{ value: number | null | undefined; language: UiLanguage; signed?: boolean; className?: string }> = ({ value, language, signed = true, className }) => (
  <span className={cn('font-mono tabular-nums', pnlClass(value), className)}>{formatMoney(value, language, { signed })}</span>
);

const Kpi: React.FC<{ label: string; value: React.ReactNode; sub?: React.ReactNode; testId?: string }> = ({ label, value, sub, testId }) => (
  <div className="rounded-xl border border-border/60 bg-card/75 px-4 py-3" data-testid={testId}>
    <p className="text-[11px] uppercase tracking-[0.18em] text-secondary-text">{label}</p>
    <div className="mt-1 text-2xl font-semibold leading-tight">{value}</div>
    {sub ? <div className="mt-0.5 text-xs font-mono tabular-nums text-secondary-text">{sub}</div> : null}
  </div>
);

type PnlChartProps = { points: PaperPnlPoint[]; language: UiLanguage; t: Translate };

const PnlChart: React.FC<PnlChartProps> = ({ points, language, t }) => {
  const data = useMemo(() => points.map((pt) => ({ ...pt, label: formatDay(pt.date, language) })), [language, points]);
  const last = data.length ? data[data.length - 1] : null;
  const stroke = last && last.pnl < 0 ? 'hsl(var(--destructive))' : 'hsl(var(--success))';

  if (data.length < 2) {
    return <p className="px-4 py-10 text-center text-sm text-secondary-text">{t('trading.chart.empty')}</p>;
  }

  return (
    <div className="h-64 w-full" data-testid="pnl-chart">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 12, right: 16, bottom: 4, left: 4 }}>
          <defs>
            <linearGradient id="botPnlFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={stroke} stopOpacity={0.28} />
              <stop offset="100%" stopColor={stroke} stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke="rgba(148, 163, 184, 0.16)" vertical={false} />
          <XAxis dataKey="label" tickLine={false} axisLine={false} minTickGap={28} tick={{ fill: 'rgba(148, 163, 184, 0.8)', fontSize: 11 }} />
          <YAxis
            tickLine={false}
            axisLine={false}
            width={64}
            tick={{ fill: 'rgba(148, 163, 184, 0.8)', fontSize: 11 }}
            tickFormatter={(value: number) => formatMoney(value, language, { signed: true, compact: true })}
          />
          <ReferenceLine y={0} stroke="rgba(148, 163, 184, 0.55)" strokeDasharray="4 4" />
          <Tooltip
            cursor={{ stroke: 'rgba(148, 163, 184, 0.45)', strokeWidth: 1 }}
            content={({ active, payload }) => {
              if (!active || !payload?.length) return null;
              const pt = payload[0].payload as PaperPnlPoint & { label: string };
              return (
                <div className="rounded-lg border border-border/70 bg-elevated px-3 py-2 text-xs shadow-xl">
                  <div className="text-secondary-text">{pt.date}</div>
                  <div className={cn('mt-0.5 font-mono text-sm font-semibold tabular-nums', pnlClass(pt.pnl))}>{formatMoney(pt.pnl, language, { signed: true })}</div>
                  <div className="font-mono tabular-nums text-secondary-text">{formatMoney(pt.equity, language)}</div>
                </div>
              );
            }}
          />
          <Area
            type="linear"
            dataKey="pnl"
            stroke={stroke}
            strokeWidth={2}
            fill="url(#botPnlFill)"
            dot={false}
            activeDot={{ r: 5, strokeWidth: 2, stroke: 'hsl(var(--background))' }}
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
};

type PositionsTableProps = {
  positions: PaperPosition[];
  language: UiLanguage;
  t: Translate;
  busySymbol: string | null;
  disabled: boolean;
  onClose: (position: PaperPosition) => void;
};

const PositionsTable: React.FC<PositionsTableProps> = ({ positions, language, t, busySymbol, disabled, onClose }) => (
  <div className="overflow-x-auto">
    <table className="min-w-full divide-y divide-border/70 text-sm">
      <thead className="bg-surface-2/70 text-left text-xs uppercase tracking-[0.16em] text-secondary-text">
        <tr>
          <th className="px-4 py-2.5 font-medium">{t('trading.table.symbol')}</th>
          <th className="px-4 py-2.5 text-right font-medium">{t('trading.table.qty')}</th>
          <th className="px-4 py-2.5 text-right font-medium">{t('trading.table.avgEntry')}</th>
          <th className="px-4 py-2.5 text-right font-medium">{t('trading.table.last')}</th>
          <th className="px-4 py-2.5 text-right font-medium">{t('trading.table.marketValue')}</th>
          <th className="px-4 py-2.5 text-right font-medium">{t('trading.table.unrealized')}</th>
          <th className="px-4 py-2.5 text-right font-medium">{t('trading.table.today')}</th>
          <th className="px-4 py-2.5 text-right font-medium">{t('trading.table.stop')}</th>
          <th className="px-4 py-2.5 text-right font-medium">{t('trading.table.target')}</th>
          <th className="px-4 py-2.5 text-right font-medium">{t('trading.table.r')}</th>
          <th className="px-4 py-2.5 text-right font-medium" />
        </tr>
      </thead>
      <tbody className="divide-y divide-border/60">
        {positions.map((position) => {
          const isBusy = busySymbol === position.symbol;
          return (
            <tr key={position.symbol} data-testid={`position-row-${position.symbol}`} className="hover:bg-hover/60">
              <td className="whitespace-nowrap px-4 py-2.5">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-base font-semibold text-foreground">{position.symbol}</span>
                  <Badge variant={position.side === 'short' ? 'warning' : 'info'}>
                    {position.side === 'short' ? t('trading.side.short') : t('trading.side.long')}
                  </Badge>
                </div>
              </td>
              <td className="whitespace-nowrap px-4 py-2.5 text-right font-mono tabular-nums text-foreground">{formatQty(position.qty, language)}</td>
              <td className="whitespace-nowrap px-4 py-2.5 text-right font-mono tabular-nums text-secondary-text">{formatPrice(position.avgEntry, language)}</td>
              <td className="whitespace-nowrap px-4 py-2.5 text-right font-mono tabular-nums text-foreground">{formatPrice(position.currentPrice, language)}</td>
              <td className="whitespace-nowrap px-4 py-2.5 text-right font-mono tabular-nums text-foreground">{formatMoney(position.marketValue, language)}</td>
              <td className="whitespace-nowrap px-4 py-2.5 text-right">
                <Money value={position.unrealizedPl} language={language} className="font-medium" />
                <div className={cn('text-xs font-mono tabular-nums', pnlClass(position.unrealizedPl))}>{formatPct(position.unrealizedPlpc, language, { signed: true })}</div>
              </td>
              <td className="whitespace-nowrap px-4 py-2.5 text-right">
                <Money value={position.unrealizedIntradayPl} language={language} />
                <div className={cn('text-xs font-mono tabular-nums', pnlClass(position.changeToday))}>{formatPct(position.changeToday, language, { signed: true })}</div>
              </td>
              <td className={cn('whitespace-nowrap px-4 py-2.5 text-right font-mono tabular-nums', position.stopPrice === null ? 'text-warning' : 'text-foreground')}>
                {position.stopPrice !== null ? formatPrice(position.stopPrice, language) : t('trading.unprotected')}
              </td>
              <td className="whitespace-nowrap px-4 py-2.5 text-right font-mono tabular-nums text-secondary-text">{formatPrice(position.targetPrice, language)}</td>
              <td className={cn('whitespace-nowrap px-4 py-2.5 text-right font-mono tabular-nums', pnlClass(position.rMultiple))}>
                {position.rMultiple !== null ? `${position.rMultiple > 0 ? '+' : ''}${position.rMultiple.toFixed(2)}R` : '-'}
              </td>
              <td className="whitespace-nowrap px-4 py-2.5 text-right">
                <Button
                  variant="danger-subtle"
                  size="sm"
                  isLoading={isBusy}
                  disabled={disabled}
                  onClick={() => onClose(position)}
                  aria-label={t('trading.close.ariaLabel', { symbol: position.symbol })}
                >
                  {t('trading.positions.close')}
                </Button>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  </div>
);

const OpenOrdersTable: React.FC<{ orders: PaperOpenOrder[]; language: UiLanguage; t: Translate }> = ({ orders, language, t }) => (
  <div className="overflow-x-auto">
    <table className="min-w-full divide-y divide-border/70 text-sm">
      <thead className="bg-surface-2/70 text-left text-xs uppercase tracking-[0.16em] text-secondary-text">
        <tr>
          <th className="px-4 py-2.5 font-medium">{t('trading.table.symbol')}</th>
          <th className="px-4 py-2.5 font-medium">{t('trading.table.side')}</th>
          <th className="px-4 py-2.5 font-medium">{t('trading.orders.table.type')}</th>
          <th className="px-4 py-2.5 text-right font-medium">{t('trading.table.qty')}</th>
          <th className="px-4 py-2.5 text-right font-medium">{t('trading.orders.table.limit')}</th>
          <th className="px-4 py-2.5 text-right font-medium">{t('trading.table.stop')}</th>
          <th className="px-4 py-2.5 font-medium">{t('trading.orders.table.status')}</th>
          <th className="px-4 py-2.5 font-medium">{t('trading.orders.table.submitted')}</th>
        </tr>
      </thead>
      <tbody className="divide-y divide-border/60">
        {orders.map((order) => (
          <tr key={order.id} className="hover:bg-hover/60">
            <td className="whitespace-nowrap px-4 py-2.5 font-mono font-semibold text-foreground">{order.symbol}</td>
            <td className="whitespace-nowrap px-4 py-2.5 uppercase text-secondary-text">{order.side}</td>
            <td className="whitespace-nowrap px-4 py-2.5 text-secondary-text">{[order.orderType, order.orderClass].filter(Boolean).join(' · ') || '-'}</td>
            <td className="whitespace-nowrap px-4 py-2.5 text-right font-mono tabular-nums text-foreground">{formatQty(order.qty, language)}</td>
            <td className="whitespace-nowrap px-4 py-2.5 text-right font-mono tabular-nums text-foreground">{formatPrice(order.limitPrice, language)}</td>
            <td className="whitespace-nowrap px-4 py-2.5 text-right font-mono tabular-nums text-foreground">{formatPrice(order.stopPrice, language)}</td>
            <td className="whitespace-nowrap px-4 py-2.5"><Badge variant="warning">{order.status || '-'}</Badge></td>
            <td className="whitespace-nowrap px-4 py-2.5 text-secondary-text">{formatTime(order.submittedAt, language)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  </div>
);

const ActivityTable: React.FC<{ items: PaperTradeActivity[]; language: UiLanguage; t: Translate }> = ({ items, language, t }) => (
  <div className="overflow-x-auto">
    <table className="min-w-full divide-y divide-border/70 text-sm">
      <thead className="bg-surface-2/70 text-left text-xs uppercase tracking-[0.16em] text-secondary-text">
        <tr>
          <th className="px-4 py-2.5 font-medium">{t('trading.activity.table.time')}</th>
          <th className="px-4 py-2.5 font-medium">{t('trading.table.symbol')}</th>
          <th className="px-4 py-2.5 font-medium">{t('trading.activity.table.action')}</th>
          <th className="px-4 py-2.5 text-right font-medium">{t('trading.table.qty')}</th>
          <th className="px-4 py-2.5 text-right font-medium">{t('trading.orders.table.limit')}</th>
          <th className="px-4 py-2.5 text-right font-medium">{t('trading.table.stop')}</th>
          <th className="px-4 py-2.5 text-right font-medium">{t('trading.table.target')}</th>
          <th className="px-4 py-2.5 font-medium">{t('trading.activity.table.status')}</th>
          <th className="px-4 py-2.5 font-medium">{t('trading.activity.table.reason')}</th>
        </tr>
      </thead>
      <tbody className="divide-y divide-border/60">
        {items.map((item) => (
          <tr key={item.id} className="hover:bg-hover/60">
            <td className="whitespace-nowrap px-4 py-2.5 text-secondary-text">{formatTime(item.createdAt, language)}</td>
            <td className="whitespace-nowrap px-4 py-2.5 font-mono font-semibold text-foreground">{item.symbol}</td>
            <td className="whitespace-nowrap px-4 py-2.5 text-foreground">
              {item.action ?? '-'}
              {item.side ? <span className="ml-1 text-xs uppercase text-secondary-text">{item.side.replace(/_/g, ' ')}</span> : null}
            </td>
            <td className="whitespace-nowrap px-4 py-2.5 text-right font-mono tabular-nums text-foreground">{formatQty(item.qty, language)}</td>
            <td className="whitespace-nowrap px-4 py-2.5 text-right font-mono tabular-nums text-secondary-text">{formatPrice(item.limitPrice, language)}</td>
            <td className="whitespace-nowrap px-4 py-2.5 text-right font-mono tabular-nums text-secondary-text">{formatPrice(item.stopPrice, language)}</td>
            <td className="whitespace-nowrap px-4 py-2.5 text-right font-mono tabular-nums text-secondary-text">{formatPrice(item.targetPrice, language)}</td>
            <td className="whitespace-nowrap px-4 py-2.5">
              <div className="flex items-center gap-1.5">
                <Badge variant={statusBadgeVariant(item.status)}>{item.status}</Badge>
                {item.dryRun ? <Badge variant="info">{t('trading.status.dryRun')}</Badge> : null}
              </div>
            </td>
            <td className="max-w-[24rem] truncate px-4 py-2.5 text-secondary-text" title={item.reason ?? undefined}>{item.reason ?? '-'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  </div>
);

type CloseAllDialogProps = {
  count: number;
  exposure: string;
  busy: boolean;
  t: Translate;
  onConfirm: () => void;
  onCancel: () => void;
};

const CloseAllDialog: React.FC<CloseAllDialogProps> = ({ count, exposure, busy, t, onConfirm, onCancel }) => {
  const [phrase, setPhrase] = useState('');
  const armed = phrase.trim().toUpperCase() === CLOSE_ALL_PHRASE;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
      onClick={() => {
        if (!busy) onCancel();
      }}
      role="presentation"
    >
      <div
        className="mx-4 w-full max-w-md rounded-xl border border-danger/40 bg-elevated p-6 shadow-2xl animate-in fade-in zoom-in duration-200"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="close-all-title"
      >
        <h3 id="close-all-title" className="mb-3 text-lg font-semibold text-foreground">{t('trading.closeAll.title')}</h3>
        <p className="mb-4 text-sm leading-relaxed text-secondary-text">
          {t('trading.closeAll.message', { count, exposure })}
        </p>
        <label className="mb-1 block text-xs uppercase tracking-[0.16em] text-secondary-text" htmlFor="close-all-phrase">
          {t('trading.closeAll.typeToConfirm', { phrase: CLOSE_ALL_PHRASE })}
        </label>
        <input
          id="close-all-phrase"
          type="text"
          autoComplete="off"
          value={phrase}
          onChange={(event) => setPhrase(event.target.value)}
          disabled={busy}
          className="mb-5 h-10 w-full rounded-lg border border-border/70 bg-card px-3 font-mono text-sm uppercase tracking-[0.2em] text-foreground outline-none focus:border-danger/60"
          placeholder={CLOSE_ALL_PHRASE}
        />
        <div className="flex justify-end gap-3">
          <Button variant="ghost" onClick={onCancel} disabled={busy}>{t('common.cancel')}</Button>
          <Button variant="danger" onClick={onConfirm} disabled={!armed || busy} isLoading={busy}>
            {t('trading.closeAll.confirm')}
          </Button>
        </div>
      </div>
    </div>
  );
};

type ActionNotice = { variant: 'success' | 'warning' | 'danger' | 'info'; title?: string; message: string };

const PaperTradingPage: React.FC = () => {
  const { language, t } = useUiLanguage();
  const [dashboard, setDashboard] = useState<PaperDashboard | null>(null);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [pendingClose, setPendingClose] = useState<PaperPosition | null>(null);
  const [closeAllOpen, setCloseAllOpen] = useState(false);
  const [busySymbol, setBusySymbol] = useState<string | null>(null);
  const [closingAll, setClosingAll] = useState(false);
  const [notice, setNotice] = useState<ActionNotice | null>(null);
  const requestSeqRef = useRef(0);

  const loadDashboard = useCallback(async (options: { silent?: boolean } = {}) => {
    const requestSeq = requestSeqRef.current + 1;
    requestSeqRef.current = requestSeq;
    if (!options.silent) {
      setLoading(true);
    }
    try {
      const data = await paperTradingApi.getDashboard();
      if (requestSeq !== requestSeqRef.current) return;
      setDashboard(data);
      setError(null);
    } catch (err) {
      if (requestSeq !== requestSeqRef.current) return;
      setError(getParsedApiError(err));
    } finally {
      if (requestSeq === requestSeqRef.current) {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    void loadDashboard();
    return () => {
      requestSeqRef.current += 1;
    };
  }, [loadDashboard]);

  useEffect(() => {
    if (!autoRefresh) return undefined;
    const tick = () => {
      if (document.visibilityState === 'visible') {
        void loadDashboard({ silent: true });
      }
    };
    const timer = window.setInterval(tick, AUTO_REFRESH_MS);
    document.addEventListener('visibilitychange', tick);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', tick);
    };
  }, [autoRefresh, loadDashboard]);

  const describeCloseResult = useCallback((result: PaperCloseResult): string => {
    const side = result.side === 'buy_to_cover' ? t('trading.closeSide.buyToCover') : t('trading.closeSide.sell');
    if (result.status === 'submitted') {
      return t('trading.result.closeSubmitted', { symbol: result.symbol, side, qty: formatQty(result.qty, language) });
    }
    if (result.status === 'dry_run') {
      return t('trading.result.closeDryRun', { symbol: result.symbol });
    }
    return t('trading.result.closeError', { symbol: result.symbol, message: result.message || '-' });
  }, [language, t]);

  const refreshAfterAction = useCallback(() => {
    void loadDashboard({ silent: true });
    window.setTimeout(() => void loadDashboard({ silent: true }), 3000);
  }, [loadDashboard]);

  const confirmClose = useCallback(async () => {
    if (!pendingClose) return;
    const position = pendingClose;
    setPendingClose(null);
    setBusySymbol(position.symbol);
    setNotice(null);
    try {
      const result = await paperTradingApi.closePosition(position.symbol);
      setNotice({
        variant: result.status === 'error' ? 'danger' : result.status === 'dry_run' ? 'info' : 'success',
        message: describeCloseResult(result),
      });
    } catch (err) {
      const parsed = getParsedApiError(err);
      setNotice({ variant: 'danger', title: parsed.title, message: parsed.message });
    } finally {
      setBusySymbol(null);
      refreshAfterAction();
    }
  }, [describeCloseResult, pendingClose, refreshAfterAction]);

  const confirmCloseAll = useCallback(async () => {
    setClosingAll(true);
    setNotice(null);
    try {
      const summary = await paperTradingApi.closeAllPositions();
      const lines = summary.results.map(describeCloseResult);
      setNotice({
        variant: summary.failed > 0 ? (summary.closed > 0 ? 'warning' : 'danger') : 'success',
        title: t('trading.result.closeAllSummary', { closed: summary.closed, failed: summary.failed, requested: summary.requested }),
        message: lines.join(' · '),
      });
      setCloseAllOpen(false);
    } catch (err) {
      const parsed = getParsedApiError(err);
      setNotice({ variant: 'danger', title: parsed.title, message: parsed.message });
    } finally {
      setClosingAll(false);
      refreshAfterAction();
    }
  }, [describeCloseResult, refreshAfterAction, t]);

  const positions = dashboard?.positions ?? [];
  const actionsDisabled = closingAll || busySymbol !== null;
  const pnl = dashboard?.pnl;
  const unavailable = error?.status === 503;

  return (
    <AppPage>
      <div className="space-y-4">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          {dashboard ? (
            <>
              <span className="inline-flex items-center gap-2 rounded-lg border border-border/60 bg-card/70 px-2.5 py-1.5 text-foreground">
                <StatusDot tone={dashboard.status.marketOpen ? 'success' : 'neutral'} />
                {dashboard.status.marketOpen ? t('trading.market.open') : t('trading.market.closed')}
              </span>
              {dashboard.status.dryRun ? <Badge variant="info" size="md">{t('trading.status.dryRun')}</Badge> : null}
              {dashboard.status.killSwitchActive ? <Badge variant="danger" size="md" glow>{t('trading.status.killSwitch')}</Badge> : null}
              {!dashboard.status.enabled ? <Badge variant="warning" size="md">{t('trading.status.botDisabled')}</Badge> : null}
              <span className="font-mono tabular-nums text-secondary-text">{formatTime(dashboard.generatedAt, language, false)}</span>
            </>
          ) : null}
          <div className="ml-auto flex items-center gap-2">
            <button
              type="button"
              className={cn('btn-secondary inline-flex h-8 items-center gap-1.5 px-2.5 text-xs', autoRefresh ? '' : 'opacity-80')}
              onClick={() => setAutoRefresh((value) => !value)}
              aria-pressed={autoRefresh}
            >
              {autoRefresh ? <Pause className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
              {autoRefresh ? t('trading.autoRefreshOn') : t('trading.autoRefreshOff')}
            </button>
            <button
              type="button"
              className="btn-secondary inline-flex h-8 items-center gap-1.5 px-2.5 text-xs"
              onClick={() => void loadDashboard()}
              disabled={loading}
              aria-label={t('trading.refresh')}
            >
              <RefreshCw className={cn('h-3.5 w-3.5', loading ? 'animate-spin' : '')} />
            </button>
          </div>
        </div>

        {error && !unavailable ? (
          <ApiErrorAlert error={error} actionLabel={t('common.retry')} onAction={() => void loadDashboard()} />
        ) : null}

        {unavailable ? (
          <EmptyState
            title={t('trading.unavailable.title')}
            description={t('trading.unavailable.description')}
            action={(
              <button type="button" className="btn-secondary" onClick={() => void loadDashboard()}>{t('common.retry')}</button>
            )}
          />
        ) : null}

        {loading && !dashboard && !error ? (
          <div className="grid gap-3 md:grid-cols-4 xl:grid-cols-7">
            {Array.from({ length: 7 }).map((_, index) => (
              <div key={index} className="h-20 animate-pulse rounded-xl border border-border/60 bg-card/60" />
            ))}
          </div>
        ) : null}

        {dashboard && pnl ? (
          <>
            <div className="grid gap-3 md:grid-cols-4 xl:grid-cols-7">
              <Kpi
                label={t('trading.kpi.totalPnl')}
                value={<Money value={pnl.totalPnl} language={language} />}
                sub={<span className={pnlClass(pnl.totalPnl)}>{formatPct(pnl.totalPnlPct, language, { signed: true, isPercent: true })}</span>}
                testId="kpi-total-pnl"
              />
              <Kpi
                label={t('trading.kpi.dayPnl')}
                value={<Money value={pnl.dayPnl} language={language} />}
                sub={<span className={pnlClass(pnl.dayPnl)}>{formatPct(pnl.dayPnlPct, language, { signed: true, isPercent: true })}</span>}
                testId="kpi-day-pnl"
              />
              <Kpi label={t('trading.kpi.unrealized')} value={<Money value={pnl.unrealizedPnl} language={language} />} sub={`${dashboard.exposure.openPositions}/${dashboard.status.maxPositions}`} />
              <Kpi
                label={t('trading.kpi.realized')}
                value={<Money value={pnl.realizedPnl} language={language} />}
                sub={t('trading.kpi.closedTrades', { count: pnl.closedTrades, wins: pnl.wins, losses: pnl.losses })}
              />
              <Kpi label={t('trading.kpi.equity')} value={<span className="font-mono tabular-nums text-foreground">{formatMoney(dashboard.account.equity, language)}</span>} />
              <Kpi label={t('trading.kpi.cash')} value={<span className="font-mono tabular-nums text-foreground">{formatMoney(dashboard.account.cash, language)}</span>} sub={formatMoney(dashboard.account.buyingPower, language, { compact: true })} />
              <Kpi label={t('trading.kpi.exposure')} value={<span className="font-mono tabular-nums text-foreground">{formatMoney(dashboard.exposure.grossExposure, language)}</span>} sub={formatPct(dashboard.exposure.grossExposurePct, language, { isPercent: true })} />
            </div>

            <section className="rounded-xl border border-border/60 bg-card/75">
              <div className="flex items-baseline justify-between gap-3 px-4 pt-3">
                <h2 className="text-sm font-semibold text-foreground">{t('trading.chart.title')}</h2>
                {pnl.since ? <span className="font-mono text-xs tabular-nums text-secondary-text">{pnl.since} →</span> : null}
              </div>
              <PnlChart points={dashboard.pnlHistory} language={language} t={t} />
            </section>

            {notice ? (
              <InlineAlert
                variant={notice.variant}
                title={notice.title}
                message={notice.message}
                action={(
                  <button type="button" className="text-xs underline-offset-2 hover:underline" onClick={() => setNotice(null)}>
                    {t('common.close')}
                  </button>
                )}
              />
            ) : null}

            <section className="space-y-2">
              <div className="flex items-center justify-between gap-3">
                <h2 className="text-sm font-semibold text-foreground">
                  {t('trading.positions.title')}
                  <span className="ml-2 font-mono text-xs text-secondary-text">{positions.length}</span>
                </h2>
                <Button
                  variant="danger"
                  size="sm"
                  disabled={positions.length === 0 || actionsDisabled}
                  isLoading={closingAll}
                  onClick={() => setCloseAllOpen(true)}
                >
                  {t('trading.positions.closeAll')}
                </Button>
              </div>
              <div className="overflow-hidden rounded-xl border border-border/60 bg-card/75">
                {positions.length ? (
                  <PositionsTable
                    positions={positions}
                    language={language}
                    t={t}
                    busySymbol={busySymbol}
                    disabled={actionsDisabled}
                    onClose={setPendingClose}
                  />
                ) : (
                  <p className="px-4 py-8 text-center text-sm text-secondary-text">{t('trading.positions.empty')}</p>
                )}
              </div>
            </section>

            <div className="grid gap-4 xl:grid-cols-2">
              <section className="space-y-2">
                <h2 className="text-sm font-semibold text-foreground">
                  {t('trading.orders.title')}
                  <span className="ml-2 font-mono text-xs text-secondary-text">{dashboard.openOrders.length}</span>
                </h2>
                <div className="overflow-hidden rounded-xl border border-border/60 bg-card/75">
                  {dashboard.openOrders.length ? (
                    <OpenOrdersTable orders={dashboard.openOrders} language={language} t={t} />
                  ) : (
                    <p className="px-4 py-6 text-center text-sm text-secondary-text">{t('trading.orders.empty')}</p>
                  )}
                </div>
              </section>

              <section className="space-y-2">
                <h2 className="text-sm font-semibold text-foreground">{t('trading.activity.title')}</h2>
                <div className="overflow-hidden rounded-xl border border-border/60 bg-card/75">
                  {dashboard.recentTrades.length ? (
                    <ActivityTable items={dashboard.recentTrades} language={language} t={t} />
                  ) : (
                    <p className="px-4 py-6 text-center text-sm text-secondary-text">{t('trading.activity.empty')}</p>
                  )}
                </div>
              </section>
            </div>
          </>
        ) : null}
      </div>

      <ConfirmDialog
        isOpen={pendingClose !== null}
        title={t('trading.close.title', { symbol: pendingClose?.symbol ?? '' })}
        message={pendingClose ? t('trading.close.message', {
          side: pendingClose.side === 'short' ? t('trading.closeSide.buyToCover') : t('trading.closeSide.sell'),
          qty: formatQty(pendingClose.qty, language),
          symbol: pendingClose.symbol,
          pnl: formatMoney(pendingClose.unrealizedPl, language, { signed: true }),
        }) : ''}
        confirmText={t('trading.close.confirm')}
        cancelText={t('common.cancel')}
        isDanger
        onConfirm={() => void confirmClose()}
        onCancel={() => setPendingClose(null)}
      />

      {closeAllOpen ? (
        <CloseAllDialog
          count={positions.length}
          exposure={formatMoney(dashboard?.exposure.grossExposure ?? 0, language)}
          busy={closingAll}
          t={t}
          onConfirm={() => void confirmCloseAll()}
          onCancel={() => setCloseAllOpen(false)}
        />
      ) : null}
    </AppPage>
  );
};

export default PaperTradingPage;
