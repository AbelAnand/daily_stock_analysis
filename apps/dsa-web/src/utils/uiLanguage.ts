import type { UiLanguage } from '../i18n/uiText';

export const UI_LANGUAGE_STORAGE_KEY = 'dsa.uiLanguage';

export function normalizeUiLanguage(value?: string | null): UiLanguage | null {
  if (value === 'zh' || value === 'en') {
    return value;
  }
  return null;
}

function getStoredUiLanguage(storage?: Storage | null): UiLanguage | null {
  if (!storage) {
    return null;
  }

  try {
    return normalizeUiLanguage(storage.getItem(UI_LANGUAGE_STORAGE_KEY));
  } catch {
    return null;
  }
}

export function getUiLanguageStorage(): Storage | null {
  if (typeof window === 'undefined') {
    return null;
  }

  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

export function persistUiLanguage(storage: Storage | null, language: UiLanguage): void {
  if (!storage) {
    return;
  }

  try {
    storage.setItem(UI_LANGUAGE_STORAGE_KEY, language);
  } catch {
    // Ignore storage failures; in-memory language still updates.
  }
}

export const DEFAULT_UI_LANGUAGE: UiLanguage = 'en';

/**
 * Resolve the initial UI language.
 *
 * Only an explicit stored preference overrides the default; browser locale is
 * intentionally ignored so the UI always starts in English unless the user
 * has chosen otherwise via the language toggle.
 */
export function resolveInitialUiLanguage({
  storage,
}: {
  storage?: Storage | null;
  navigatorLike?: Pick<Navigator, 'language' | 'languages'> | null;
} = {}): UiLanguage {
  return getStoredUiLanguage(storage) ?? DEFAULT_UI_LANGUAGE;
}

export function getRuntimeInitialLanguage(): UiLanguage {
  if (typeof window === 'undefined') {
    return DEFAULT_UI_LANGUAGE;
  }

  return resolveInitialUiLanguage({ storage: getUiLanguageStorage() });
}
