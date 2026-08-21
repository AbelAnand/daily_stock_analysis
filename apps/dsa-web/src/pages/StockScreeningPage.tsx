import type React from 'react';
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity,
  Bookmark,
  Building2,
  CheckCircle2,
  ChevronDown,
  CircleAlert,
  Clock3,
  Droplet,
  Factory,
  Flame,
  Gem,
  Landmark,
  Pickaxe,
  Plane,
  Play,
  PlusCircle,
  RefreshCw,
  Search,
  Shield,
  SlidersHorizontal,
  Stethoscope,
  Trees,
  Utensils,
  Wrench,
} from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import {
  screeningApi,
  type ScreeningCandidate,
  type ScreeningHotspotDetail,
  type ScreeningHotspot,
  type ScreeningHotspotsResponse,
  type ScreeningScreenResponse,
  type ScreeningScreenTaskStatus,
  type ScreeningStrategy,
} from '../api/screening';
import { formatParsedApiError, getParsedApiError, toApiErrorMessage, type ParsedApiError } from '../api/error';
import { AppPage, Button, InlineAlert, Select } from '../components/common';
import { useUiLanguage } from '../contexts/UiLanguageContext';
import type { UiLanguage } from '../i18n/uiText';

const SCREENING_TEXT: Record<UiLanguage, Record<string, string>> = {
  zh: {
    customStrategy: '自定义',
    customStrategyOption: '自定义策略…',
    customStrategyTemplate: '自定义策略 ({strategy})',
    unrecoverableTaskFallback: '选股任务不可恢复，请重新提交。',
    pollingTimeoutRetry: '选股任务仍在后台运行，状态轮询暂时超时，将自动重试。',
    pollingNetworkRetry: '选股任务仍在后台运行，暂时无法连接本地服务获取状态，将自动重试。',
    pollingUnknownRetry: '暂时无法获取选股任务状态，稍后将自动重试。',
    llmRankedThesis: 'LLM 已完成相对排序。',
    noSummary: '暂无摘要，请查看因子和风险信息。',
    watchSignal: '观察',
    mainAdvantagesTemplate: '主要优势：{factors}',
    tagsTemplate: '；标签：{tags}',
    modelNoStructuredResult: '模型未返回可用的结构化排序结果',
    modelCallFailed: '模型调用失败',
    noTradingCalendarDays: '交易日历暂无可用开市日',
    tooManyRequests: '请求过于频繁',
    accessDenied: '访问被拒绝',
    requestTimeout: '请求超时',
    networkDisconnected: '网络连接中断',
    missingLlmApiKey: '缺少可用 LLM API Key',
    noDataReturned: '未返回可用数据',
    marketPrefix: '行情：',
    newsPrefix: '新闻：',
    eventPrefix: '事件：',
    llmRankingSkipped: '未配置智能重排模型，当前使用确定性因子排序。',
    llmRankingFailedTemplate: '未完成智能重排：{detail}；当前结果继续使用确定性因子评分。',
    dailyKlineEnrichmentErrors: '部分候选的日线数据未能补齐，结果已按可用数据生成。',
    dailyKlineEnrichmentSkipped: '可选日线数据补充未完成，结果已按快照数据生成。',
    candidateContextErrors: '部分候选的辅助数据未能补齐。',
    industryConceptsIncomplete: '部分行业或题材信息未能补齐。',
    deepAnalysisIncomplete: '部分候选的深度分析未完成。',
    dataSourceDowngradedWithDetailTemplate: '数据源降级：{source}（{detail}）',
    dataSourceDowngradedTemplate: '数据源降级：{detail}',
    screenTaskFailedGeneric: '选股任务失败，请稍后重试。',
    screenTaskFailedTemplate: '选股任务失败：{detail}',
    noHotspotCache: '暂无热点缓存',
    hotspotNoDataTemplate: '热点题材暂未返回数据：{detail}',
    hotspotNoData: '热点题材暂未返回数据',
    riskHigh: '高',
    riskMedium: '中',
    riskLow: '低',
    riskPending: '待评估',
    timeUnconfirmed: '时间待确认',
    routeCurrentFermentation: '当前发酵',
    routeNewsCatalyst: '消息催化',
    hotspotChangeFallback: '热点变化',
    noMoreDescription: '暂无更多说明。',
    heatSuffix: '热度',
    stagePrefix: '阶段',
    coreStocksPrefix: '核心股',
    hotspotDataChanged: '热点数据出现新变化。',
    someDetailsMissing: '部分明细',
    hotspotDetailTimeoutTemplate: '热点明细请求超时（{seconds} 秒）',
    hotspotDetailTimeout: '热点明细请求超时',
    hotspotSourceDisconnected: '热点数据源连接中断',
    hotspotSourceNoData: '热点数据源暂未返回数据',
    hotspotRequestTooFrequent: '热点数据请求过于频繁',
    hotspotDataUnavailable: '部分热点数据暂不可用',
    cacheFallbackTemplate: '缓存回退 {hours}h',
    cacheFallback: '缓存回退',
    backupDataSource: '备用数据源',
    hotspotSummaryTemplate: '{name}：{parts}。',
    hotspotDetailLoaded: '已加载热点详情。',
    watching: '观察中',
    activeStocksWatching: '活跃股观察中',
    coverageStocksTemplate: '覆盖 {count} 股',
    noQuoteData: '暂无行情',
    pendingRefresh: '待刷新',
    strengthLeading: '强势领先',
    strengthStrong: '强势',
    strengthModerate: '较强',
    conceptStockFallback: '概念股',
    strategyLoadFailed: '策略列表加载失败',
    hotspotLoadFailedTemplate: '热点题材加载失败，请稍后重试。',
    messageSearchFailed: '消息搜索失败，请稍后重试。',
    hotspotDetailLoadFailed: '热点题材详情加载失败，请稍后重试。',
    noRecentNewsFound: '暂未搜到该题材近期的有效消息。',
    restoringScreenTaskStatus: '正在恢复选股任务状态...',
    screenTaskCompletedNoResult: '选股任务已完成，但服务端未返回候选结果。',
    screenTaskUnknownStatusTemplate: '选股任务返回未知状态：{status}',
    submittingScreenTask: '正在提交选股任务...',
    screenTaskSubmitted: '选股任务已提交',
    screenTaskSubmitFailed: '选股任务提交失败，请稍后重试。',
    enableScreeningFailed: '开启选股失败',
    title: '选股',
    screeningEnabled: '选股已开启',
    screeningDisabled: '选股未开启',
    screeningNotEnabledTitle: '选股未开启',
    screeningNotEnabledMessage: '开启后即可运行选股策略。',
    enabling: '开启中...',
    enableScreening: '开启选股',
    screeningUnavailableTitle: '选股功能不可用',
    screeningUnavailableMessage: '请检查后端日志、策略文件和基础数据依赖后重启服务。',
    callFailed: '调用失败',
    hotThemes: '热点题材',
    collapseHotThemes: '收起热点题材',
    expandHotThemesTemplate: '展开热点题材（{count}）',
    expandHotThemes: '展开热点题材',
    refreshing: '刷新中...',
    refreshHotThemes: '刷新热点题材',
    updatedAtTemplate: '更新于 {time}',
    noHotspotDataHint: '暂无热点数据，点击“刷新热点题材”获取。',
    changePct: '涨跌幅',
    trend: '趋势',
    persistence: '持续',
    leaders: '龙头',
    loadingHotspotRoute: '正在读取发酵路线与概念股...',
    clickHotspotHint: '点击题材查看发酵路线与概念股。',
    canonicalTopicTemplate: '标准题材：{topic}',
    searchLatestNews: '搜索最新消息',
    searching: '搜索中...',
    supplementingDetail: '正在补充详情',
    qualityTemplate: '质量 {label}',
    conceptStockCountTemplate: '概念股 {count}',
    detailDegraded: '详情数据已降级，展开查看原因',
    missingFieldsTemplate: '暂缺：{fields}',
    fermentationTimeline: '发酵时间线',
    viewMessage: '查看消息',
    conceptStocks: '概念股',
    analyzeStockAriaTemplate: '分析 {name}',
    analyze: '分析',
    stockMetricsTemplate: '涨跌幅 {change} · 热度 {heat}',
    runScreening: '运行选股',
    market: '市场',
    strategy: '策略',
    customStrategyId: '自定义策略 ID',
    inputStrategyId: '输入策略 ID',
    resultCount: '返回数量',
    filtering: '筛选中...',
    strategyDefaultDescription: '策略会先执行硬过滤和因子评分，再进行风险与组合约束。',
    screeningRunning: '选股运行中',
    screeningCompleted: '选股完成',
    runningScreening: '正在执行选股',
    runDetails: '运行详情',
    taskLabelTemplate: '任务：{id}',
    runIdTemplate: 'Run ID：{id}',
    snapshotSummaryTemplate: '快照 {snapshot} · 过滤后 {afterFilter} · 候选 {candidates}',
    sortingLlm: '智能重排',
    sortingFactor: '确定性因子',
    sortingTemplate: '排序：{mode}',
    coverageTemplate: ' · 覆盖 {coverage}',
    candidateVariancePoolTemplate: '候选差异：近分池 {pool} ·',
    variantAppliedTemplate: '本次替换 {slots} 位',
    variantBaseline: '本次保持基准',
    dsaEnrichmentTemplate: '深度补充：{enriched} / {requested}',
    usingFactorRanking: '当前使用因子排序',
    screeningHint: '选股提示',
    llmRankingIncomplete: '智能重排未完成，当前候选继续使用确定性因子评分。',
    screeningResults: '选股结果',
    candidatesCountTemplate: '{count} 条候选',
    noMatchingCandidates: '暂无符合条件的候选',
    columnCode: '代码',
    columnName: '名称',
    columnIndustry: '行业',
    columnPrice: '价格',
    columnChangePct: '涨跌幅',
    columnScore: '评分',
    columnRankingBasis: '排序依据',
    columnRisk: '风险',
    columnDetails: '详情',
    factorRankingLabel: '因子排序',
    collapse: '收起',
    expandView: '展开查看',
    summary: '摘要',
    operationSignal: '操作信号',
    deepAnalysis: '进一步深度分析',
    enrichedSummary: '增强摘要',
    llmInsight: '智能判断',
    llmInsightMetaTemplate: '板块 {sector} · 主题 {theme} · 置信度 {confidence}',
    riskTags: '风险标签',
    none: '无',
    mainFactors: '主要因子',
    noFactorDetail: '无因子明细',
    turnover: '成交额',
    llmWatchItems: '智能关注项',
    llmCatalysts: '催化因素',
    relatedNews: '相关新闻',
    announcementsAndEvents: '公告与事件',
    dataSupplementHint: '数据补充提示',
  },
  en: {
    customStrategy: 'Custom',
    customStrategyOption: 'Custom strategy…',
    customStrategyTemplate: 'Custom strategy ({strategy})',
    unrecoverableTaskFallback: 'The screening task cannot be recovered; please submit again.',
    pollingTimeoutRetry: 'The screening task is still running in the background. Status polling timed out; retrying automatically.',
    pollingNetworkRetry: 'The screening task is still running in the background. Unable to reach the local service for status; retrying automatically.',
    pollingUnknownRetry: 'Unable to fetch the screening task status right now; will retry automatically.',
    llmRankedThesis: 'LLM has completed relative ranking.',
    noSummary: 'No summary yet; check the factor and risk details.',
    watchSignal: 'Watch',
    mainAdvantagesTemplate: 'Main strengths: {factors}',
    tagsTemplate: '; Tags: {tags}',
    modelNoStructuredResult: 'The model did not return a usable structured ranking result',
    modelCallFailed: 'Model call failed',
    noTradingCalendarDays: 'No open trading days available in the trading calendar',
    tooManyRequests: 'Too many requests',
    accessDenied: 'Access denied',
    requestTimeout: 'Request timed out',
    networkDisconnected: 'Network connection interrupted',
    missingLlmApiKey: 'Missing a usable LLM API key',
    noDataReturned: 'No usable data was returned',
    marketPrefix: 'Market: ',
    newsPrefix: 'News: ',
    eventPrefix: 'Events: ',
    llmRankingSkipped: 'No smart re-ranking model configured; using deterministic factor ranking.',
    llmRankingFailedTemplate: 'Smart re-ranking incomplete: {detail}; results continue to use deterministic factor scoring.',
    dailyKlineEnrichmentErrors: 'Daily K-line data could not be completed for some candidates; results were generated with available data.',
    dailyKlineEnrichmentSkipped: 'Optional daily K-line enrichment was not completed; results were generated from snapshot data.',
    candidateContextErrors: 'Supplementary data for some candidates could not be completed.',
    industryConceptsIncomplete: 'Some industry or theme information could not be completed.',
    deepAnalysisIncomplete: 'Deep analysis was not completed for some candidates.',
    dataSourceDowngradedWithDetailTemplate: 'Data source downgraded: {source} ({detail})',
    dataSourceDowngradedTemplate: 'Data source downgraded: {detail}',
    screenTaskFailedGeneric: 'The screening task failed; please try again later.',
    screenTaskFailedTemplate: 'Screening task failed: {detail}',
    noHotspotCache: 'No cached hotspot topics',
    hotspotNoDataTemplate: 'Hotspot topics returned no data: {detail}',
    hotspotNoData: 'Hotspot topics returned no data',
    riskHigh: 'High',
    riskMedium: 'Medium',
    riskLow: 'Low',
    riskPending: 'Pending',
    timeUnconfirmed: 'Time unconfirmed',
    routeCurrentFermentation: 'Current buildup',
    routeNewsCatalyst: 'News catalyst',
    hotspotChangeFallback: 'Hotspot change',
    noMoreDescription: 'No further details.',
    heatSuffix: 'heat',
    stagePrefix: 'stage',
    coreStocksPrefix: 'core stocks',
    hotspotDataChanged: 'New changes in hotspot data.',
    someDetailsMissing: 'some details',
    hotspotDetailTimeoutTemplate: 'Hotspot detail request timed out ({seconds}s)',
    hotspotDetailTimeout: 'Hotspot detail request timed out',
    hotspotSourceDisconnected: 'Hotspot data source connection interrupted',
    hotspotSourceNoData: 'Hotspot data source returned no data',
    hotspotRequestTooFrequent: 'Too many hotspot data requests',
    hotspotDataUnavailable: 'Some hotspot data is unavailable',
    cacheFallbackTemplate: 'Cache fallback {hours}h',
    cacheFallback: 'Cache fallback',
    backupDataSource: 'Backup data source',
    hotspotSummaryTemplate: '{name}: {parts}.',
    hotspotDetailLoaded: 'Hotspot details loaded.',
    watching: 'Watching',
    activeStocksWatching: 'Active stocks pending',
    coverageStocksTemplate: 'Covers {count} stocks',
    noQuoteData: 'No quote data',
    pendingRefresh: 'Pending refresh',
    strengthLeading: 'Leading',
    strengthStrong: 'Strong',
    strengthModerate: 'Moderate',
    conceptStockFallback: 'Concept stock',
    strategyLoadFailed: 'Failed to load the strategy list',
    hotspotLoadFailedTemplate: 'Failed to load hotspot topics; please try again later.',
    messageSearchFailed: 'News search failed; please try again later.',
    hotspotDetailLoadFailed: 'Failed to load hotspot topic details; please try again later.',
    noRecentNewsFound: 'No recent valid news found for this topic.',
    restoringScreenTaskStatus: 'Restoring screening task status...',
    screenTaskCompletedNoResult: 'The screening task completed, but the server did not return candidate results.',
    screenTaskUnknownStatusTemplate: 'Screening task returned an unknown status: {status}',
    submittingScreenTask: 'Submitting the screening task...',
    screenTaskSubmitted: 'Screening task submitted',
    screenTaskSubmitFailed: 'Failed to submit the screening task; please try again later.',
    enableScreeningFailed: 'Failed to enable screening',
    title: 'Screening',
    screeningEnabled: 'Screening enabled',
    screeningDisabled: 'Screening not enabled',
    screeningNotEnabledTitle: 'Screening is not enabled',
    screeningNotEnabledMessage: 'Enable it to run screening strategies.',
    enabling: 'Enabling...',
    enableScreening: 'Enable screening',
    screeningUnavailableTitle: 'Screening is unavailable',
    screeningUnavailableMessage: 'Check backend logs, strategy files, and base data dependencies, then restart the service.',
    callFailed: 'Call failed',
    hotThemes: 'Hot themes',
    collapseHotThemes: 'Collapse hot themes',
    expandHotThemesTemplate: 'Expand hot themes ({count})',
    expandHotThemes: 'Expand hot themes',
    refreshing: 'Refreshing...',
    refreshHotThemes: 'Refresh hot themes',
    updatedAtTemplate: 'Updated at {time}',
    noHotspotDataHint: 'No hotspot data yet. Click "Refresh hot themes" to fetch it.',
    changePct: 'Change',
    trend: 'Trend',
    persistence: 'Persistence',
    leaders: 'Leaders',
    loadingHotspotRoute: 'Loading the buildup route and concept stocks...',
    clickHotspotHint: 'Click a theme to see its buildup route and concept stocks.',
    canonicalTopicTemplate: 'Canonical topic: {topic}',
    searchLatestNews: 'Search latest news',
    searching: 'Searching...',
    supplementingDetail: 'Supplementing details',
    qualityTemplate: 'Quality {label}',
    conceptStockCountTemplate: '{count} concept stocks',
    detailDegraded: 'Detail data degraded; expand to see why',
    missingFieldsTemplate: 'Missing: {fields}',
    fermentationTimeline: 'Buildup timeline',
    viewMessage: 'View message',
    conceptStocks: 'Concept stocks',
    analyzeStockAriaTemplate: 'Analyze {name}',
    analyze: 'Analyze',
    stockMetricsTemplate: 'Change {change} · Heat {heat}',
    runScreening: 'Run screening',
    market: 'Market',
    strategy: 'Strategy',
    customStrategyId: 'Custom strategy ID',
    inputStrategyId: 'Enter a strategy ID',
    resultCount: 'Result count',
    filtering: 'Filtering...',
    strategyDefaultDescription: 'The strategy first runs hard filters and factor scoring, then applies risk and portfolio constraints.',
    screeningRunning: 'Screening running',
    screeningCompleted: 'Screening completed',
    runningScreening: 'Running screening',
    runDetails: 'Run details',
    taskLabelTemplate: 'Task: {id}',
    runIdTemplate: 'Run ID: {id}',
    snapshotSummaryTemplate: 'Snapshot {snapshot} · After filter {afterFilter} · Candidates {candidates}',
    sortingLlm: 'Smart re-ranking',
    sortingFactor: 'Deterministic factors',
    sortingTemplate: 'Sorting: {mode}',
    coverageTemplate: ' · Coverage {coverage}',
    candidateVariancePoolTemplate: 'Candidate variance: recent pool {pool} ·',
    variantAppliedTemplate: '{slots} slots rotated this run',
    variantBaseline: 'Baseline kept this run',
    dsaEnrichmentTemplate: 'Deep enrichment: {enriched} / {requested}',
    usingFactorRanking: 'Using factor ranking',
    screeningHint: 'Screening notice',
    llmRankingIncomplete: 'Smart re-ranking incomplete; current candidates continue to use deterministic factor scoring.',
    screeningResults: 'Screening results',
    candidatesCountTemplate: '{count} candidates',
    noMatchingCandidates: 'No matching candidates',
    columnCode: 'Code',
    columnName: 'Name',
    columnIndustry: 'Industry',
    columnPrice: 'Price',
    columnChangePct: 'Change',
    columnScore: 'Score',
    columnRankingBasis: 'Ranking basis',
    columnRisk: 'Risk',
    columnDetails: 'Details',
    factorRankingLabel: 'Factor ranking',
    collapse: 'Collapse',
    expandView: 'Expand',
    summary: 'Summary',
    operationSignal: 'Signal',
    deepAnalysis: 'Run deep analysis',
    enrichedSummary: 'Enriched summary',
    llmInsight: 'AI insight',
    llmInsightMetaTemplate: 'Sector {sector} · Theme {theme} · Confidence {confidence}',
    riskTags: 'Risk tags',
    none: 'None',
    mainFactors: 'Main factors',
    noFactorDetail: 'No factor detail',
    turnover: 'Turnover',
    llmWatchItems: 'AI watch items',
    llmCatalysts: 'Catalysts',
    relatedNews: 'Related news',
    announcementsAndEvents: 'Announcements & events',
    dataSupplementHint: 'Data supplement hint',
  },
};

const formatTemplate = (template: string, params: Record<string, string | number>) =>
  Object.entries(params).reduce(
    (result, [key, value]) => result.replace(`{${key}}`, String(value)),
    template,
  );

const MARKETS: Array<{ id: string; label: Record<UiLanguage, string> }> = [
  { id: 'cn', label: { zh: 'A 股', en: 'A-shares' } },
];
const SCREEN_TASK_STORAGE_KEY = 'dsa.screening.activeScreenTask.v1';
const SCREEN_TASK_POLL_INTERVAL_MS = 2000;
const CUSTOM_STRATEGY_OPTION_VALUE = '__custom_strategy__';
const STRATEGY_CATEGORY_LABELS: Record<UiLanguage, Record<string, string>> = {
  zh: {
    framework: '综合',
    income: '收益',
    momentum: '动量',
    quality: '质量',
    reversal: '反转',
    trend: '趋势',
    value: '价值',
  },
  en: {
    framework: 'Framework',
    income: 'Income',
    momentum: 'Momentum',
    quality: 'Quality',
    reversal: 'Reversal',
    trend: 'Trend',
    value: 'Value',
  },
};

const formatStrategyCategory = (value: string | undefined, language: UiLanguage) => {
  const normalized = value?.trim();
  if (!normalized) {
    return SCREENING_TEXT[language].customStrategy;
  }
  return STRATEGY_CATEGORY_LABELS[language][normalized.toLowerCase()] || normalized;
};

type PersistedScreenTask = {
  taskId: string;
  market: string;
  strategy: string;
  maxResults: number;
};

const readPersistedScreenTask = (): PersistedScreenTask | null => {
  if (typeof window === 'undefined') {
    return null;
  }
  try {
    const raw = window.sessionStorage.getItem(SCREEN_TASK_STORAGE_KEY);
    if (!raw) {
      return null;
    }
    const parsed = JSON.parse(raw) as Partial<PersistedScreenTask>;
    if (typeof parsed.taskId !== 'string' || !parsed.taskId.trim()) {
      return null;
    }
    const restoredMaxResults = Number(parsed.maxResults);
    return {
      taskId: parsed.taskId,
      market: typeof parsed.market === 'string' && parsed.market.trim() ? parsed.market : 'cn',
      strategy: typeof parsed.strategy === 'string' && parsed.strategy.trim() ? parsed.strategy : 'dual_low',
      maxResults: Number.isFinite(restoredMaxResults) ? Math.min(100, Math.max(1, restoredMaxResults)) : 3,
    };
  } catch {
    return null;
  }
};

const persistScreenTask = (task: PersistedScreenTask) => {
  try {
    window.sessionStorage.setItem(SCREEN_TASK_STORAGE_KEY, JSON.stringify(task));
  } catch {
    // Session storage is best-effort; polling still works while the page stays mounted.
  }
};

const clearPersistedScreenTask = () => {
  try {
    window.sessionStorage.removeItem(SCREEN_TASK_STORAGE_KEY);
  } catch {
    // Ignore storage cleanup failures.
  }
};

const isUnrecoverableScreenTaskError = (error: ParsedApiError) =>
  error.title === 'Screening task cannot be recovered';

const formatRecoverableScreenTaskPollingError = (error: ParsedApiError, language: UiLanguage) => {
  if (error.category === 'upstream_timeout') {
    return SCREENING_TEXT[language].pollingTimeoutRetry;
  }
  if (error.category === 'upstream_network' || error.category === 'local_connection_failed') {
    return SCREENING_TEXT[language].pollingNetworkRetry;
  }
  return formatParsedApiError(error) || SCREENING_TEXT[language].pollingUnknownRetry;
};

const formatScore = (score: ScreeningCandidate['score']) => {
  if (score == null || Number.isNaN(Number(score))) {
    return '-';
  }
  return Number(score).toFixed(2);
};

const formatNumber = (value: unknown, digits = 2) => {
  if (value == null || value === '' || Number.isNaN(Number(value))) {
    return '-';
  }
  return Number(value).toFixed(digits);
};

const formatAmount = (value: unknown, language: UiLanguage) => {
  if (value == null || value === '' || Number.isNaN(Number(value))) {
    return '-';
  }
  const amount = Number(value);
  if (language === 'zh') {
    if (Math.abs(amount) >= 100_000_000) {
      return `${(amount / 100_000_000).toFixed(2)} 亿`;
    }
    if (Math.abs(amount) >= 10_000) {
      return `${(amount / 10_000).toFixed(2)} 万`;
    }
    return amount.toFixed(2);
  }
  if (Math.abs(amount) >= 1_000_000_000) {
    return `${(amount / 1_000_000_000).toFixed(2)}B`;
  }
  if (Math.abs(amount) >= 1_000_000) {
    return `${(amount / 1_000_000).toFixed(2)}M`;
  }
  if (Math.abs(amount) >= 1_000) {
    return `${(amount / 1_000).toFixed(2)}K`;
  }
  return amount.toFixed(2);
};

const formatPercent = (value: unknown) => {
  if (value == null || value === '' || Number.isNaN(Number(value))) {
    return '-';
  }
  return `${(Number(value) * 100).toFixed(0)}%`;
};

const FACTOR_LABELS: Record<UiLanguage, Record<string, string>> = {
  zh: {
    value: '估值',
    liquidity: '流动性',
    momentum: '动量',
    reversal: '反转',
    activity: '活跃度',
    stability: '稳定性',
    size: '规模',
    theme_heat: '题材热度',
    topic_alignment: '题材匹配',
  },
  en: {
    value: 'Value',
    liquidity: 'Liquidity',
    momentum: 'Momentum',
    reversal: 'Reversal',
    activity: 'Activity',
    stability: 'Stability',
    size: 'Size',
    theme_heat: 'Theme heat',
    topic_alignment: 'Topic alignment',
  },
};

const POST_TAG_LABELS: Record<UiLanguage, Record<string, string>> = {
  zh: {
    value_quality: '价值质量',
    controlled_reversal: '受控反转',
    momentum: '趋势动量',
    liquidity: '流动性',
  },
  en: {
    value_quality: 'Value quality',
    controlled_reversal: 'Controlled reversal',
    momentum: 'Trend momentum',
    liquidity: 'Liquidity',
  },
};

const HOTSPOT_QUALITY_LABELS: Record<UiLanguage, Record<string, string>> = {
  zh: {
    available: '可用',
    failed: '不可用',
    partial: '部分可用',
    stale: '缓存',
  },
  en: {
    available: 'Available',
    failed: 'Unavailable',
    partial: 'Partially available',
    stale: 'Cached',
  },
};

const HOTSPOT_STAGE_LABELS: Record<UiLanguage, Record<string, string>> = {
  zh: {
    accelerating: '加速主升',
    cooling: '降温退潮',
    diverging: '分歧放量',
    initial: '初次异动',
    persistent_hot: '确认扩散',
    warming: '确认扩散',
    weakening: '降温退潮',
  },
  en: {
    accelerating: 'Accelerating',
    cooling: 'Cooling off',
    diverging: 'Diverging volume',
    initial: 'Initial move',
    persistent_hot: 'Confirmed spread',
    warming: 'Confirmed spread',
    weakening: 'Cooling off',
  },
};

const HOTSPOT_ROLE_LABELS: Record<UiLanguage, Record<string, string>> = {
  zh: {
    core_leader: '核心龙头',
    follower: '助攻',
    laggard: '掉队',
    leader: '核心龙头',
    secondary: '补涨',
  },
  en: {
    core_leader: 'Core leader',
    follower: 'Follower',
    laggard: 'Laggard',
    leader: 'Core leader',
    secondary: 'Catch-up',
  },
};

const HOTSPOT_MISSING_FIELD_LABELS: Record<UiLanguage, Record<string, string>> = {
  zh: {
    canonical_topic: '标准题材',
    hotspot_constituents: '概念股列表',
    leader_stocks: '核心股',
    live_stocks: '实时概念股行情',
    route: '发酵路径',
    source: '数据来源',
    stocks: '概念股列表',
    timeline: '发酵时间线',
  },
  en: {
    canonical_topic: 'Canonical topic',
    hotspot_constituents: 'Concept stock list',
    leader_stocks: 'Core stocks',
    live_stocks: 'Live concept stock quotes',
    route: 'Buildup route',
    source: 'Data source',
    stocks: 'Concept stock list',
    timeline: 'Buildup timeline',
  },
};

const getHotspotStageLabel = (value: unknown, language: UiLanguage) => {
  const text = String(value || '').trim();
  if (!text) {
    return '';
  }
  return HOTSPOT_STAGE_LABELS[language][text.toLowerCase()] || text;
};

const getHotspotRoleLabel = (value: unknown, language: UiLanguage) => {
  const text = String(value || '').trim();
  if (!text) {
    return SCREENING_TEXT[language].conceptStockFallback;
  }
  return HOTSPOT_ROLE_LABELS[language][text.toLowerCase()] || text;
};

const getHotspotQualityLabel = (value: unknown, language: UiLanguage) => {
  const text = String(value || '').trim();
  return HOTSPOT_QUALITY_LABELS[language][text.toLowerCase()] || SCREENING_TEXT[language].riskPending;
};

const getLocalFactorReason = (item: ScreeningCandidate, language: UiLanguage) => {
  const factors = Object.entries(item.factorScores || {})
    .filter(([, value]) => typeof value === 'number')
    .sort((a, b) => Number(b[1]) - Number(a[1]))
    .slice(0, 3)
    .map(([key, value]) => `${FACTOR_LABELS[language][key] || key} ${Number(value).toFixed(0)}`);
  const tags = (item.postAnalysisTags || [])
    .slice(0, 2)
    .map((tag) => POST_TAG_LABELS[language][tag] || tag);
  if (factors.length > 0) {
    const separator = language === 'zh' ? '、' : ', ';
    return (
      formatTemplate(SCREENING_TEXT[language].mainAdvantagesTemplate, { factors: factors.join(separator) }) +
      (tags.length > 0 ? formatTemplate(SCREENING_TEXT[language].tagsTemplate, { tags: tags.join(separator) }) : '')
    );
  }
  return '';
};

const getCandidateReason = (item: ScreeningCandidate, language: UiLanguage) => {
  if (item.llmThesis || item.llmScore != null) {
    return item.reason || item.llmThesis || SCREENING_TEXT[language].llmRankedThesis;
  }
  const localReason = getLocalFactorReason(item, language);
  if (localReason) {
    return localReason;
  }
  if (item.reason) {
    return item.reason;
  }
  const summaries = item.postAnalysisSummaries || {};
  const summary = Object.values(summaries).find((value) => typeof value === 'string' && value.trim());
  if (typeof summary === 'string') {
    return summary;
  }
  return SCREENING_TEXT[language].noSummary;
};

const getSignal = (item: ScreeningCandidate, language: UiLanguage) => {
  const rawSignal = item.raw.action ?? item.raw.signal ?? item.raw.recommendation;
  return typeof rawSignal === 'string' && rawSignal.trim() ? rawSignal : SCREENING_TEXT[language].watchSignal;
};

const getFactorEntries = (item: ScreeningCandidate) =>
  Object.entries(item.factorScores || {})
    .filter(([, value]) => typeof value === 'number')
    .sort((a, b) => Number(b[1]) - Number(a[1]))
    .slice(0, 6);

const toMessageList = (values: string[] | undefined) =>
  Array.isArray(values) ? values.map((value) => String(value).trim()).filter(Boolean) : [];

const KNOWN_SNAPSHOT_SOURCES = new Set(['tushare', 'sina', 'efinance', 'akshare_em', 'em_datacenter', 'baostock']);
const MAX_MESSAGE_DETAIL_LENGTH = 96;

const truncateMessageDetail = (value: string, maxLength = MAX_MESSAGE_DETAIL_LENGTH) => {
  const text = value.replace(/\s+/g, ' ').trim();
  if (text.length <= maxLength) {
    return text;
  }
  return `${text.slice(0, maxLength - 1)}…`;
};

const summarizeScreeningDiagnostic = (detail: string, language: UiLanguage) => {
  if (/no_json_found|invalid_response|coverage below threshold/i.test(detail)) {
    return SCREENING_TEXT[language].modelNoStructuredResult;
  }
  if (/call_failed/i.test(detail)) {
    return SCREENING_TEXT[language].modelCallFailed;
  }
  if (/trade_cal returned no open trading days/i.test(detail)) {
    return SCREENING_TEXT[language].noTradingCalendarDays;
  }
  if (/too many requests|rate limit|http\s*429/i.test(detail)) {
    return SCREENING_TEXT[language].tooManyRequests;
  }
  if (/403 forbidden|forbidden|access denied/i.test(detail)) {
    return SCREENING_TEXT[language].accessDenied;
  }
  if (/timeout|timed out/i.test(detail)) {
    return SCREENING_TEXT[language].requestTimeout;
  }
  if (/RemoteDisconnected|Connection aborted|ProtocolError|ConnectionPool|Max retries exceeded|ProxyError|NameResolutionError/i.test(detail)) {
    return SCREENING_TEXT[language].networkDisconnected;
  }
  if (/missing .*api key|GEMINI_API_KEY|GOOGLE_API_KEY|gemini_api_key/i.test(detail)) {
    return SCREENING_TEXT[language].missingLlmApiKey;
  }
  if (/returned no data|empty/i.test(detail)) {
    return SCREENING_TEXT[language].noDataReturned;
  }

  const withoutUrl = detail
    .replace(/https?:\/\/\S+/gi, 'URL')
    .replace(/\bwith url:\s*\S+/gi, 'with url: URL')
    .replace(/\burl:\s*\S+/gi, 'url: URL');
  return truncateMessageDetail(withoutUrl);
};

const parseSourceDiagnostic = (value: string) => {
  const match = value.match(/^([a-zA-Z0-9_-]+)\s*[:：]\s*(.+)$/);
  if (!match) {
    return null;
  }
  return {
    source: match[1],
    detail: match[2],
  };
};

const normalizeScreenMessageKey = (value: string, language: UiLanguage) => {
  const formatted = formatScreenMessage(value, language);
  return formatted ? formatted.trim().toLowerCase() : value.trim().toLowerCase();
};

const formatEnrichmentSummary = (value: string, language: UiLanguage) =>
  value
    .replace(/DSA行情\s*[:：]\s*/gi, SCREENING_TEXT[language].marketPrefix)
    .replace(/DSA新闻\s*[:：]\s*/gi, SCREENING_TEXT[language].newsPrefix)
    .replace(/DSA事件\s*[:：]\s*/gi, SCREENING_TEXT[language].eventPrefix);

const formatScreenMessage = (value: string, language: UiLanguage) => {
  if (/^DSA provider context applied \d+ of \d+ candidates/i.test(value)) {
    return '';
  }
  if (/^LLM ranking skipped:\s*no LLM config/i.test(value)) {
    return SCREENING_TEXT[language].llmRankingSkipped;
  }
  if (/^LLM ranking failed/i.test(value)) {
    return formatTemplate(SCREENING_TEXT[language].llmRankingFailedTemplate, { detail: summarizeScreeningDiagnostic(value, language) });
  }
  if (/no_json_found|invalid_response|coverage below threshold|call_failed/i.test(value)) {
    return formatTemplate(SCREENING_TEXT[language].llmRankingFailedTemplate, { detail: summarizeScreeningDiagnostic(value, language) });
  }
  if (/^(?:LLM ranking prompt|LLM context) truncated:/i.test(value)) {
    return '';
  }
  if (/^(?:Remote post-analysis cap|Risk veto excluded|Snapshot hard-filter waterfall|Daily hard-filter waterfall|Daily hard-filter rejections|Candidate context collected rows=)/i.test(value)) {
    return '';
  }
  if (/^Daily K-line (?:enrichment attempted|sources|quality flags|source ordering|source health):?/i.test(value)) {
    return '';
  }
  if (/^Daily K-line enrichment row errors:/i.test(value)) {
    return SCREENING_TEXT[language].dailyKlineEnrichmentErrors;
  }
  if (/^Daily K-line enrichment skipped:/i.test(value)) {
    return SCREENING_TEXT[language].dailyKlineEnrichmentSkipped;
  }
  if (/^Candidate context row errors:/i.test(value)) {
    return SCREENING_TEXT[language].candidateContextErrors;
  }
  if (/^Industry\/concepts enrichment:/i.test(value)) {
    return SCREENING_TEXT[language].industryConceptsIncomplete;
  }
  if (/^DSA deep analysis failed for /i.test(value)) {
    return SCREENING_TEXT[language].deepAnalysisIncomplete;
  }

  const snapshotFallback = value.match(/^Snapshot source fallback:\s*(.+)$/i);
  if (snapshotFallback) {
    const parsed = parseSourceDiagnostic(snapshotFallback[1]);
    if (parsed) {
      return formatTemplate(SCREENING_TEXT[language].dataSourceDowngradedWithDetailTemplate, {
        source: parsed.source,
        detail: summarizeScreeningDiagnostic(parsed.detail, language),
      });
    }
    return formatTemplate(SCREENING_TEXT[language].dataSourceDowngradedTemplate, {
      detail: summarizeScreeningDiagnostic(snapshotFallback[1], language),
    });
  }

  const parsed = parseSourceDiagnostic(value);
  if (parsed && KNOWN_SNAPSHOT_SOURCES.has(parsed.source.toLowerCase())) {
    return formatTemplate(SCREENING_TEXT[language].dataSourceDowngradedWithDetailTemplate, {
      source: parsed.source,
      detail: summarizeScreeningDiagnostic(parsed.detail, language),
    });
  }
  return truncateMessageDetail(value);
};

const getScreenMessages = (meta: ScreeningScreenResponse | null, language: UiLanguage) => {
  if (!meta) {
    return [];
  }
  const messages: string[] = [];
  const seen = new Set<string>();
  [...toMessageList(meta.warnings), ...toMessageList(meta.sourceErrors), ...toMessageList(meta.llmParseErrors)].forEach(
    (value) => {
      const key = normalizeScreenMessageKey(value, language);
      if (seen.has(key)) {
        return;
      }
      const message = formatScreenMessage(value, language);
      if (!message) {
        return;
      }
      seen.add(key);
      messages.push(message);
    },
  );
  return messages;
};

const isRunningScreenTask = (status: string | undefined | null) => status === 'pending' || status === 'processing';

const formatScreenTaskFailure = (value: string | null | undefined, language: UiLanguage) => {
  const text = String(value || '').trim();
  if (!text) {
    return SCREENING_TEXT[language].screenTaskFailedGeneric;
  }
  return formatTemplate(SCREENING_TEXT[language].screenTaskFailedTemplate, { detail: summarizeScreeningDiagnostic(text, language) });
};

const SCREENING_HOTSPOT_NO_CACHE_HINT = 'No cached Screening hotspot snapshot. Click refresh to fetch live hotspots.';
const SCREENING_HOTSPOT_UNAVAILABLE_CODE = 'eastmoney_hotspot_unavailable';

const formatHotspotEmptyMessage = (result: ScreeningHotspotsResponse, language: UiLanguage) => {
  const message = String(result.message || '').trim();
  const sourceErrors = result.sourceErrors || [];
  if (message && sourceErrors.includes(SCREENING_HOTSPOT_UNAVAILABLE_CODE)) {
    return message;
  }
  if (message === SCREENING_HOTSPOT_NO_CACHE_HINT) {
    return SCREENING_TEXT[language].noHotspotCache;
  }
  const sourceError = sourceErrors[0];
  if (sourceError) {
    return formatTemplate(SCREENING_TEXT[language].hotspotNoDataTemplate, { detail: summarizeScreeningDiagnostic(sourceError, language) });
  }
  return SCREENING_TEXT[language].hotspotNoData;
};

const ScreenAlertMessage: React.FC<{ messages: string[] }> = ({ messages }) => {
  if (messages.length <= 1) {
    return <span>{messages[0]}</span>;
  }
  return (
    <ul className="list-disc space-y-1 pl-4">
      {messages.map((message) => (
        <li key={message}>{message}</li>
      ))}
    </ul>
  );
};

const hasLlmInsight = (item: ScreeningCandidate) =>
  Boolean(
    item.llmThesis ||
      item.llmSector ||
      item.llmTheme ||
      item.llmConfidence != null ||
      item.llmWatchItems?.length ||
      item.llmCatalysts?.length,
  );

const getRiskClassName = (riskLevel: string | undefined) => {
  if (riskLevel === 'high') {
    return 'bg-danger/10 text-danger';
  }
  if (riskLevel === 'medium') {
    return 'bg-warning/10 text-warning';
  }
  if (riskLevel === 'low') {
    return 'bg-success/10 text-success';
  }
  return 'bg-surface text-secondary-text';
};

const getRiskLabel = (riskLevel: string | undefined, language: UiLanguage) => {
  if (riskLevel === 'high') return SCREENING_TEXT[language].riskHigh;
  if (riskLevel === 'medium') return SCREENING_TEXT[language].riskMedium;
  if (riskLevel === 'low') return SCREENING_TEXT[language].riskLow;
  return SCREENING_TEXT[language].riskPending;
};

const getRouteTimeLabel = (item: ScreeningHotspotDetail['route'][number], language: UiLanguage) => {
  const rawTime = item.publishedAt || item.date || item.time || '';
  if (!rawTime) {
    return SCREENING_TEXT[language].timeUnconfirmed;
  }
  if (/^\d{4}-\d{2}-\d{2}$/.test(rawTime)) {
    return rawTime;
  }
  const parsed = new Date(rawTime);
  if (!Number.isNaN(parsed.getTime())) {
    return parsed.toLocaleString(language === 'zh' ? 'zh-CN' : 'en-US', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    });
  }
  return rawTime;
};

const formatHotspotRouteTitle = (value: string, language: UiLanguage) => {
  const text = String(value || '').trim();
  const normalized = text.toLowerCase();
  if (normalized === 'current fermentation') {
    return SCREENING_TEXT[language].routeCurrentFermentation;
  }
  if (normalized === 'news catalyst') {
    return SCREENING_TEXT[language].routeNewsCatalyst;
  }
  return text || SCREENING_TEXT[language].hotspotChangeFallback;
};

const formatHotspotRouteDescription = (value: string, language: UiLanguage) => {
  const text = String(value || '').trim();
  if (!text) {
    return SCREENING_TEXT[language].noMoreDescription;
  }
  const parts = text.split(/\s*;\s*/).map((part) => part.trim()).filter(Boolean);
  const joinChar = language === 'zh' ? '、' : ', ';
  const sentenceJoinChar = language === 'zh' ? '，' : ', ';
  if (parts.some((part) => /\b(?:heat|stage|leaders?)\b/i.test(part))) {
    const localized = parts.map((part) => {
      const heat = part.match(/^(.*?)\s+heat\s+(-?\d+(?:\.\d+)?)$/i);
      if (heat) {
        return `${heat[1]}${SCREENING_TEXT[language].heatSuffix} ${formatNumber(heat[2], 1)}`;
      }
      const stage = part.match(/^stage\s+(.+)$/i);
      if (stage) {
        return `${SCREENING_TEXT[language].stagePrefix} ${getHotspotStageLabel(stage[1], language)}`;
      }
      const leaders = part.match(/^leaders?\s+(.+)$/i);
      if (leaders) {
        return `${SCREENING_TEXT[language].coreStocksPrefix} ${leaders[1].split(/\s*,\s*/).filter(Boolean).join(joinChar)}`;
      }
      return part;
    });
    return `${localized.join(sentenceJoinChar)}。`;
  }
  if (/Dsa[A-Z]|Provider\b|stock_board_|concept_constituents|leader_stocks|last_good_cache/i.test(text)) {
    return SCREENING_TEXT[language].hotspotDataChanged;
  }
  return text;
};

const getHotspotMissingFieldLabels = (values: string[] | undefined, language: UiLanguage) => {
  const labels = (values || []).map((value) => HOTSPOT_MISSING_FIELD_LABELS[language][String(value).trim().toLowerCase()] || SCREENING_TEXT[language].someDetailsMissing);
  return [...new Set(labels)];
};

const formatHotspotDiagnostic = (value: string, language: UiLanguage) => {
  const text = String(value || '').trim();
  const timeoutSeconds = text.match(/timed out after\s*(\d+(?:\.\d+)?)s/i);
  if (timeoutSeconds) {
    return formatTemplate(SCREENING_TEXT[language].hotspotDetailTimeoutTemplate, { seconds: timeoutSeconds[1] });
  }
  if (/timeout|timed out/i.test(text)) {
    return SCREENING_TEXT[language].hotspotDetailTimeout;
  }
  if (/RemoteDisconnected|Connection aborted|ProtocolError|ConnectionPool|Max retries exceeded|ProxyError|NameResolutionError/i.test(text)) {
    return SCREENING_TEXT[language].hotspotSourceDisconnected;
  }
  if (/eastmoney_hotspot_unavailable|returned no data|no live hotspot rows|\bempty\b/i.test(text)) {
    return SCREENING_TEXT[language].hotspotSourceNoData;
  }
  if (/rate limit|too many requests|http\s*429/i.test(text)) {
    return SCREENING_TEXT[language].hotspotRequestTooFrequent;
  }
  if (/Dsa[A-Z]|Provider\b|stock_board_|concept_constituents|leader_stocks|last_good_cache|^[a-z0-9_.:-]+$/i.test(text)) {
    return SCREENING_TEXT[language].hotspotDataUnavailable;
  }
  if (/[\u0080-\uFFFF]/.test(text)) {
    return truncateMessageDetail(text);
  }
  return SCREENING_TEXT[language].hotspotDataUnavailable;
};

const getHotspotDiagnosticMessages = (values: string[] | undefined, language: UiLanguage) =>
  [...new Set((values || []).map((value) => formatHotspotDiagnostic(value, language)).filter(Boolean))].slice(0, 4);

const hasHotspotDetailDegradation = (detail: ScreeningHotspotDetail) => {
  if ((detail.missingFields || []).length > 0) {
    return true;
  }
  const qualityStatus = String(detail.qualityStatus || '').trim().toLowerCase();
  if (qualityStatus) {
    return qualityStatus !== 'available';
  }
  return (detail.sourceErrors || []).length > 0;
};

const getHotspotFallbackLabel = (detail: ScreeningHotspotDetail, language: UiLanguage) => {
  if (detail.stale || detail.cacheUsed) {
    return detail.staleAgeHours != null
      ? formatTemplate(SCREENING_TEXT[language].cacheFallbackTemplate, { hours: formatNumber(detail.staleAgeHours, 1) })
      : SCREENING_TEXT[language].cacheFallback;
  }
  return SCREENING_TEXT[language].backupDataSource;
};

const getHotspotSummaryText = (detail: ScreeningHotspotDetail, hotspot: ScreeningHotspot | undefined, language: UiLanguage) => {
  const summaryDetail = detail.summaryDetail || {};
  const heatScore = summaryDetail.heatScore ?? summaryDetail.heat_score ?? hotspot?.heatScore;
  const stage = summaryDetail.stage ?? hotspot?.stage ?? hotspot?.state;
  const rawLeaders = summaryDetail.leaders ?? hotspot?.leaders;
  const leaders = Array.isArray(rawLeaders)
    ? rawLeaders.map((value) => String(value).trim()).filter(Boolean).slice(0, 3)
    : [];
  const joinChar = language === 'zh' ? '、' : ', ';
  const sentenceJoinChar = language === 'zh' ? '，' : ', ';
  const parts: string[] = [];
  if (heatScore != null && !Number.isNaN(Number(heatScore))) {
    parts.push(`${SCREENING_TEXT[language].heatSuffix} ${formatNumber(heatScore, 1)}`);
  }
  if (stage) {
    parts.push(`${SCREENING_TEXT[language].stagePrefix} ${getHotspotStageLabel(stage, language)}`);
  }
  if (leaders.length > 0) {
    parts.push(`${SCREENING_TEXT[language].coreStocksPrefix} ${leaders.join(joinChar)}`);
  }
  if (parts.length > 0) {
    return formatTemplate(SCREENING_TEXT[language].hotspotSummaryTemplate, {
      name: detail.name || detail.canonicalTopic || detail.topic,
      parts: parts.join(sentenceJoinChar),
    });
  }
  const summary = String(detail.summary || '').trim();
  if (summary && !/\b(?:heat|stage|leaders?|quality status|available|partial|stale|failed)\b|Dsa[A-Z]|Provider\b|stock_board_/i.test(summary)) {
    return summary;
  }
  return SCREENING_TEXT[language].hotspotDetailLoaded;
};

const buildHotspotPreviewDetail = (hotspot: ScreeningHotspot, language: UiLanguage): ScreeningHotspotDetail => {
  const leaders = (hotspot.leaders || []).map((value) => String(value).trim()).filter(Boolean);
  const stage = getHotspotStageLabel(hotspot.stage || hotspot.state, language);
  const joinChar = language === 'zh' ? '、' : ', ';
  const sentenceJoinChar = language === 'zh' ? '，' : ', ';
  const descriptionParts = [`${hotspot.name || hotspot.topic}${SCREENING_TEXT[language].heatSuffix} ${formatHotspotMetric(hotspot.heatScore, 1, language)}`];
  if (stage) {
    descriptionParts.push(`${SCREENING_TEXT[language].stagePrefix} ${stage}`);
  }
  if (leaders.length > 0) {
    descriptionParts.push(`${SCREENING_TEXT[language].coreStocksPrefix} ${leaders.slice(0, 3).join(joinChar)}`);
  }
  const stocks = (hotspot.leaderStocks || []).slice(0, 10);
  return {
    enabled: true,
    provider: 'akshare',
    topic: hotspot.topic,
    name: hotspot.name || hotspot.topic,
    canonicalTopic: hotspot.topic,
    summaryDetail: {
      heatScore: hotspot.heatScore,
      stage: hotspot.stage || hotspot.state,
      leaders,
    },
    route: [{ title: SCREENING_TEXT[language].routeCurrentFermentation, description: `${descriptionParts.join(sentenceJoinChar)}。` }],
    stocks,
    stockCount: hotspot.sampleStockCount ?? stocks.length,
    sourceErrors: hotspot.sourceErrors,
    qualityStatus: hotspot.qualityStatus,
    missingFields: hotspot.missingFields,
    fallbackUsed: hotspot.fallbackUsed,
    stale: hotspot.stale,
    staleAgeHours: hotspot.staleAgeHours,
    cacheUsed: hotspot.cacheUsed,
    cachedAt: hotspot.cachedAt,
  };
};

const stripHotspotSearchAugmentation = (detail: ScreeningHotspotDetail): ScreeningHotspotDetail => {
  const baseDetail: ScreeningHotspotDetail = {
    ...detail,
    route: (detail.route || []).filter((item) => !item.searchResult),
    ...(detail.timeline
      ? { timeline: detail.timeline.filter((item) => !item.searchResult) }
      : {}),
  };
  delete baseDetail.newsSearchRequested;
  delete baseDetail.newsSearchStatus;
  return baseDetail;
};

const stripHotspotSearchAugmentationByTopic = (
  details: Record<string, ScreeningHotspotDetail>,
) => Object.fromEntries(
  Object.entries(details).map(([topic, detail]) => [topic, stripHotspotSearchAugmentation(detail)]),
) as Record<string, ScreeningHotspotDetail>;

const getHotspotRouteItems = (detail: ScreeningHotspotDetail) => {
  const route = detail.route || [];
  if (route.length > 0) {
    return route;
  }
  return detail.timeline || [];
};

const formatHotspotMetric = (value: unknown, digits: number, language: UiLanguage) => {
  const formatted = formatNumber(value, digits);
  return formatted === '-' ? SCREENING_TEXT[language].watching : formatted;
};

const getHotspotLeadersText = (item: ScreeningHotspot, language: UiLanguage) => {
  const leaders = (item.leaders || []).map((value) => String(value).trim()).filter(Boolean);
  if (leaders.length > 0) {
    return leaders.slice(0, 2).join(language === 'zh' ? '、' : ', ');
  }
  return SCREENING_TEXT[language].watching;
};

const getHotspotSampleText = (item: ScreeningHotspot, language: UiLanguage) => {
  if (item.sampleStockCount == null || Number.isNaN(Number(item.sampleStockCount))) {
    return SCREENING_TEXT[language].activeStocksWatching;
  }
  return formatTemplate(SCREENING_TEXT[language].coverageStocksTemplate, { count: item.sampleStockCount });
};

const formatStockChangeText = (value: unknown, language: UiLanguage) => {
  const formatted = formatNumber(value);
  return formatted === '-' ? SCREENING_TEXT[language].noQuoteData : `${formatted}%`;
};

const formatHotspotUpdatedAt = (value: string | null, language: UiLanguage) => {
  if (!value) {
    return SCREENING_TEXT[language].pendingRefresh;
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleString(language === 'zh' ? 'zh-CN' : 'en-US', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
};

const getHotspotStrength = (item: ScreeningHotspot, index: number, language: UiLanguage) => {
  const heat = Number(item.heatScore ?? 0);
  const changePct = Number(item.changePct ?? 0);
  if (index === 0 || heat >= 90 || changePct >= 8) {
    return { label: SCREENING_TEXT[language].strengthLeading, className: 'bg-red-500/10 text-red-500' };
  }
  if (heat >= 80 || changePct >= 5) {
    return { label: SCREENING_TEXT[language].strengthStrong, className: 'bg-blue-500/10 text-blue-500' };
  }
  return { label: SCREENING_TEXT[language].strengthModerate, className: 'bg-cyan/10 text-cyan' };
};

const HOTSPOT_ICON_RULES: Array<{
  pattern: RegExp;
  icon: React.ComponentType<{ className?: string }>;
  className: string;
}> = [
  { pattern: /金|银|铜|铝|铅|锌|钼|钴|镍|贵金属|矿|有色/, icon: Pickaxe, className: 'bg-orange-500/10 text-orange-500' },
  { pattern: /黄金|珠宝/, icon: Gem, className: 'bg-amber-500/10 text-amber-500' },
  { pattern: /油|气|能源|煤/, icon: Droplet, className: 'bg-yellow-700/10 text-yellow-700' },
  { pattern: /金融|券商|银行|保险|资本/, icon: Landmark, className: 'bg-orange-500/10 text-orange-500' },
  { pattern: /航空|机场|航天|运输/, icon: Plane, className: 'bg-blue-500/10 text-blue-500' },
  { pattern: /林业|农业|种植/, icon: Trees, className: 'bg-emerald-500/10 text-emerald-500' },
  { pattern: /医疗|诊断|卫生|医药/, icon: Stethoscope, className: 'bg-teal-500/10 text-teal-500' },
  { pattern: /食品|餐饮|酒/, icon: Utensils, className: 'bg-violet-500/10 text-violet-500' },
  { pattern: /工业|制造|修理|机械|设备/, icon: Wrench, className: 'bg-blue-500/10 text-blue-500' },
  { pattern: /租赁|地产|建筑/, icon: Building2, className: 'bg-emerald-500/10 text-emerald-500' },
  { pattern: /电|芯片|算力|AI|机器人/, icon: Factory, className: 'bg-indigo-500/10 text-indigo-500' },
  { pattern: /保险|安全/, icon: Shield, className: 'bg-blue-500/10 text-blue-500' },
];

const getHotspotIcon = (topic: string) => {
  const match = HOTSPOT_ICON_RULES.find((rule) => rule.pattern.test(topic));
  return match || { icon: Activity, className: 'bg-cyan/10 text-cyan' };
};

const MiniSparkline: React.FC<{ score?: number | null; selected?: boolean }> = ({ score, selected }) => {
  const normalizedScore = Number.isFinite(Number(score)) ? Math.max(0, Math.min(100, Number(score))) : 65;
  const lift = Math.max(0, Math.min(16, normalizedScore / 7));
  const path = `M2 35 C12 ${32 - lift / 4}, 16 ${34 - lift / 2}, 24 ${28 - lift / 3} S38 ${29 - lift}, 46 ${23 - lift / 2} S62 ${24 - lift}, 72 ${16 - lift / 3} S86 ${15 - lift}, 94 ${7}`;
  return (
    <svg className="h-8 w-20" viewBox="0 0 96 40" aria-hidden="true">
      <path d={`${path} L94 40 L2 40 Z`} fill={selected ? 'rgba(249,115,22,0.14)' : 'rgba(59,130,246,0.12)'} />
      <path d={path} fill="none" stroke={selected ? '#f97316' : '#3b82f6'} strokeLinecap="round" strokeWidth="2" />
    </svg>
  );
};

const StockScreeningPage: React.FC = () => {
  const navigate = useNavigate();
  const { language } = useUiLanguage();
  const [restoredTask] = useState<PersistedScreenTask | null>(() => readPersistedScreenTask());
  const [enabled, setEnabled] = useState(false);
  const [available, setAvailable] = useState(false);
  const [market, setMarket] = useState(restoredTask?.market || 'cn');
  const [strategy, setStrategy] = useState(restoredTask?.strategy || 'dual_low');
  const [strategies, setStrategies] = useState<ScreeningStrategy[]>([]);
  const [maxResults, setMaxResults] = useState(restoredTask?.maxResults || 3);
  const [candidates, setCandidates] = useState<ScreeningCandidate[]>([]);
  const [hotspots, setHotspots] = useState<ScreeningHotspot[]>([]);
  const [hotspotsUpdatedAt, setHotspotsUpdatedAt] = useState<string | null>(null);
  const [hotspotsExpanded, setHotspotsExpanded] = useState(false);
  const [selectedHotspotTopic, setSelectedHotspotTopic] = useState<string | null>(null);
  const selectedHotspotTopicRef = useRef<string | null>(null);
  const hotspotDetailRequestIdRef = useRef(0);
  const hotspotDetailsByTopicRef = useRef<Record<string, ScreeningHotspotDetail>>({});
  const [hotspotDetail, setHotspotDetail] = useState<ScreeningHotspotDetail | null>(null);
  const [loadingHotspotDetail, setLoadingHotspotDetail] = useState(false);
  const [searchingHotspotNews, setSearchingHotspotNews] = useState(false);
  const [hotspotDetailError, setHotspotDetailError] = useState('');
  const [loadingHotspots, setLoadingHotspots] = useState(false);
  const [hotspotError, setHotspotError] = useState('');
  const [screenMeta, setScreenMeta] = useState<ScreeningScreenResponse | null>(null);
  const [expandedCode, setExpandedCode] = useState<string | null>(null);
  const [loading, setLoading] = useState(Boolean(restoredTask?.taskId));
  const [enabling, setEnabling] = useState(false);
  const [loadingStrategies, setLoadingStrategies] = useState(false);
  const [error, setError] = useState('');
  const [strategyLoadError, setStrategyLoadError] = useState('');
  const [activeTaskId, setActiveTaskId] = useState<string | null>(restoredTask?.taskId ?? null);
  const [taskProgress, setTaskProgress] = useState(restoredTask?.taskId ? 10 : 0);
  const [taskMessage, setTaskMessage] = useState(restoredTask?.taskId ? SCREENING_TEXT[language].restoringScreenTaskStatus : '');

  const selectedStrategy = useMemo(() => strategies.find((item) => item.id === strategy), [strategies, strategy]);
  const selectedStrategyTitle = selectedStrategy?.name || selectedStrategy?.title || SCREENING_TEXT[language].customStrategy;
  const selectedStrategyTag = formatStrategyCategory(
    selectedStrategy?.category || selectedStrategy?.tag || selectedStrategy?.tags?.[0],
    language,
  );
  const displayedStrategy = selectedStrategy ? selectedStrategyTitle : formatTemplate(SCREENING_TEXT[language].customStrategyTemplate, { strategy });
  const screenMessages = useMemo(() => getScreenMessages(screenMeta, language), [screenMeta, language]);
  const selectedHotspot = useMemo(
    () => hotspots.find((item) => item.topic === selectedHotspotTopic),
    [hotspots, selectedHotspotTopic],
  );
  const factorRanking = Boolean(screenMeta && (screenMeta.rankingMode === 'factor' || screenMeta.llmRanked === false));
  const llmFailed = Boolean(factorRanking && screenMeta?.llmFailureReason);
  const alertMessages = llmFailed
    ? screenMessages.length > 0
      ? screenMessages
      : [SCREENING_TEXT[language].llmRankingIncomplete]
    : screenMessages;
  const isScreeningEnabled = enabled && available;
  const statusText = isScreeningEnabled ? SCREENING_TEXT[language].screeningEnabled : SCREENING_TEXT[language].screeningDisabled;

  const applyScreenResult = useCallback((result: ScreeningScreenResponse) => {
    const nextCandidates = result.candidates || [];
    setScreenMeta(result);
    setCandidates(nextCandidates);
    setExpandedCode(nextCandidates[0]?.code ?? null);
  }, []);

  const clearScreeningResults = () => {
    setCandidates([]);
    setScreenMeta(null);
    setExpandedCode(null);
  };

  const loadHotspotDetail = useCallback(async (
    topic: string,
    options: { refresh?: boolean; includeSearch?: boolean } = {},
  ) => {
    if (!topic) {
      return;
    }
    const cachedDetail = !options.refresh && !options.includeSearch
      ? hotspotDetailsByTopicRef.current[topic]
      : null;
    if (cachedDetail) {
      setHotspotDetail(cachedDetail);
      setHotspotDetailError('');
      setLoadingHotspotDetail(false);
      return;
    }
    const requestId = hotspotDetailRequestIdRef.current + 1;
    hotspotDetailRequestIdRef.current = requestId;
    const isCurrentRequest = () => hotspotDetailRequestIdRef.current === requestId;
    const canApplyRequest = () => isCurrentRequest() && selectedHotspotTopicRef.current === topic;
    setLoadingHotspotDetail(!options.includeSearch);
    setSearchingHotspotNews(Boolean(options.includeSearch));
    setHotspotDetail((currentDetail) => (currentDetail?.topic === topic ? currentDetail : null));
    setHotspotDetailError('');
    try {
      const detail = await screeningApi.getHotspotDetail({
        topic,
        provider: 'akshare',
        refresh: options.refresh ?? false,
        ...(options.includeSearch ? { includeSearch: true } : {}),
      });
      if (!canApplyRequest()) {
        return;
      }
      const cacheableDetail = options.includeSearch
        ? hotspotDetailsByTopicRef.current[topic] || stripHotspotSearchAugmentation(detail)
        : stripHotspotSearchAugmentation(detail);
      hotspotDetailsByTopicRef.current = {
        ...hotspotDetailsByTopicRef.current,
        [topic]: cacheableDetail,
      };
      setHotspotDetail(options.includeSearch ? detail : cacheableDetail);
      if (options.includeSearch && detail.newsSearchStatus === 'no_results') {
        setHotspotDetailError(SCREENING_TEXT[language].noRecentNewsFound);
      } else if (options.includeSearch && detail.newsSearchStatus !== 'available') {
        setHotspotDetailError(SCREENING_TEXT[language].messageSearchFailed);
      }
    } catch (err) {
      if (!canApplyRequest()) {
        return;
      }
      if (!options.includeSearch) {
        setHotspotDetail(null);
      }
      setHotspotDetailError(toApiErrorMessage(
        err,
        options.includeSearch ? SCREENING_TEXT[language].messageSearchFailed : SCREENING_TEXT[language].hotspotDetailLoadFailed,
      ));
    } finally {
      if (isCurrentRequest()) {
        setLoadingHotspotDetail(false);
        setSearchingHotspotNews(false);
      }
    }
  }, [language]);

  const loadStrategies = useCallback(async () => {
    setLoadingStrategies(true);
    try {
      setStrategyLoadError('');
      const result = await screeningApi.getStrategies();
      const loadedStrategies = result.strategies || [];
      setStrategies(loadedStrategies);
      if (loadedStrategies.length > 0) {
        setStrategy((currentStrategy) =>
          loadedStrategies.some((item) => item.id === currentStrategy) ? currentStrategy : loadedStrategies[0].id,
        );
      }
    } catch (err) {
      setStrategies([]);
      setStrategyLoadError(err instanceof Error ? err.message : SCREENING_TEXT[language].strategyLoadFailed);
    } finally {
      setLoadingStrategies(false);
    }
  }, [language]);

  const loadHotspots = useCallback(async (refresh = false) => {
    setLoadingHotspots(true);
    setHotspotError('');
    try {
      const result = await screeningApi.getHotspots({ provider: 'akshare', top: 12, refresh });
      const nextHotspots = result.hotspots || [];
      const nextDetails = stripHotspotSearchAugmentationByTopic(result.details || {});
      hotspotDetailsByTopicRef.current = {
        ...hotspotDetailsByTopicRef.current,
        ...nextDetails,
      };
      const currentTopic = selectedHotspotTopicRef.current;
      const retainedTopic = Boolean(currentTopic && nextHotspots.some((item) => item.topic === currentTopic));
      const nextTopic = retainedTopic ? currentTopic : null;
      setHotspots(nextHotspots);
      setHotspotsUpdatedAt(result.cachedAt || (nextHotspots.length > 0 ? new Date().toISOString() : null));
      setSelectedHotspotTopic(nextTopic);
      selectedHotspotTopicRef.current = nextTopic;
      setHotspotDetailError('');
      if (nextTopic && nextDetails[nextTopic]) {
        setHotspotDetail(nextDetails[nextTopic]);
        setLoadingHotspotDetail(false);
      } else if (!retainedTopic) {
        setHotspotDetail(null);
      } else if (refresh && nextTopic) {
        // A refreshed list and a retained detail must describe the same source
        // snapshot. The list endpoint intentionally omits details by default,
        // so explicitly bypass the detail cache for the retained topic.
        await loadHotspotDetail(nextTopic, { refresh: true });
      }
      if (nextHotspots.length === 0) {
        setHotspotError(formatHotspotEmptyMessage(result, language));
      }
    } catch (err) {
      setHotspotError(toApiErrorMessage(err, SCREENING_TEXT[language].hotspotLoadFailedTemplate));
    } finally {
      setLoadingHotspots(false);
    }
  }, [loadHotspotDetail, language]);

  const handleHotspotSelect = useCallback((topic: string) => {
    selectedHotspotTopicRef.current = topic;
    setSelectedHotspotTopic(topic);
    const cachedDetail = hotspotDetailsByTopicRef.current[topic];
    if (cachedDetail) {
      setHotspotDetail(cachedDetail);
      setHotspotDetailError('');
      setLoadingHotspotDetail(false);
    } else {
      const preview = hotspots.find((item) => item.topic === topic);
      setHotspotDetail((currentDetail) => (
        currentDetail?.topic === topic ? currentDetail : preview ? buildHotspotPreviewDetail(preview, language) : null
      ));
    }
  }, [hotspots, language]);

  const toggleHotspotsExpanded = useCallback(() => {
    setHotspotsExpanded((expanded) => {
      const nextExpanded = !expanded;
      if (!nextExpanded) {
        selectedHotspotTopicRef.current = null;
        setSelectedHotspotTopic(null);
        setHotspotDetail(null);
        setHotspotDetailError('');
      }
      return nextExpanded;
    });
  }, []);

  const handleAnalyzeHotspotStock = useCallback((stock: ScreeningHotspotDetail['stocks'][number]) => {
    const stockCode = String(stock.code || '').trim();
    if (!stockCode) {
      return;
    }
    const stockName = String(stock.name || stockCode).trim();
    navigate('/', {
      state: {
        stockCode,
        stockName,
        autoAnalyze: true,
        selectionSource: 'screening_hotspot',
        skills: ['hot_theme'],
      },
    });
  }, [navigate]);

  const handleAnalyzeCandidate = useCallback((candidate: ScreeningCandidate) => {
    const stockCode = String(candidate.code || '').trim();
    if (!stockCode) {
      return;
    }
    const stockName = String(candidate.name || stockCode).trim();
    const analysisSkills = (selectedStrategy?.analysisSkills || []).filter(Boolean);
    navigate('/', {
      state: {
        stockCode,
        stockName,
        autoAnalyze: true,
        selectionSource: 'screening_result',
        ...(analysisSkills.length > 0 ? { skills: analysisSkills } : {}),
      },
    });
  }, [navigate, selectedStrategy]);

  useEffect(() => {
    selectedHotspotTopicRef.current = selectedHotspotTopic;
  }, [selectedHotspotTopic]);

  useEffect(() => {
    if (!selectedHotspotTopic) {
      return;
    }
    void loadHotspotDetail(selectedHotspotTopic);
  }, [loadHotspotDetail, selectedHotspotTopic]);

  useEffect(() => {
    let active = true;
    screeningApi
      .getStatus()
      .then((status) => {
        if (!active) {
          return;
        }
        setEnabled(status.enabled);
        setAvailable(status.available);
        if (status.enabled && status.available) {
          void loadStrategies();
          void loadHotspots(false);
        }
      })
      .catch(() => {
        if (active) {
          setEnabled(false);
          setAvailable(false);
        }
      });
    return () => {
      active = false;
    };
  }, [loadHotspots, loadStrategies]);

  useEffect(() => {
    if (!activeTaskId) {
      return undefined;
    }

    const pollingTaskId = activeTaskId;
    let active = true;
    let timer: ReturnType<typeof window.setTimeout> | undefined;

    function finishTask() {
      clearPersistedScreenTask();
      setActiveTaskId(null);
      setLoading(false);
    }

    function applyTaskStatus(task: ScreeningScreenTaskStatus) {
      const nextProgress = Number(task.progress ?? 0);
      setTaskProgress(Number.isFinite(nextProgress) ? nextProgress : 0);
      setTaskMessage(task.message || '');

      if (task.status === 'completed') {
        if (task.result) {
          applyScreenResult(task.result);
          setError('');
        } else {
          setError(SCREENING_TEXT[language].screenTaskCompletedNoResult);
          setCandidates([]);
          setScreenMeta(null);
        }
        finishTask();
        return;
      }

      if (task.status === 'failed') {
        setCandidates([]);
        setScreenMeta(null);
        setExpandedCode(null);
        setError(formatScreenTaskFailure(task.error || task.message, language));
        finishTask();
        return;
      }

      if (isRunningScreenTask(task.status)) {
        setLoading(true);
        timer = window.setTimeout(pollTask, SCREEN_TASK_POLL_INTERVAL_MS);
        return;
      }

      setError(formatTemplate(SCREENING_TEXT[language].screenTaskUnknownStatusTemplate, { status: task.status || 'unknown' }));
      finishTask();
    }

    async function pollTask() {
      try {
        const task = await screeningApi.getScreenTask(pollingTaskId);
        if (!active) {
          return;
        }
        applyTaskStatus(task);
      } catch (err) {
        if (!active) {
          return;
        }
        const parsedError = getParsedApiError(err);
        if (isUnrecoverableScreenTaskError(parsedError)) {
          setError(formatParsedApiError(parsedError) || SCREENING_TEXT[language].unrecoverableTaskFallback);
          setCandidates([]);
          setScreenMeta(null);
          finishTask();
          return;
        }
        setError(formatRecoverableScreenTaskPollingError(parsedError, language));
        setLoading(true);
        timer = window.setTimeout(pollTask, SCREEN_TASK_POLL_INTERVAL_MS);
      }
    }

    void pollTask();

    return () => {
      active = false;
      if (timer) {
        window.clearTimeout(timer);
      }
    };
  }, [activeTaskId, applyScreenResult, language]);

  const handleEnable = async () => {
    setEnabling(true);
    setError('');
    try {
      await screeningApi.enable();
      setEnabled(true);
      setAvailable(true);
      await loadStrategies();
    } catch (err) {
      try {
        const status = await screeningApi.getStatus();
        setEnabled(status.enabled);
        setAvailable(status.available);
      } catch {
        setEnabled(false);
        setAvailable(false);
      }
      setError(err instanceof Error ? err.message : SCREENING_TEXT[language].enableScreeningFailed);
    } finally {
      setEnabling(false);
    }
  };

  const handleStrategyChange = (nextStrategy: string) => {
    if (nextStrategy !== strategy) {
      clearScreeningResults();
    }
    setStrategy(nextStrategy);
  };

  const handleMarketChange = (nextMarket: string) => {
    if (nextMarket !== market) {
      clearScreeningResults();
    }
    setMarket(nextMarket);
  };

  const handleMaxResultsChange = (nextMaxResults: number) => {
    if (nextMaxResults !== maxResults) {
      clearScreeningResults();
    }
    setMaxResults(nextMaxResults);
  };

  const handleSubmit = async () => {
    setLoading(true);
    setError('');
    setScreenMeta(null);
    setTaskProgress(0);
    setTaskMessage(SCREENING_TEXT[language].submittingScreenTask);
    try {
      const task = await screeningApi.startScreen({ market, strategy, maxResults });
      persistScreenTask({
        taskId: task.taskId,
        market,
        strategy,
        maxResults,
      });
      setActiveTaskId(task.taskId);
      setTaskProgress(0);
      setTaskMessage(task.message || SCREENING_TEXT[language].screenTaskSubmitted);
    } catch (err) {
      setCandidates([]);
      setLoading(false);
      setError(toApiErrorMessage(err, SCREENING_TEXT[language].screenTaskSubmitFailed));
    }
  };

  return (
    <AppPage className="max-w-6xl space-y-6 pb-12 pt-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex items-center gap-3">
          <span className="grid h-7 w-7 place-items-center rounded-full border-2 border-cyan text-cyan shadow-[0_0_24px_hsl(var(--primary)/0.18)]">
            <PlusCircle className="h-4 w-4" />
          </span>
          <h1 className="text-2xl font-bold tracking-normal text-foreground">{SCREENING_TEXT[language].title}</h1>
        </div>

        <div className="inline-flex w-fit items-center gap-2 rounded-2xl border border-border/70 bg-card/80 px-4 py-2 text-sm shadow-soft-card">
          <span className={`h-2.5 w-2.5 rounded-full ${isScreeningEnabled ? 'bg-success' : 'bg-warning'}`} />
          <span className="font-medium text-secondary-text">{statusText}</span>
        </div>
      </div>

      {!enabled ? (
        <InlineAlert
          variant="info"
          title={SCREENING_TEXT[language].screeningNotEnabledTitle}
          message={SCREENING_TEXT[language].screeningNotEnabledMessage}
          action={
            <Button size="sm" isLoading={enabling} loadingText={SCREENING_TEXT[language].enabling} onClick={() => void handleEnable()}>
              {SCREENING_TEXT[language].enableScreening}
            </Button>
          }
        />
      ) : null}

      {enabled && !available ? (
        <InlineAlert
          variant="warning"
          title={SCREENING_TEXT[language].screeningUnavailableTitle}
          message={SCREENING_TEXT[language].screeningUnavailableMessage}
        />
      ) : null}

      {error ? <InlineAlert variant="danger" title={SCREENING_TEXT[language].callFailed} message={error} /> : null}

      <section className="rounded-2xl border border-border/80 bg-card/95 p-4 shadow-soft-card">
        <div className="mb-4 flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div className="flex items-start gap-3">
            <span className="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-orange-500/10 text-orange-500 shadow-[0_10px_30px_rgba(249,115,22,0.16)]">
              <Flame className="h-5 w-5" />
            </span>
            <h2 className="text-lg font-bold tracking-normal text-foreground">{SCREENING_TEXT[language].hotThemes}</h2>
          </div>
          <div className="flex flex-col items-start gap-2 lg:items-end">
            <div className="flex flex-wrap items-center gap-2">
              <Button
                size="sm"
                variant="secondary"
                disabled={!isScreeningEnabled}
                onClick={toggleHotspotsExpanded}
              >
                <Bookmark className="h-4 w-4" />
                {hotspotsExpanded
                  ? SCREENING_TEXT[language].collapseHotThemes
                  : (hotspots.length
                    ? formatTemplate(SCREENING_TEXT[language].expandHotThemesTemplate, { count: hotspots.length })
                    : SCREENING_TEXT[language].expandHotThemes)}
                <ChevronDown className={`h-4 w-4 transition-transform ${hotspotsExpanded ? 'rotate-180' : ''}`} />
              </Button>
              {hotspotsExpanded ? (
              <Button
                size="sm"
                variant="secondary"
                isLoading={loadingHotspots}
                loadingText={SCREENING_TEXT[language].refreshing}
                disabled={!isScreeningEnabled || loadingHotspots}
                onClick={() => void loadHotspots(true)}
              >
                <RefreshCw className="h-4 w-4" />
                {SCREENING_TEXT[language].refreshHotThemes}
              </Button>
              ) : null}
            </div>
            {hotspotsUpdatedAt ? (
              <p className="text-xs text-secondary-text">{formatTemplate(SCREENING_TEXT[language].updatedAtTemplate, { time: formatHotspotUpdatedAt(hotspotsUpdatedAt, language) })}</p>
            ) : null}
          </div>
        </div>

        {hotspotsExpanded && hotspotError ? (
          <p className="mb-3 rounded-xl border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">
            {hotspotError}
          </p>
        ) : null}

        {!hotspotsExpanded ? null : hotspots.length === 0 ? (
          <div className="rounded-xl border border-dashed border-border bg-surface/70 px-4 py-6 text-sm text-secondary-text">
            {SCREENING_TEXT[language].noHotspotDataHint}
          </div>
        ) : (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
            {hotspots.map((item, index) => {
              const selected = selectedHotspotTopic === item.topic;
              const strength = getHotspotStrength(item, index, language);
              const iconMeta = getHotspotIcon(item.name || item.topic);
              const Icon = iconMeta.icon;
              return (
              <button
                key={`${item.topic}-${item.rank ?? ''}`}
                className={`group relative min-h-[116px] overflow-hidden rounded-xl border px-3 py-3 text-left transition-all ${
                  selected
                    ? 'border-orange-400 bg-gradient-to-br from-orange-500/10 via-card to-card shadow-[0_0_0_1px_rgba(249,115,22,0.16),0_18px_44px_rgba(249,115,22,0.14)]'
                    : 'border-border/80 bg-card hover:-translate-y-0.5 hover:border-orange-300/70 hover:shadow-soft-card'
                }`}
                type="button"
                onClick={() => handleHotspotSelect(item.topic)}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="flex min-w-0 items-start gap-3">
                    <span
                      className={`grid h-6 w-6 shrink-0 place-items-center rounded-full text-xs font-bold ${
                        index < 3 ? 'bg-orange-500 text-white shadow-[0_8px_24px_rgba(249,115,22,0.24)]' : 'bg-surface text-secondary-text'
                      }`}
                    >
                      {index + 1}
                    </span>
                    <span className={`grid h-9 w-9 shrink-0 place-items-center rounded-full ${iconMeta.className}`}>
                      <Icon className="h-5 w-5" />
                    </span>
                    <div className="min-w-0">
                      <p className="truncate text-sm font-bold text-foreground">{item.name || item.topic}</p>
                      <span className={`mt-1 inline-flex rounded-md px-1.5 py-0.5 text-[11px] font-semibold ${strength.className}`}>
                        {strength.label}
                      </span>
                    </div>
                  </div>
                  <span className="shrink-0 text-2xl font-black leading-none text-orange-500">
                    {formatNumber(item.heatScore, 0)}
                  </span>
                </div>
                <div className="mt-4 grid max-w-[72%] gap-1 text-[11px] text-secondary-text">
                  <span>{SCREENING_TEXT[language].changePct} <strong className="font-semibold text-foreground">{formatHotspotMetric(item.changePct, 1, language)}%</strong></span>
                  <span>{SCREENING_TEXT[language].trend} <strong className="font-semibold text-foreground">{formatHotspotMetric(item.trendScore, 1, language)}</strong> · {SCREENING_TEXT[language].persistence} <strong className="font-semibold text-foreground">{formatHotspotMetric(item.persistenceScore, 1, language)}</strong></span>
                  <span>{getHotspotSampleText(item, language)} · {SCREENING_TEXT[language].leaders} {getHotspotLeadersText(item, language)}</span>
                </div>
                <div className="absolute bottom-3 right-3 opacity-95 transition-transform group-hover:scale-105">
                  <MiniSparkline score={item.heatScore} selected={selected} />
                </div>
              </button>
              );
            })}
          </div>
        )}

        {hotspotsExpanded && selectedHotspotTopic ? (
          <div className="mt-4 rounded-xl border border-border/80 bg-surface/80 p-4">
            <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <h3 className="text-sm font-semibold text-foreground">
                  {hotspotDetail?.name || selectedHotspotTopic}
                </h3>
                <p className="mt-1 text-xs leading-5 text-secondary-text">
                  {hotspotDetail
                    ? getHotspotSummaryText(hotspotDetail, selectedHotspot, language)
                    : loadingHotspotDetail
                      ? SCREENING_TEXT[language].loadingHotspotRoute
                      : SCREENING_TEXT[language].clickHotspotHint}
                </p>
                {hotspotDetail?.canonicalTopic && hotspotDetail.canonicalTopic !== selectedHotspotTopic ? (
                  <p className="mt-1 text-[11px] text-secondary-text">{formatTemplate(SCREENING_TEXT[language].canonicalTopicTemplate, { topic: hotspotDetail.canonicalTopic })}</p>
                ) : null}
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  size="sm"
                  variant="secondary"
                  isLoading={searchingHotspotNews}
                  loadingText={SCREENING_TEXT[language].searching}
                  disabled={loadingHotspotDetail || searchingHotspotNews}
                  onClick={() => void loadHotspotDetail(selectedHotspotTopic, { includeSearch: true })}
                >
                  <Search className="h-3.5 w-3.5" />
                  {SCREENING_TEXT[language].searchLatestNews}
                </Button>
                {loadingHotspotDetail ? (
                  <span className="w-fit rounded-full bg-cyan/10 px-3 py-1 text-xs font-semibold text-cyan">
                    {SCREENING_TEXT[language].supplementingDetail}
                  </span>
                ) : null}
                {hotspotDetail?.qualityStatus ? (
                  <span className="w-fit rounded-full bg-warning/10 px-3 py-1 text-xs font-semibold text-warning">
                    {formatTemplate(SCREENING_TEXT[language].qualityTemplate, { label: getHotspotQualityLabel(hotspotDetail.qualityStatus, language) })}
                  </span>
                ) : null}
                {hotspotDetail?.fallbackUsed || hotspotDetail?.stale ? (
                  <span className="w-fit rounded-full bg-warning/10 px-3 py-1 text-xs font-semibold text-warning">
                    {getHotspotFallbackLabel(hotspotDetail, language)}
                  </span>
                ) : null}
                {hotspotDetail?.stockCount != null ? (
                  <span className="w-fit rounded-full bg-orange-500/10 px-3 py-1 text-xs font-semibold text-orange-500">
                    {formatTemplate(SCREENING_TEXT[language].conceptStockCountTemplate, { count: hotspotDetail.stockCount })}
                  </span>
                ) : null}
              </div>
            </div>

            {hotspotDetailError ? (
              <p className="mb-3 rounded-xl border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">
                {hotspotDetailError}
              </p>
            ) : null}

            {hotspotDetail && hasHotspotDetailDegradation(hotspotDetail) ? (
              <details className="mb-3 rounded-xl border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">
                <summary className="cursor-pointer font-semibold">{SCREENING_TEXT[language].detailDegraded}</summary>
                <div className="mt-2 space-y-1 leading-5">
                  {(hotspotDetail.missingFields || []).length > 0 ? (
                    <p>{formatTemplate(SCREENING_TEXT[language].missingFieldsTemplate, { fields: getHotspotMissingFieldLabels(hotspotDetail.missingFields, language).join(language === 'zh' ? '、' : ', ') })}</p>
                  ) : null}
                  {getHotspotDiagnosticMessages(hotspotDetail.sourceErrors, language).map((message, index) => (
                    <p key={`${message}-${index}`}>{message}</p>
                  ))}
                </div>
              </details>
            ) : null}

            {hotspotDetail ? (
              <div className="grid gap-4 lg:grid-cols-[1fr_1.3fr]">
                <div>
                  <p className="mb-3 flex items-center gap-1.5 text-xs font-semibold text-secondary-text">
                    <Clock3 className="h-3.5 w-3.5 text-orange-500" />
                    {SCREENING_TEXT[language].fermentationTimeline}
                  </p>
                  <div className="relative space-y-0 pl-4 before:absolute before:bottom-3 before:left-[5px] before:top-2 before:w-px before:bg-border">
                    {getHotspotRouteItems(hotspotDetail).map((item, index) => (
                      <div key={`${item.title}-${index}`} className="relative pb-4 last:pb-0">
                        <span className="absolute -left-4 top-1 h-2.5 w-2.5 rounded-full border border-orange-400 bg-card" />
                        <div className="rounded-lg border border-border/70 bg-card/80 p-3">
                          <p className="text-[11px] font-semibold text-orange-500">{getRouteTimeLabel(item, language)}</p>
                          <p className="mt-1 text-xs font-semibold text-foreground">{formatHotspotRouteTitle(item.title, language)}</p>
                          <p className="mt-1 text-xs leading-5 text-secondary-text">{formatHotspotRouteDescription(item.description, language)}</p>
                          {item.url ? (
                            <a
                              className="mt-2 inline-flex text-[11px] font-semibold text-cyan hover:text-foreground"
                              href={item.url}
                              target="_blank"
                              rel="noreferrer"
                            >
                              {SCREENING_TEXT[language].viewMessage}
                            </a>
                          ) : null}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
                <div>
                  <p className="mb-2 text-xs font-semibold text-secondary-text">{SCREENING_TEXT[language].conceptStocks}</p>
                  <div className="grid gap-2 sm:grid-cols-2">
                    {(hotspotDetail.stocks || []).slice(0, 10).map((stock) => (
                      <div key={`${stock.code || stock.name}`} className="rounded-lg border border-border/70 bg-card/80 p-3">
                        <div className="flex items-start justify-between gap-2">
                          <div className="min-w-0">
                            <p className="truncate text-xs font-semibold text-foreground">{stock.name || stock.code || '-'}</p>
                            <p className="mt-1 text-[11px] text-secondary-text">{stock.code || '-'}</p>
                          </div>
                          <div className="flex shrink-0 items-center gap-1">
                            <span className="rounded-full bg-cyan/10 px-2 py-1 text-[11px] font-semibold text-cyan">
                              {getHotspotRoleLabel(stock.role, language)}
                            </span>
                            {stock.code ? (
                              <button
                                type="button"
                                aria-label={formatTemplate(SCREENING_TEXT[language].analyzeStockAriaTemplate, { name: stock.name || stock.code })}
                                className="inline-flex h-7 items-center gap-1 rounded-full border border-cyan/30 bg-cyan/10 px-2 text-[11px] font-semibold text-cyan transition-colors hover:border-cyan hover:bg-cyan/15 hover:text-foreground"
                                onClick={() => handleAnalyzeHotspotStock(stock)}
                              >
                                <Play className="h-3 w-3" />
                                {SCREENING_TEXT[language].analyze}
                              </button>
                            ) : null}
                          </div>
                        </div>
                        <p className="mt-2 text-[11px] text-secondary-text">
                          {formatTemplate(SCREENING_TEXT[language].stockMetricsTemplate, { change: formatStockChangeText(stock.changePct, language), heat: formatNumber(stock.hotStockScore, 0) })}
                        </p>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            ) : null}
          </div>
        ) : null}
      </section>

      <section className="rounded-2xl border border-cyan/35 bg-card/95 p-4 shadow-soft-card">
        <div className="mb-4 flex items-start justify-between gap-3">
          <div className="flex items-center gap-2 text-sm font-semibold text-foreground">
            <SlidersHorizontal className="h-4 w-4 text-cyan" />
            {SCREENING_TEXT[language].runScreening}
          </div>
          <span className="rounded-full border border-cyan/30 bg-cyan/10 px-3 py-1 text-xs font-semibold text-cyan">
            {selectedStrategyTag}
          </span>
        </div>

        <div className="grid gap-4 lg:grid-cols-[1fr_1.2fr_180px_auto] lg:items-end">
          <label className="space-y-2 text-xs font-medium text-secondary-text">
            {SCREENING_TEXT[language].market}
            <select
              className="h-11 w-full rounded-xl border border-border bg-surface px-3 text-sm text-foreground outline-none transition-colors focus:border-cyan"
              value={market}
              disabled={loading}
              onChange={(event) => handleMarketChange(event.target.value)}
            >
              {MARKETS.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.label[language]}
                </option>
              ))}
            </select>
          </label>

          <div className="space-y-2 text-xs font-medium text-secondary-text">
            <label htmlFor="screening-strategy">{SCREENING_TEXT[language].strategy}</label>
            <Select
              id="screening-strategy"
              value={selectedStrategy ? strategy : CUSTOM_STRATEGY_OPTION_VALUE}
              disabled={loading || loadingStrategies}
              placeholder=""
              options={[
                ...strategies.map((item) => ({
                  value: item.id,
                  label: item.name || item.title || item.id,
                })),
                { value: CUSTOM_STRATEGY_OPTION_VALUE, label: SCREENING_TEXT[language].customStrategyOption },
              ]}
              onChange={(value) =>
                handleStrategyChange(
                  value === CUSTOM_STRATEGY_OPTION_VALUE ? '' : value,
                )
              }
            />
          </div>

          {!selectedStrategy && !loadingStrategies ? (
            <label className="space-y-2 text-xs font-medium text-secondary-text lg:col-start-2">
              {SCREENING_TEXT[language].customStrategyId}
              <input
                aria-label={SCREENING_TEXT[language].customStrategyId}
                className="h-11 w-full rounded-xl border border-border bg-surface px-3 text-sm text-foreground outline-none transition-colors focus:border-cyan"
                value={strategy}
                disabled={loading}
                placeholder={SCREENING_TEXT[language].inputStrategyId}
                onChange={(event) => handleStrategyChange(event.target.value)}
              />
            </label>
          ) : null}

          <label className="space-y-2 text-xs font-medium text-secondary-text">
            {SCREENING_TEXT[language].resultCount}
            <input
              className="h-11 w-full rounded-xl border border-border bg-surface px-3 text-sm text-foreground outline-none transition-colors focus:border-cyan"
              type="number"
              min={1}
              max={100}
              value={maxResults}
              disabled={loading}
              onChange={(event) => handleMaxResultsChange(Number(event.target.value))}
            />
          </label>

          <Button
            className="h-11 min-w-40"
            isLoading={loading}
            loadingText={SCREENING_TEXT[language].filtering}
            disabled={!isScreeningEnabled || loading || !strategy.trim()}
            onClick={() => void handleSubmit()}
          >
            <Play className="h-4 w-4" />
            {SCREENING_TEXT[language].runScreening}
          </Button>
        </div>

        <div className="mt-3 rounded-xl border border-border/75 bg-surface/55 px-3 py-2 text-xs leading-5 text-secondary-text">
          {strategyLoadError
            ? strategyLoadError
            : selectedStrategy?.description || SCREENING_TEXT[language].strategyDefaultDescription}
        </div>
      </section>

      {loading || screenMeta ? (
        <section className="rounded-2xl border border-border bg-card/95 p-4 shadow-soft-card">
          <div className="flex items-center gap-3">
            <div className="flex items-center gap-3">
              <span className={`grid h-7 w-7 place-items-center rounded-full ${loading ? 'text-cyan' : 'text-success'}`}>
                {loading ? <CircleAlert className="h-5 w-5" /> : <CheckCircle2 className="h-5 w-5" />}
              </span>
              <div>
                <h2 className="text-sm font-semibold text-foreground">{loading ? SCREENING_TEXT[language].screeningRunning : SCREENING_TEXT[language].screeningCompleted}</h2>
                <p className="mt-1 text-xs text-secondary-text">
                  {loading
                    ? `${taskMessage || SCREENING_TEXT[language].runningScreening} · ${taskProgress}%`
                    : `${displayedStrategy} · ${MARKETS.find((item) => item.id === market)?.label[language]}`}
                </p>
              </div>
            </div>
          </div>

          <details className="mt-3 border-t border-border/70 pt-3 text-xs text-secondary-text">
            <summary className="w-fit cursor-pointer font-medium text-secondary-text">{SCREENING_TEXT[language].runDetails}</summary>
            <div className="mt-2 grid gap-1">
              <span>{formatTemplate(SCREENING_TEXT[language].taskLabelTemplate, { id: activeTaskId ? activeTaskId.slice(0, 12) : '-' })}</span>
              <span>{formatTemplate(SCREENING_TEXT[language].runIdTemplate, { id: screenMeta?.runId || '-' })}</span>
              <span>
                {formatTemplate(SCREENING_TEXT[language].snapshotSummaryTemplate, {
                  snapshot: screenMeta?.snapshotCount ?? '-',
                  afterFilter: screenMeta?.afterFilterCount ?? '-',
                  candidates: screenMeta?.candidateCount ?? candidates.length,
                })}
              </span>
              <span>
                {formatTemplate(SCREENING_TEXT[language].sortingTemplate, {
                  mode: screenMeta?.llmRanked ? SCREENING_TEXT[language].sortingLlm : screenMeta ? SCREENING_TEXT[language].sortingFactor : '-',
                })}
                {screenMeta?.llmModelUsed ? ` · ${screenMeta.llmModelUsed}` : ''}
                {screenMeta?.llmCoverage != null ? formatTemplate(SCREENING_TEXT[language].coverageTemplate, { coverage: formatPercent(screenMeta.llmCoverage) }) : ''}
              </span>
              {screenMeta?.resultVariantPoolSize ? (
                <span>
                  {formatTemplate(SCREENING_TEXT[language].candidateVariancePoolTemplate, { pool: screenMeta.resultVariantPoolSize })}{' '}
                  {screenMeta.resultVariantApplied
                    ? formatTemplate(SCREENING_TEXT[language].variantAppliedTemplate, { slots: screenMeta.resultVariantRotatedSlots ?? 0 })
                    : SCREENING_TEXT[language].variantBaseline}
                </span>
              ) : null}
              <span>
                {formatTemplate(SCREENING_TEXT[language].dsaEnrichmentTemplate, {
                  enriched: screenMeta?.dsaEnrichment?.enrichedCount ?? '-',
                  requested: screenMeta?.dsaEnrichment?.requestedCount ?? '-',
                })}
              </span>
            </div>
          </details>
        </section>
      ) : null}

      {screenMeta && alertMessages.length > 0 ? (
        <InlineAlert
          variant={llmFailed ? 'warning' : 'info'}
          title={llmFailed ? SCREENING_TEXT[language].usingFactorRanking : SCREENING_TEXT[language].screeningHint}
          message={<ScreenAlertMessage messages={alertMessages} />}
        />
      ) : null}

      {screenMeta ? (
        <section className="rounded-2xl border border-border bg-card/95 p-4 shadow-soft-card">
          <div className="mb-5 flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
            <h2 className="text-base font-semibold text-foreground">{SCREENING_TEXT[language].screeningResults}</h2>
          <div className="flex items-center gap-2 rounded-full border border-border bg-surface px-3 py-2 text-xs text-secondary-text">
            <Search className="h-4 w-4 text-cyan" />
            {formatTemplate(SCREENING_TEXT[language].candidatesCountTemplate, { count: candidates.length })}
          </div>
          </div>

        {candidates.length === 0 ? (
          <div className="rounded-xl border border-dashed border-border bg-surface/70 px-5 py-10 text-center">
            <p className="text-sm font-medium text-foreground">{SCREENING_TEXT[language].noMatchingCandidates}</p>
          </div>
        ) : (
          <div className="overflow-hidden rounded-xl border border-border">
            <table className="w-full min-w-[860px] border-collapse text-sm">
              <thead className="bg-surface text-left text-xs text-secondary-text">
                <tr>
                  <th className="w-14 px-4 py-3 font-semibold">#</th>
                  <th className="px-4 py-3 font-semibold">{SCREENING_TEXT[language].columnCode}</th>
                  <th className="px-4 py-3 font-semibold">{SCREENING_TEXT[language].columnName}</th>
                  <th className="px-4 py-3 font-semibold">{SCREENING_TEXT[language].columnIndustry}</th>
                  <th className="px-4 py-3 font-semibold">{SCREENING_TEXT[language].columnPrice}</th>
                  <th className="px-4 py-3 font-semibold">{SCREENING_TEXT[language].columnChangePct}</th>
                  <th className="px-4 py-3 font-semibold">{SCREENING_TEXT[language].columnScore}</th>
                  <th className="px-4 py-3 font-semibold">{SCREENING_TEXT[language].columnRankingBasis}</th>
                  <th className="px-4 py-3 font-semibold">{SCREENING_TEXT[language].columnRisk}</th>
                  <th className="px-4 py-3 font-semibold">{SCREENING_TEXT[language].columnDetails}</th>
                </tr>
              </thead>
              <tbody>
                {candidates.map((item) => {
                  const expanded = expandedCode === item.code;
                  const factors = getFactorEntries(item);
                  const llmInsightAvailable = hasLlmInsight(item);
                  const dsaWarnings = item.dsaContext?.warnings || [];
                  const dsaNews = item.dsaNews || [];
                  const dsaEvents = item.dsaEvents || [];
                  return (
                    <Fragment key={`${item.rank}-${item.code}`}>
                      <tr className="border-t border-border align-top transition-colors hover:bg-hover/50">
                        <td className="px-4 py-3 text-secondary-text">{item.rank}</td>
                        <td className="px-4 py-3 font-mono font-semibold text-foreground">{item.code}</td>
                        <td className="px-4 py-3 font-semibold text-foreground">{item.name || '-'}</td>
                        <td className="px-4 py-3 text-secondary-text">{item.industry || '-'}</td>
                        <td className="px-4 py-3 text-secondary-text">{formatNumber(item.price)}</td>
                        <td className="px-4 py-3 text-secondary-text">{formatNumber(item.changePct)}%</td>
                        <td className="px-4 py-3 font-bold text-cyan">{formatScore(item.score)}</td>
                        <td className="px-4 py-3 text-secondary-text">{factorRanking ? SCREENING_TEXT[language].factorRankingLabel : formatScore(item.llmScore)}</td>
                        <td className="px-4 py-3">
                          <span className={`rounded-lg px-2.5 py-1 text-xs font-semibold ${getRiskClassName(item.riskLevel)}`}>
                            {getRiskLabel(item.riskLevel, language)}
                          </span>
                        </td>
                        <td className="px-4 py-3">
                          <button
                            className="text-sm font-semibold text-cyan transition-colors hover:text-foreground"
                            type="button"
                            onClick={() => setExpandedCode(expanded ? null : item.code)}
                          >
                            {expanded ? SCREENING_TEXT[language].collapse : SCREENING_TEXT[language].expandView}
                          </button>
                        </td>
                      </tr>
                      {expanded ? (
                        <tr className="border-t border-border bg-surface/45">
                          <td colSpan={10} className="px-4 py-4">
                            <div className="grid gap-4 lg:grid-cols-[1.1fr_1fr]">
                              <div className="space-y-3">
                                <div>
                                  <p className="text-xs font-semibold text-secondary-text">{SCREENING_TEXT[language].summary}</p>
                                  <p className="mt-1 text-sm leading-6 text-foreground">{getCandidateReason(item, language)}</p>
                                </div>
                                <div>
                                  <p className="text-xs font-semibold text-secondary-text">{SCREENING_TEXT[language].operationSignal}</p>
                                  <p className="mt-1 text-sm text-foreground">{getSignal(item, language)}</p>
                                  <button
                                    className="mt-2 rounded-lg border border-cyan/40 px-3 py-1.5 text-xs font-semibold text-cyan transition-colors hover:bg-cyan/10"
                                    type="button"
                                    onClick={() => handleAnalyzeCandidate(item)}
                                  >
                                    {SCREENING_TEXT[language].deepAnalysis}
                                  </button>
                                </div>
                                {item.dsaAnalysisSummary ? (
                                  <div>
                                    <p className="text-xs font-semibold text-secondary-text">{SCREENING_TEXT[language].enrichedSummary}</p>
                                    <p className="mt-1 text-sm leading-6 text-foreground">
                                      {formatEnrichmentSummary(item.dsaAnalysisSummary, language)}
                                    </p>
                                  </div>
                                ) : null}
                                {llmInsightAvailable ? (
                                  <div>
                                    <p className="text-xs font-semibold text-secondary-text">{SCREENING_TEXT[language].llmInsight}</p>
                                    <p className="mt-1 text-sm leading-6 text-foreground">{item.llmThesis || item.reason}</p>
                                    <p className="mt-1 text-xs text-secondary-text">
                                      {formatTemplate(SCREENING_TEXT[language].llmInsightMetaTemplate, {
                                        sector: item.llmSector || '-',
                                        theme: item.llmTheme || '-',
                                        confidence: formatPercent(item.llmConfidence),
                                      })}
                                    </p>
                                  </div>
                                ) : null}
                                <div>
                                  <p className="text-xs font-semibold text-secondary-text">{SCREENING_TEXT[language].riskTags}</p>
                                  <p className="mt-1 text-sm text-foreground">
                                    {[...(item.riskFlags || []), ...(item.llmRisks || [])].length
                                      ? [...(item.riskFlags || []), ...(item.llmRisks || [])].join(language === 'zh' ? '，' : ', ')
                                      : SCREENING_TEXT[language].none}
                                  </p>
                                </div>
                              </div>
                              <div className="space-y-3">
                                <div>
                                  <p className="text-xs font-semibold text-secondary-text">{SCREENING_TEXT[language].mainFactors}</p>
                                  <div className="mt-2 grid grid-cols-2 gap-2">
                                    {factors.length > 0 ? (
                                      factors.map(([key, value]) => (
                                        <div key={key} className="rounded-lg border border-border bg-card px-3 py-2">
                                          <span className="block text-xs text-secondary-text">{FACTOR_LABELS[language][key] || key}</span>
                                          <span className="text-sm font-semibold text-foreground">{formatNumber(value)}</span>
                                        </div>
                                      ))
                                    ) : (
                                      <span className="text-sm text-secondary-text">{SCREENING_TEXT[language].noFactorDetail}</span>
                                    )}
                                  </div>
                                </div>
                                <div>
                                  <p className="text-xs font-semibold text-secondary-text">{SCREENING_TEXT[language].turnover}</p>
                                  <p className="mt-1 text-sm text-foreground">{formatAmount(item.amount, language)}</p>
                                </div>
                                {item.llmWatchItems?.length ? (
                                  <div>
                                    <p className="text-xs font-semibold text-secondary-text">{SCREENING_TEXT[language].llmWatchItems}</p>
                                    <p className="mt-1 text-sm text-foreground">{item.llmWatchItems.join(language === 'zh' ? '，' : ', ')}</p>
                                  </div>
                                ) : null}
                                {item.llmCatalysts?.length ? (
                                  <div>
                                    <p className="text-xs font-semibold text-secondary-text">{SCREENING_TEXT[language].llmCatalysts}</p>
                                    <p className="mt-1 text-sm text-foreground">{item.llmCatalysts.join(language === 'zh' ? '，' : ', ')}</p>
                                  </div>
                                ) : null}
                                <div>
                                  <p className="text-xs font-semibold text-secondary-text">{SCREENING_TEXT[language].relatedNews}</p>
                                  {dsaNews.length > 0 ? (
                                    <ul className="mt-1 space-y-1 text-sm text-foreground">
                                      {dsaNews.slice(0, 3).map((newsItem, newsIndex) => (
                                        <li key={`${item.code}-dsa-news-${newsIndex}`}>
                                          {newsItem.title || newsItem.snippet || '-'}
                                        </li>
                                      ))}
                                    </ul>
                                  ) : (
                                    <p className="mt-1 text-sm text-secondary-text">{SCREENING_TEXT[language].none}</p>
                                  )}
                                </div>
                                <div>
                                  <p className="text-xs font-semibold text-secondary-text">{SCREENING_TEXT[language].announcementsAndEvents}</p>
                                  {dsaEvents.length > 0 ? (
                                    <ul className="mt-1 space-y-1 text-sm text-foreground">
                                      {dsaEvents.slice(0, 3).map((eventItem, eventIndex) => (
                                        <li key={`${item.code}-dsa-event-${eventIndex}`}>
                                          {eventItem.title || eventItem.snippet || '-'}
                                        </li>
                                      ))}
                                    </ul>
                                  ) : (
                                    <p className="mt-1 text-sm text-secondary-text">{SCREENING_TEXT[language].none}</p>
                                  )}
                                </div>
                                {dsaWarnings.length > 0 ? (
                                  <div>
                                    <p className="text-xs font-semibold text-secondary-text">{SCREENING_TEXT[language].dataSupplementHint}</p>
                                    <p className="mt-1 text-sm text-secondary-text">{dsaWarnings.join(language === 'zh' ? '，' : ', ')}</p>
                                  </div>
                                ) : null}
                              </div>
                            </div>
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        </section>
      ) : null}
    </AppPage>
  );
};

export default StockScreeningPage;
