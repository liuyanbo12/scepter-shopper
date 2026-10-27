"""Independent DeepSeek shopping loop with a selectable browser input driver.

The model proposes actions; this module owns permission boundaries, budgets,
state transitions, and the at-most-once add-to-cart journal.
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from . import prompts
from .listings import compact_judge_record, extract_product_record, extract_search_listings
from .llm import safe_error
from .model_context import compact_observation, candidate_context, search_plan_context, json_chars
from .rules import (amount, compact, grounded_candidate, guard_action, jd_url, normalize_plan,
                    page_text, price_is_visible, valid_price_evidence, selected_spec_matches, sku_from_url,
                    validate_selection as check_selection)


class Suspended(Exception):
    """A persisted pause, stop, or request for human attention."""


class SearchObservationChanged(Exception):
    """Discard a search-stage decision and read again; never replay its input."""


class RepeatedSearchRequest(ValueError):
    """A known query has already been submitted enough times for this category."""


_NOT_SPEC_CONTROL = re.compile(
    r"加入购物车|加购物车|加购|立即购买|搜索|数量|客服|收藏|关注|店铺|评价|问大家|领券|优惠券|广告|规格参数|售后服务")
_SPEC_AREA = re.compile(r"choose|sku|attr|规格|颜色|尺码|尺寸|版本|容量|内存|款式|型号", re.I)
_VARIANT_LABEL = re.compile(
    r"(?:[2-9]?[xX]{0,3}[sSmMlL]\b|[2-9][xX]+[lL]|均码|\d{2,3}\s*/\s*\d{2,3}|"
    r"黑色|白色|灰色|蓝色|红色|绿色|粉色|米色|卡其|藏青|紫色|盒装|散片|散装|第?\d+版|"
    r"\d+\s*(?:GB|TB|gb|tb))")
_CART_RECOMMEND = re.compile(r"猜你喜欢|看了又看|为你推荐|还没逛够")
_CART_FOOTER = re.compile(r"关于我们|京ICP|京公网安备|京东旗下网站|出版物经营许可证|营业执照")
_CART_LIST = re.compile(r"到手价|旗舰店|移入收藏|全选")
_CART_EMPTY = re.compile(r"购物车(?:还是|空空|中)?空|购物车里(?:还)?没有商品|购物车内暂时没有商品")


def json_object(content) -> dict:
    if isinstance(content, dict):
        return content
    text = str(content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("模型应返回 JSON 对象")
    return data


def safe_navigation(url: str) -> str:
    url = jd_url(url)
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    allowed = ((host == "www.jd.com" and parsed.path in {"", "/"})
               or (host == "search.jd.com" and parsed.path.lower() in {"/search", "/search/"})
               or (host == "item.jd.com" and bool(sku_from_url(url)))
               or (host == "cart.jd.com" and parsed.path in {"/cart.action", "/cart_index"} and not parsed.query))
    if not allowed:
        raise ValueError("只允许京东首页、商品搜索、商品详情及只读购物车网址")
    return url


class ShoppingEngine:
    def __init__(self, store, llm, desktop, settings, judge=None):
        self.store, self.llm, self.desktop, self.settings, self.judge = store, llm, desktop, settings, judge
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="shopper-desktop")
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()
        self._active_id: str | None = None
        self._started: dict[str, float] = {}
        self._epochs: dict[str, int] = {}
        self._closed = False
        self._close_lock = asyncio.Lock()
        self._shutdown_done = False
        if hasattr(desktop, "stop_check"):
            desktop.stop_check = self._desktop_should_stop

    def _desktop_should_stop(self):
        if self._closed:
            return True
        run_id = self._active_id
        if not run_id:
            return False
        run = self.store.get(run_id)
        return (run.get("status") in {"paused", "stopped", "needs_user", "failed", "completed"}
                or run.get("control_generation", 0) != self._epochs.get(run_id, run.get("control_generation", 0)))

    def _setting(self, name, default):
        return self.settings.get(name, default) if isinstance(self.settings, dict) else getattr(self.settings, name, default)

    async def call_desktop(self, method: str, *args, **kwargs):
        if self._closed:
            raise RuntimeError("执行器已经关闭")
        function = getattr(self.desktop, method)
        return await asyncio.get_running_loop().run_in_executor(self._executor, functools.partial(function, *args, **kwargs))

    async def close(self):
        async with self._close_lock:
            if self._shutdown_done:
                return
            # The synchronous driver sees this flag at its own checkpoints,
            # including during an in-flight action canceled by the asyncio side.
            self._closed = True
            tasks = [task for task in self._tasks if task is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            try:
                desktop_close = getattr(self.desktop, "close", None)
                if callable(desktop_close):
                    # The native browser handles and COM objects belong to
                    # this thread. Do not use call_desktop, which rejects closed
                    # engines, or a generic worker that breaks thread affinity.
                    await asyncio.get_running_loop().run_in_executor(self._executor, desktop_close)
            finally:
                self._executor.shutdown(wait=False, cancel_futures=True)
                self._shutdown_done = True

    def _checkpoint(self, run_id):
        run = self.store.get(run_id)
        if run_id in self._epochs and run.get("control_generation", 0) != self._epochs[run_id]:
            raise Suspended()
        if run.get("status") in {"paused", "stopped", "needs_user", "awaiting_choice", "awaiting_approval", "completed", "failed"}:
            raise Suspended()
        if run.get("step_count", 0) >= self._setting("max_steps", 400):
            self._needs_user(run_id, "已达到本次动作上限。请检查当前结果；需要更多搜索时可调整 MAX_STEPS 后新建任务。")
        elapsed = run.get("active_seconds", 0) + (time.monotonic() - self._started.get(run_id, time.monotonic()))
        if elapsed >= self._setting("max_run_seconds", 3600):
            self._needs_user(run_id, "已达到本次运行时间上限，已停止后续动作。请检查现有结果。")
        return run

    def _needs_user(self, run_id, reason):
        if self.store.get(run_id).get("status") in {"paused", "stopped"}:
            raise Suspended()
        self.store.update(run_id, status="needs_user", error=str(reason), summary=str(reason))
        self.store.event(run_id, str(reason), "warning")
        raise Suspended()

    def _unwrap(self, run_id, result):
        if not isinstance(result, dict):
            raise RuntimeError("桌面执行器返回了无法识别的数据")
        observation = result.get("observation", result)
        blocked = {"needs_user", "blocked", "error"}
        # An attached observation does not cancel a failed focus/navigation
        # result. In particular, a readable background page is not permission
        # to keep driving the browser while a Windows dialog has the focus.
        if result.get("status") in blocked or result.get("needs_user"):
            self._store_browser_tabs(run_id, result)
            self._needs_user(run_id, result.get("message") or result.get("reason") or observation.get("message") or "当前页面需要手动处理，处理后点击继续。")
        if observation.get("status") in blocked or observation.get("needs_user"):
            self._store_browser_tabs(run_id, observation)
            self._needs_user(run_id, observation.get("message") or observation.get("reason") or result.get("message") or "当前页面需要手动处理，处理后点击继续。")
        window = observation.get("window") or {}
        virtual_mouse = (window.get("requires_foreground") is False
                         or window.get("mode") in {"existing_browser", "browser_mouse"}
                         or self._uses_virtual_mouse())
        if window.get("foreground") is False and not virtual_mouse:
            self._needs_user(run_id, "Edge 当前不在前台，可能被系统提示框或其他窗口遮挡。请先处理提示框，再点击继续；已停止后续操作。")
        if not observation.get("obs_id"):
            raise RuntimeError("桌面执行器没有返回页面观察编号")
        self.store.update(run_id, last_observation={"obs_id": observation["obs_id"],
                          "url": observation.get("url", ""), "title": observation.get("title", ""),
                          "elements_count": len(observation.get("elements", []))},
                          browser_tabs=[])
        return observation

    def _can_refresh_search_observation(self, run):
        return (run.get("stage") == "search" and not self._plan_is_committed(run)
                and self._setting("input_mode", "existing_browser") in {"existing_browser", "browser_mouse"})

    async def _desktop_step(self, run_id, method, *args, description=None, recover_navigation=False):
        run = self._checkpoint(run_id)
        self.store.update(run_id, step_count=run.get("step_count", 0) + 1)
        if description:
            self.store.event(run_id, description)
        result = await self.call_desktop(method, *args)
        current = self._checkpoint(run_id)
        if (recover_navigation and self._can_refresh_search_observation(current)
                and isinstance(result, dict)):
            observation = result.get("observation") or result
            error = result.get("error") or observation.get("error")
            if error in {"page_changed", "stale_observation", "target_unavailable", "target_occluded",
                         "element_stale", "element_changed", "element_unavailable", "search_page_unavailable"}:
                # The old target/URL remains invalid. Only the search loop may
                # obtain a new observation and ask for a new model decision.
                # Target errors are raised before dispatch; they never grant
                # permission to click through an overlay. Cart actions and
                # unknown input errors retain stop-and-verify behavior.
                raise SearchObservationChanged()
        return self._unwrap(run_id, result)

    def _uses_virtual_mouse(self):
        return getattr(self.desktop, "input_mode", None) in {"existing_browser", "browser_mouse"}

    def _mark_detail_unusable(self, run_id, observation, reason):
        category_id, memory = self._search_memory(run_id)
        page = self._search_page_key(observation)
        if not page.startswith("https://item.jd.com/"):
            return
        products = [dict(item) for item in memory.get("visited_products", [])]
        previous = next((item for item in products if item.get("url") == page), None)
        if previous:
            products.remove(previous)
        products.append({"url": page, "title": (previous or {}).get("title") or self._search_label(observation.get("title")),
                         "visits": max((previous or {}).get("visits", 1), 2), "rejected": True,
                         "reason": str(reason)[:80]})
        memory["visited_products"] = products[-16:]
        self._save_search_memory(run_id, category_id, memory)

    async def _leave_unusable_detail(self, run_id, observation, reason):
        observation = observation or {}
        if not observation.get("obs_id"):
            return None
        self._mark_detail_unusable(run_id, observation, reason)
        if not callable(getattr(self.desktop, "return_to_search", None)):
            return None
        self.store.event(run_id, reason, "warning")
        try:
            return await self._return_to_search(run_id, observation)
        except (ValueError, SearchObservationChanged):
            return None

    async def _refresh_search_observation(self, run_id, attempts, observation=None):
        if attempts > 3:
            left = await self._leave_unusable_detail(run_id, observation or {},
                                                     "当前详情控件连续失效，已返回搜索结果找其他商品。")
            if left is not None:
                return left
            self._needs_user(run_id, "页面或控件连续变化或仍被遮挡，已停止重复读取。请等京东页面稳定或处理遮挡后继续。")
        self.store.event(run_id, "页面或控件已变化，重新读取当前京东标签；不重放上一条动作。")
        return await self._observe(run_id)

    def _guard_search_action(self, run_id, action, observation):
        try:
            return guard_action(action, observation)
        except ValueError:
            # Only an obsolete observation/target is recoverable here. Other
            # guard failures (payment, add-to-cart, unsafe typing, etc.) retain
            # their normal rejection and never reach the input driver.
            kind = action.get("action", action.get("kind"))
            target = next((e for e in observation.get("elements", [])
                           if str(e.get("id")) == str(action.get("element_id"))), None)
            stale = (action.get("obs_id") != observation.get("obs_id")
                     or (kind in {"click", "type"}
                         and (not target or target.get("visible") is False or target.get("enabled") is False)))
            if (kind in {"click", "type", "press", "scroll", "back"} and stale
                    and self._can_refresh_search_observation(self._checkpoint(run_id))):
                raise SearchObservationChanged() from None
            raise

    async def _observe(self, run_id):
        return await self._desktop_step(run_id, "observe")

    async def _navigate(self, run_id, url):
        url = safe_navigation(url)
        return await self._desktop_step(run_id, "navigate", url, description=f"打开页面：{url}")

    async def _model(self, run_id, system, data, tools=None, json_mode=False):
        run = self._checkpoint(run_id)
        count = run.get("model_calls", 0)
        if count >= self._setting("max_model_calls", 240):
            self._needs_user(run_id, "已达到模型调用上限，已保留候选和操作日志。可调整 MAX_MODEL_CALLS 后新建任务。")
        if (run.get("usage") or {}).get("prompt_tokens", 0) >= self._setting("max_input_tokens", 2000000):
            self._needs_user(run_id, "已达到本任务的输入 token 上限，已保留候选。请检查结果或调整 MAX_INPUT_TOKENS 后继续。")
        self.store.update(run_id, model_calls=count + 1)
        response = await self.llm.complete(
            [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(data, ensure_ascii=False, separators=(",", ":"))}],
            tools=tools, json_mode=json_mode)
        self._checkpoint(run_id)
        usage = self.store.get(run_id).get("usage") or {}
        for key in ("prompt_tokens", "completion_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
            if key not in (response.get("usage") or {}) and key not in usage:
                continue
            usage[key] = int(usage.get(key, 0)) + int((response.get("usage") or {}).get(key, 0))
        metrics = run.get("model_usage") or []
        metrics.append({"call": count + 1, "stage": run.get("stage", "planning"),
                        "request_chars": len(system) + json_chars(data) + (json_chars(tools) if tools else 0),
                        **(response.get("usage") or {})})
        self.store.update(run_id, usage=usage, model_usage=metrics[-500:])
        return response.get("message") or {}

    def _uses_local_judge(self):
        return bool(self.judge and getattr(self.judge, "available", lambda: False)())

    def _local_judge_requested(self):
        if not self.judge:
            return False
        status = self.judge.status() if hasattr(self.judge, "status") else {}
        return bool(isinstance(status, dict) and status.get("enabled"))

    def _store_browser_tabs(self, run_id, payload):
        if not isinstance(payload, dict):
            return
        tabs = payload.get("tabs")
        if payload.get("error") != "browser_tab_selection_required" or not isinstance(tabs, list):
            return
        cleaned = []
        for tab in tabs[:30]:
            if not isinstance(tab, dict):
                continue
            tab_id, title, url = tab.get("id"), tab.get("title"), tab.get("url")
            if isinstance(tab_id, str) and tab_id and isinstance(title, str) and isinstance(url, str):
                cleaned.append({"id": tab_id[:128], "title": title[:240], "url": url[:500]})
        self.store.update(run_id, browser_tabs=cleaned)

    async def _judge_records(self, run_id, category, records):
        run = self._checkpoint(run_id)
        count = run.get("local_model_calls", 0)
        if count >= self._setting("max_local_calls", 80):
            self._needs_user(run_id, "本地筛选次数已达上限，已保留已核实候选。可调整 MAX_LOCAL_CALLS 后继续。")
        compact_records = [compact_judge_record(record) for record in records]
        body = {"category": {"id": category.get("id"), "name": category.get("name"), "query": category.get("query")},
                "requirement": category.get("query") or category.get("name"),
                "candidates": compact_records}
        if json_chars(body) > self._setting("max_local_input_chars", 8000):
            body["candidates"] = compact_records[:3]
        self.store.update(run_id, local_model_calls=count + 1)
        result = await self.judge.judge(body, body["candidates"])
        usage = self.store.get(run_id).get("local_usage") or {}
        for key in ("prompt_tokens", "completion_tokens"):
            usage[key] = int(usage.get(key, 0)) + int((result.get("usage") or {}).get(key, 0))
        self.store.update(run_id, local_usage=usage)
        return result.get("items") or []

    async def _qwen_json(self, run_id, system, data):
        run = self._checkpoint(run_id)
        count = run.get("local_model_calls", 0)
        if count >= self._setting("max_local_calls", 80):
            self._needs_user(run_id, "本地筛选次数已达上限，已保留已核实候选。可调整 MAX_LOCAL_CALLS 后继续。")
        self.store.update(run_id, local_model_calls=count + 1)
        result = await self.judge.complete_json(system, data)
        usage = self.store.get(run_id).get("local_usage") or {}
        for key in ("prompt_tokens", "completion_tokens"):
            usage[key] = int(usage.get(key, 0)) + int((result.get("usage") or {}).get(key, 0))
        self.store.update(run_id, local_usage=usage)
        parsed = result.get("data")
        if not isinstance(parsed, dict):
            raise ValueError("本地模型没有返回可核验的 JSON")
        return parsed

    @staticmethod
    def _signature(observation):
        data = {"url": observation.get("url"), "text": page_text(observation)}
        return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()

    @staticmethod
    def _model_observation(observation):
        return compact_observation(observation)

    @staticmethod
    def _search_label(value):
        # Store product/search labels only, never page text, account addresses,
        # URL tracking parameters, or credentials accidentally present in labels.
        text = re.sub(r"https?://\S+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[已省略]", str(value or ""))
        text = re.sub(r"(?i)\b(?:cookie|authorization|token)\s*[:=]\s*\S+", "[已省略]", text)
        return " ".join(text.split())[:180]

    @staticmethod
    def _search_page_key(observation):
        parsed = urlparse(str(observation.get("url") or ""))
        host = (parsed.hostname or "").lower()
        sku = sku_from_url(observation.get("url", ""))
        if host == "item.jd.com" and sku:
            return f"https://item.jd.com/{sku}.html"
        if host == "search.jd.com":
            keyword = parse_qs(parsed.query).get("keyword", [""])[0]
            return "https://search.jd.com/Search?keyword=" + quote(keyword)
        return f"https://{host}{parsed.path}"

    def _search_memory(self, run_id):
        run = self.store.get(run_id)
        categories = (run.get("plan") or {}).get("categories", [])
        index = run.get("search_category_index", 0)
        category_id = categories[index]["id"] if 0 <= index < len(categories) else "search"
        memory = dict((run.get("search_memory") or {}).get(category_id) or {})
        return category_id, memory

    def _save_search_memory(self, run_id, category_id, memory):
        memories = dict(self.store.get(run_id).get("search_memory") or {})
        memories[category_id] = memory
        self.store.update(run_id, search_memory=memories)

    @staticmethod
    def _query_key(query):
        return " ".join(str(query).split()).casefold()

    def _check_search_query_history(self, run_id, query):
        _, memory = self._search_memory(run_id)
        key = self._query_key(query)
        previous = next((item for item in memory.get("queries", []) if self._query_key(item["query"]) == key), None)
        if previous and previous.get("submissions", 0) >= 2:
            raise RepeatedSearchRequest("该搜索词已提交两次，未再次输入。请读取当前详情、返回结果寻找其他商品，或改用不同搜索词；不要重复搜索同一商品。")

    def _remember_search_query(self, run_id, query):
        category_id, memory = self._search_memory(run_id)
        queries = [dict(item) for item in memory.get("queries", [])]
        key = self._query_key(query)
        previous = next((item for item in queries if self._query_key(item["query"]) == key), None)
        if previous:
            queries.remove(previous)
        queries.append({"query": query, "submissions": (previous or {}).get("submissions", 0) + 1})
        memory["queries"] = queries[-12:]
        self._save_search_memory(run_id, category_id, memory)

    def _remember_search_observation(self, run_id, observation, candidate_count):
        category_id, memory = self._search_memory(run_id)
        if memory.get("candidate_count", 0) != candidate_count:
            memory["state_history"], memory["recent_product_visits"] = [], []
        memory["candidate_count"] = candidate_count
        page = self._search_page_key(observation)
        products = [dict(item) for item in memory.get("visited_products", [])]
        visits = list(memory.get("recent_product_visits", []))
        if page.startswith("https://item.jd.com/") and sku_from_url(page):
            previous = next((item for item in products if item["url"] == page), None)
            entering = memory.get("last_page") != page
            if previous:
                products.remove(previous)
            products.append({"url": page, "title": self._search_label(observation.get("title")),
                             "visits": (previous or {}).get("visits", 0) + int(entering or not previous),
                             **({"rejected": True, "reason": previous.get("reason")} if previous and previous.get("rejected") else {})})
            if entering:
                visits.append(page)
        memory["visited_products"], memory["recent_product_visits"] = products[-16:], visits[-16:]
        # Persist only a hash of the page identity and visible content. A/B/A
        # loops count even when URL tracking parameters change, while scrolling
        # to new text on the same detail page remains useful progress.
        state = self._signature({**observation, "url": page})
        states = [*memory.get("state_history", []), state][-24:]
        memory["state_history"], memory["last_page"] = states, page.split("?", 1)[0]
        looped = states.count(state) > self._setting("max_no_progress", 5) or (visits and visits.count(visits[-1]) >= 4)
        dead_listing = page.startswith("https://item.jd.com/") and any(
            item.get("url") == page and item.get("visits", 0) >= 2 for item in products)
        if (looped or dead_listing) and candidate_count >= 1:
            memory["exhausted"] = True
        self._save_search_memory(run_id, category_id, memory)
        if looped and candidate_count < 1:
            self._needs_user(run_id, "搜索在相同页面或同一商品间反复循环，尚未新增可核实候选，已停止重复操作。请查看当前详情的规格和价格后继续。")
        return memory

    def _selection_ids(self, selection, run):
        if selection is None:
            selection = run.get("selected_ids", [])
        if isinstance(selection, dict):
            selection = selection.get("candidate_ids", selection.get("selected_ids", []))
        if not isinstance(selection, list) or not all(isinstance(item, str) for item in selection):
            raise ValueError("选择参数必须为商品 ID 列表")
        return selection

    def validate_selection(self, run_id, selection):
        run = self.store.get(run_id)
        if run.get("status") not in {"awaiting_choice", "awaiting_approval"}:
            raise ValueError("只有待确认的方案可以更换候选")
        ids = self._selection_ids(selection, run)
        validation = check_selection(run.get("plan") or {}, run.get("candidates") or [], ids)
        return self.store.update(run_id, selected_ids=ids, validation=validation)

    @staticmethod
    def _plan_is_committed(run):
        return bool(run.get("approved_ids") or run.get("approved_at") or run.get("add_journal")
                    or run.get("cart_items") or run.get("stage") == "cart")

    @staticmethod
    def _plan_input(run):
        data = {"user_request": run["prompt"]}
        if run.get("plan"):
            data["previous_plan"] = run["plan"]
        if run.get("candidates"):
            data["verified_candidates"] = [{key: item.get(key) for key in
                ("id", "category", "title", "brand", "model", "spec", "url")} for item in run["candidates"]]
        return data

    def _category_search_queries(self, category):
        queries, seen = [], set()
        for raw in [category.get("query"), *(
                (target or {}).get("search_query") for target in category.get("target_models") or [])]:
            query = str(raw or "").strip()
            key = self._query_key(query)
            if not query or key in seen:
                continue
            seen.add(key)
            queries.append(query)
        return queries[:3]

    def _empty_required_categories(self, run):
        candidates = run.get("candidates") or []
        return [category for category in (run.get("plan") or {}).get("categories") or []
                if category.get("required", True)
                and not any(item.get("category") == category["id"] for item in candidates)]

    def _first_gap_index(self, run):
        gaps = {category["id"] for category in self._empty_required_categories(run)}
        categories = (run.get("plan") or {}).get("categories") or []
        return next((index for index, category in enumerate(categories) if category["id"] in gaps), len(categories))

    def _failed_query_keys(self, run, category_ids):
        memories = run.get("search_memory") or {}
        keys = set()
        for category_id in category_ids:
            for item in (memories.get(category_id) or {}).get("queries") or []:
                keys.add(self._query_key(item.get("query")))
        return keys

    def _gap_revision_input(self, run, gaps):
        data = self._plan_input(run)
        data["search_gaps"] = []
        memories = run.get("search_memory") or {}
        for category in gaps:
            memory = memories.get(category["id"]) or {}
            data["search_gaps"].append({
                "id": category["id"], "name": category["name"],
                "failed_queries": [item.get("query") for item in memory.get("queries") or []],
                "verified_count": sum(1 for item in run.get("candidates") or [] if item.get("category") == category["id"]),
            })
        data["instruction"] = "空缺类别必须更换不同书名或型号与 search_query，禁止重复 failed_queries。已核实候选所属类别保持 id。"
        return data

    def _queries_changed_for_gaps(self, run, plan, gaps):
        tried = self._failed_query_keys(run, [category["id"] for category in gaps])
        gap_ids = {category["id"] for category in gaps}
        for category in plan.get("categories") or []:
            if category["id"] not in gap_ids:
                continue
            for query in self._category_search_queries(category):
                if self._query_key(query) not in tried:
                    return True
        return False

    def _relax_unfilled_book_categories(self, run_id, gaps):
        run = self.store.get(run_id)
        plan = dict(run.get("plan") or {})
        if plan.get("scenario") != "books":
            return run
        names, categories = [], []
        gap_ids = {category["id"] for category in gaps}
        for category in plan.get("categories") or []:
            item = dict(category)
            if item["id"] in gap_ids:
                item["required"] = False
                names.append(item.get("name") or item["id"])
            categories.append(item)
        if not names:
            return run
        plan["categories"] = categories
        assumptions = list(plan.get("assumptions") or [])
        assumptions.append("京东页面未核实到：" + "、".join(names) + "。已改为可选，可用已核实书籍确认方案。")
        plan["assumptions"] = assumptions
        self.store.update(run_id, plan=plan)
        self.store.event(run_id, "页面未找到「" + "、".join(names) + "」的可核实商品，已改为可选，可用现有候选确认。", "warning")
        return self.store.get(run_id)

    def _save_plan(self, run_id, plan, *, keep_status=False, resume_search=False):
        run = self.store.get(run_id)
        category_ids = {category["id"] for category in plan["categories"]}
        old_categories = (run.get("plan") or {}).get("categories", [])
        candidates = run.get("candidates") or []
        if any(item.get("category") not in category_ids for item in candidates):
            raise ValueError("新计划与已核实候选的类别不一致，已保留原计划；请保持原类别 ID 后重试")
        by_id = {item["id"]: item for item in candidates}
        if any(cid not in by_id for cid in run.get("selected_ids", [])):
            raise ValueError("已有选择无法对应已核实候选，已保留原计划供检查")
        completed = {category["id"] for category in old_categories[:run.get("search_category_index", 0)]}
        target = self._setting("candidates_per_category", 3)
        index = next((i for i, category in enumerate(plan["categories"])
                      if category["id"] not in completed and sum(item.get("category") == category["id"] for item in candidates) < target), len(plan["categories"]))
        reviewing = (not resume_search) and (run.get("stage") == "approval" or run.get("status") == "awaiting_approval")
        if resume_search:
            self.store.update(run_id, plan=plan)
            index = self._first_gap_index(self.store.get(run_id))
        updates = {"plan": plan, "search_category_index": index, "search_started_for": None,
                   "stage": "search" if resume_search else ("approval" if reviewing else "search"),
                   "summary": "搜索计划已细化。型号仅为未核实搜索建议；预算分配不是商品报价。",
                   "phase": "按更新后的备选继续搜索" if resume_search else (
                       "等待确认购物方案" if reviewing else "具体型号搜索计划已就绪")}
        if run.get("selected_ids"):
            updates["validation"] = check_selection(plan, candidates, run["selected_ids"])
        if resume_search:
            updates["status"] = "searching"
            updates["search_resume_gaps"] = True
        elif not keep_status:
            updates["status"] = "awaiting_approval" if reviewing else "searching"
        self.store.update(run_id, **updates)
        self.store.event(run_id, "需求与具体搜索型号已整理：" + "、".join(c["name"] for c in plan["categories"]) + "；售价将在京东页面读取。")
        return self.store.get(run_id)

    async def regenerate_plan(self, run_id):
        """Replan only: never open/focus a browser, search, approve, or add.

        A paused/stopped task stays in that state. Existing evidence and user
        choices remain intact, and any task that reached purchase is immutable.
        """
        lock = self._locks.setdefault(run_id, asyncio.Lock())
        if lock.locked() or self._active_id:
            raise ValueError("已有任务正在执行，请等任务暂停后再细化计划")
        async with lock:
            run = self.store.get(run_id)
            if self._closed:
                raise ValueError("执行器已经关闭")
            if run.get("status") not in {"paused", "needs_user", "awaiting_approval", "stopped"}:
                raise ValueError("只有已暂停、等待处理、待确认或停止的任务可以单独细化计划")
            if self._plan_is_committed(run):
                raise ValueError("该任务已经批准或尝试加购，不能重整计划；已有购物车记录保持不变")
            count = run.get("model_calls", 0)
            if count >= self._setting("max_model_calls", 240):
                raise ValueError("已达到模型调用上限，无法重新生成计划")
            generation, status = run.get("control_generation", 0), run["status"]
            own_task = asyncio.current_task()
            self._tasks.add(own_task)
            self.store.update(run_id, planning_only=True, replan_error=None, model_calls=count + 1)
            self.store.event(run_id, "正在仅细化搜索型号；本次不会启动浏览器或执行购物。")
            try:
                response = await self.llm.complete([
                    {"role": "system", "content": prompts.PLANNER},
                    {"role": "user", "content": json.dumps(self._plan_input(run), ensure_ascii=False)}], json_mode=True)
                current = self.store.get(run_id)
                if self._closed or current.get("control_generation", 0) != generation or current["status"] != status:
                    raise Suspended()  # Stop/pause wins over an in-flight response.
                usage = current.get("usage") or {}
                for key in ("prompt_tokens", "completion_tokens"):
                    usage[key] = int(usage.get(key, 0)) + int((response.get("usage") or {}).get(key, 0))
                self.store.update(run_id, usage=usage)
                plan = normalize_plan(json_object((response.get("message") or {}).get("content")), run["prompt"])
                self._save_plan(run_id, plan, keep_status=True)
            except Suspended:
                pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                message = safe_error(exc, self.settings if not isinstance(self.settings, dict) else None)
                key = self._setting("api_key", "")
                if key:
                    message = message.replace(key, "[已隐藏]")
                self.store.update(run_id, replan_error=message)
                self.store.event(run_id, "搜索计划未修改：" + message, "error")
                raise ValueError(message) from None
            finally:
                self.store.update(run_id, planning_only=False)
                self._tasks.discard(own_task)
            return self.store.get(run_id)

    async def _revise_empty_categories(self, run_id, gaps):
        if not gaps:
            return False
        run = self.store.get(run_id)
        message = await self._model(run_id, prompts.PLANNER, self._gap_revision_input(run, gaps), json_mode=True)
        plan = normalize_plan(json_object(message.get("content")), run["prompt"])
        if not self._queries_changed_for_gaps(run, plan, gaps):
            self.store.event(run_id, "DeepSeek 未给出与已失败搜索不同的备选，停止自动换书。", "warning")
            return False
        self._save_plan(run_id, plan, resume_search=True)
        self.store.event(run_id, "已按页面未找到的类别更换备选，继续由本地筛选搜索。")
        return True

    async def _finish_search_cycle(self, run_id):
        run = self._checkpoint(run_id)
        gaps = self._empty_required_categories(run)
        rounds = int(run.get("gap_revisions") or 0)
        if gaps and rounds < 2:
            if await self._revise_empty_categories(run_id, gaps):
                self.store.update(run_id, gap_revisions=rounds + 1, status="searching", stage="search",
                                  search_category_index=self._first_gap_index(self.store.get(run_id)),
                                  search_started_for=None, search_resume_gaps=True, error=None,
                                  phase="按更新后的备选继续搜索")
                await self._search(run_id)
                return
        if gaps:
            self._relax_unfilled_book_categories(run_id, gaps)
        await self._recommend(run_id)

    async def _approve_and_start(self, run_id, selection=None, automatic=False):
        run = self.store.get(run_id)
        if run.get("status") not in {"awaiting_choice", "awaiting_approval"}:
            raise ValueError("当前没有等待确认的购物方案")
        if (run.get("plan") or {}).get("version") != 2 and not self._plan_is_committed(run):
            await self.regenerate_plan(run_id)
            raise ValueError("搜索计划已更新为具体型号，请检查保留的候选并再次确认")
        if selection is None:
            selection = run.get("selected_ids") or []
        run = self.validate_selection(run_id, selection)
        if run["validation"]["errors"]:
            raise ValueError("；".join(run["validation"]["errors"]))
        self.store.update(run_id, status="adding", stage="cart", phase="核对原购物车",
                          approved_ids=run["selected_ids"], approved_at=datetime.now(timezone.utc).isoformat(),
                          choice_deadline=None, focus_requested=True, error=None)
        if automatic:
            self.store.event(run_id, f"用户在倒计时内未选择，已自动采用推荐方案；计划总额 {run['validation']['total']:.2f} 元。")
        else:
            self.store.event(run_id, f"用户确认 {len(run['selected_ids'])} 件商品，计划总额 {run['validation']['total']:.2f} 元。")
        task = asyncio.create_task(self.start(run_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return self.store.get(run_id)

    async def auto_approve(self, run_id):
        """Apply the persisted default after the server-side choice deadline."""
        run = self.store.get(run_id)
        if run.get("status") != "awaiting_choice":
            return run
        proposal_id = run.get("default_proposal_id")
        proposal = next((item for item in run.get("proposals", []) if item.get("id") == proposal_id), None)
        selection = proposal.get("selected_ids") if proposal else run.get("selected_ids")
        if run.get("choice_policy") == "low_risk_auto":
            return await self._approve_and_start(run_id, selection, automatic=True)
        selected = self.validate_selection(run_id, selection or [])
        self.store.update(run_id, status="awaiting_approval", selected_ids=selected["selected_ids"],
                          validation=selected["validation"], choice_deadline=None,
                          phase="已自动采用推荐方案，等待加购确认", summary="已按倒计时采用推荐方案；请确认后加入购物车。")
        self.store.event(run_id, "用户未在倒计时内选择，已自动采用推荐方案；加购前仍等待一次确认。")
        return self.store.get(run_id)

    async def control(self, run_id, action, selection=None):
        run = self.store.get(run_id)
        if run.get("planning_only") and action not in {"pause", "stop"}:
            raise ValueError("正在细化搜索计划，请完成后再继续或确认")
        if action == "pause":
            if run.get("status") in {"completed", "failed", "stopped", "awaiting_choice", "awaiting_approval"}:
                raise ValueError("当前任务不可暂停")
            self.store.event(run_id, "用户暂停：正在进行的 API 请求完成后不会继续发出鼠标动作。")
            return self.store.update(run_id, status="paused", summary="已暂停，可继续或停止。",
                                     control_generation=run.get("control_generation", 0) + 1)
        if action == "stop":
            self.store.event(run_id, "用户停止任务；已加购的商品保留在购物车中。")
            return self.store.update(run_id, status="stopped", summary="任务已停止。",
                                     control_generation=run.get("control_generation", 0) + 1)
        if action == "approve":
            return await self._approve_and_start(run_id, selection, automatic=False)
        elif action == "revise_search":
            if run.get("status") not in {"awaiting_choice", "awaiting_approval"}:
                raise ValueError("只有待确认的方案可以更换备选并继续搜索")
            if self._plan_is_committed(run):
                raise ValueError("该任务已经批准或尝试加购，不能再更换备选")
            if int(run.get("gap_revisions") or 0) >= 3:
                raise ValueError("已多次更换备选仍未补齐，请确认现有商品或新建任务")
            gaps = self._empty_required_categories(run)
            if not gaps:
                raise ValueError("各类都已有核实候选，无需再搜索")
            self.store.update(run_id, status="searching", stage="search", phase="更换空缺类别备选", error=None)
            if not await self._revise_empty_categories(run_id, gaps):
                self._relax_unfilled_book_categories(run_id, gaps)
                await self._recommend(run_id)
                return self.store.get(run_id)
            run = self.store.get(run_id)
            self.store.update(run_id, status="searching", stage="search",
                              search_category_index=self._first_gap_index(run), search_started_for=None,
                              search_resume_gaps=True, gap_revisions=int(run.get("gap_revisions") or 0) + 1,
                              focus_requested=True, error=None, phase="按更新后的备选继续搜索")
            self.store.event(run_id, "用户确认更换备选，继续在京东页面搜索空缺类别。")
        elif action == "resume":
            if run.get("status") not in {"paused", "needs_user"}:
                raise ValueError("只有暂停或等待处理的任务可以继续")
            self.store.update(run_id, status="searching" if run.get("stage") != "cart" else "adding",
                              focus_requested=bool(run.get("browser_opened") or run.get("stage") == "cart"),
                              error=None, summary="继续检查当前页面。")
            self.store.event(run_id, "用户继续：切回已打开的 Edge，并重新读取页面。")
        else:
            raise ValueError("未知控制动作")
        task = asyncio.create_task(self.start(run_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return self.store.get(run_id)

    async def start(self, run_id):
        lock = self._locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            run = self.store.get(run_id)
            if run.get("status") in {"paused", "stopped", "needs_user", "awaiting_choice", "awaiting_approval", "completed", "failed"}:
                return run
            if self._active_id and self._active_id != run_id:
                self.store.update(run_id, status="needs_user", error="另一个任务正在使用鼠标，请先停止或完成该任务。")
                return self.store.get(run_id)
            self._active_id = run_id
            self._started[run_id] = time.monotonic()
            self._epochs[run_id] = run.get("control_generation", 0)
            own_task = asyncio.current_task()
            self._tasks.add(own_task)
            try:
                if not run.get("plan") or (run["plan"].get("version") != 2 and not self._plan_is_committed(run)):
                    self.store.update(run_id, status="planning", phase="理解购物需求", stage="plan")
                    # Preserve an existing approval checkpoint through a schema
                    # upgrade; it must not restart search or change selections.
                    if run.get("stage") == "approval":
                        self.store.update(run_id, stage="approval")
                    self.store.event(run_id, "正在用 DeepSeek 理解预算、用途及需要搜索的完整品牌型号。")
                    message = await self._model(run_id, prompts.PLANNER, self._plan_input(run), json_mode=True)
                    plan = normalize_plan(json_object(message.get("content")), run["prompt"])
                    self._save_plan(run_id, plan)
                run = self._checkpoint(run_id)
                if run.get("focus_requested"):
                    if self._uses_virtual_mouse():
                        result = await self.call_desktop("focus")
                        if isinstance(result, dict) and result.get("status") not in {"needs_user", "blocked", "error"}:
                            self._unwrap(run_id, result)
                        self.store.event(run_id, "继续读取已连接的京东标签页；助手窗口在旁边不会当成遮挡。")
                    else:
                        await self._desktop_step(run_id, "focus", description="恢复京东浏览器并刷新页面观察。")
                    self.store.update(run_id, focus_requested=False)
                if run.get("stage") == "cart":
                    await self._purchase(run_id)
                else:
                    await self._search(run_id)
            except Suspended:
                pass
            except asyncio.CancelledError:
                if self.store.get(run_id).get("status") not in {"stopped", "completed"}:
                    self.store.update(run_id, status="stopped", error="应用停止，任务已中断；请检查购物车后再继续购物。")
                raise
            except Exception as exc:
                current = self.store.get(run_id)
                if current.get("status") not in {"paused", "stopped"}:
                    message = safe_error(exc, self.settings if not isinstance(self.settings, dict) else None) or exc.__class__.__name__
                    key = self._setting("api_key", "")
                    if key:
                        message = message.replace(key, "[已隐藏]")
                    self.store.update(run_id, status="failed", error=message, summary="执行未完成，已保留日志和已核实的商品。")
                    self.store.event(run_id, message, "error")
            finally:
                elapsed = max(0, time.monotonic() - self._started.pop(run_id, time.monotonic()))
                current = self.store.get(run_id)
                self.store.update(run_id, active_seconds=round(current.get("active_seconds", 0) + elapsed, 2))
                self._active_id = None
                self._epochs.pop(run_id, None)
                self._tasks.discard(own_task)
        return self.store.get(run_id)

    async def _leave_exhausted_product(self, run_id, observation):
        """After resume, do not keep clicking a detail page that already failed."""
        run = self.store.get(run_id)
        category_id, memory = self._search_memory(run_id)
        current = [item for item in run.get("candidates", []) if item.get("category") == category_id]
        page = self._search_page_key(observation)
        visits = next((item.get("visits", 0) for item in memory.get("visited_products", []) if item.get("url") == page), 0)
        rejected = any(item.get("url") == page and item.get("rejected") for item in memory.get("visited_products", []))
        if not page.startswith("https://item.jd.com/"):
            return observation
        if not (rejected or (current and visits >= 2)):
            return observation
        if not callable(getattr(self.desktop, "return_to_search", None)):
            return observation
        self.store.event(run_id, "当前详情已反复打开且未能核实，返回搜索结果找其他商品。")
        try:
            return await self._return_to_search(run_id, observation)
        except (ValueError, SearchObservationChanged):
            return observation

    async def _return_to_search(self, run_id, observation):
        run = self._checkpoint(run_id)
        if run.get("stage") != "search" or self._plan_is_committed(run):
            raise ValueError("只有未批准的搜索阶段可以返回主搜索页")
        if not callable(getattr(self.desktop, "return_to_search", None)):
            raise ValueError("当前浏览器执行器不支持返回已保存的搜索页，请使用页面返回操作")
        return await self._desktop_step(run_id, "return_to_search", observation["obs_id"],
                                        description="返回已有搜索主页，保留结果和浏览位置。", recover_navigation=True)

    async def _search_navigation(self, run_id, url, observation):
        url = safe_navigation(url)  # Validate the full URL before extracting a query.
        parsed = urlparse(url)
        if (parsed.hostname or "").lower() != "search.jd.com":
            return await self._navigate(run_id, url)
        params = parse_qs(parsed.query, keep_blank_values=True)
        # Only a plain product search can be translated into the search tool.
        # Never discard arbitrary redirect/action parameters from a model URL.
        if (parsed.port not in {None, 80, 443} or parsed.fragment
                or set(params) - {"keyword", "enc", "wq", "pvid"}
                or len(params.get("keyword", [])) != 1
                or any(len(values) != 1 for values in params.values())):
            raise ValueError("仅支持包含单个关键词的普通京东搜索网址；请用搜索工具指定关键词，不附带跳转、动作或其他参数")
        return await self._search_query(run_id, params["keyword"][0], observation)

    async def _search_query(self, run_id, query, observation=None):
        query = str(query).strip()
        if not query or len(query) > 160 or "\n" in query:
            raise ValueError("搜索词为空、太长或包含换行")
        observation = observation or await self._observe(run_id)
        navigation = observation.get("navigation") or {}
        if navigation.get("can_return_search"):
            observation = await self._return_to_search(run_id, observation)
        parsed = urlparse(observation.get("url", ""))
        existing_query = parse_qs(parsed.query).get("keyword", [])
        if ((parsed.hostname or "").lower() == "search.jd.com" and parsed.path.lower() in {"/search", "/search/"}
                and len(existing_query) == 1 and self._query_key(existing_query[0]) == self._query_key(query)):
            self.store.event(run_id, f"复用已有搜索结果：{query}；不重新输入或刷新页面。")
            return observation
        self._check_search_query_history(run_id, query)
        elements = observation.get("elements", [])
        named_edits = [e for e in elements if str(e.get("role", "")).lower() in {"edit", "textbox", "combobox"}
                 and re.search(r"搜索|search|关键词", str(e.get("name", "")), re.I)
                 and not re.search(r"地址|address|网址", str(e.get("name", "")), re.I)]
        edits = [e for e in named_edits if e.get("enabled") is not False and e.get("visible") is not False
                 and not e.get("blocked") and not e.get("occluded")]
        if named_edits and not edits:
            # An overlay is not permission to bypass the page with navigation.
            # Refresh within the same search recovery budget, then re-decide.
            if self._can_refresh_search_observation(self._checkpoint(run_id)):
                raise SearchObservationChanged()
            self._needs_user(run_id, "页面搜索框暂不可用或被遮挡，请处理当前页面后继续。")
        if edits:
            action = self._guard_search_action(run_id, {"action": "type", "obs_id": observation["obs_id"],
                                                       "element_id": edits[0]["id"], "text": query}, observation)
            if callable(getattr(self.desktop, "search", None)):
                # Keep verified typing and Enter in one temporary page-focus
                # scope; a website blur handler cannot interleave the two.
                result = await self._desktop_step(run_id, "search", action, description=f"输入并提交搜索词：{query}", recover_navigation=True)
            else:
                observation = await self._desktop_step(run_id, "act", action, description=f"输入搜索词：{query}")
                result = await self._desktop_step(run_id, "act", {"action": "press", "obs_id": observation["obs_id"], "key": "enter"})
        else:
            # Navigate the real browser; no private API or scraped response is used.
            self.store.event(run_id, "页面没有命名的搜索框，通过浏览器打开京东搜索页。")
            result = await self._navigate(run_id, "https://search.jd.com/Search?keyword=" + quote(query))
        self._remember_search_query(run_id, query)
        return result

    async def _open_listing(self, run_id, listing, observation):
        sku = listing.get("sku")
        target = next((element for element in observation.get("elements", [])
                       if sku_from_url(str(element.get("href") or element.get("sku") or "")) == sku
                       and str(element.get("role", "")).lower() in {"hyperlink", "link", "button"}
                       and element.get("visible") is not False and element.get("enabled") is not False
                       and not element.get("blocked") and not element.get("occluded")), None)
        if target:
            action = self._guard_search_action(run_id, {"action": "click", "obs_id": observation["obs_id"],
                                                       "element_id": target["id"]}, observation)
            return await self._desktop_step(run_id, "act", action, description=f"打开商品：{self._search_label(listing.get('title'))}",
                                            recover_navigation=True)
        return await self._navigate(run_id, listing["url"])

    def _candidate_args_from_record(self, record, observation):
        text = page_text(observation)
        title = str(record.get("title") or "")
        if len(compact(title)) < 6 or compact(title) not in compact(text):
            title = str(observation.get("title") or title)
        spec = str(record.get("spec") or observation.get("selected_spec") or "")
        if isinstance(observation.get("selected_specs"), (list, tuple)) and not spec:
            spec = " ".join(str(value) for value in observation["selected_specs"])
        if not spec:
            spec = title
        return {"title": title, "spec": spec, "price": record.get("price"),
                "price_evidence": record.get("price_evidence") or "",
                "brand": record.get("brand") or "", "model": record.get("model") or "",
                "url": record.get("url") or observation.get("url")}

    async def _search_category_programmatic(self, run_id, category, observation):
        queries = self._category_search_queries(category) or [str(category.get("query") or category.get("name") or "")]
        for query_index, query in enumerate(queries):
            run = self._checkpoint(run_id)
            current = [item for item in run.get("candidates", []) if item.get("category") == category["id"]]
            if current:
                break
            try:
                if query_index == 0 and run.get("search_started_for") != category["id"]:
                    observation = await self._search_query(run_id, query, observation)
                    self.store.update(run_id, search_started_for=category["id"])
                elif query_index:
                    self.store.event(run_id, f"{category['name']}上一搜索词未找到可核实商品，改用备选：{query}")
                    observation = await self._search_query(run_id, query, observation)
                observation = await self._inspect_programmatic_listings(run_id, category, observation, query)
            except RepeatedSearchRequest:
                self.store.event(run_id, f"不再重复搜索「{query}」。已找到的商品留在当前任务，继续下一个备选或下一类。", "warning")
        return observation

    async def _inspect_programmatic_listings(self, run_id, category, observation, query):
        target = self._setting("candidates_per_category", 3)
        seen, inspected, scrolls = set(), 0, 0
        max_inspect = max(target * 3, 6)
        while True:
            run = self._checkpoint(run_id)
            current = [item for item in run.get("candidates", []) if item.get("category") == category["id"]]
            if len(current) >= target or inspected >= max_inspect:
                if inspected >= max_inspect and len(current) < target:
                    self.store.event(run_id, f"{category['name']}已检查 {inspected} 件页面商品，有效候选 {len(current)} 个。", "warning")
                break
            listings = [item for item in extract_search_listings(observation) if item["sku"] not in seen]
            if not listings:
                if scrolls < 3:
                    observation = await self._desktop_step(
                        run_id, "act", {"action": "scroll", "obs_id": observation["obs_id"], "amount": -5},
                        description="向下滚动搜索结果。", recover_navigation=True)
                    scrolls += 1
                    continue
                if not current:
                    self.store.event(run_id, f"{category['name']}搜索结果中没有可打开的商品链接。", "warning")
                break
            listing = listings[0]
            seen.add(listing["sku"])
            inspected += 1
            try:
                observation = await self._open_listing(run_id, listing, observation)
            except SearchObservationChanged:
                observation = await self._refresh_search_observation(run_id, 1, observation)
                continue
            record = extract_product_record(observation, listing)
            if not record.get("price") and sku_from_url(str(observation.get("url") or "")):
                observation = await self._observe(run_id)
                record = extract_product_record(observation, listing)
            if not record.get("price") or record.get("stock") == "缺货":
                self.store.event(run_id, f"未核实：{self._search_label(record.get('title'))} 缺少可买价格或已缺货。", "warning")
            else:
                try:
                    judged = await self._judge_records(run_id, category, [record])
                    verdict = next((item for item in judged if str(item.get("id")) == record["id"]), judged[0] if judged else {})
                    if str(verdict.get("verdict") or "") == "accept":
                        candidate = grounded_candidate(
                            self._candidate_args_from_record(record, observation), observation,
                            category["id"], scenario=(run.get("plan") or {}).get("scenario"))
                        all_candidates = run.get("candidates", [])
                        if not any(item["id"] == candidate["id"] for item in all_candidates):
                            all_candidates.append(candidate)
                            self.store.update(run_id, candidates=all_candidates)
                            self.store.event(run_id, f"已核实候选：{candidate['title']} · {candidate['spec']} · ¥{candidate['price']:.2f}")
                    else:
                        self.store.event(run_id, f"本地筛选未通过：{self._search_label(record.get('title'))}（{verdict.get('reason') or verdict.get('verdict') or '待核实'}）", "warning")
                except (ValueError, KeyError, TypeError) as exc:
                    self.store.event(run_id, f"候选未保存：{exc}", "warning")
            if callable(getattr(self.desktop, "return_to_search", None)):
                try:
                    observation = await self._return_to_search(run_id, observation)
                except SearchObservationChanged:
                    self.store.event(run_id, "搜索结果页已离开。同一搜索词不再重输，本任务改看下一个备选。", "warning")
                    try:
                        observation = await self._search_query(run_id, query)
                    except RepeatedSearchRequest:
                        self.store.event(run_id, "该搜索词已经提交过，已找到的商品保留在当前任务，不再新开任务。", "warning")
                        break
                except ValueError:
                    observation = await self._observe(run_id)
            else:
                observation = await self._observe(run_id)
        return observation

    async def _search(self, run_id):
        run = self._checkpoint(run_id)
        if not run.get("browser_opened"):
            description = {"existing_browser": "连接你已打开的京东标签页，沿用当前登录状态。",
                           "browser_mouse": "打开专用京东窗口，使用浏览器内虚拟鼠标。",
                           "native": "连接已打开的浏览器并打开京东。"}.get(self._setting("input_mode", "existing_browser"))
            observation = await self._desktop_step(run_id, "open_jd", description=description)
            self.store.update(run_id, browser_opened=True)
        else:
            observation = await self._observe(run_id)
        plan = run["plan"]
        categories = plan["categories"]
        for index in range(run.get("search_category_index", 0), len(categories)):
            self._checkpoint(run_id)
            category = categories[index]
            current = [item for item in self.store.get(run_id).get("candidates", []) if item.get("category") == category["id"]]
            if self.store.get(run_id).get("search_resume_gaps") and current:
                self.store.update(run_id, search_category_index=index + 1, search_started_for=None)
                continue
            self.store.update(run_id, status="searching", stage="search", phase=f"搜索{category['name']}", search_category_index=index)
            if self._local_judge_requested() or self._uses_local_judge():
                if self._local_judge_requested() and not self._uses_local_judge():
                    ensure = getattr(self.judge, "ensure_running", None)
                    if callable(ensure):
                        await ensure()
                if self._local_judge_requested() and not self._uses_local_judge():
                    self._needs_user(run_id, "本地 Qwen 已启用但还不能筛选，已停止调用 DeepSeek 搜索以免继续产生费用。请确认 Ollama 正在运行后继续。")
                self.store.event(run_id, "本类别商品由本地 Qwen 筛选，不调用 DeepSeek 搜索。")
                try:
                    observation = await self._search_category_programmatic(run_id, category, observation)
                except SearchObservationChanged:
                    observation = await self._refresh_search_observation(run_id, 1, observation)
                    try:
                        observation = await self._search_category_programmatic(run_id, category, observation)
                    except RepeatedSearchRequest:
                        self.store.event(run_id, "同一搜索词不再重复提交。已找到的商品留在当前任务，继续下一类。", "warning")
                except RepeatedSearchRequest:
                    self.store.event(run_id, "同一搜索词不再重复提交。已找到的商品留在当前任务，继续下一类。", "warning")
                self.store.update(run_id, search_category_index=index + 1, search_started_for=None)
                continue
            navigation_refreshes, outcomes = 0, []
            if self.store.get(run_id).get("search_started_for") != category["id"]:
                try:
                    observation = await self._search_query(run_id, category["query"], observation)
                    self.store.update(run_id, search_started_for=category["id"])
                except SearchObservationChanged:
                    navigation_refreshes += 1
                    observation = await self._refresh_search_observation(run_id, navigation_refreshes, observation)
                    outcomes.append("搜索期间页面或控件发生变化，上一条动作未重放。请依据新观察决定下一步。")
                except RepeatedSearchRequest as exc:
                    outcomes.append(str(exc))
                    self.store.event(run_id, str(exc), "warning")
            last_signature, stagnant, failures = None, 0, 0
            while True:
                run = self._checkpoint(run_id)
                current = [c for c in run.get("candidates", []) if c["category"] == category["id"]]
                if len(current) >= self._setting("candidates_per_category", 3):
                    break
                observation = await self._leave_exhausted_product(run_id, observation)
                memory = self._remember_search_observation(run_id, observation, len(current))
                if memory.get("exhausted"):
                    self.store.event(run_id, f"{category['name']}已有 {len(current)} 个可核实候选，同一商品页反复无新结果，转入下一类别。", "warning")
                    break
                signature = self._signature(observation) + str(len(current))
                stagnant = stagnant + 1 if signature == last_signature else 0
                last_signature = signature
                if stagnant >= self._setting("max_no_progress", 5):
                    self._needs_user(run_id, f"搜索{category['name']}时页面连续没有进展。请查看浏览器，关闭遮挡弹窗或打开有明确规格和价格的详情页后继续。")
                message = await self._model(run_id, prompts.SHOPPER, {
                    "request": run["prompt"], "plan": search_plan_context(plan), "category": category,
                    "target_candidates": self._setting("candidates_per_category", 3), "recorded_candidates": [candidate_context(c) for c in current],
                    "observation": self._model_observation(observation), "recent_outcomes": outcomes[-8:],
                    "search_history": memory.get("queries", []), "visited_products": memory.get("visited_products", []),
                    "rejected_products": [item for item in memory.get("visited_products", [])
                                          if item.get("visits", 0) >= 2 or item.get("rejected")],
                }, tools=prompts.SEARCH_TOOLS)
                calls = message.get("tool_calls") or []
                if not calls:
                    failures += 1
                    outcomes.append("请使用工具推进当前类别搜索，不能只回答文字。")
                    if failures >= 3:
                        self._needs_user(run_id, "模型未给出可执行操作。请检查模型配置或当前页面后继续。")
                    continue
                call = calls[0].get("function", {})
                name = call.get("name")
                try:
                    args = json_object(call.get("arguments", "{}"))
                    if name == "action":
                        action = self._guard_search_action(run_id, args, observation)
                        clicked = next((e for e in observation.get("elements", [])
                                        if e.get("id") == action.get("element_id")), {}) if action["action"] == "click" else {}
                        if action["action"] == "scroll":
                            action["amount"] = abs(action["amount"]) * (-1 if action.pop("direction") == "down" else 1)
                        observation = await self._desktop_step(run_id, "act", action, description=f"页面操作：{action['action']}", recover_navigation=True)
                        outcome = f"页面操作 {action['action']} 已执行，已重新观察页面。"
                        if clicked:
                            outcome += f" 点击目标：{self._search_label(clicked.get('name'))}；当前页面：{self._search_label(observation.get('title'))}。"
                    elif name == "navigate":
                        observation = await self._search_navigation(run_id, args["url"], observation)
                        outcome = "已打开目标页面或复用主搜索页，并重新观察。"
                    elif name == "search":
                        observation = await self._search_query(run_id, args["query"], observation)
                        outcome = "已获取该关键词的搜索结果；已有结果会直接复用，不重新提交。"
                    elif name == "return_to_search":
                        observation = await self._return_to_search(run_id, observation)
                        outcome = "已返回已有搜索主页，保留浏览位置。请点击另一个尚未核实的版本或店铺商品。"
                    elif name == "read_page":
                        observation = await self._observe(run_id)
                        outcome = "已刷新页面信息。"
                    elif name == "record_candidate":
                        candidate = grounded_candidate(args, observation, category["id"], scenario=plan.get("scenario"))
                        all_candidates = run.get("candidates", [])
                        if any(c["id"] == candidate["id"] for c in all_candidates):
                            raise ValueError("此 SKU 已经记录，请寻找不同候选")
                        all_candidates.append(candidate)
                        self.store.update(run_id, candidates=all_candidates)
                        self.store.event(run_id, f"已核实候选：{candidate['title']} · {candidate['spec']} · ¥{candidate['price']:.2f}")
                        outcome = f"已保存候选 {candidate['id']}。"
                    elif name == "finish_search":
                        if not current:
                            self._needs_user(run_id, f"{category['name']}尚无可核实候选：{args.get('reason', '')}。请打开该类商品详情后继续。")
                        self.store.event(run_id, f"{category['name']}找到 {len(current)} 个可核实候选：{args.get('reason', '')}", "warning")
                        break
                    elif name == "request_user":
                        self._needs_user(run_id, args.get("reason") or "请处理当前京东页面后点击继续。")
                    else:
                        raise ValueError("模型请求了当前阶段不支持的工具")
                    failures = 0
                    navigation_refreshes = 0
                except SearchObservationChanged:
                    navigation_refreshes += 1
                    observation = await self._refresh_search_observation(run_id, navigation_refreshes, observation)
                    outcome = "页面或控件已经变化，已获取新观察，上一条动作未重放。请使用新页面中的控件重新决定下一步。"
                except (ValueError, KeyError, TypeError) as exc:
                    failures += 1
                    outcome = f"操作未执行或候选未保存：{str(exc)}"
                    self.store.event(run_id, outcome, "warning")
                    if failures >= 4:
                        left = await self._leave_unusable_detail(
                            run_id, observation, "当前详情连续无法核实，已返回搜索结果找其他商品。最近原因：" + str(exc))
                        if left is not None:
                            observation, failures = left, 0
                            outcome = "当前详情连续无法核实，已返回搜索结果找其他商品。"
                        else:
                            self._needs_user(run_id, "连续操作无法核实。请查看页面后继续。最近原因：" + str(exc))
                outcomes.append(outcome)
            self.store.update(run_id, search_category_index=index + 1, search_started_for=None)
        await self._finish_search_cycle(run_id)

    async def _recommend(self, run_id):
        run = self._checkpoint(run_id)
        self.store.update(run_id, phase="比较价格与兼容性", stage="recommend")
        message = await self._model(run_id, prompts.RECOMMENDER, {"request": run["prompt"], "plan": run["plan"],
                                    "candidates": [candidate_context(c) for c in run["candidates"]]}, json_mode=True)
        recommendation = json_object(message.get("content"))
        proposals = self._normalize_proposals(recommendation, run)
        default_id = str(recommendation.get("default_proposal_id") or "")
        default = next((item for item in proposals if item["id"] == default_id), None)
        if default is None:
            default = next((item for item in proposals if item.get("recommended")), proposals[0])
        default_id = default["id"]
        ids = default["selected_ids"]
        validation = default["validation"]
        candidates = [{**c, "recommended": c["id"] in ids} for c in run["candidates"]]
        timeout = int(self._setting("choice_timeout_seconds", 10))
        deadline = datetime.now(timezone.utc).timestamp() + timeout
        deadline_iso = datetime.fromtimestamp(deadline, timezone.utc).isoformat()
        reason = str(recommendation.get("reason") or default.get("reason") or "请选择每类商品并检查规格、预算与待核实参数。")
        self.store.update(run_id, candidates=candidates, selected_ids=ids, validation=validation,
                          proposals=proposals, default_proposal_id=default_id,
                          choice_deadline=deadline_iso, choice_policy=self._setting("choice_policy", "recommend_only"),
                          status="awaiting_choice", stage="approval", phase="等待方案选择", summary=reason, error=None)
        self.store.event(run_id, f"已生成 {len(proposals)} 套购物方案；{timeout} 秒无操作将采用“{default['label']}”。")

    def _normalize_proposals(self, recommendation, run):
        """Validate model proposals and provide deterministic fallbacks.

        The recommender is allowed to return the older selected_ids-only shape;
        this keeps existing runs usable while the UI migrates to proposal cards.
        """
        candidates = list(run.get("candidates") or [])
        by_category = {}
        for candidate in candidates:
            by_category.setdefault(candidate.get("category"), []).append(candidate)

        def ids_for(raw):
            if not isinstance(raw, list):
                return []
            try:
                return self._selection_ids([str(value) for value in raw], run)
            except (ValueError, TypeError):
                return []

        def make(item, fallback_id, fallback_label, recommended=False):
            item = item if isinstance(item, dict) else {}
            ids = ids_for(item.get("selected_ids"))
            validation = check_selection(run["plan"], candidates, ids)
            return {
                "id": str(item.get("id") or fallback_id),
                "label": str(item.get("label") or fallback_label),
                "selected_ids": ids,
                "total": validation.get("total", 0),
                "validation": validation,
                "reason": str(item.get("reason") or "根据已核实商品和预算生成。"),
                "tradeoffs": item.get("tradeoffs") if isinstance(item.get("tradeoffs"), list) else ([str(item["tradeoffs"])] if item.get("tradeoffs") else []),
                "recommended": bool(item.get("recommended", recommended)),
            }

        raw = recommendation.get("proposals")
        proposals = []
        if isinstance(raw, list):
            for index, item in enumerate(raw[:3]):
                proposals.append(make(item, f"proposal-{index + 1}", f"方案 {index + 1}", index == 1))

        base_ids = ids_for(recommendation.get("selected_ids"))
        if not base_ids and proposals:
            base_ids = proposals[0]["selected_ids"]
        if not base_ids:
            base_ids = self._selection_ids([], run)

        def deterministic(mode):
            selected = []
            for category, values in by_category.items():
                usable = [value for value in values if value.get("price") is not None]
                if not usable:
                    continue
                if mode == "budget":
                    choice = min(usable, key=lambda value: float(value.get("price") or 0))
                elif mode == "safe":
                    choice = max(usable, key=lambda value: (bool(value.get("self_operated")), float(value.get("rating") or 0) if str(value.get("rating") or "").replace('.', '', 1).isdigit() else 0))
                else:
                    choice = max(usable, key=lambda value: (bool(value.get("self_operated")), float(value.get("rating") or 0) if str(value.get("rating") or "").replace('.', '', 1).isdigit() else 0, -float(value.get("price") or 0)))
                selected.append(choice["id"])
            return selected

        fallback_items = [
            {"id": "safe", "label": "稳妥方案", "selected_ids": base_ids, "reason": "优先自营、评分和已核实的兼容性。", "tradeoffs": ["价格可能略高"], "recommended": False},
            {"id": "balanced", "label": "均衡推荐", "selected_ids": base_ids, "reason": str(recommendation.get("reason") or "综合预算、质量和可买性。"), "tradeoffs": ["在价格和风险之间取平衡"], "recommended": True},
            {"id": "budget", "label": "省钱方案", "selected_ids": deterministic("budget") or base_ids, "reason": "优先选择已核实的较低价格商品。", "tradeoffs": ["品牌、店铺或配送可能需要妥协"], "recommended": False},
        ]
        existing_ids = {item["id"] for item in proposals}
        for item in fallback_items:
            if item["id"] not in existing_ids:
                proposals.append(make(item, item["id"], item["label"], item.get("recommended", False)))
        # Always keep three cards in the UI; malformed model entries are replaced
        # by deterministic proposals rather than exposing an empty choice.
        proposals = proposals[:3]
        while len(proposals) < 3:
            item = fallback_items[len(proposals)]
            proposals.append(make(item, item["id"], item["label"], item.get("recommended", False)))
        used_ids = set()
        for item in proposals:
            base_id = item["id"] or "proposal"
            unique_id, suffix = base_id, 2
            while unique_id in used_ids:
                unique_id = f"{base_id}-{suffix}"
                suffix += 1
            item["id"] = unique_id
            used_ids.add(unique_id)
        if not any(item.get("recommended") for item in proposals):
            proposals[min(1, len(proposals) - 1)]["recommended"] = True
        return proposals

    async def _cart_snapshot(self, run_id, selected):
        observation = await self._navigate(run_id, "https://cart.jd.com/cart_index")
        if self._cart_is_empty(observation):
            return {"items": {}, "empty": True, "complete": True, "observations": [observation]}
        if observation.get("truncated"):
            self._needs_user(run_id, "购物车可见控件超出读取上限，无法证明已完整核对。请提高 MAX_ELEMENTS 或关闭遮挡区域后重启任务；不会猜测原有数量。")
        # Added goods stay in the cart list near the top. Footer, 猜你喜欢 and
        # the ICP block are not cart rows; never verify from those viewports.
        # Recover with bounded, observable steps instead of one large blind jump.
        recovery_attempts = 0
        while self._cart_past_items(observation) and not self._cart_has_item_rows(observation) and recovery_attempts < 3:
            recovery_attempts += 1
            self.store.update(run_id, status="recovering", phase=f"从购物车推荐区回退到商品列表（{recovery_attempts}/3）")
            observation = await self._desktop_step(
                run_id, "act", {"action": "scroll", "obs_id": observation["obs_id"], "amount": 4},
                description="购物车停在页底，向上回退到商品列表。")
            if self._cart_is_empty(observation):
                return {"items": {}, "empty": True, "complete": True, "observations": [observation]}
        if recovery_attempts:
            self.store.update(run_id, status="verifying", phase="读取购物车商品行")
        observations = []
        last_scroll_y = None
        scroll_direction = -2
        for attempt in range(6):
            if observation.get("truncated"):
                self._needs_user(run_id, "购物车可见控件超出读取上限，无法证明已完整核对。请提高 MAX_ELEMENTS 或关闭遮挡区域后重启任务；不会猜测原有数量。")
            if self._cart_is_empty(observation):
                return {"items": {}, "empty": True, "complete": True, "observations": observations or [observation]}
            observations.append(observation)
            list_views = [item for item in observations if self._cart_has_item_rows(item)]
            found = self._visible_selected_cart_items(list_views or observations[:1], selected)
            if len(found) == len(selected):
                self.store.event(run_id, "已按购物车上方商品行的 SKU、数量和到手价完成核验，没有滑到页底。")
                return {"items": found, "empty": False, "complete": True, "observations": list_views or observations}
            if self._cart_past_items(observation) or self._selected_skus_visible(list_views, selected):
                break
            if list_views and len(observations) >= 3:
                break
            scroll_meta = observation.get("scroll_meta") or {}
            scroll_y = scroll_meta.get("scroll_y")
            if scroll_y is not None and scroll_y == last_scroll_y:
                scroll_direction = 2 if scroll_direction < 0 else -2
                self.store.event(run_id, "购物车滚动位置没有变化，已反向探测相邻商品行。", "warning")
            last_scroll_y = scroll_y
            observation = await self._desktop_step(
                run_id, "act", {"action": "scroll", "obs_id": observation["obs_id"], "amount": scroll_direction},
                description="商品列表还有一行没露全，小步滚动并保留重叠区域。")
        cart_views = [item for item in observations if self._cart_has_item_rows(item) and not self._cart_past_items(item)]
        if not cart_views:
            cart_views = [item for item in observations if not self._cart_past_items(item)] or observations[:1]
        local_items = self._visible_selected_cart_items(cart_views, selected)
        if len(local_items) == len(selected):
            self.store.event(run_id, "已按当前购物车商品行的 SKU、数量框和单价完成本地核验，无需模型读取整页。")
            return {"items": local_items, "empty": False, "complete": True, "observations": cart_views}
        payload = {
            "candidates": [{k: c[k] for k in ("id", "title", "spec", "sku")} for c in selected],
            "observations": [self._model_observation(o) for o in cart_views],
        }
        if (self._local_judge_requested() or self._uses_local_judge()) and hasattr(self.judge, "complete_json"):
            try:
                data = await self._qwen_json(run_id, prompts.CART_READER, payload)
            except Exception as exc:
                self.store.event(run_id, "本地购物车摘录失败：" + type(exc).__name__ + " " + str(exc)[:200], "warning")
                self._needs_user(run_id, "购物车商品还在上方列表里，本地模型没读全这一屏的商品行。请把商品名称、价格和数量留在屏幕上后继续，不要停在页脚。")
        else:
            message = await self._model(run_id, prompts.CART_READER, payload, json_mode=True)
            data = json_object(message.get("content"))
        page_says_cart = any("购物车" in page_text(o) for o in cart_views)
        on_cart_host = any((urlparse(o.get("url", "")).hostname or "") == "cart.jd.com" for o in cart_views)
        if data.get("is_cart") is not True and not (page_says_cart and on_cart_host):
            self._needs_user(run_id, "未能确认当前页面是已加载的京东购物车，请手动打开购物车并完成登录后继续。")
        by_id = {c["id"]: c for c in selected}
        items = dict(local_items)
        for row in data.get("items", []):
            try:
                cid = row["candidate_id"]
                candidate = by_id[cid]
                if cid in local_items:
                    continue
                if any(e.get("cart_row") and e.get("cart_sku") == candidate["sku"]
                       for o in cart_views for e in o.get("elements", [])):
                    raise ValueError("当前 SKU 的结构化商品行尚未完整核实，不能用位置猜测替代")
                index = row["observation_index"]
                if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(cart_views):
                    raise ValueError("购物车证据页面编号无效")
                obs = cart_views[index]
                elements = {str(e["id"]): e for e in obs.get("elements", [])}
                title_element = elements[str(row["title_element_id"])]
                qty_element = elements[str(row["quantity_element_id"])]
                title_href = title_element.get("href")
                if title_href and sku_from_url(title_href) != candidate["sku"]:
                    raise ValueError("购物车标题链接 SKU 与批准商品不一致")
                title = compact(title_element.get("name"))
                if len(title) < 6 or not (title in compact(candidate["title"]) or compact(candidate["title"]) in title):
                    raise ValueError("购物车商品名称与批准商品不一致")
                proof = str(row.get("spec_evidence") or "")
                if not proof or compact(proof) not in compact(page_text(obs)) or not (compact(proof) in compact(candidate["spec"]) or compact(candidate["spec"]) in compact(proof)):
                    raise ValueError("购物车规格缺少同一商品的可核实证据")
                raw_quantity = str(qty_element.get("value") or qty_element.get("name") or "").strip()
                if str(qty_element.get("role", "")).lower() not in {"edit", "textbox", "spinner", "spinbutton"} or not re.fullmatch(r"\d{1,3}", raw_quantity):
                    raise ValueError("未读到商品数量输入框的真实数值")
                if not self._same_cart_row(title_element, qty_element, list(elements.values())):
                    raise ValueError("无法确认数量输入框与该商品属于同一行")
                if not self._spec_in_cart_row(proof, title_element, qty_element, list(elements.values())):
                    raise ValueError("规格原文未定位到该商品行")
                unit_price = amount(row.get("unit_price"))
                price_element = elements[str(row["price_element_id"])]
                price_proof = str(row.get("price_evidence") or "")
                price_text = str(price_element.get("name", "")) + " " + str(price_element.get("value", ""))
                if not valid_price_evidence(unit_price, price_proof) or compact(price_proof) not in compact(price_text):
                    raise ValueError("购物车单价没有可核实的价格控件证据")
                if not self._spec_in_cart_row(price_proof, title_element, qty_element, list(elements.values())):
                    raise ValueError("价格控件不属于该商品行")
                quantity = int(raw_quantity)
                if not 1 <= quantity <= 999:
                    raise ValueError("购物车数量无效")
                items[cid] = {"candidate_id": cid, "title": candidate["title"], "sku": candidate["sku"],
                              "spec": proof, "quantity": quantity, "price": unit_price, "verified": True,
                              "evidence": {"obs_id": obs["obs_id"], "title_element_id": row["title_element_id"],
                                           "quantity_element_id": row["quantity_element_id"], "quantity_value": raw_quantity,
                                           "price_element_id": row["price_element_id"], "price": price_proof}}
            except (ValueError, KeyError, TypeError, IndexError) as exc:
                self.store.event(run_id, "购物车行证据未通过核验：" + str(exc), "warning")
                continue
        unresolved = []
        for candidate in selected:
            if candidate["id"] in items:
                continue
            grounded = self._grounded_cart_row(cart_views, candidate)
            if grounded:
                items[candidate["id"]] = grounded
            elif self._unreadable_cart_quantity(cart_views, candidate):
                unresolved.append(candidate)
        if unresolved:
            self._needs_user(run_id, "购物车中有相关商品，但商品行的规格或数量尚未读到。请将商品名称、规格和数量框滚动到同屏后点击继续；不会重复加购。")
        return {"items": items, "empty": False, "complete": True, "observations": cart_views}

    @staticmethod
    def _cart_is_empty(observation):
        return bool(_CART_EMPTY.search(page_text(observation)))

    @staticmethod
    def _cart_has_item_rows(observation):
        if (urlparse(observation.get("url", "")).hostname or "") != "cart.jd.com":
            return False
        elements = observation.get("elements") or []
        if any((element.get("cart_row") and element.get("cart_sku")) or element.get("input_purpose") == "quantity"
               for element in elements):
            return True
        text = page_text(observation)
        if _CART_FOOTER.search(text) or _CART_RECOMMEND.search(text):
            return False
        has_product = any(sku_from_url(str(element.get("href") or "")) for element in elements)
        return bool(has_product and _CART_LIST.search(text))

    @staticmethod
    def _cart_past_items(observation):
        """Footer and recommendation viewports are below the cart list."""
        if (urlparse(observation.get("url", "")).hostname or "") != "cart.jd.com":
            return False
        text = page_text(observation)
        if not (_CART_FOOTER.search(text) or _CART_RECOMMEND.search(text)):
            return False
        return not (_CART_LIST.search(text) and ShoppingEngine._cart_has_item_rows(observation))

    def _selected_skus_visible(self, observations, selected):
        wanted = {str(item.get("sku") or "") for item in selected if item.get("sku")}
        if not wanted:
            return False
        seen = set()
        for observation in observations:
            if not self._cart_has_item_rows(observation):
                continue
            for element in observation.get("elements") or []:
                sku = str(element.get("cart_sku") or sku_from_url(str(element.get("href") or "")) or "")
                if sku in wanted:
                    seen.add(sku)
        return seen == wanted

    def _visible_selected_cart_items(self, observations, selected):
        items = {}
        for candidate in selected:
            item = self._grounded_cart_row(observations, candidate)
            if item:
                items[candidate["id"]] = item
        return items

    @staticmethod
    def _cart_row_price(elements):
        """One unconditional price, or the 到手价 when a strikethrough sits beside it."""
        tagged, plain = {}, {}
        for element in elements:
            if element.get("visible") is False or element.get("occluded") is True:
                continue
            for proof in (str(element.get("name") or ""), str(element.get("parent_name") or "")):
                if re.search(r"小计|合计|总价|节省|已优惠|满\d+", proof):
                    continue
                matches = re.findall(r"[￥¥]\s*((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)(?![\d.])", proof)
                if len(matches) != 1:
                    continue
                try:
                    price = amount(matches[0])
                except ValueError:
                    continue
                if not valid_price_evidence(price, proof):
                    continue
                bucket = tagged if re.search(r"到手价", proof) else plain
                bucket.setdefault(price, (element, proof))
        if len(tagged) == 1:
            price, (element, proof) = next(iter(tagged.items()))
            return price, element, proof
        if len(plain) == 1 and not tagged:
            price, (element, proof) = next(iter(plain.items()))
            return price, element, proof
        return None

    @staticmethod
    def _cart_quantity_controls(elements):
        return [element for element in elements
                if str(element.get("role", "")).lower() in {"edit", "textbox", "spinner", "spinbutton"}
                or element.get("input_purpose") == "quantity"]

    def _cart_title_elements(self, elements, sku):
        return [element for element in elements if sku_from_url(str(element.get("href") or "")) == sku]

    def _structured_cart_row(self, observations, candidate):
        """DOM row membership supersedes fragile 60-pixel proximity estimates.

        Membership is emitted only for a container with one SKU and one explicit
        quantity field. Never infer a price from the approved candidate itself.
        """
        found = []
        for observation in observations:
            if observation.get("source") != "dom" or urlparse(observation.get("url", "")).hostname != "cart.jd.com":
                continue
            groups = {}
            for element in observation.get("elements", []):
                if element.get("cart_row") and element.get("cart_sku") == candidate["sku"]:
                    groups.setdefault(element["cart_row"], []).append(element)
            for row_id, elements in groups.items():
                titles = self._cart_title_elements(elements, candidate["sku"])
                if not titles:
                    continue
                quantities = [e for e in self._cart_quantity_controls(elements)
                              if e.get("input_purpose") == "quantity" and e.get("visible") is not False
                              and e.get("occluded") is not True]
                if len(quantities) != 1:
                    continue
                quantity = quantities[0]
                raw = str(quantity.get("value") or "").strip()
                if not re.fullmatch(r"[1-9]\d{0,2}", raw):
                    continue
                priced = self._cart_row_price(elements)
                if not priced:
                    continue
                price, price_element, proof = priced
                title = next((e for e in titles if len(compact(e.get("name"))) >= 6), titles[0])
                found.append({"candidate_id": candidate["id"], "title": candidate["title"], "sku": candidate["sku"],
                              "spec": candidate["spec"], "quantity": int(raw), "price": price, "verified": True,
                              "evidence": {"obs_id": observation.get("obs_id"), "cart_row": row_id,
                                           "sku": candidate["sku"], "spec_source": "exact_sku_row",
                                           "title_element_id": title.get("id"), "quantity_element_id": quantity.get("id"),
                                           "quantity_value": raw, "price_element_id": price_element.get("id"), "price": proof}})
        if not found:
            return None
        if len({(x["evidence"]["cart_row"], x["quantity"], x["price"]) for x in found}) != 1:
            return None  # Duplicate SKU rows or a quantity/price change need review.
        return found[-1]

    def _grounded_cart_row(self, observations, candidate):
        structured = self._structured_cart_row(observations, candidate)
        if structured:
            return structured
        if any(e.get("cart_row") and e.get("cart_sku") == candidate["sku"]
               for o in observations for e in o.get("elements", [])):
            return None
        sku = candidate["sku"]
        for observation in observations:
            elements = list(observation.get("elements") or [])
            titles = self._cart_title_elements(elements, sku)
            quantities = [element for element in self._cart_quantity_controls(elements)
                          if re.fullmatch(r"\d{1,3}", str(element.get("value") or element.get("name") or "").strip())]
            for title in titles:
                for quantity in quantities:
                    if not self._same_cart_row(title, quantity, elements):
                        continue
                    raw = str(quantity.get("value") or quantity.get("name") or "").strip()
                    price, price_element, price_proof = None, None, ""
                    for element in elements:
                        text = f"{element.get('name', '')} {element.get('value', '')}"
                        if not re.search(r"[￥¥]", text) or not self._spec_in_cart_row(text, title, quantity, elements):
                            continue
                        try:
                            price = amount(re.search(r"[￥¥]\s*[\d,.]+", text).group())
                        except (AttributeError, ValueError):
                            continue
                        price_element, price_proof = element, text.strip()
                        break
                    if price_element is None or not valid_price_evidence(price, price_proof):
                        continue
                    return {"candidate_id": candidate["id"], "title": candidate["title"], "sku": sku,
                            "spec": candidate["spec"], "quantity": int(raw), "price": price, "verified": True,
                            "evidence": {"obs_id": observation.get("obs_id"), "title_element_id": title.get("id"),
                                         "quantity_element_id": quantity.get("id"), "quantity_value": raw,
                                         "price_element_id": (price_element or {}).get("id"), "price": price_proof}}
        return None

    def _unreadable_cart_quantity(self, observations, candidate):
        sku = candidate["sku"]
        for observation in observations:
            # Only a structured cart row proves the SKU is already in the cart.
            # 猜你喜欢 / 看了又看 links share the same item.jd.com SKU and must
            # not pause add-to-cart as “quantity not readable”.
            if any(e.get("cart_row") and e.get("cart_sku") == sku
                   for e in observation.get("elements") or []):
                return True
        return False

    @staticmethod
    def _rect(element):
        rect = element.get("rect")
        if isinstance(rect, dict):
            left, top = rect.get("left", rect.get("x")), rect.get("top", rect.get("y"))
            right = rect.get("right", (left + rect.get("width", 0)) if left is not None else None)
            bottom = rect.get("bottom", (top + rect.get("height", 0)) if top is not None else None)
            return (left, top, right, bottom) if None not in (left, top, right, bottom) else None
        return tuple(rect) if isinstance(rect, (list, tuple)) and len(rect) == 4 else None

    @staticmethod
    def _same_cart_row(title, quantity, elements=None):
        # Ancestor text alone is not proof: outer cart containers contain every
        # product title. A spatial row association must also be demonstrated.
        title_name = compact(title.get("name"))
        a, b = ShoppingEngine._rect(title), ShoppingEngine._rect(quantity)
        if not a or not b:
            return False
        cy = (b[1] + b[3]) / 2
        distance = abs((a[1] + a[3]) / 2 - cy)
        if distance > 60:
            return False
        # A different nearby product row must not be closer to this quantity.
        for other in elements or []:
            if not sku_from_url(str(other.get("href") or "")) or compact(other.get("name")) == title_name:
                continue
            rect = ShoppingEngine._rect(other)
            if rect and abs((rect[1] + rect[3]) / 2 - cy) <= distance + 5:
                return False
        return max(a[1], b[1]) < min(a[3], b[3])

    @staticmethod
    def _spec_in_cart_row(proof, title, quantity, elements):
        wanted, title_name = compact(proof), compact(title.get("name"))
        title_rect, qty_rect = ShoppingEngine._rect(title), ShoppingEngine._rect(quantity)
        for element in elements:
            value = compact(str(element.get("name", "")) + str(element.get("value", "")))
            if wanted not in value:
                continue
            rect = ShoppingEngine._rect(element)
            if not rect or not title_rect or not qty_rect:
                continue
            cy = (rect[1] + rect[3]) / 2
            low, high = min(title_rect[1], qty_rect[1]) - 15, max(title_rect[3], qty_rect[3]) + 45
            if not low <= cy <= high:
                continue
            closer_other = False
            for other in elements:
                other_rect = ShoppingEngine._rect(other)
                if not other_rect or not sku_from_url(str(other.get("href") or "")) or compact(other.get("name")) == title_name:
                    continue
                if abs((other_rect[1] + other_rect[3]) / 2 - cy) < abs((title_rect[1] + title_rect[3]) / 2 - cy):
                    closer_other = True
                    break
            if not closer_other:
                return True
        return False

    async def _purchase(self, run_id):
        run = self._checkpoint(run_id)
        ids = run.get("approved_ids") or []
        validation = check_selection(run["plan"], run["candidates"], ids)
        if validation["errors"]:
            self._needs_user(run_id, "已确认方案不再通过校验：" + "；".join(validation["errors"]))
        selected = [c for c in run["candidates"] if c["id"] in ids]
        self.store.update(run_id, status="verifying", phase="核对购物车原有数量")
        snapshot = await self._cart_snapshot(run_id, selected)
        current = snapshot["items"]
        if run.get("baseline_cart") is None:
            baseline = {c["id"]: current.get(c["id"], {}).get("quantity", 0) for c in selected}
            self.store.update(run_id, baseline_cart=baseline)
            self.store.event(run_id, "已记录所选商品在原购物车中的数量，保留所有原有商品。")
        else:
            baseline = run["baseline_cart"]
        ledger = run.get("add_journal") or {}
        for candidate in selected:
            self._checkpoint(run_id)
            cid = candidate["id"]
            expected = int(baseline.get(cid, 0)) + candidate.get("quantity", 1)
            found = current.get(cid, {}).get("quantity", 0)
            entry = ledger.get(cid, {})
            if entry.get("state") in {"click_sent", "verified"}:
                if found == expected:
                    ledger[cid] = {**entry, "state": "verified", "expected_quantity": expected}
                    self.store.update(run_id, add_journal=ledger)
                    continue
                self._needs_user(run_id, f"{candidate['title']}已发出过加购动作，但购物车数量为 {found}，预期为 {expected}。请手动核实并调整到预期数量后继续；程序不会再次点击加购。")
            if found != int(baseline.get(cid, 0)):
                self._needs_user(run_id, f"{candidate['title']}的购物车数量在执行期间发生变化。请手动核实后停止并新建任务，避免重复加购。")
            self.store.update(run_id, status="adding", phase=f"核对并加入{candidate['title'][:24]}")
            await self._add_one(run_id, candidate, expected)
            ledger = self.store.get(run_id).get("add_journal") or {}
            self.store.update(run_id, status="verifying", phase="读取购物车验证本次加购")
            snapshot = await self._cart_snapshot(run_id, selected)
            current = snapshot["items"]
            self.store.update(run_id, cart_items=list(current.values()))
            if current.get(cid, {}).get("quantity", 0) != expected:
                self._needs_user(run_id, f"已经点击{candidate['title']}的加购按钮，但购物车尚未核实到预期数量 {expected}。请检查购物车后继续，程序不会重复点击。")
            if current[cid]["price"] > candidate["price"]:
                self._needs_user(run_id, f"{candidate['title']}的购物车单价变为 ¥{current[cid]['price']:.2f}，高于批准单价 ¥{candidate['price']:.2f}。请手动核实价格，或停止后重新选择方案。")
            ledger[cid] = {**ledger[cid], "state": "verified", "expected_quantity": expected}
            deltas = self.store.get(run_id).get("cart_deltas") or {}
            deltas[cid] = {"sku": candidate.get("sku"), "before_quantity": int(baseline.get(cid, 0)),
                           "after_quantity": int(current[cid].get("quantity", 0)),
                           "price": current[cid].get("price"), "title": candidate.get("title")}
            self.store.update(run_id, add_journal=ledger, cart_deltas=deltas, cart_items=list(current.values()))
            self.store.event(run_id, f"购物车已核实：{candidate['title']}，总数量 {expected}（原有 {baseline.get(cid, 0)}）。")
        # A fresh read is required even after each click has individually verified.
        self.store.update(run_id, status="verifying", phase="最终核验全部商品")
        final = await self._cart_snapshot(run_id, selected)
        failures = [c["title"] for c in selected if final["items"].get(c["id"], {}).get("quantity", 0)
                    != int(baseline.get(c["id"], 0)) + c.get("quantity", 1)]
        if failures:
            self._needs_user(run_id, "最终购物车核验不一致：" + "、".join(failures) + "。请手动查看后继续核验。")
        final_total = round(sum(final["items"][c["id"]]["price"] * c.get("quantity", 1) for c in selected), 2)
        if any(final["items"][c["id"]]["price"] > c["price"] for c in selected):
            self._needs_user(run_id, "最终购物车中有商品单价高于批准价格。请核实价格变化后停止并重新选择方案。")
        if run["plan"].get("budget") and final_total > run["plan"]["budget"]:
            self._needs_user(run_id, f"最终购物车本次商品金额 ¥{final_total:.2f} 超过预算，已停止后续动作。")
        evidence_path = None
        if self._setting("screenshot_evidence", False):
            root = Path(self._setting("root", Path.cwd()))
            target = root / "data" / "evidence" / f"{run_id}-cart.png"
            target.parent.mkdir(parents=True, exist_ok=True)
            result = await self.call_desktop("screenshot", str(target))
            self._checkpoint(run_id)
            if result.get("status") == "ok":
                evidence_path = str(target)
            else:
                self.store.event(run_id, "购物车文字已核实，但截图留证失败：" + str(result.get("message", "")), "warning")
        if self._local_judge_requested() or self._uses_local_judge():
            await self._confirm_cart_selection(run_id, selected, final["items"])
        self._checkpoint(run_id)
        self.store.update(run_id, status="completed", phase="购物车核验完成", stage="done", error=None,
                          cart_items=list(final["items"].values()), screenshot_path=evidence_path,
                          cart_total=final_total,
                          summary=f"已在京东购物车重新读取并核实 {len(selected)} 件所选商品的规格、数量和单价；本次商品金额 ¥{final_total:.2f}。运费及条件优惠以京东结算页为准。")
        self.store.event(run_id, "全部所选商品已通过购物车实际页面核验。任务到此完成。")

    async def _confirm_cart_selection(self, run_id, selected, cart_items):
        message = await self._model(run_id, prompts.CART_CONFIRM, {
            "approved": [{key: item.get(key) for key in ("id", "title", "sku", "spec", "quantity", "price")} for item in selected],
            "cart": [{key: (cart_items.get(item["id"]) or {}).get(key) for key in
                      ("candidate_id", "title", "sku", "quantity", "price")} for item in selected],
        }, json_mode=True)
        decision = json_object(message.get("content"))
        if decision.get("ok") is True:
            return
        missing = decision.get("missing") or []
        reason = str(decision.get("reason") or "批准商品与购物车摘录不一致")
        if missing:
            reason = reason + "：" + "、".join(str(item) for item in missing[:6])
        self._needs_user(run_id, "购物车核验未通过：" + reason)

    def _local_add_args(self, candidate, observation):
        elements = observation.get("elements") or []
        button = next((element for element in elements
                       if re.search(r"加入购物车|加购物车|加购", str(element.get("name") or ""))
                       and element.get("visible") is not False and element.get("enabled") is not False
                       and not element.get("blocked") and not element.get("occluded")), None)
        if not button:
            return None
        text = page_text(observation)
        spec = str(candidate.get("spec") or "")
        if not spec or compact(spec) not in compact(text):
            spec = str(observation.get("title") or candidate.get("title") or "")
        proof = self._approved_spec_price_proof(candidate, observation)
        quantity = next((element for element in elements
                         if element.get("input_purpose") == "quantity"
                         or (str(element.get("role", "")).lower() in {"edit", "textbox", "spinner", "spinbutton"}
                             and re.fullmatch(r"\d+", str(element.get("value") or "").strip()))), None)
        return {"element_id": button["id"], "spec_evidence": spec, "price_evidence": proof,
                "quantity_element_id": quantity["id"] if quantity else ""}

    @staticmethod
    def _approved_spec_price_proof(candidate, observation):
        """Use the current selected-spec selling price, never a leftover listing/title price."""
        record = extract_product_record(observation)
        proof = str(record.get("price_evidence") or "")
        if proof and price_is_visible(candidate["price"], proof) and valid_price_evidence(candidate["price"], proof):
            return proof
        if proof:
            # A different visible selling price is either the default landing
            # spec or a real price change. Do not harvest a stray matching
            # number from recommendations or the page title.
            return proof
        text = page_text(observation)
        for match in re.finditer(r"((?:京东价|价格|售价)\s*[￥¥]\s*[\d,.]+|[￥¥]\s*[\d,.]+)", text):
            candidate_proof = match.group(1)
            if price_is_visible(candidate["price"], candidate_proof) and valid_price_evidence(candidate["price"], candidate_proof):
                return candidate_proof.strip()
        return proof

    @staticmethod
    def _spec_chip_selected(element, observation):
        if element.get("selected") is True or element.get("checked") is True:
            return True
        selected = observation.get("selected_spec") or observation.get("selected_specs") or ""
        if isinstance(selected, (list, tuple)):
            selected = " ".join(str(value) for value in selected)
        name = compact(element.get("name"))
        return bool(name and name in compact(selected))

    @staticmethod
    def _spec_option_chips(candidate, observation):
        spec_text = compact(candidate.get("spec") or "")
        if not spec_text:
            return []
        page_sku = sku_from_url(observation.get("url") or "")
        chips = []
        for element in observation.get("elements") or []:
            if element.get("visible") is False or element.get("enabled") is False:
                continue
            if element.get("blocked") or element.get("occluded"):
                continue
            name = str(element.get("name") or "").strip()
            if not name or _NOT_SPEC_CONTROL.search(name):
                continue
            label = compact(name)
            if len(label) < 2 or len(label) > 48 or label not in spec_text:
                continue
            role = str(element.get("role") or "").lower()
            clickable = role in {"button", "hyperlink", "link", "radiobutton", "checkbox", "tabitem"}
            if role not in {"text"} and not clickable:
                continue
            href = str(element.get("href") or "")
            if href:
                host = (urlparse(href).hostname or "").lower()
                if host and host != "item.jd.com":
                    continue
                href_sku = sku_from_url(href)
                if href_sku and page_sku and href_sku != page_sku:
                    continue
            area = " ".join(str(element.get(key) or "") for key in ("class_name", "parent_name"))
            area += " " + " ".join(str(value) for value in (element.get("ancestor_names") or []))
            in_area = bool(_SPEC_AREA.search(area))
            looks_variant = bool(_VARIANT_LABEL.search(name))
            if role == "text" and not (in_area or looks_variant):
                continue
            if clickable and not (in_area or looks_variant):
                continue
            chips.append(element)
        chips.sort(key=lambda item: len(compact(item.get("name"))), reverse=True)
        return chips

    @staticmethod
    def _next_unselected_spec_chip(candidate, observation, clicked=None):
        seen = {str(value) for value in (clicked or set())}
        for element in ShoppingEngine._spec_option_chips(candidate, observation):
            if str(element.get("id")) in seen:
                continue
            if not ShoppingEngine._spec_chip_selected(element, observation):
                return element
        return None

    async def _add_one_local(self, run_id, candidate, expected):
        observation = await self._navigate(run_id, candidate["url"])
        clicked_specs = set()
        for _ in range(10):
            self._checkpoint(run_id)
            if sku_from_url(observation.get("url", "")) != candidate["sku"]:
                self._needs_user(run_id, "商品页面跳转到了其它 SKU。请返回已批准商品后继续，程序不会替换商品。")
            chip = self._next_unselected_spec_chip(candidate, observation, clicked_specs)
            if chip:
                try:
                    action = guard_action({"action": "click", "obs_id": observation["obs_id"],
                                           "element_id": chip["id"]}, observation)
                    clicked_specs.add(str(chip["id"]))
                    observation = await self._desktop_step(
                        run_id, "act", action,
                        description=f"选择已批准规格：{chip.get('name') or candidate.get('spec')}")
                    continue
                except (ValueError, KeyError, TypeError) as exc:
                    self.store.event(run_id, "未能点选已批准规格：" + str(exc), "warning")
            args = self._local_add_args(candidate, observation)
            if args:
                try:
                    self._validate_add(candidate, observation, args)
                    ledger = self.store.get(run_id).get("add_journal") or {}
                    if ledger.get(candidate["id"], {}).get("state") in {"click_sent", "verified"}:
                        self._needs_user(run_id, "当前商品已有加购记录，必须核对购物车后才能继续。")
                    action = guard_action({"action": "click", "obs_id": observation["obs_id"],
                                           "element_id": args["element_id"]}, observation, allow_add=True,
                                          approved_sku=candidate["sku"],
                                          approved_quantity=candidate.get("quantity", 1))
                    action.update(allow_add=True, approved_sku=candidate["sku"],
                                  approved_quantity=candidate.get("quantity", 1))
                    ledger[candidate["id"]] = {"state": "click_sent", "expected_quantity": expected,
                                                "obs_id": observation["obs_id"],
                                                "at": datetime.now(timezone.utc).isoformat()}
                    self.store.update(run_id, add_journal=ledger)
                    await self._desktop_step(run_id, "act", action, description=f"点击一次加入购物车：{candidate['title']}")
                    return
                except (ValueError, KeyError, TypeError) as exc:
                    self.store.event(run_id, "本地加购核验未通过：" + str(exc), "warning")
            observation = await self._desktop_step(
                run_id, "act", {"action": "scroll", "obs_id": observation["obs_id"], "amount": -5},
                description="滚动查找已批准规格、加购按钮和该规格售价。")
        self._needs_user(run_id, "未能在当前商品页用已批准规格和售价完成本地加购。请核对该页颜色、尺码和价格后继续。")

    async def _add_one(self, run_id, candidate, expected):
        if self._local_judge_requested() or self._uses_local_judge():
            return await self._add_one_local(run_id, candidate, expected)
        observation = await self._navigate(run_id, candidate["url"])
        outcomes, last_signature, stagnant, failures = [], None, 0, 0
        for _ in range(24):
            self._checkpoint(run_id)
            if sku_from_url(observation.get("url", "")) != candidate["sku"]:
                self._needs_user(run_id, "商品页面跳转到了其它 SKU。请返回已批准商品后继续，程序不会替换商品。")
            signature = self._signature(observation)
            stagnant = stagnant + 1 if signature == last_signature else 0
            last_signature = signature
            if stagnant >= self._setting("max_no_progress", 5):
                self._needs_user(run_id, "加购前页面连续没有进展。请核对当前商品规格、价格和数量后继续。")
            message = await self._model(run_id, prompts.BUYER, {"approved_candidate": candidate_context(candidate),
                                        "observation": self._model_observation(observation), "recent_outcomes": outcomes[-6:]}, tools=prompts.BUY_TOOLS)
            calls = message.get("tool_calls") or []
            if not calls:
                outcomes.append("请调用工具进行操作或请求用户处理。")
                failures += 1
                continue
            call = calls[0].get("function", {})
            try:
                args = json_object(call.get("arguments", "{}"))
                name = call.get("name")
                if name == "action":
                    action = guard_action(args, observation, allow_quantity=True, approved_sku=candidate["sku"],
                                          approved_quantity=candidate.get("quantity", 1))
                    action.update(allow_quantity=True, approved_sku=candidate["sku"], approved_quantity=candidate.get("quantity", 1))
                    if action["action"] == "scroll":
                        action["amount"] = abs(action["amount"]) * (-1 if action.pop("direction") == "down" else 1)
                    observation = await self._desktop_step(run_id, "act", action, description="核对商品规格/数量。")
                elif name == "read_page":
                    observation = await self._observe(run_id)
                elif name == "request_user":
                    self._needs_user(run_id, args.get("reason") or "请查看当前商品页后继续。")
                elif name == "add_to_cart":
                    self._validate_add(candidate, observation, args)
                    ledger = self.store.get(run_id).get("add_journal") or {}
                    if ledger.get(candidate["id"], {}).get("state") in {"click_sent", "verified"}:
                        self._needs_user(run_id, "当前商品已有加购记录，必须核对购物车后才能继续。")
                    action = guard_action({"action": "click", "obs_id": observation["obs_id"],
                                           "element_id": args["element_id"]}, observation, allow_add=True, approved_sku=candidate["sku"],
                                          approved_quantity=candidate.get("quantity", 1))
                    action.update(allow_add=True, approved_sku=candidate["sku"], approved_quantity=candidate.get("quantity", 1))
                    # Durable write BEFORE input. Crash/timeout is uncertain,
                    # never permission to repeat an add-to-cart mutation.
                    ledger[candidate["id"]] = {"state": "click_sent", "expected_quantity": expected,
                                                "obs_id": observation["obs_id"], "at": datetime.now(timezone.utc).isoformat()}
                    self.store.update(run_id, add_journal=ledger)
                    await self._desktop_step(run_id, "act", action, description=f"点击一次加入购物车：{candidate['title']}")
                    return
                else:
                    raise ValueError("当前阶段没有该工具")
                failures = 0
                outcomes.append("已执行并重新观察。")
            except (ValueError, KeyError, TypeError) as exc:
                failures += 1
                outcomes.append("尚未执行加购：" + str(exc))
                self.store.event(run_id, outcomes[-1], "warning")
                if failures >= 4:
                    self._needs_user(run_id, "加购前信息未通过核验：" + str(exc) + "。请手动查看后继续。")
        self._needs_user(run_id, "加购前核验步骤过多，已暂停。请核对当前商品页面后继续。")

    @staticmethod
    def _validate_add(candidate, observation, args):
        text = page_text(observation)
        if sku_from_url(observation.get("url", "")) != candidate["sku"]:
            raise ValueError("当前 SKU 未获批准")
        spec = str(args.get("spec_evidence") or "")
        if not spec or compact(spec) not in compact(text):
            raise ValueError("当前规格未与批准规格逐字核对")
        quoted, approved = compact(spec), compact(candidate["spec"])
        if approved not in quoted and quoted not in approved:
            if not selected_spec_matches(candidate["spec"], observation, candidate["title"]):
                raise ValueError("当前规格未与批准规格逐字核对")
        if not selected_spec_matches(candidate["spec"], observation, candidate["title"]):
            raise ValueError("当前已选规格与批准规格不同，或页面未暴露已选状态")
        price_proof = str(args.get("price_evidence") or "")
        if not price_proof or compact(price_proof) not in compact(text) or not price_is_visible(candidate["price"], price_proof):
            raise ValueError("当前售价未与批准价格核对，可能已经变价")
        if not valid_price_evidence(candidate["price"], price_proof):
            raise ValueError("当前提供的是有条件价格")
        elements = {str(e["id"]): e for e in observation.get("elements", [])}
        button = elements.get(str(args.get("element_id")), {})
        if not re.search(r"加入购物车|加购物车|加购", str(button.get("name", ""))):
            raise ValueError("指定控件不是加入购物车按钮")
        quantity_id = str(args.get("quantity_element_id") or "")
        if quantity_id:
            element = elements.get(quantity_id, {})
            quantity = str(element.get("value") or element.get("name") or "").strip()
            if not quantity.isdigit() or int(quantity) != candidate.get("quantity", 1):
                raise ValueError("详情页数量与批准数量不一致")
        else:
            # Absence is only acceptable when no numeric quantity control exists.
            numeric_edits = [e for e in elements.values() if str(e.get("role", "")).lower() in {"edit", "textbox", "spinner", "spinbutton"}
                             and re.fullmatch(r"\d+", str(e.get("value") or e.get("name") or "").strip())]
            if candidate.get("quantity", 1) != 1 or numeric_edits:
                raise ValueError("请明确读取商品数量输入框")
