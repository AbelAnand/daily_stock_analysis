import type { UiLanguage } from '../i18n/uiText';

interface ValidationResult {
  valid: boolean;
  message?: string;
  normalized: string;
}

const VALIDATION_TEXT = {
  zh: {
    required: '请输入股票代码',
    invalidFormat: '股票代码格式不正确',
  },
  en: {
    required: 'Please enter a stock code',
    invalidFormat: 'Invalid stock code format',
  },
} as const;

const SUPPORTED_QUERY_CHARACTERS = /^[A-Z0-9.\u3400-\u9FFF\s]+$/;

const STOCK_CODE_PATTERNS = [
  /^\d{6}$/, // A-share 6-digit code
  /^(SH|SZ|BJ)\d{6}$/, // A-share code with exchange prefix
  /^\d{6}\.(SH|SZ|SS|BJ)$/, // A-share code with exchange suffix
  /^\d{5}$/, // HK code without prefix
  /^HK\d{1,5}$/, // HK-prefixed code, for example HK00700
  /^\d{1,5}\.HK$/, // HK suffix format, for example 00700.HK
  /^\d{4,5}\.T$/, // Japan Yahoo suffix format, for example 7203.T
  /^\d{6}\.(KS|KQ)$/, // Korea Yahoo suffix format, for example 005930.KS or 035720.KQ
  /^[A-Z]{1,5}(?:\.(?:US|[A-Z]))?$/, // Common US ticker format
];

/**
 * Check whether the input looks like a stock code.
 */
export const looksLikeStockCode = (value: string): boolean => {
  const normalized = value.trim().toUpperCase();
  return STOCK_CODE_PATTERNS.some((regex) => regex.test(normalized));
};

/**
 * Validate common A-share, HK, US, JP, and KR stock code formats.
 */
export const validateStockCode = (value: string, language: UiLanguage = 'zh'): ValidationResult => {
  const normalized = value.trim().toUpperCase();
  const text = VALIDATION_TEXT[language];

  if (!normalized) {
    return { valid: false, message: text.required, normalized };
  }

  const valid = looksLikeStockCode(normalized);

  return {
    valid,
    message: valid ? undefined : text.invalidFormat,
    normalized,
  };
};

/**
 * Reject obviously invalid free-text queries before they reach the backend.
 */
export const isObviouslyInvalidStockQuery = (value: string): boolean => {
  const normalized = value.trim().toUpperCase();

  if (!normalized || looksLikeStockCode(normalized)) {
    return false;
  }

  if (!SUPPORTED_QUERY_CHARACTERS.test(normalized)) {
    return true;
  }

  const hasLetters = /[A-Z]/.test(normalized);
  const hasDigits = /\d/.test(normalized);

  return hasLetters && hasDigits;
};
