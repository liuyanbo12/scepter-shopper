'use strict';

// Browser-local mouse/keyboard only. No Windows focus API, account/profile
// copying, arbitrary selectors, model JavaScript, DOM click(), or fill().
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {chromium} = require('playwright');
const {sameUrl} = require('./page_reader.cjs');
const SNAPSHOT = fs.readFileSync(path.join(__dirname, '../shopper/dom_snapshot.js'), 'utf8');

const BLOCKED = /立即购买|马上购买|去结算|立即结算|提交订单|确认订单|确认付款|立即支付|去支付|支付订单|货到付款|删除|清空|注销|退出登录|修改密码|账号设置|账户设置|收货地址|联系客服|在线客服|联系卖家|发送消息|确认收货|取消订单|申请退款|退换货|\b(?:checkout|pay\s*now|place\s*order|buy\s*now|delete|sign\s*out)\b/i;
const BLOCKED_EXACT = new Set(['结算', '支付', '付款', '购买', '下单', '+', '-', '＋', '－', '增加', '减少']);
const ADD = /加入购物车|加购物车|加购|加入购物袋|add\s*to\s*cart/i;
const SENSITIVE = /密码|验证码|短信|手机号|手机号码|身份证|银行卡|password|verification|otp|邮箱|email/i;
const CAPTCHA = /拖动滑块|拖动.*拼图|请完成.*验证|安全验证|访问过于频繁|请验证您是|点击.*完成验证|人机验证|captcha/i;
const LOGIN = /扫码登录|扫描二维码登录|短信登录|账户登录|账号密码登录|密码登录|登录京东|京东登录/;
const JD_AUTH_TITLE = /^(?:京东验证|京东安全验证|京东身份验证)(?:\s*[-—|_].*)?$/;
const KEYS = {enter:'Enter', esc:'Escape', escape:'Escape', tab:'Tab', backspace:'Backspace', left:'ArrowLeft', right:'ArrowRight', up:'ArrowUp', down:'ArrowDown', home:'Home', end:'End', pageup:'PageUp', pagedown:'PageDown'};

class VirtualBrowserError extends Error {
  constructor(code, message, needsUser = true) {
    super(message);
    this.name = 'VirtualBrowserError';
    this.code = code;
    this.needs_user = needsUser;
  }
}
const fail = (code, message) => { throw new VirtualBrowserError(code, message); };
const bounded = async (promise, ms = 6000) => {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new VirtualBrowserError('browser_timeout', '浏览器页面响应超时，请等待加载后继续。')), ms);
      timer.unref?.();
    })]);
  } finally { clearTimeout(timer); }
};
const compact = value => String(value || '').replace(/\s+/g, '').trim();
const blocked = name => BLOCKED_EXACT.has(compact(name)) || BLOCKED.test(String(name || ''));

function allowedUrl(raw, localOrigins = new Set()) {
  try {
    const url = new URL(raw);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return false;
    if (url.hostname === 'jd.com' || url.hostname.endsWith('.jd.com')) {
      return !url.port || ['80', '443'].includes(url.port);
    }
    return url.protocol === 'http:' && url.hostname === '127.0.0.1' && localOrigins.has(url.origin);
  } catch { return false; }
}
function protectedUrl(raw) {
  const url = new URL(raw);
  if (['passport.jd.com', 'plogin.m.jd.com', 'login.jd.com', 'auth.jd.com'].includes(url.hostname)) return 'authentication_required';
  if (url.hostname === 'cfe.m.jd.com' && /^\/privatedomain\/risk_handler(?:\/|$)/.test(url.pathname)) return 'authentication_required';
  if (/^(?:cashier|pay|trade|order)\./i.test(url.hostname) || /\/(?:checkout|pay|payment)(?:\/|$)/i.test(url.pathname)) return 'protected_page';
  return null;
}
function skuFromUrl(raw) {
  try {
    const url = new URL(raw);
    return url.hostname === 'item.jd.com' ? (url.pathname.match(/^\/(\d+)\.html$/)?.[1] || '') : '';
  } catch { return ''; }
}
function searchUrl(raw) {
  try { const url = new URL(raw); return url.hostname === 'search.jd.com' && /^\/search\/?$/i.test(url.pathname); }
  catch { return false; }
}
function productShell(snapshot) {
  if (snapshot.status !== 'ok' || !skuFromUrl(snapshot.url)) return false;
  const title = String(snapshot.title || '').trim();
  // JD can commit the product URL and DOMContentLoaded before its product
  // components render, leaving this generic homepage title and a tiny shell.
  // A real product title with sparse text (e.g. after scrolling) is not a shell.
  const genericTitle = !title || /^京东(?:\s*[（(]JD\.COM[）)])?\s*[-—|]\s*正品低价.*品质保障/i.test(title);
  return genericTitle && String(snapshot.text || '').trim().length < 800 && (snapshot.elements || []).length < 30;
}
function validateLocalOrigins(values) {
  const result = new Set();
  for (const value of values || []) {
    const url = new URL(value);
    if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || url.username || url.password || url.pathname !== '/' || url.search || url.hash) {
      fail('invalid_fixture_origin', '测试页面必须使用显式的 127.0.0.1 HTTP 来源。');
    }
    result.add(url.origin);
  }
  return result;
}

class VirtualBrowser {
  constructor({root, allowed_local_origins = [], chromiumImpl = chromium} = {}) {
    if (!root) throw new TypeError('root is required');
    this.root = path.resolve(root);
    this.localOrigins = validateLocalOrigins(allowed_local_origins);
    this.chromium = chromiumImpl;
    this.context = null;
    this.page = null;
    this.parentPages = new WeakMap();
    this.observedPages = new WeakSet();
    this.profile = null;
    this.last = null;
    this.typed = null;
    this.counter = 0;
    this.session = crypto.randomBytes(8).toString('hex');
    this.pending = Promise.resolve();
    this._resetTaskFlow();
  }

  _resetTaskFlow(page = null) {
    this.searchPage = page;
    this.searchPageUrl = page && searchUrl(page.url()) ? page.url() : '';
    this.searchPageLastUrl = page ? page.url() : '';
    this.detailPage = null;
    this.taskOwnedPages = new WeakMap();
    this.taskAction = null;
    this.sameTabDetail = false;
  }

  _taskNavigation() {
    const main = this.searchPage;
    const attached = !!(main && !main.isClosed() && this.context?.pages().includes(main) && this.searchPageUrl);
    const onSavedSearch = attached && sameUrl(main.url(), this.searchPageUrl);
    // Opening a product by URL reuses the search tab. That is still the saved
    // results page; return_to_search goes back instead of asking the user.
    const onSameTabDetail = attached && this.page === main && Boolean(skuFromUrl(main.url()));
    const available = Boolean(onSavedSearch || onSameTabDetail);
    return {has_search_page:available, can_return_search:available && (this.page !== main || !sameUrl(main.url(), this.searchPageUrl)),
      detail_sku:this.page ? skuFromUrl(this.page.url()) : ''};
  }

  _boundPopup(source, popup) {
    const action = this.taskAction;
    if (action && action.source === source && !action.before.has(popup)) {
      // Ownership comes from the selected page's popup event during our input,
      // never from enumerating existing user tabs or matching their URLs.
      this.taskOwnedPages.set(popup, {url:null});
      action.popups.add(popup);
    }
    this._bindPage(popup, source);
  }

  _run(operation) {
    const result = this.pending.then(operation);
    this.pending = result.catch(() => {});
    return result;
  }

  _validateProfile(profileDir, channel) {
    if (!['edge', 'msedge', 'chrome'].includes(channel)) fail('invalid_browser', '仅支持 Edge 或 Chrome。');
    const profile = path.resolve(profileDir || '');
    const expected = path.join(this.root, 'data', 'browser-profile', channel === 'chrome' ? 'virtual-chrome' : 'virtual-edge');
    const normalize = value => process.platform === 'win32' ? value.toLowerCase() : value;
    if (normalize(profile) !== normalize(expected)) fail('profile_not_allowed', '虚拟浏览器只能使用项目内专用的 virtual-edge/virtual-chrome 配置目录。');
    let existing = profile;
    while (!fs.existsSync(existing) && path.dirname(existing) !== existing) existing = path.dirname(existing);
    const realRoot = fs.realpathSync(this.root);
    const realExisting = fs.realpathSync(existing);
    const relative = path.relative(realRoot, realExisting);
    if (relative.startsWith('..' + path.sep) || relative === '..' || path.isAbsolute(relative)) fail('profile_not_allowed', '浏览器配置目录不能通过链接指向项目外。');
    return profile;
  }

  _requireUrl(url, {manualLogin = false} = {}) {
    if (!allowedUrl(url, this.localOrigins)) fail('outside_allowed_site', '只允许京东和显式登记的本地测试页面。');
    const gate = protectedUrl(url);
    if (gate && !(manualLogin && gate === 'authentication_required')) fail(gate, gate === 'authentication_required' ? '登录需要你在浏览器中手动完成。' : '自动操作止于购物车，不进入订单、结算或支付页面。');
    return new URL(url).href;
  }

  _assertBoundPage(page, expectedUrl) {
    if (this.page !== page || page.isClosed() || !sameUrl(page.url(), expectedUrl)) fail('page_changed', '输入前页面已切换，请重新观察。');
  }

  _bindPage(page, parent = null) {
    if (parent) this.parentPages.set(page, parent);
    this.page = page;
    this.last = null;
    this.typed = null;
    if (this.observedPages.has(page)) return;
    this.observedPages.add(page);
    // Only a popup emitted by our currently bound page may continue this
    // browsing flow. An unrelated user-opened tab never becomes an input target.
    page.on('popup', popup => {
      if (this.page === page && this.context) this._boundPopup(page, popup);
    });
  }

  async _selectPage(expectedUrl) {
    if (!this.context) fail('browser_not_open', '请先打开虚拟京东浏览器。');
    while (this.page?.isClosed()) {
      const parent = this.parentPages.get(this.page);
      if (!parent) { this.page = null; break; }
      this._bindPage(parent);
    }
    if (!this.page || !this.context.pages().includes(this.page)) fail('browser_not_open', '执行器绑定的京东标签已关闭，请重新打开。');
    if (expectedUrl && !sameUrl(this.page.url(), expectedUrl)) fail('page_changed', '当前网址与上次观察不一致，请重新观察。');
    return this.page;
  }

  _protectedSnapshot(url, error) {
    return {status:'needs_user', error, url, title:'', text:'', elements:[], selected_specs:[],
      message:error === 'authentication_required' ? '请在此浏览器中手动登录或完成验证，然后点击继续。' :
        error === 'site_error' ? '京东页面显示当前页面异常，请先确认该标签页能正常显示商品后继续。' :
        error === 'outside_allowed_site' ? '页面已离开京东，请返回京东后继续。' : '自动操作止于购物车，当前页面需要你手动处理。'};
  }

  async _capture(page, timeout = 6000) {
    const url = page.url();
    if (!allowedUrl(url, this.localOrigins)) return this._protectedSnapshot(url, 'outside_allowed_site');
    const gate = protectedUrl(url);
    if (gate) return this._protectedSnapshot(url, gate);
    let snapshot;
    try { snapshot = await bounded(page.evaluate(SNAPSHOT), timeout); }
    catch (error) { throw new VirtualBrowserError('dom_unavailable', `页面结构暂不可读：${String(error.message).slice(0, 180)}`); }
    if (!snapshot || !sameUrl(snapshot.url, url) || !sameUrl(page.url(), url)) fail('page_changed', '读取过程中页面发生跳转，请重新观察。');
    if (CAPTCHA.test(snapshot.text) || JD_AUTH_TITLE.test(String(snapshot.title || '').trim()) || LOGIN.test(snapshot.title) || (LOGIN.test(snapshot.text) && /二维码|密码/.test(snapshot.text))) {
      return this._protectedSnapshot(url, 'authentication_required');
    }
    const pageText = `${snapshot.title || ''}\n${snapshot.text || ''}`;
    if (pageText.includes('当前页面异常') && pageText.includes('请刷新或切换账户试试')) {
      return this._protectedSnapshot(url, 'site_error');
    }
    return {...snapshot, status:'ok'};
  }

  _remember(snapshot) {
    if (this.page === this.searchPage && snapshot.status === 'ok') this.searchPageLastUrl = snapshot.url;
    if (this.page === this.searchPage && searchUrl(snapshot.url) && snapshot.status === 'ok') {
      this.searchPageUrl = snapshot.url;
      this.sameTabDetail = false;
    }
    const observation = {...snapshot, obs_id:`${this.session}-${++this.counter}`, source:'dom', input_method:'browser_virtual_mouse', screenshot:null,
      navigation:this._taskNavigation(),
      elements:(snapshot.elements || []).map((element, index) => ({...element, id:`e${index + 1}`}))};
    // Never retain the caller's mutable copy as authority for a later click.
    this.last = structuredClone(observation);
    return observation;
  }

  async _snapshot(expectedUrl, deadline = Date.now() + 8000) {
    let lastError, navigationFailures = 0;
    while (Date.now() < deadline) {
      // Every attempt resolves the same bound page flow again. An explicit
      // expected URL is never relaxed just because navigation is in progress.
      let page = await this._selectPage(expectedUrl);
      if (page.url() === 'about:blank' && this.parentPages.has(page)) {
        try { await page.waitForURL(url => url.href !== 'about:blank', {timeout:Math.max(1, Math.min(5000, deadline - Date.now())), waitUntil:'domcontentloaded'}); }
        catch { /* A still-empty popup is returned as an unreadable page. */ }
        page = await this._selectPage(expectedUrl);
      }
      if (Date.now() >= deadline) throw lastError || new VirtualBrowserError('browser_timeout', '页面导航仍未完成，请等待加载后重新观察。');
      try {
        // _capture rechecks the URL allowlist and authentication gate before
        // reading any DOM on each attempt, including after redirects.
        const snapshot = await this._capture(page, Math.min(6000, deadline - Date.now()));
        if (page !== this.page) fail('page_changed', '读取过程中标签页发生切换，请重新观察。');
        if (expectedUrl && !sameUrl(snapshot.url, expectedUrl)) fail('page_changed', '当前网址与指定观察网址不一致，请重新观察。');
        if (productShell(snapshot)) {
          this.last = null;
          this.typed = null;
          lastError = new VirtualBrowserError('page_loading', '商品详情组件仍在加载，尚未读到商品信息。请等待页面显示完整后继续；不要重复搜索或点击商品。');
          // Re-read only the bound page. The next iteration rechecks explicit
          // URL expectations, allowed origins and authentication before DOM.
          await new Promise(resolve => setTimeout(resolve, Math.max(0, Math.min(350, deadline - Date.now()))));
          continue;
        }
        return this._remember(snapshot);
      } catch (error) {
        const navigationInterrupted = error.code === 'page_changed' || (error.code === 'dom_unavailable' &&
          /execution context was destroyed|cannot find context with (?:specified )?id|inspected target navigated|because of a navigation/i.test(error.message));
        if (!navigationInterrupted || ++navigationFailures >= 3 || Date.now() >= deadline) throw error;
        lastError = error;
        this.last = null;
        const current = await this._selectPage(expectedUrl);
        if (allowedUrl(current.url(), this.localOrigins) && !protectedUrl(current.url())) {
          // This only waits for the already-triggered navigation. No keyboard,
          // click, goto, refresh or other input is replayed here.
          try { await current.waitForLoadState('domcontentloaded', {timeout:Math.max(1, Math.min(1200, deadline - Date.now()))}); }
          catch { /* Bounded retry below also handles another redirect. */ }
          await new Promise(resolve => setTimeout(resolve, Math.max(0, Math.min(100, deadline - Date.now()))));
        }
      }
    }
    throw lastError || new VirtualBrowserError('browser_timeout', '页面导航仍未完成，请等待加载后重新观察。');
  }

  open({profile_dir, channel = 'msedge', executable, start_url = 'https://www.jd.com/'} = {}) {
    return this._run(async () => {
      const profile = this._validateProfile(profile_dir, channel);
      if (this.context) {
        if (this.profile !== profile) fail('profile_in_use', '当前虚拟浏览器已使用另一专用配置，请先关闭它。');
        // Keep the user's current product, cart, or manual login flow intact.
        return this._snapshot();
      }
      const href = this._requireUrl(start_url, {manualLogin:true});
      const options = {headless:false, chromiumSandbox:true, viewport:null, timeout:25000,
        args:['--no-first-run', '--no-default-browser-check'],
        env:{...process.env, TEMP:path.join(this.root, 'data', 'virtual-browser-temp'), TMP:path.join(this.root, 'data', 'virtual-browser-temp')}};
      fs.mkdirSync(options.env.TEMP, {recursive:true});
      if (executable) {
        const base = path.basename(executable).toLowerCase();
        if (base !== (channel === 'chrome' ? 'chrome.exe' : 'msedge.exe') || !fs.existsSync(executable)) fail('invalid_browser', '浏览器程序路径无效。');
        options.executablePath = path.resolve(executable);
      } else options.channel = channel === 'edge' ? 'msedge' : channel;
      this.context = await this.chromium.launchPersistentContext(profile, options);
      this.profile = profile;
      const ownContext = this.context;
      ownContext.on('close', () => {
        if (this.context === ownContext) { this.context = null; this.page = null; this.last = null; this.typed = null; }
      });
      const page = ownContext.pages()[0] || await ownContext.newPage();
      this._resetTaskFlow(page);
      this._bindPage(page);
      await page.goto(href, {waitUntil:'domcontentloaded', timeout:20000});
      await page.bringToFront();
      return this._snapshot();
    });
  }

  snapshot({expected_url} = {}) { return this._run(() => this._snapshot(expected_url)); }
  show() { return this._run(async () => { const page = await this._selectPage(); await page.bringToFront(); return this._snapshot(); }); }
  focus() { return this.show(); }
  status() { return {closed:!this.context || !this.context.pages().some(page => !page.isClosed()), mode:'virtual', profile_dir:this.profile}; }
  isclosed() { return this.status().closed; }

  navigate({url} = {}) {
    return this._run(async () => {
      const href = this._requireUrl(url);
      let page = await this._selectPage();
      const leavingSearch = !!(this.searchPage && page === this.searchPage
        && (searchUrl(page.url()) || this.sameTabDetail && skuFromUrl(page.url())));
      if (searchUrl(href) && this.searchPage && !this.searchPage.isClosed() && this.context.pages().includes(this.searchPage)) {
        if (!sameUrl(this.searchPage.url(), this.searchPageLastUrl)) fail('search_page_unavailable', '主搜索标签页已被切换到其他网址，已停止覆盖该页面。');
        const current = await this._capture(this.searchPage);
        if (current.status !== 'ok') fail(current.error, current.message);
        if (!sameUrl(this.searchPage.url(), this.searchPageLastUrl)) fail('search_page_unavailable', '检查期间主搜索标签页已发生变化，已停止导航。');
        page = this.searchPage;
        this._bindPage(page);
        await this._activateBoundPage(page, this.searchPageLastUrl);
      }
      this.last = null; this.typed = null;
      if (!sameUrl(page.url(), href)) await page.goto(href, {waitUntil:'domcontentloaded', timeout:20000});
      if (leavingSearch && skuFromUrl(href) && page === this.searchPage) this.sameTabDetail = true;
      return this._snapshot();
    });
  }

  _freshTarget(entry, snapshot, request, forType = false) {
    const target = snapshot.elements.find(element => element.dom_path === entry.dom_path);
    if (!target) fail('element_stale', '目标控件已消失，请重新观察。');
    const a = entry.css_rect, b = target.css_rect;
    if (target.name !== entry.name || target.role !== entry.role || (target.href || '') !== (entry.href || '') ||
        target.click_owner_name !== entry.click_owner_name || target.input_purpose !== entry.input_purpose || !Array.isArray(a) || !Array.isArray(b) || a.length !== 4 || b.length !== 4 ||
        b.some((value, index) => !Number.isFinite(value) || Math.abs(value - a[index]) > 4)) fail('element_changed', '目标名称、链接或位置已变化，请重新观察。');
    if (!target.enabled || !target.visible) fail('element_unavailable', '目标控件当前不可操作。');
    if (target.occluded !== false) fail('target_occluded', '目标被页面弹层遮挡，请处理弹层后继续。');
    if (blocked(target.name) || blocked(target.click_owner_name)) fail('blocked_action', '此控件涉及下单、支付、删除、账户变更或对外沟通，执行器拒绝操作。');
    if (target.href) this._requireUrl(target.href);
    if (new URL(snapshot.url).hostname === 'cart.jd.com') fail('cart_read_only', '购物车仅允许读取和滚动，不修改原有商品。');
    const label = `${target.name || ''} ${target.click_owner_name || ''}`;
    const sku = skuFromUrl(snapshot.url);
    const approvedQuantity = request.approved_quantity ?? 1;
    const validQuantity = Number.isInteger(approvedQuantity) && approvedQuantity >= 1 && approvedQuantity <= 20;
    const approvedAdd = request.allow_add === true && /^\d+$/.test(String(request.approved_sku || '')) && sku === String(request.approved_sku) && validQuantity;
    if (ADD.test(label) && !approvedAdd) {
      fail('add_not_approved', '该商品尚未获得加购授权，或当前 SKU 与批准商品不一致。');
    }
    if (target.href) {
      const href = new URL(target.href);
      if (href.hostname === 'cart.jd.com' && href.pathname === '/gate.action') {
        const keys = [...href.searchParams.keys()];
        const valid = approvedAdd && ADD.test(label) && href.protocol === 'https:' && !href.port && !href.hash &&
          keys.length === 3 && new Set(keys).size === 3 && keys.every(key => ['pid', 'pcount', 'ptype'].includes(key)) &&
          href.searchParams.get('pid') === sku && href.searchParams.get('pcount') === String(approvedQuantity) && href.searchParams.get('ptype') === '1';
        if (!valid) fail('add_link_mismatch', '加购链接的商品、数量或参数与批准方案不一致。');
      } else if (/(?:add|delete|remove|submit|checkout)[^/]*\.(?:action|html)/i.test(href.pathname)) {
        fail('link_not_allowed', '该链接可能直接修改购物车或提交交易，执行器拒绝操作。');
      }
    }
    if (forType) {
      if (!['Edit', 'ComboBox'].includes(target.role) || SENSITIVE.test(target.name) || /地址|address|location/i.test(target.name)) fail('not_safe_input', '只可向已识别的搜索框或已批准商品数量框输入。');
      const search = /搜索|search|关键词/i.test(`${target.name} ${target.value || ''}`);
      const quantity = request.allow_quantity === true && !!sku && sku === String(request.approved_sku || '') && validQuantity &&
        (target.input_purpose === 'quantity' || /数量|quantity|qty|件数/i.test(target.name)) && /^\d{1,2}$/.test(String(request.text || '')) && Number(request.text) === approvedQuantity;
      if (!search && !quantity) fail('not_safe_input', '只可输入搜索词，或设置已批准商品的购买数量。');
    }
    const x = (b[0] + b[2]) / 2, y = (b[1] + b[3]) / 2;
    if (x < 0 || y < 0 || x >= snapshot.viewport.width || y >= snapshot.viewport.height) fail('target_outside_viewport', '目标不在当前页面可见区域。');
    return {...target, x, y};
  }

  async _withActionFocus(page, expectedUrl, kind, operation) {
    return operation();
  }

  async _activateBoundPage(page, expectedUrl) {
    this._assertBoundPage(page, expectedUrl);
    const before = await this._capture(page);
    if (before.status !== 'ok') fail(before.error, before.message);
    this._assertBoundPage(page, expectedUrl);
    // Selecting a browser tab is required for wheel input to reach its page.
    // This is browser-local activation; no Windows foreground API is involved.
    await page.bringToFront();
    // Activation can trigger focus handlers or a popup. Never continue with
    // coordinates/navigation from the old binding if either changed meanwhile.
    this._assertBoundPage(page, expectedUrl);
    const after = await this._capture(page);
    if (after.status !== 'ok') fail(after.error, after.message);
    this._assertBoundPage(page, expectedUrl);
  }

  async _restoreSearchTab(main, expected) {
    const landed = () => searchUrl(main.url());
    try {
      // JD search often keeps loading after history commit. Waiting for
      // domcontentloaded surfaces a Playwright timeout even when the results
      // URL is already back.
      await main.goBack({waitUntil:'commit', timeout:8000});
    } catch {
      if (!landed() && expected && searchUrl(expected)) {
        try { await main.goto(expected, {waitUntil:'commit', timeout:12000}); }
        catch { /* URL is checked below. */ }
      }
    }
    if (!landed() && expected && searchUrl(expected) && !sameUrl(main.url(), expected)) {
      try { await main.goto(expected, {waitUntil:'commit', timeout:12000}); }
      catch { /* URL is checked below. */ }
    }
    if (!landed()) fail('search_page_unavailable', '未能回到搜索结果页，将重新搜索当前类别。');
    this.searchPageUrl = main.url();
    this.searchPageLastUrl = main.url();
    this.sameTabDetail = false;
  }

  async _returnSearch() {
    const state = this._taskNavigation();
    const main = this.searchPage, expected = this.searchPageUrl;
    const sameTabProduct = !!(main && this.page === main && expected && skuFromUrl(main.url()) && !sameUrl(main.url(), expected));
    if (!state.has_search_page && !sameTabProduct) fail('search_page_unavailable', '保存的搜索结果页已关闭或发生变化，请重新选择搜索页面。');
    if (sameTabProduct || (this.page === main && this.sameTabDetail && !sameUrl(main.url(), expected))) {
      this.last = null; this.typed = null;
      await this._restoreSearchTab(main, expected);
    }
    this._bindPage(main);
    await this._activateBoundPage(main, main.url());
  }

  async _closeOwnedTaskPage(page) {
    const ownership = this.taskOwnedPages.get(page);
    if (!ownership?.url || page === this.searchPage) return false;
    if (page.isClosed()) { this.taskOwnedPages.delete(page); return true; }
    if (!this.context?.pages().includes(page) || !sameUrl(page.url(), ownership.url)) {
      // A page navigated independently is no longer safe to retire as ours.
      this.taskOwnedPages.delete(page);
      return false;
    }
    const fresh = await this._capture(page);
    if (fresh.status !== 'ok' || !sameUrl(page.url(), ownership.url)) {
      this.taskOwnedPages.delete(page);
      return false;
    }
    await page.close({runBeforeUnload:false});
    this.taskOwnedPages.delete(page);
    return true;
  }

  async _reuseDetail(href) {
    const sku = skuFromUrl(href), detail = this.detailPage;
    if (!sku || !detail || detail.isClosed() || !this.context?.pages().includes(detail)) return false;
    const ownership = this.taskOwnedPages.get(detail);
    if (!ownership?.url || !sameUrl(detail.url(), ownership.url) || skuFromUrl(detail.url()) !== sku) return false;
    const fresh = await this._capture(detail);
    if (fresh.status !== 'ok') fail(fresh.error, fresh.message);
    if (!sameUrl(detail.url(), ownership.url)) return false;
    this._bindPage(detail);
    await this._activateBoundPage(detail, ownership.url);
    return true;
  }

  async _finishTaskNavigation(snapshot, sourcePage, sourceUrl) {
    if (snapshot.status !== 'ok') return snapshot;
    const page = this.page;
    if (page === this.searchPage && searchUrl(sourceUrl) && skuFromUrl(snapshot.url)) {
      this.sameTabDetail = true;
      return this._remember(snapshot);
    }
    if (!this.taskAction?.popups.has(page) || !this.taskOwnedPages.has(page)) return snapshot;
    this.taskOwnedPages.set(page, {url:snapshot.url});
    if (searchUrl(snapshot.url) && this.searchPage && page !== this.searchPage && !this.searchPage.isClosed()) {
      const main = this.searchPage;
      if (!sameUrl(main.url(), this.searchPageLastUrl)) fail('search_page_unavailable', '主搜索标签页已被切换到其他网址，已保留新结果页并停止覆盖原页面。');
      const before = await this._capture(main);
      if (before.status !== 'ok') fail(before.error, before.message);
      if (!sameUrl(main.url(), this.searchPageLastUrl)) fail('search_page_unavailable', '检查期间主搜索标签页已发生变化，已停止导航。');
      this._requireUrl(snapshot.url);
      this._bindPage(main);
      await this._activateBoundPage(main, this.searchPageLastUrl);
      // JD search sometimes uses window.open. Reuse the designated main page,
      // then retire only that newly observed, action-owned result popup.
      if (!sameUrl(main.url(), snapshot.url)) await main.goto(snapshot.url, {waitUntil:'domcontentloaded', timeout:20000});
      const result = await this._snapshot();
      if (result.status !== 'ok') return result;
      await this._closeOwnedTaskPage(page);
      return result;
    }
    if (skuFromUrl(snapshot.url)) {
      const previous = this.detailPage;
      if (previous && previous !== page && await this._reuseDetail(snapshot.url)) {
        await this._closeOwnedTaskPage(page);
        return this._snapshot();
      }
      if (previous && previous !== page) await this._closeOwnedTaskPage(previous);
      this.detailPage = page;
      return this._remember(snapshot);
    }
    return snapshot;
  }

  async _snapshotAfterAction(sourcePage, sourceUrl, mayNavigate) {
    // Enter/window.open and SPA navigation can start after the input command
    // has acknowledged. Do not immediately hand the old document to the model.
    // Follow only our bound page flow, and never retry any input while waiting.
    const started = Date.now(), deadline = started + 8000;
    let observedPage = sourcePage, observedUrl = sourceUrl, changedAt = started;
    let navigationStarted = false;
    while (Date.now() < deadline) {
      const page = await this._selectPage();
      const url = page.url();
      if (page !== observedPage || !sameUrl(url, observedUrl)) {
        observedPage = page;
        observedUrl = url;
        changedAt = Date.now();
        navigationStarted = true;
        this.last = null;
        this.typed = null;
      }
      const now = Date.now();
      // A quick navigation needs only its quiet period; an unchanged page gets
      // a bounded grace period for a delayed navigation or popup to appear.
      const ready = now - changedAt >= (mayNavigate ? 300 : 180) &&
        (!mayNavigate || navigationStarted || now - started >= 1500);
      if (ready) {
        const snapshot = await this._snapshot(undefined, deadline);
        if (this.page === page && sameUrl(snapshot.url, url) && snapshot.ready_state !== 'loading') return snapshot;
        // _snapshot may itself have followed a redirect/popup. Discard that
        // intermediate observation and require the resulting page to settle.
        this.last = null;
      }
      await new Promise(resolve => setTimeout(resolve, Math.max(0, Math.min(100, deadline - Date.now()))));
    }
    this.last = null;
    this.typed = null;
    fail('page_changed', '动作已发送，但页面仍在跳转或加载，请重新观察当前页面；不要重复刚才的操作。');
  }

  _verifyTypedFocus(fresh, request = {}) {
    if (!this.typed || !sameUrl(this.typed.url, fresh.url) || !fresh.active_edit || fresh.active_path !== this.typed.entry.dom_path) fail('unsafe_key_focus', '此按键仅可用于刚输入且仍获得焦点的普通网页输入框。');
    if (request.element_id && this.last?.elements.find(element => element.id === request.element_id)?.dom_path !== this.typed.entry.dom_path) fail('unsafe_key_focus', '按键目标与刚输入的控件不一致。');
    const edit = this._freshTarget(this.typed.entry, fresh, this.typed.request, true);
    if (edit.value !== this.typed.value) fail('element_changed', '输入框内容已被改变，请重新观察并输入。');
    return edit;
  }

  act(request = {}) {
    return this._run(async () => {
      const kind = request.kind || request.action;
      if (!['click', 'type', 'press', 'scroll', 'back', 'return_search'].includes(kind)) fail('invalid_action', '不支持此浏览器动作。');
      const submitSearch = request.submit_search === true;
      if ((request.submit_search !== undefined && typeof request.submit_search !== 'boolean') ||
          (submitSearch && (kind !== 'type' || request.clear === false || request.allow_add === true || request.allow_quantity === true))) {
        fail('invalid_search_transaction', '直接搜索只允许替换已识别搜索框的内容并提交，不适用于数量或加购操作。');
      }
      if (!this.last || request.obs_id !== this.last.obs_id) fail('stale_observation', '页面观察编号已过期，请重新观察。');
      if (!request.expected_url || !sameUrl(request.expected_url, this.last.url)) fail('page_changed', '动作网址与上次观察不一致。');
      if (kind !== 'back' && this.last.status !== 'ok') fail(this.last.error || 'page_needs_user', this.last.message || '当前页面需要手动处理。');
      const page = await this._selectPage(request.expected_url);
      try {
      await this._withActionFocus(page, request.expected_url, kind, async () => {
        this._assertBoundPage(page, request.expected_url);
        const fresh = await this._capture(page);
        this._assertBoundPage(page, request.expected_url);
        if (kind !== 'back' && fresh.status !== 'ok') fail(fresh.error, fresh.message);
        let target;
        if (['click', 'type'].includes(kind)) {
          const entry = this.last.elements.find(element => element.id === request.element_id);
          if (!entry) fail('unknown_element', '控件不属于最新观察，不能点击。');
          target = this._freshTarget(entry, fresh, request, kind === 'type');
        }
        if (submitSearch && (target.input_purpose === 'quantity' || !/搜索|search|关键词/i.test(target.name) || /数量|quantity|qty|件数/i.test(target.name))) {
          fail('invalid_search_transaction', '直接搜索仅适用于名称明确的商品搜索框。');
        }
        if (kind === 'type' && (typeof request.text !== 'string' || request.text.length > 400 || /[\x00-\x1f\x7f]/.test(request.text))) fail('invalid_text', '输入最多 400 字且不能包含控制字符。');
        let key;
        if (kind === 'press') {
          key = String(request.key || '').toLowerCase();
          if (!Object.hasOwn(KEYS, key)) fail('invalid_key', '此按键不在允许列表中。');
          if (!['esc', 'escape', 'pageup', 'pagedown'].includes(key)) {
            this._verifyTypedFocus(fresh, request);
          }
        }
        if (kind === 'scroll' && (!Number.isInteger(request.amount) || request.amount === 0 || Math.abs(request.amount) > 12)) fail('invalid_scroll', '滚动量必须为 -12 至 12 之间的非零整数。');

        // Consume the observation before any input, so a timeout cannot replay a
        // potentially successful add-to-cart click using the same observation.
        this._assertBoundPage(page, fresh.url);
        this.last = null;
        this.taskAction = {source:page, before:new Set(this.context.pages()), popups:new Set()};
        if (kind === 'click') {
          this.typed = null;
          if (!target.href || !await this._reuseDetail(target.href)) await page.mouse.click(target.x, target.y);
        } else if (kind === 'type') {
          this.typed = null;
          await page.mouse.click(target.x, target.y);
          const focused = await this._capture(page);
          if (focused.status !== 'ok') fail(focused.error, focused.message);
          if (!focused.active_edit || focused.active_path !== target.dom_path) fail('input_focus_unverified', '点击后未能确认输入框焦点，已停止输入。');
          this._assertBoundPage(page, fresh.url);
          if (request.clear !== false) await page.keyboard.press('Control+A');
          this._assertBoundPage(page, fresh.url);
          await page.keyboard.insertText(request.text);
          const typed = await this._capture(page);
          if (typed.status !== 'ok') fail(typed.error, typed.message);
          const actual = typed.elements.find(element => element.dom_path === target.dom_path);
          if (!actual || !typed.active_edit || typed.active_path !== target.dom_path) fail('input_focus_unverified', '输入后页面焦点已变化，请重新观察。');
          if (submitSearch) {
            this._freshTarget(target, typed, request, true);
            if (actual.value !== request.text) fail('input_value_changed', '搜索框回读内容与输入词不一致，已停止提交。');
          }
          this.typed = {url:typed.url, entry:actual, value:actual.value, request:{allow_quantity:request.allow_quantity === true, approved_sku:request.approved_sku, approved_quantity:request.approved_quantity, text:request.text}};
          if (submitSearch) {
            // A trusted search request keeps the same short focus scope across
            // typing and Enter, so background blur handlers cannot split them.
            // Reuse the ordinary Enter guards and never retry this key on failure.
            this._verifyTypedFocus(typed);
            this._assertBoundPage(page, fresh.url);
            try { await page.keyboard.press(KEYS.enter); }
            finally { this.typed = null; }
          }
        } else if (kind === 'press') {
          try { await page.keyboard.press(KEYS[key]); }
          finally { if (['enter', 'tab', 'esc', 'escape'].includes(key)) this.typed = null; }
        } else if (kind === 'scroll') {
          this.typed = null;
          await page.mouse.move(fresh.viewport.width / 2, fresh.viewport.height / 2);
          await page.mouse.wheel(0, -request.amount * 120);
        } else if (kind === 'return_search') {
          this.typed = null;
          await this._returnSearch();
        } else if (this._taskNavigation().can_return_search) {
          this.typed = null;
          await this._returnSearch();
        } else {
          this.typed = null;
          await page.goBack({waitUntil:'domcontentloaded', timeout:15000});
        }
      });
      const snapshot = await this._snapshotAfterAction(page, request.expected_url,
        submitSearch || kind === 'click' || kind === 'back' || (kind === 'press' && String(request.key).toLowerCase() === 'enter'));
      return await this._finishTaskNavigation(snapshot, page, request.expected_url);
      } finally { this.taskAction = null; }
    });
  }

  close() {
    return this._run(async () => {
      const ownContext = this.context;
      this.context = null; this.page = null; this.last = null; this.typed = null;
      this._resetTaskFlow();
      if (ownContext) await bounded(ownContext.close(), 10000);
      return {status:'ok'};
    });
  }

  screenshot({path: destination} = {}) {
    return this._run(async () => {
      const page = await this._selectPage();
      this._requireUrl(page.url());
      const snapshot = await this._capture(page);
      if (snapshot.status !== 'ok') fail(snapshot.error, snapshot.message);
      const target = path.resolve(destination || '');
      const relative = path.relative(this.root, target);
      if (!relative || relative === '..' || relative.startsWith('..' + path.sep) || path.isAbsolute(relative) || !/\.png$/i.test(target)) fail('path_not_allowed', '证据截图只能保存为项目目录内的 PNG 文件。');
      let existing = target;
      while (!fs.existsSync(existing) && path.dirname(existing) !== existing) existing = path.dirname(existing);
      const realRelative = path.relative(fs.realpathSync(this.root), fs.realpathSync(existing));
      if (realRelative === '..' || realRelative.startsWith('..' + path.sep) || path.isAbsolute(realRelative)) fail('path_not_allowed', '截图目标不能通过链接指向项目外。');
      fs.mkdirSync(path.dirname(target), {recursive:true});
      await page.screenshot({path:target, fullPage:false});
      return {status:'ok', path:target};
    });
  }
}

module.exports = {VirtualBrowser, VirtualBrowserError, allowedUrl, protectedUrl, skuFromUrl};
