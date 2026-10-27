'use strict';

// Attach-only adapter. The connection belongs to this Agent; the browser,
// contexts, tabs, profile and login state always belong to the user.
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {VirtualBrowser, VirtualBrowserError, allowedUrl} = require('./virtual_browser.cjs');
const {sameUrl} = require('./page_reader.cjs');

const fail = (code, message) => { throw new VirtualBrowserError(code, message); };
const ENABLE_MESSAGE = '尚未连接到你已打开的 Edge。请在该 Edge 中打开 edge://inspect/#remote-debugging，勾选“允许对此浏览器实例进行远程调试”，保留已登录的京东标签页，然后重试；若 Edge 弹出连接授权，请手动允许。Agent 不会另开或重启浏览器。';
const CONNECTION_TIMEOUT = 180000;

function revealUserEdge() {
  if (process.platform !== 'win32') return;
  try {
    const {spawn} = require('node:child_process');
    spawn('powershell.exe', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command',
      "$w = Get-Process msedge -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle -and $_.MainWindowTitle -notmatch '京东购物 Agent' } | Sort-Object { if ($_.MainWindowTitle -match '京东|JD\\.COM') { 0 } else { 1 } } | Select-Object -First 1; if ($w) { Add-Type -AssemblyName Microsoft.VisualBasic; [Microsoft.VisualBasic.Interaction]::AppActivate($w.Id) }"],
      {windowsHide: true, stdio: 'ignore'}).unref();
  } catch { /* Revealing Edge is best-effort; the user can still Alt+Tab. */ }
}

function connectionFailure(error) {
  const detail = String(error?.message || '');
  let message = 'Edge 已提供调试端口，但连接未成功。请确认该 Edge 仍在运行；若浏览器正在询问是否允许调试连接，请手动允许后重试。';
  if (/timeout|timed out/i.test(detail)) {
    return new VirtualBrowserError('existing_browser_authorization_timeout', '本次 Edge 连接请求等待三分钟后已超时并过期。旧弹窗中的“允许”不能授权下一次连接。请关闭旧授权弹窗，再从 Agent 点击一次连接，并在新的等待期间允许 Edge 的调试请求。');
  } else if (/ECONNREFUSED|connection refused/i.test(detail)) {
    message = 'Edge 已提供调试端口，但连接被拒绝。请确认已开启调试的 Edge 仍在运行，然后重试。';
  } else if (/\b(?:401|403)\b|not allowed|access denied|permission denied/i.test(detail)) {
    message = 'Edge 调试连接未获允许。请检查浏览器中的调试授权提示，手动允许该 Agent 连接后重试。';
  } else if (/\b(?:404|410)\b|not found/i.test(detail)) {
    message = 'Edge 调试连接地址已失效。请确认当前 Edge 调试页面仍处于开启状态，然后重新连接。';
  }
  // Raw Playwright messages contain the endpoint token and profile paths.
  // Keep them out of exceptions returned through the broker and UI.
  return new VirtualBrowserError('existing_browser_connection_failed', message);
}

class ExistingBrowser extends VirtualBrowser {
  constructor({readPortFile = (file, encoding) => fs.promises.readFile(file, encoding), revealEdge, chromiumImpl, ...options} = {}) {
    super({chromiumImpl, ...options});
    this.readPortFile = readPortFile;
    this.revealEdge = typeof revealEdge === 'function' ? revealEdge : (chromiumImpl ? () => {} : revealUserEdge);
    this.browser = null;
    this.candidates = new Map();
    this.pageIds = new WeakMap();
    this.popupListeners = new Map();
    this.selectedTitle = '';
    this.openingPromise = null;
  }

  _connected() { return !!this.browser && this.browser.isConnected(); }

  async _existingEndpoint() {
    const localAppData = process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local');
    const portFile = path.join(localAppData, 'Microsoft', 'Edge', 'User Data', 'DevToolsActivePort');
    let raw;
    try { raw = await this.readPortFile(portFile, 'utf8'); }
    catch (error) {
      if (['ENOENT', 'ENOTDIR'].includes(error?.code)) throw new VirtualBrowserError('existing_browser_not_enabled', ENABLE_MESSAGE);
      fail('existing_browser_connection_failed', '无法读取当前 Edge 的调试端口信息。请确认 Agent 与 Edge 由同一 Windows 用户正常启动，然后重试。');
    }
    if (typeof raw !== 'string' || !raw.trim() || raw.length > 1024) fail('existing_browser_connection_failed', 'Edge 调试端口信息无效，请确认当前 Edge 的远程调试已开启，然后重新连接。');
    const lines = raw.trim().split(/\r?\n/);
    const port = Number(lines[0]);
    const browserPath = lines.length === 1 ? '/devtools/browser' : lines[1];
    // The only endpoint authority is Edge's own fixed-location metadata file.
    // Never accept hostnames, query strings, fragments or arbitrary CDP paths.
    if (lines.length > 2 || !/^[1-9]\d{0,4}$/.test(lines[0]) || port > 65535 ||
        !/^\/devtools\/browser(?:\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?$/i.test(browserPath)) {
      fail('existing_browser_connection_failed', 'Edge 调试端口信息无效，请确认当前 Edge 的远程调试已开启，然后重新连接。');
    }
    // Preserve Edge's metadata route and select IPv4 explicitly. Approval mode
    // accepts both this route and /devtools/browser; a UUID is not evidence that
    // a missing UUID caused a connection failure.
    return `ws://127.0.0.1:${port}${browserPath}`;
  }

  _forgetBinding() {
    for (const [page, listener] of this.popupListeners) page.removeListener('popup', listener);
    this.popupListeners.clear();
    this.context = null;
    this.page = null;
    this.last = null;
    this.typed = null;
    this.selectedTitle = '';
    this.candidates.clear();
    this.pageIds = new WeakMap();
    this.parentPages = new WeakMap();
    this._resetTaskFlow();
  }

  _pageId(page) {
    if (!this.pageIds.has(page)) this.pageIds.set(page, crypto.randomBytes(12).toString('hex'));
    return this.pageIds.get(page);
  }

  _bindPage(page, parent = null) {
    if (parent) this.parentPages.set(page, parent);
    this.page = page;
    this.last = null;
    this.typed = null;
    this.selectedTitle = '';
    this._pageId(page);
    if (this.popupListeners.has(page)) return;
    const attachedBrowser = this.browser;
    const attachedContext = this.context;
    const listener = popup => {
      // Only descendants of the explicitly selected tab may continue the flow.
      if (this.browser === attachedBrowser && this.context === attachedContext && this.page === page && this._connected()) {
        this._boundPopup(page, popup);
      }
    };
    page.on('popup', listener);
    this.popupListeners.set(page, listener);
  }

  async _selectPage(expectedUrl) {
    if (!this._connected()) fail('existing_browser_disconnected', '与现有 Edge 的连接已断开，请重新连接并选择京东标签页。');
    if (!this.page || !this.context) fail('browser_tab_selection_required', '请先在 Agent 中选择要操作的现有京东标签页。');
    return super._selectPage(expectedUrl);
  }

  async _withActionFocus(page, expectedUrl, kind, operation) {
    if (kind === 'back' || kind === 'return_search') return operation();
    // noDefaults:true keeps every other user tab untouched. A short, separate
    // session supplies browser-local focus only for this selected page's input.
    this._assertBoundPage(page, expectedUrl);
    const before = await this._capture(page);
    if (before.status !== 'ok') fail(before.error, before.message);
    this._assertBoundPage(page, expectedUrl);
    const context = this.context;
    let session, attemptedEnable = false, operationError;
    try {
      try { session = await context.newCDPSession(page); }
      catch { fail('input_focus_unavailable', '无法为当前京东标签页建立临时输入焦点，请重新连接后继续。'); }
      this._assertBoundPage(page, expectedUrl);
      attemptedEnable = true;
      try { await session.send('Emulation.setFocusEmulationEnabled', {enabled:true}); }
      catch { fail('input_focus_unavailable', '当前京东标签页无法启用临时输入焦点，已停止输入。'); }
      // Focus events may navigate or alter the page. The operation repeats the
      // fresh DOM/URL/target/active_path checks after enabling, before any input.
      this._assertBoundPage(page, expectedUrl);
      return await operation();
    } catch (error) {
      operationError = error;
      throw error;
    } finally {
      let cleanupFailed = false;
      if (session) {
        // Always try both steps, including when enable failed after reaching
        // the renderer, navigation swapped it, or the input itself threw.
        if (attemptedEnable) {
          try { await session.send('Emulation.setFocusEmulationEnabled', {enabled:false}); }
          catch { cleanupFailed = true; }
        }
        try { await session.detach(); }
        catch { cleanupFailed = true; }
      }
      if (cleanupFailed && this._connected() && !page.isClosed()) {
        this.last = null;
        this.typed = null;
        if (operationError) operationError.focus_cleanup_failed = true;
        else fail('input_focus_cleanup_failed', '临时输入焦点清理未获确认，已停止后续动作；请重新连接后继续。');
      }
    }
  }

  _remember(snapshot) {
    this.selectedTitle = snapshot.status === 'ok' ? String(snapshot.title || '') : '';
    return super._remember(snapshot);
  }

  _selectionResult(error, tabs = []) {
    this.last = null;
    this.typed = null;
    return {status:'needs_user', error, mode:'existing', connected:this._connected(), tabs,
      message:error === 'existing_browser_no_jd_tab'
        ? '已连接到 Edge，但没有可用的京东标签页。请在你已登录的 Edge 中打开京东，再点击连接。'
        : '检测到多个京东标签页，请选择需要 Agent 操作的那个页面。',
      url:'', title:'', text:'', elements:[], selected_specs:[], screenshot:null};
  }

  async _listCandidates() {
    this.candidates.clear();
    const tabs = [];
    for (const context of this.browser.contexts()) {
      for (const page of context.pages()) {
        if (page.isClosed()) continue;
        const url = page.url();
        // Do not read titles, DOM, cookies or content of unrelated user tabs.
        if (!allowedUrl(url, this.localOrigins)) continue;
        let title;
        try { title = await page.title(); } catch { continue; }
        if (page.isClosed() || !sameUrl(page.url(), url)) continue;
        const id = this._pageId(page);
        const item = {id, title:String(title || '').slice(0, 240), url};
        this.candidates.set(id, {page, context, url});
        tabs.push(item);
      }
    }
    if (!this._connected()) fail('existing_browser_disconnected', '读取标签页时 Edge 连接已断开，请重新连接。');
    return tabs;
  }

  async _choose(pageId) {
    const candidate = this.candidates.get(pageId);
    if (!candidate || candidate.page.isClosed() || !this.browser.contexts().includes(candidate.context) ||
        !candidate.context.pages().includes(candidate.page) || !sameUrl(candidate.page.url(), candidate.url) ||
        !allowedUrl(candidate.page.url(), this.localOrigins)) {
      fail('browser_tab_selection_stale', '所选标签页不属于当前列表或已发生变化，请重新连接刷新列表后选择。');
    }
    this.context = candidate.context;
    this._resetTaskFlow(candidate.page);
    this._bindPage(candidate.page);
    this.candidates.clear();
    return this._snapshot();
  }

  open({channel = 'msedge', page_id, ...unsupported} = {}) {
    try {
      if (!['edge', 'msedge'].includes(channel)) fail('invalid_browser', '当前仅连接你已打开的 Microsoft Edge。');
      // Production callers cannot supply another endpoint, executable or profile.
      // Tests can inject the metadata reader and transport independently.
      if (Object.keys(unsupported).length) fail('invalid_browser_options', '接入现有浏览器不接受启动地址、配置目录或远程连接地址。');
      if (page_id !== undefined && (typeof page_id !== 'string' || !page_id)) fail('browser_tab_selection_stale', '请选择当前列表中的京东标签页。');
    } catch (error) { return Promise.reject(error); }
    if (this.openingPromise) {
      // A second click must not queue a fresh authorization request after the
      // first one times out. Contradictory tab selections are never merged.
      if (page_id !== undefined) return Promise.reject(new VirtualBrowserError('existing_browser_connection_in_progress', '正在等待当前 Edge 连接授权，请完成连接后再选择标签页。'));
      return this.openingPromise;
    }
    const isInitialConnection = !this._connected();
    if (isInitialConnection && page_id !== undefined) return Promise.reject(new VirtualBrowserError('browser_tab_selection_stale', '浏览器连接已变化，请重新连接刷新标签列表后选择。'));
    const operation = this._run(async () => {
      if (!this._connected()) {
        this._forgetBinding();
        this.browser = null;
        if (page_id !== undefined) fail('browser_tab_selection_stale', '浏览器连接已变化，请重新连接刷新标签列表后选择。');
        const endpoint = await this._existingEndpoint();
        let browser;
        try {
          try { this.revealEdge(); } catch { /* Best-effort only. */ }
          browser = await this.chromium.connectOverCDP(endpoint, {noDefaults:true, timeout:CONNECTION_TIMEOUT});
        } catch (error) {
          throw connectionFailure(error);
        }
        this.browser = browser;
        browser.on('disconnected', () => {
          if (this.browser !== browser) return;
          this.browser = null;
          this._forgetBinding();
        });
        if (!this._connected()) fail('existing_browser_disconnected', 'Edge 在连接时已断开，请重新连接。');
      }
      if (page_id !== undefined) return this._choose(page_id);
      // A repeated open/continue preserves the chosen tab and current URL.
      if (this.page && (!this.page.isClosed() || this.parentPages.has(this.page))) return this._snapshot();
      this.context = null;
      this.page = null;
      const tabs = await this._listCandidates();
      if (tabs.length === 1) return this._choose(tabs[0].id);
      return this._selectionResult(tabs.length ? 'browser_tab_selection_required' : 'existing_browser_no_jd_tab', tabs);
    });
    if (!isInitialConnection) return operation;
    const flight = operation.then(result => {
      if (this.openingPromise === flight) this.openingPromise = null;
      return result;
    }, error => {
      if (this.openingPromise === flight) this.openingPromise = null;
      throw error;
    });
    this.openingPromise = flight;
    return flight;
  }

  async _unselectedTabs() {
    if (!this._connected()) fail('existing_browser_disconnected', '与现有 Edge 的连接已断开，请重新连接并选择京东标签页。');
    if (this.page && this.context) return null;
    const tabs = await this._listCandidates();
    return this._selectionResult(tabs.length ? 'browser_tab_selection_required' : 'existing_browser_no_jd_tab', tabs);
  }

  show() {
    return this._run(async () => {
      const pending = await this._unselectedTabs();
      if (pending) return pending;
      const page = await this._selectPage();
      await page.bringToFront();
      return this._snapshot();
    });
  }

  snapshot(options = {}) {
    return this._run(async () => {
      const pending = await this._unselectedTabs();
      if (pending) return pending;
      return this._snapshot(options.expected_url);
    });
  }

  status() {
    const connected = this._connected();
    const page = connected && this.page && !this.page.isClosed() ? this.page : null;
    const url = page && allowedUrl(page.url(), this.localOrigins) ? page.url() : '';
    return {mode:'existing', connected, connecting:!!this.openingPromise, closed:!connected, page_id:page ? this._pageId(page) : null,
      url, title:url && this.last && sameUrl(this.last.url, url) ? this.selectedTitle : ''};
  }

  close() {
    return this._run(async () => {
      const attachedBrowser = this.browser;
      this.browser = null;
      this._forgetBinding();
      // Playwright connectOverCDP's Browser.close() closes its CDP transport.
      // Disconnect does not close any pages or contexts. During a task only
      // proven action-owned, unchanged result/detail popups may be retired.
      // See pinned 1.62.1 Chromium._connectOverCDPImpl: browserProcess.close
      // is closeAndWait() on the WebSocket, not a browser termination command.
      if (attachedBrowser?.isConnected()) await attachedBrowser.close();
      return {status:'ok', mode:'existing', connected:false};
    });
  }
}

module.exports = {ExistingBrowser};
