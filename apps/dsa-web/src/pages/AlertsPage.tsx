import type React from 'react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { BellRing } from 'lucide-react';
import { alertsApi } from '../api/alerts';
import type { ParsedApiError } from '../api/error';
import { getParsedApiError } from '../api/error';
import { AlertRuleForm } from '../components/alerts/AlertRuleForm';
import {
  AlertRuleList,
  type AlertRuleBusyState,
  type AlertRuleEnabledFilter,
  type AlertTypeFilter,
} from '../components/alerts/AlertRuleList';
import { AlertTriggerHistory } from '../components/alerts/AlertTriggerHistory';
import { ApiErrorAlert, AppPage, Card, EmptyState, InlineAlert, Loading, PageHeader } from '../components/common';
import { useUiLanguage } from '../contexts/UiLanguageContext';
import type { UiLanguage } from '../i18n/uiText';
import type {
  AlertNotificationItem,
  AlertRuleCreateRequest,
  AlertRuleItem,
  AlertRuleTestResponse,
  AlertTriggerItem,
  AlertType,
} from '../types/alerts';
import { formatDateTime } from '../utils/format';

const PAGE_SIZE = 20;

const ALERTS_PAGE_TEXT: Record<UiLanguage, {
  documentTitle: string;
  title: string;
  description: string;
  createSuccessTitle: string;
  createSuccessMessage: (name: string) => string;
  close: string;
  testResultTitle: string;
  status: string;
  triggered: string;
  observedValue: string;
  yes: string;
  no: string;
  evaluated: string;
  triggeredCount: string;
  degraded: string;
  skipped: string;
  notificationsTitle: string;
  notificationsSubtitle: string;
  loadingNotifications: string;
  noNotificationsTitle: string;
  noNotificationsDescription: string;
  channel: string;
  errorCode: string;
  latency: string;
  time: string;
  diagnostics: string;
  channelLabels: Record<string, string>;
  success: string;
  cooldownSuppressed: string;
  cooldownReadFailed: string;
  noiseSuppressed: string;
  noChannel: string;
  failure: string;
}> = {
  zh: {
    documentTitle: '告警中心 - DSA',
    title: '告警中心',
    description: '管理事件告警、日线技术指标、自选股、持仓/账户联动和大盘红绿灯规则，执行一次性测试，并查看后台评估任务记录的触发历史。',
    createSuccessTitle: '创建成功',
    createSuccessMessage: (name) => `已创建告警规则「${name}」`,
    close: '关闭',
    testResultTitle: '测试结果',
    status: '状态',
    triggered: '触发',
    observedValue: '观察值',
    yes: '是',
    no: '否',
    evaluated: '评估',
    triggeredCount: '触发',
    degraded: '降级',
    skipped: '跳过',
    notificationsTitle: '通知尝试记录',
    notificationsSubtitle: '通知结果',
    loadingNotifications: '正在加载通知尝试记录',
    noNotificationsTitle: '暂无通知尝试记录',
    noNotificationsDescription: '当前没有可展示的通知尝试明细；告警触发仍会按已配置通知渠道发送。',
    channel: '渠道',
    errorCode: '错误码',
    latency: '耗时',
    time: '时间',
    diagnostics: '诊断',
    channelLabels: {
      __cooldown__: '业务冷却',
      __cooldown_read_failed__: '冷却读取失败',
      __noise_suppressed__: '通知降噪',
      __no_channel__: '无可用渠道',
      __dispatch__: '通知调度',
      __context__: '会话渠道',
    },
    success: '成功',
    cooldownSuppressed: '冷却抑制',
    cooldownReadFailed: '冷却读取失败',
    noiseSuppressed: '降噪抑制',
    noChannel: '无渠道',
    failure: '失败',
  },
  en: {
    documentTitle: 'Alert Center - DSA',
    title: 'Alert center',
    description: 'Manage event alerts, daily technical indicator rules, watchlist, portfolio/account, and market traffic-light rules; run one-off tests and review the trigger history recorded by background evaluation tasks.',
    createSuccessTitle: 'Created',
    createSuccessMessage: (name) => `Alert rule "${name}" created`,
    close: 'Close',
    testResultTitle: 'Test result',
    status: 'Status',
    triggered: 'Triggered',
    observedValue: 'Observed value',
    yes: 'Yes',
    no: 'No',
    evaluated: 'Evaluated',
    triggeredCount: 'Triggered',
    degraded: 'Degraded',
    skipped: 'Skipped',
    notificationsTitle: 'Notification attempts',
    notificationsSubtitle: 'Notification results',
    loadingNotifications: 'Loading notification attempts',
    noNotificationsTitle: 'No notification attempts',
    noNotificationsDescription: 'There are no notification attempt details to show yet; alert triggers still send through configured notification channels.',
    channel: 'Channel',
    errorCode: 'Error code',
    latency: 'Latency',
    time: 'Time',
    diagnostics: 'Diagnostics',
    channelLabels: {
      __cooldown__: 'Cooldown',
      __cooldown_read_failed__: 'Cooldown read failed',
      __noise_suppressed__: 'Noise suppressed',
      __no_channel__: 'No channel available',
      __dispatch__: 'Dispatch',
      __context__: 'Session channel',
    },
    success: 'Success',
    cooldownSuppressed: 'Cooldown suppressed',
    cooldownReadFailed: 'Cooldown read failed',
    noiseSuppressed: 'Noise suppressed',
    noChannel: 'No channel',
    failure: 'Failed',
  },
};

function enabledFilterToQuery(value: AlertRuleEnabledFilter): boolean | undefined {
  if (value === 'enabled') return true;
  if (value === 'disabled') return false;
  return undefined;
}

function alertTypeFilterToQuery(value: AlertTypeFilter): AlertType | undefined {
  return value === 'all' ? undefined : value;
}

function testVariant(result: AlertRuleTestResponse): 'success' | 'warning' | 'danger' {
  if (result.status === 'evaluation_error') return 'danger';
  return result.triggered ? 'success' : 'warning';
}

function renderTestResultMessage(
  result: AlertRuleTestResponse,
  text: (typeof ALERTS_PAGE_TEXT)[UiLanguage],
): React.ReactNode {
  const targetResults = result.targetResults ?? [];
  return (
    <div className="space-y-2">
      <div>
        {result.message}
        {` · ${text.status}: `}
        {result.status}
        {` · ${text.triggered}: `}
        {result.triggered ? text.yes : text.no}
        {` · ${text.observedValue}: `}
        {result.observedValue == null ? '--' : String(result.observedValue)}
      </div>
      {result.evaluatedCount != null && result.evaluatedCount > 1 ? (
        <div className="text-xs">
          {text.evaluated} {result.evaluatedCount} · {text.triggeredCount} {result.triggeredCount ?? 0} · {text.degraded} {result.degradedCount ?? 0} · {text.skipped} {result.skippedCount ?? 0}
        </div>
      ) : null}
      {targetResults.length > 1 ? (
        <div className="grid gap-1 text-xs">
          {targetResults.slice(0, 20).map((item) => (
            <div key={`${item.target}-${item.status}`} className="flex flex-wrap justify-between gap-2">
              <span>{item.displayTarget ?? item.target}</span>
              <span>
                {item.status}
                {item.recordStatus ? ` / ${item.recordStatus}` : ''}
              </span>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function formatNotificationChannel(channel: string, text: (typeof ALERTS_PAGE_TEXT)[UiLanguage]): string {
  return text.channelLabels[channel] ?? channel;
}

function formatNotificationStatus(
  notification: AlertNotificationItem,
  text: (typeof ALERTS_PAGE_TEXT)[UiLanguage],
): string {
  if (notification.success) return text.success;
  if (notification.errorCode === 'cooldown_active') return text.cooldownSuppressed;
  if (notification.errorCode === 'cooldown_read_failed') return text.cooldownReadFailed;
  if (notification.errorCode === 'noise_suppressed') return text.noiseSuppressed;
  if (notification.errorCode === 'no_channel') return text.noChannel;
  return text.failure;
}

const AlertsPage: React.FC = () => {
  const { language } = useUiLanguage();
  const text = ALERTS_PAGE_TEXT[language];

  useEffect(() => {
    document.title = text.documentTitle;
  }, [text.documentTitle]);

  const [rules, setRules] = useState<AlertRuleItem[]>([]);
  const [rulesTotal, setRulesTotal] = useState(0);
  const [rulesPage, setRulesPage] = useState(1);
  const [enabledFilter, setEnabledFilter] = useState<AlertRuleEnabledFilter>('all');
  const [alertTypeFilter, setAlertTypeFilter] = useState<AlertTypeFilter>('all');
  const [rulesLoading, setRulesLoading] = useState(false);
  const [rulesError, setRulesError] = useState<ParsedApiError | null>(null);
  const [rulesLoaded, setRulesLoaded] = useState(false);

  const [triggers, setTriggers] = useState<AlertTriggerItem[]>([]);
  const [triggersLoading, setTriggersLoading] = useState(false);
  const [triggersError, setTriggersError] = useState<ParsedApiError | null>(null);

  const [notifications, setNotifications] = useState<AlertNotificationItem[]>([]);
  const [notificationsLoading, setNotificationsLoading] = useState(false);
  const [notificationsError, setNotificationsError] = useState<ParsedApiError | null>(null);

  const [createLoading, setCreateLoading] = useState(false);
  const [createError, setCreateError] = useState<ParsedApiError | null>(null);
  const [createSuccess, setCreateSuccess] = useState<string | null>(null);
  const [busyRule, setBusyRule] = useState<AlertRuleBusyState | null>(null);
  const [testResult, setTestResult] = useState<AlertRuleTestResponse | null>(null);
  const rulesRequestIdRef = useRef(0);

  const loadRules = useCallback(async (pageOverride?: number) => {
    const requestId = rulesRequestIdRef.current + 1;
    rulesRequestIdRef.current = requestId;
    const isLatestRequest = () => rulesRequestIdRef.current === requestId;
    const requestedPage = pageOverride ?? rulesPage;
    const baseQuery = {
      enabled: enabledFilterToQuery(enabledFilter),
      alertType: alertTypeFilterToQuery(alertTypeFilter),
      pageSize: PAGE_SIZE,
    };
    setRulesLoading(true);
    try {
      let response = await alertsApi.listRules({ ...baseQuery, page: requestedPage });
      if (!isLatestRequest()) return null;
      const lastPage = Math.max(1, Math.ceil(response.total / PAGE_SIZE));
      if (response.items.length === 0 && response.total > 0 && requestedPage > lastPage) {
        setRulesPage(lastPage);
        response = await alertsApi.listRules({ ...baseQuery, page: lastPage });
        if (!isLatestRequest()) return null;
      } else if (pageOverride !== undefined && pageOverride !== rulesPage) {
        setRulesPage(pageOverride);
      }
      setRules(response.items);
      setRulesTotal(response.total);
      setRulesError(null);
      setRulesLoaded(true);
      return response;
    } catch (error) {
      if (!isLatestRequest()) return null;
      setRulesError(getParsedApiError(error));
      return null;
    } finally {
      if (isLatestRequest()) {
        setRulesLoading(false);
      }
    }
  }, [alertTypeFilter, enabledFilter, rulesPage]);

  const loadTriggers = useCallback(async () => {
    setTriggersLoading(true);
    try {
      const response = await alertsApi.listTriggers({ page: 1, pageSize: PAGE_SIZE });
      setTriggers(response.items);
      setTriggersError(null);
    } catch (error) {
      setTriggersError(getParsedApiError(error));
    } finally {
      setTriggersLoading(false);
    }
  }, []);

  const loadNotifications = useCallback(async () => {
    setNotificationsLoading(true);
    try {
      const response = await alertsApi.listNotifications({ page: 1, pageSize: PAGE_SIZE });
      setNotifications(response.items);
      setNotificationsError(null);
    } catch (error) {
      setNotificationsError(getParsedApiError(error));
    } finally {
      setNotificationsLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadRules();
  }, [loadRules]);

  useEffect(() => {
    if (!rulesLoaded) return;
    void loadTriggers();
    void loadNotifications();
  }, [loadNotifications, loadTriggers, rulesLoaded]);

  const handleCreateRule = async (payload: AlertRuleCreateRequest) => {
    setCreateLoading(true);
    setCreateError(null);
    setCreateSuccess(null);
    try {
      const created = await alertsApi.createRule(payload);
      setCreateSuccess(text.createSuccessMessage(created.name));
      await loadRules(1);
      return true;
    } catch (error) {
      setCreateError(getParsedApiError(error));
      return false;
    } finally {
      setCreateLoading(false);
    }
  };

  const handleToggleEnabled = async (rule: AlertRuleItem) => {
    setBusyRule({ id: rule.id, action: 'toggle' });
    try {
      if (rule.enabled) {
        await alertsApi.disableRule(rule.id);
      } else {
        await alertsApi.enableRule(rule.id);
      }
      await loadRules();
    } catch (error) {
      setRulesError(getParsedApiError(error));
    } finally {
      setBusyRule(null);
    }
  };

  const handleDeleteRule = async (rule: AlertRuleItem) => {
    setBusyRule({ id: rule.id, action: 'delete' });
    try {
      await alertsApi.deleteRule(rule.id);
      await loadRules();
    } catch (error) {
      setRulesError(getParsedApiError(error));
    } finally {
      setBusyRule(null);
    }
  };

  const handleTestRule = async (rule: AlertRuleItem) => {
    setBusyRule({ id: rule.id, action: 'test' });
    setTestResult(null);
    try {
      const result = await alertsApi.testRule(rule.id);
      setTestResult(result);
    } catch (error) {
      setRulesError(getParsedApiError(error));
    } finally {
      setBusyRule(null);
    }
  };

  return (
    <AppPage className="space-y-5">
      <PageHeader
        eyebrow="Alert Center"
        title={text.title}
        description={text.description}
      />

      {createError ? <ApiErrorAlert error={createError} onDismiss={() => setCreateError(null)} /> : null}
      {createSuccess ? (
        <InlineAlert
          title={text.createSuccessTitle}
          message={createSuccess}
          variant="success"
          action={(
            <button type="button" className="text-sm underline" onClick={() => setCreateSuccess(null)}>
              {text.close}
            </button>
          )}
        />
      ) : null}
      {rulesError ? <ApiErrorAlert error={rulesError} onDismiss={() => setRulesError(null)} /> : null}

      <div className="grid items-stretch gap-5 xl:grid-cols-[380px_minmax(0,1fr)]">
        <AlertRuleForm onSubmit={handleCreateRule} isSubmitting={createLoading} />
        <div className="flex h-full min-h-0 flex-col gap-4">
          <AlertRuleList
            className="flex h-full min-h-0 flex-col"
            rules={rules}
            total={rulesTotal}
            page={rulesPage}
            pageSize={PAGE_SIZE}
            isLoading={rulesLoading}
            enabledFilter={enabledFilter}
            alertTypeFilter={alertTypeFilter}
            onEnabledFilterChange={(value) => {
              setEnabledFilter(value);
              setRulesPage(1);
            }}
            onAlertTypeFilterChange={(value) => {
              setAlertTypeFilter(value);
              setRulesPage(1);
            }}
            onPageChange={setRulesPage}
            onToggleEnabled={(rule) => void handleToggleEnabled(rule)}
            onDelete={(rule) => void handleDeleteRule(rule)}
            onTest={(rule) => void handleTestRule(rule)}
            busyRule={busyRule}
          />
          {testResult ? (
            <InlineAlert
              title={text.testResultTitle}
              variant={testVariant(testResult)}
              message={renderTestResultMessage(testResult, text)}
            />
          ) : null}
        </div>
      </div>

      {triggersError ? <ApiErrorAlert error={triggersError} onDismiss={() => setTriggersError(null)} /> : null}
      <AlertTriggerHistory triggers={triggers} isLoading={triggersLoading} />

      {notificationsError ? <ApiErrorAlert error={notificationsError} onDismiss={() => setNotificationsError(null)} /> : null}
      <Card title={text.notificationsTitle} subtitle={text.notificationsSubtitle} variant="bordered" padding="md">
        {notificationsLoading ? <Loading label={text.loadingNotifications} /> : null}
        {!notificationsLoading && notifications.length === 0 ? (
          <EmptyState
            icon={<BellRing className="h-6 w-6" />}
            title={text.noNotificationsTitle}
            description={text.noNotificationsDescription}
          />
        ) : null}
        {!notificationsLoading && notifications.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[680px] text-left text-sm">
              <thead className="border-b border-border/60 text-xs uppercase text-muted-text">
                <tr>
                  <th className="px-3 py-2 font-medium">{text.channel}</th>
                  <th className="px-3 py-2 font-medium">{text.status}</th>
                  <th className="px-3 py-2 font-medium">{text.errorCode}</th>
                  <th className="px-3 py-2 font-medium">{text.latency}</th>
                  <th className="px-3 py-2 font-medium">{text.time}</th>
                  <th className="px-3 py-2 font-medium">{text.diagnostics}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border/40">
                {notifications.map((notification) => (
                  <tr key={notification.id}>
                    <td className="px-3 py-3">{formatNotificationChannel(notification.channel, text)}</td>
                    <td className="px-3 py-3">{formatNotificationStatus(notification, text)}</td>
                    <td className="px-3 py-3">{notification.errorCode ?? '--'}</td>
                    <td className="px-3 py-3">{notification.latencyMs == null ? '--' : `${notification.latencyMs}ms`}</td>
                    <td className="px-3 py-3">{formatDateTime(notification.createdAt)}</td>
                    <td className="px-3 py-3">{notification.diagnostics ?? '--'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </Card>
    </AppPage>
  );
};

export default AlertsPage;
