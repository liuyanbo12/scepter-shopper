"""Small model views; the executor retains the full authoritative observation."""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit


def display_url(value):
    raw = str(value or "")
    parsed = urlsplit(raw)
    if parsed.hostname == "item.jd.com":
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    if parsed.hostname == "search.jd.com":
        query = parse_qs(parsed.query).get("keyword", [])
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                           urlencode({"keyword": query[0]}) if query else "", ""))
    return raw


def compact_observation(observation):
    # No DOM paths, CSS classes, duplicate ancestor chains or browser coordinates
    # are needed to choose an ID. Fresh-target checks still use the original.
    keys = ("obs_id", "title", "source", "ready_state", "viewport", "navigation",
            "selected_spec", "selected_specs", "truncated")
    result = {k: observation[k] for k in keys if k in observation}
    result["url"] = display_url(observation.get("url"))
    text = str(observation.get("text") or "")
    result["text"] = text
    known = {"".join(text.split()).lower()}
    context, rows, elements = [], {}, []
    cart = urlsplit(str(observation.get("url") or "")).hostname == "cart.jd.com"
    for entry in observation.get("elements", []):
        item = {k: entry[k] for k in ("id", "role", "name", "value", "input_purpose",
                "selected", "checked", "blocked", "occluded", "enabled", "visible", "sku")
                if entry.get(k) not in (None, "")}
        if entry.get("href"):
            item["href"] = display_url(entry["href"])
        if cart:
            if entry.get("rect") or entry.get("css_rect"):
                item["rect"] = entry.get("rect") or entry["css_rect"]
            if entry.get("cart_row"):
                item["cart_row"] = rows.setdefault(entry["cart_row"], f"r{len(rows) + 1}")
                item["cart_sku"] = entry.get("cart_sku")
        # Parent/group strings can hold price labels or ISBN not present in a
        # text node. Preserve each distinct original once, never summarize it.
        for key in ("parent_name", "group_text"):
            value = str(entry.get(key) or "")
            normalized = "".join(value.split()).lower()
            if normalized and not any(normalized in existing for existing in known):
                known.add(normalized)
                context.append(value)
        elements.append(item)
    result["elements"] = elements
    if context:
        result["context"] = context
    return result


def candidate_context(candidate):
    return {k: candidate[k] for k in ("id", "category", "title", "sku", "spec", "brand", "model",
            "price", "quantity", "store", "rating", "promotions", "compatibility", "reason") if k in candidate}


def search_plan_context(plan):
    result = {k: plan[k] for k in ("scenario", "purpose", "summary", "budget", "reused_components", "needs_monitor") if k in plan}
    result["categories"] = [{k: c[k] for k in ("id", "name", "required", "budget_allocation") if k in c}
                            for c in plan.get("categories", [])]
    return result


def json_chars(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
