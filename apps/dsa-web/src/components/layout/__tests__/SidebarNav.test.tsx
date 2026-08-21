import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../../contexts/UiLanguageContext';
import { UI_LANGUAGE_STORAGE_KEY } from '../../../utils/uiLanguage';
import { SidebarNav } from '../SidebarNav';

const mockLogout = vi.fn().mockResolvedValue(undefined);
const mockGetScreeningStatus = vi.fn().mockResolvedValue({ enabled: false, available: false });
const mockThemeToggle = vi.fn(({ collapsed }: { collapsed?: boolean }) => (
  <button type="button">{collapsed ? '切换主题(折叠)' : '切换主题'}</button>
));

const completionBadgeState = { value: true };

vi.mock('../../../contexts/AuthContext', () => ({
  useAuth: () => ({
    authEnabled: true,
    logout: mockLogout,
  }),
}));

vi.mock('../../../stores/agentChatStore', () => ({
  useAgentChatStore: (selector: (state: { completionBadge: boolean }) => unknown) =>
    selector({ completionBadge: completionBadgeState.value }),
}));

vi.mock('../../../api/screening', () => ({
  SCREENING_CONFIG_CHANGED_EVENT: 'screening-config-changed',
  SYSTEM_CONFIG_CHANGED_EVENT: 'dsa-system-config-changed',
  screeningApi: {
    getStatus: () => mockGetScreeningStatus(),
  },
}));

vi.mock('../../theme/ThemeToggle', () => ({
  ThemeToggle: (props: { collapsed?: boolean }) => mockThemeToggle(props),
}));

describe('SidebarNav', () => {
  beforeEach(() => {
    window.localStorage.setItem(UI_LANGUAGE_STORAGE_KEY, 'zh');
  });

  it('hides the screening navigation item while Screening is disabled', () => {
    mockGetScreeningStatus.mockResolvedValueOnce({ enabled: false, available: true });

    render(
      <UiLanguageProvider>
        <MemoryRouter initialEntries={['/']}>
          <SidebarNav />
        </MemoryRouter>
      </UiLanguageProvider>,
    );

    expect(screen.queryByRole('link', { name: '选股' })).not.toBeInTheDocument();
  });

  it('shows screening directly after chat when Screening is enabled', async () => {
    mockGetScreeningStatus.mockResolvedValueOnce({ enabled: true, available: true });

    render(
      <UiLanguageProvider>
        <MemoryRouter initialEntries={['/']}>
          <SidebarNav />
        </MemoryRouter>
      </UiLanguageProvider>,
    );

    expect(await screen.findByRole('link', { name: '选股' })).toHaveAttribute('href', '/screening');
    const hrefs = screen.getAllByRole('link').map((link) => link.getAttribute('href'));
    expect(hrefs.slice(0, 5)).toEqual(['/', '/chat', '/screening', '/portfolio', '/decision-signals']);
  });

  it('refreshes the controlled screening entry after config changes', async () => {
    mockGetScreeningStatus
      .mockResolvedValueOnce({ enabled: false, available: true })
      .mockResolvedValueOnce({ enabled: true, available: true });

    render(
      <UiLanguageProvider>
        <MemoryRouter initialEntries={['/']}>
          <SidebarNav />
        </MemoryRouter>
      </UiLanguageProvider>,
    );

    expect(screen.queryByRole('link', { name: '选股' })).not.toBeInTheDocument();
    window.dispatchEvent(new Event('screening-config-changed'));

    expect(await screen.findByRole('link', { name: '选股' })).toHaveAttribute('href', '/screening');
    await waitFor(() => expect(mockGetScreeningStatus.mock.calls.length).toBeGreaterThanOrEqual(2));
  });

  it('shows the shared completion badge only when chat completion is pending', () => {
    completionBadgeState.value = true;

    const { rerender } = render(
      <UiLanguageProvider>
        <MemoryRouter initialEntries={['/chat']}>
          <SidebarNav />
        </MemoryRouter>
      </UiLanguageProvider>,
    );

    expect(screen.getByTestId('chat-completion-badge')).toBeInTheDocument();
    expect(screen.getByLabelText('问股有新消息')).toBeInTheDocument();

    completionBadgeState.value = false;
    rerender(
      <UiLanguageProvider>
        <MemoryRouter initialEntries={['/chat']}>
          <SidebarNav />
        </MemoryRouter>
      </UiLanguageProvider>,
    );

    expect(screen.queryByTestId('chat-completion-badge')).not.toBeInTheDocument();
  });

  it('renders the collapsed theme toggle variant when the sidebar is collapsed', () => {
    render(
      <UiLanguageProvider>
        <MemoryRouter initialEntries={['/']}>
          <SidebarNav collapsed />
        </MemoryRouter>
      </UiLanguageProvider>,
    );

    expect(mockThemeToggle).toHaveBeenCalledWith(
      expect.objectContaining({ variant: 'nav', collapsed: true }),
    );
    expect(screen.getByRole('button', { name: '切换主题(折叠)' })).toBeInTheDocument();
  });

  it('renders the alerts navigation item and marks it active', () => {
    render(
      <UiLanguageProvider>
        <MemoryRouter initialEntries={['/alerts']}>
          <SidebarNav />
        </MemoryRouter>
      </UiLanguageProvider>,
    );

    const alertsLink = screen.getByRole('link', { name: '告警' });
    expect(alertsLink).toHaveAttribute('href', '/alerts');
    expect(alertsLink).toHaveClass('font-medium');
  });

  it('renders the AI signals navigation item and marks it active', () => {
    render(
      <UiLanguageProvider>
        <MemoryRouter initialEntries={['/decision-signals']}>
          <SidebarNav />
        </MemoryRouter>
      </UiLanguageProvider>,
    );

    const signalsLink = screen.getByRole('link', { name: 'AI 建议' });
    expect(signalsLink).toHaveAttribute('href', '/decision-signals');
    expect(signalsLink).toHaveClass('font-medium');
  });

  it('opens the logout confirmation and confirms logout', async () => {
    render(
      <UiLanguageProvider>
        <MemoryRouter initialEntries={['/chat']}>
          <SidebarNav />
        </MemoryRouter>
      </UiLanguageProvider>,
    );

    fireEvent.click(screen.getByRole('button', { name: '退出' }));

    expect(await screen.findByRole('heading', { name: '退出登录' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '确认退出' }));
    expect(mockLogout).toHaveBeenCalled();
  });
});
