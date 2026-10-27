import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { isTerminal } from '../types';
import type { AppStatus, BrowserPreview, BrowserTab, Control, Run } from '../types';

const DRAFT_KEY = 'jd-shopper:shopping-draft';
const storedDraft = () => { try { return sessionStorage.getItem(DRAFT_KEY)?.slice(0, 5000) || ''; } catch { return ''; } };
const loadedEntry = () => Array.from(document.scripts).map(script => { try { return new URL(script.src).pathname; } catch { return ''; } }).find(path => /^\/assets\/[^/]+\.js$/.test(path));
const connectionError = (message: string, existingBrowser: boolean) => {
  if (!existingBrowser) return message;
  let detail = message;
  if (/超时|timed?\s*out|timeout|过期/i.test(detail) && !/旧.*弹窗/.test(detail)) detail += '。旧授权弹窗可能已失效；请关闭旧弹窗后重新连接，并在新弹窗中点击允许。';
  if (!detail.includes('不会自动另开')) detail += '；不会自动另开购物窗口。';
  return detail;
};

function currentRun(run: Run | null | undefined) {
  return run && !isTerminal(run.status) ? run : null;
}

export function useShopper() {
  const [status, setStatus] = useState<AppStatus | null>(null);
  const [history, setHistory] = useState<Run[]>([]);
  const [viewed, setViewed] = useState<Run | null>(null);
  const [networkError, setNetworkError] = useState('');
  const [actionError, setActionError] = useState('');
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState('');
  const [browserPreview, setBrowserPreview] = useState<BrowserPreview | null>(null);
  const [browserTabs, setBrowserTabs] = useState<BrowserTab[]>([]);
  const [draft, setDraft] = useState(storedDraft);
  const [updateAvailable, setUpdateAvailable] = useState(false);
  const initialRuntimeBuild = useRef<string | null>(null);
  const initialFrontendEntry = useRef(loadedEntry());
  const mounted = useRef(true);
  const refreshHistory = useCallback(async () => { try { const runs = await api.history(); if (mounted.current) setHistory(runs); } catch { /* Primary connection errors are shown by status polling. */ } }, []);
  const refresh = useCallback(async () => {
    try {
      const next = await api.status();
      if (mounted.current) {
        if (next.runtime_build && !initialRuntimeBuild.current) initialRuntimeBuild.current = next.runtime_build;
        const runtimeChanged = !!next.runtime_build && initialRuntimeBuild.current !== next.runtime_build;
        const entryChanged = !!next.frontend_entry && !!initialFrontendEntry.current && next.frontend_entry !== initialFrontendEntry.current;
        setUpdateAvailable(runtimeChanged || entryChanged);
        setStatus(next); setNetworkError('');
        const tabs = next.active_run?.browser_tabs;
        if (Array.isArray(tabs) && tabs.length) {
          setBrowserTabs(tabs.filter((tab): tab is BrowserTab => !!tab && typeof tab === 'object' && typeof tab.id === 'string' && !!tab.id && tab.id.length <= 200 && typeof tab.title === 'string' && typeof tab.url === 'string').slice(0, 30));
        }
      }
    } catch (error) {
      if (mounted.current) { setNetworkError(error instanceof Error ? error.message : '无法连接本地服务。'); setBrowserPreview(null); setBrowserTabs([]); setNotice(''); }
    }
  }, []);
  useEffect(() => {
    mounted.current = true;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    let count = 0;
    const poll = async () => { await refresh(); if (cancelled) return; if (count++ % 5 === 0) await refreshHistory(); if (!cancelled) timer = setTimeout(poll, 2000); };
    void poll();
    return () => { cancelled = true; mounted.current = false; clearTimeout(timer); };
  }, [refresh, refreshHistory]);

  const active = currentRun(status?.active_run);
  const updateRun = (run: Run) => { setStatus(previous => previous ? { ...previous, active_run: currentRun(run) } : previous); setViewed(null); void refreshHistory(); };
  const perform = async <T,>(label: string, operation: () => Promise<T>, onSuccess?: (data: T) => void) => {
    setBusy(label); setActionError(''); setNotice('');
    try { const result = await operation(); onSuccess?.(result); }
    catch (error) { const message = error instanceof Error ? error.message : '操作失败，请重试。'; setActionError(label === 'browser' ? connectionError(message, !status?.config.input_mode || status.config.input_mode === 'existing_browser') : message); }
    finally { setBusy(''); }
  };
  return {
    status, history, viewed, run: viewed ?? active ?? null, activeRun: active, networkError, actionError, busy, notice, browserPreview, browserTabs, draft, setDraft, updateAvailable,
    readOnly: viewed != null, refresh,
    start: (prompt: string) => perform('start', () => api.start(prompt), updateRun),
    control: (action: Control, selection?: string[]) => { const id = active?.id; if (id) return perform(action, () => api.control(id, action, selection), updateRun); return Promise.resolve(); },
    updateInterface: () => {
      try { sessionStorage.setItem(DRAFT_KEY, draft); }
      catch { setActionError('浏览器无法保存未提交的购物需求。请先复制输入内容，再手动刷新界面。'); return; }
      window.location.reload();
    },
    openBrowser: (pageId?: string) => {
      // Clear the previous successful preview before an attempt; HTTP errors
      // must not leave an old "ready" state visible behind the error banner.
      setBrowserPreview(null);
      setBrowserTabs([]);
      return perform('browser', () => api.openBrowser(pageId), result => {
      const observation = result.observation && typeof result.observation === 'object' ? result.observation as Record<string, unknown> : result;
      const statuses = [result.status, observation.status];
      const errorCode = result.error || observation.error;
      const selectionRequired = errorCode === 'browser_tab_selection_required';
      const needsUser = selectionRequired || statuses.some(value => value === 'needs_user' || value === 'blocked') || !!result.needs_user || !!observation.needs_user;
      const failed = !selectionRequired && (statuses.some(value => value === 'error' || value === 'failed') || !!result.error || !!observation.error);
      const ready = !failed && !needsUser && statuses.some(value => value === 'ok' || value === 'ready' || value === 'success');
      const issue = failed ? [result, observation].find(value => value.status === 'error' || value.status === 'failed' || value.error) : needsUser ? [result, observation].find(value => value.status === 'needs_user' || value.status === 'blocked' || value.needs_user) : observation;
      const detail = issue?.message || issue?.reason || issue?.error || result.message;
      const tabs = result.tabs || observation.tabs;
      if (selectionRequired && Array.isArray(tabs)) setBrowserTabs(tabs.filter((tab): tab is BrowserTab => !!tab && typeof tab === 'object' && typeof tab.id === 'string' && !!tab.id && tab.id.length <= 200 && typeof tab.title === 'string' && typeof tab.url === 'string').slice(0, 30));
      const preview: BrowserPreview = {
        title: typeof observation.title === 'string' ? observation.title : '',
        url: typeof observation.url === 'string' ? observation.url : '',
        text: typeof observation.text === 'string' ? observation.text : '',
        status: failed ? 'error' : needsUser ? 'needs_user' : ready ? 'ready' : 'unknown',
        message: typeof detail === 'string' ? detail : '',
        error: typeof errorCode === 'string' ? errorCode : '',
        elements: Array.isArray(observation.elements) ? observation.elements.length : 0,
        input_mode: status?.config.input_mode,
      };
      setBrowserPreview(preview);
      if (!ready && !selectionRequired) {
        const message = typeof detail === 'string' && detail ? detail : status?.config.input_mode === 'browser_mouse' ? needsUser ? '请在专用京东窗口完成登录或处理页面提示，再继续任务。' : '尚未确认京东窗口可用，请检查窗口后重试。' : needsUser ? '请在当前 Edge 中完成登录或允许连接，再重试；不会自动另开购物窗口。' : '尚未连接到当前 Edge 中的京东，请检查连接设置后重试；不会自动另开购物窗口。';
        setActionError(connectionError(message, !status?.config.input_mode || status.config.input_mode === 'existing_browser'));
      }
      else if (ready) setNotice(typeof detail === 'string' && detail ? detail : status?.config.input_mode === 'native' ? '京东窗口已连接。原生键鼠模式需要将浏览器保持在前台。' : status?.config.input_mode === 'browser_mouse' ? '专用京东窗口已打开。首次使用请先登录一次；Agent 可以保持打开。' : '已连接你当前 Edge 中的京东。沿用现有登录状态，Agent 可以保持打开。');
      });
    },
    selectHistory: (id: string) => perform('history', () => api.run(id), setViewed),
    returnToActive: () => setViewed(null),
    dismissError: () => setActionError(''),
    enableLocalModel: () => perform('local-model', () => api.enableLocalModel(), () => {
      setNotice('已开始下载或启动本地 Qwen 3B，完成后会自动用于商品筛选。');
      void refresh();
    }),
  };
}
