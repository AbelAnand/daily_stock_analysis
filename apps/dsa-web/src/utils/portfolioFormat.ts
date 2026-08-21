import type {
  PortfolioCashDirection,
  PortfolioCorporateActionType,
  PortfolioFxRefreshResponse,
  PortfolioImportCommitResponse,
  PortfolioImportParseResponse,
  PortfolioPositionItem,
  PortfolioSide,
} from '../types/portfolio';
import type { UiLanguage } from '../i18n/uiText';
import {
  PORTFOLIO_CASH_DIRECTION_LABELS,
  PORTFOLIO_CORPORATE_ACTION_LABELS,
  PORTFOLIO_SIDE_LABELS,
} from '../locales/featureText';
import { toDateInputValue } from './format';

export type FxRefreshFeedback = {
  tone: 'neutral' | 'success' | 'warning';
  text: string;
};

export type PortfolioAlertVariant = 'info' | 'success' | 'warning' | 'danger';

export function getTodayIso(): string {
  return toDateInputValue(new Date());
}

export function formatMoney(value: number | undefined | null, currency = 'CNY'): string {
  if (value == null || Number.isNaN(value)) return '--';
  return `${currency} ${Number(value).toLocaleString('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

export function formatPct(value: number | undefined | null): string {
  if (value == null || Number.isNaN(value)) return '--';
  return `${value.toFixed(2)}%`;
}

export function formatSignedPct(value: number | undefined | null): string {
  if (value == null || Number.isNaN(value)) return '--';
  const sign = value > 0 ? '+' : '';
  return `${sign}${value.toFixed(2)}%`;
}

export function hasPositionPrice(row: PortfolioPositionItem): boolean {
  return row.priceAvailable !== false && row.priceSource !== 'missing';
}

export function formatPositionPrice(row: PortfolioPositionItem): string {
  if (!hasPositionPrice(row)) return '--';
  return row.lastPrice.toFixed(4);
}

export function formatPositionMoney(value: number, row: PortfolioPositionItem): string {
  if (!hasPositionPrice(row)) return '--';
  return formatMoney(value, row.valuationCurrency);
}

const POSITION_PRICE_LABEL_TEXT: Record<UiLanguage, {
  missingPrice: string;
  realtimePrice: string;
  closePrice: string;
  unknownSource: string;
}> = {
  zh: {
    missingPrice: '缺价',
    realtimePrice: '实时价',
    closePrice: '收盘价',
    unknownSource: '未知来源',
  },
  en: {
    missingPrice: 'Price unavailable',
    realtimePrice: 'Realtime',
    closePrice: 'Close',
    unknownSource: 'Unknown source',
  },
};

export function getPositionPriceLabel(row: PortfolioPositionItem, language: UiLanguage): string {
  const text = POSITION_PRICE_LABEL_TEXT[language];
  if (!hasPositionPrice(row)) return text.missingPrice;
  if (row.priceSource === 'realtime_quote') {
    return row.priceProvider ? `${text.realtimePrice} · ${row.priceProvider}` : text.realtimePrice;
  }
  if (row.priceSource === 'history_close') {
    return row.priceStale && row.priceDate ? `${text.closePrice} · ${row.priceDate}` : text.closePrice;
  }
  return row.priceSource || text.unknownSource;
}

export function formatSideLabel(value: PortfolioSide, language: UiLanguage): string {
  return PORTFOLIO_SIDE_LABELS[language][value];
}

export function formatCashDirectionLabel(value: PortfolioCashDirection, language: UiLanguage): string {
  return PORTFOLIO_CASH_DIRECTION_LABELS[language][value];
}

export function formatCorporateActionLabel(value: PortfolioCorporateActionType, language: UiLanguage): string {
  return PORTFOLIO_CORPORATE_ACTION_LABELS[language][value];
}

const FALLBACK_BROKER_DISPLAY_NAMES: Record<UiLanguage, Record<'huatai' | 'citic' | 'cmb', string>> = {
  zh: { huatai: '华泰', citic: '中信', cmb: '招商' },
  en: { huatai: 'Huatai', citic: 'CITIC', cmb: 'CMB' },
};

export function formatBrokerLabel(value: string, displayName: string | undefined, language: UiLanguage): string {
  if (displayName && displayName.trim()) return `${value}（${displayName.trim()}）`;
  const fallbackName = FALLBACK_BROKER_DISPLAY_NAMES[language][value as 'huatai' | 'citic' | 'cmb'];
  return fallbackName ? `${value}（${fallbackName}）` : value;
}

const FX_REFRESH_FEEDBACK_TEXT: Record<UiLanguage, {
  disabled: string;
  noPairs: string;
  refreshed: (count: number) => string;
  summary: (updated: number, stale: number, errors: number) => string;
  partialStale: (summary: string) => string;
  partialFailure: (summary: string) => string;
}> = {
  zh: {
    disabled: '汇率在线刷新已被禁用。',
    noPairs: '当前范围无可刷新的汇率对。',
    refreshed: (count) => `汇率已刷新，共更新 ${count} 对。`,
    summary: (updated, stale, errors) => `更新 ${updated} 对，仍过期 ${stale} 对，失败 ${errors} 对。`,
    partialStale: (summary) => `已尝试刷新，但仍有部分货币对使用 stale/fallback 汇率。${summary}`,
    partialFailure: (summary) => `在线刷新未完全成功。${summary}`,
  },
  en: {
    disabled: 'Online FX refresh is disabled.',
    noPairs: 'No refreshable FX pairs in the current scope.',
    refreshed: (count) => `FX rates refreshed; updated ${count} pair(s).`,
    summary: (updated, stale, errors) => `Updated ${updated}, still stale ${stale}, failed ${errors} pair(s).`,
    partialStale: (summary) => `Refresh was attempted, but some currency pairs still use stale/fallback rates. ${summary}`,
    partialFailure: (summary) => `Online refresh did not fully succeed. ${summary}`,
  },
};

export function buildFxRefreshFeedback(data: PortfolioFxRefreshResponse, language: UiLanguage): FxRefreshFeedback {
  const text = FX_REFRESH_FEEDBACK_TEXT[language];
  if (data.refreshEnabled === false) {
    return {
      tone: 'neutral',
      text: text.disabled,
    };
  }

  if (data.pairCount === 0) {
    return {
      tone: 'neutral',
      text: text.noPairs,
    };
  }

  if (data.updatedCount > 0 && data.staleCount === 0 && data.errorCount === 0) {
    return {
      tone: 'success',
      text: text.refreshed(data.updatedCount),
    };
  }

  const summary = text.summary(data.updatedCount, data.staleCount, data.errorCount);
  if (data.staleCount > 0) {
    return {
      tone: 'warning',
      text: text.partialStale(summary),
    };
  }

  return {
    tone: 'warning',
    text: text.partialFailure(summary),
  };
}

export function getFxRefreshFeedbackVariant(tone: FxRefreshFeedback['tone']): PortfolioAlertVariant {
  if (tone === 'success') return 'success';
  if (tone === 'warning') return 'warning';
  return 'info';
}

export function getCsvParseVariant(result: PortfolioImportParseResponse): PortfolioAlertVariant {
  return result.errorCount > 0 || result.skippedCount > 0 ? 'warning' : 'info';
}

export function getCsvCommitVariant(result: PortfolioImportCommitResponse, isDryRun: boolean): PortfolioAlertVariant {
  if (isDryRun) return 'info';
  return result.failedCount > 0 || result.duplicateCount > 0 ? 'warning' : 'success';
}
