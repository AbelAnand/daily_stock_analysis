import type { Message } from '../stores/agentChatStore';
import type { UiLanguage } from '../i18n/uiText';

const EXPORT_TEXT: Record<UiLanguage, { heading: string; generatedAt: string; user: string; assistant: string; filenamePrefix: string }> = {
  zh: {
    heading: '# 问股会话',
    generatedAt: '生成时间',
    user: '## 用户',
    assistant: '## AI',
    filenamePrefix: '问股会话',
  },
  en: {
    heading: '# Ask Stock Session',
    generatedAt: 'Generated at',
    user: '## User',
    assistant: '## AI',
    filenamePrefix: 'ask-stock-session',
  },
};

/**
 * Format chat messages as Markdown for export.
 */
export function formatSessionAsMarkdown(messages: Message[], language: UiLanguage = 'zh'): string {
  const text = EXPORT_TEXT[language];
  const now = new Date();
  const timeStr = now.toLocaleString(language === 'en' ? 'en-US' : 'zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });

  const lines: string[] = [
    text.heading,
    '',
    `${text.generatedAt}: ${timeStr}`,
    '',
  ];

  for (const msg of messages) {
    const heading = msg.role === 'user' ? text.user : text.assistant;
    if (msg.role === 'assistant' && msg.skillName) {
      lines.push(`${heading} (${msg.skillName})`);
    } else {
      lines.push(heading);
    }
    lines.push('');
    lines.push(msg.content);
    lines.push('');
  }

  return lines.join('\n');
}

/**
 * Trigger browser download of session as .md file.
 * Revokes object URL after download to prevent memory leak.
 */
export function downloadSession(messages: Message[], language: UiLanguage = 'zh'): void {
  const content = formatSessionAsMarkdown(messages, language);
  const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' });
  const now = new Date();
  const dateStr = now.toISOString().slice(0, 10).replace(/-/g, '');
  const pad = (n: number) => n.toString().padStart(2, '0');
  const timeStr = pad(now.getHours()) + pad(now.getMinutes());
  const filename = `${EXPORT_TEXT[language].filenamePrefix}_${dateStr}_${timeStr}.md`;

  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
