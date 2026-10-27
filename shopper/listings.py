"""Compact product records extracted from the current observation only."""
from __future__ import annotations

import re

from .rules import amount, compact, page_text, sku_from_url, valid_price_evidence

_PROMO_TITLE = re.compile(
    r"券满\d*享[\d.]+折|领券|满\d+减\d+|满\d+元?减|广告商品|赞助商品|^广告$|^赞助$"
)


def extract_search_listings(observation: dict, limit: int = 12) -> list[dict]:
    """Keep one search-result card per SKU; never invent a price."""
    listings: dict[str, dict] = {}
    for element in observation.get("elements", []):
        sku = str(element.get("sku") or sku_from_url(str(element.get("href") or "")) or "")
        title = _listing_title(element)
        if not sku or len(compact(title)) < 6:
            continue
        evidence = " ".join(part for part in (title, element.get("group_text"), element.get("parent_name")) if part)
        price, proof = _visible_price(evidence) or (None, "")
        current = listings.get(sku)
        if current:
            if len(compact(title)) > len(compact(current["title"])):
                current["title"] = title[:180]
            if not current.get("price") and price:
                current["price"] = price
                current["price_evidence"] = proof
            continue
        if len(listings) >= limit:
            continue
        listings[sku] = {
            "id": f"listing_{sku}",
            "sku": sku,
            "title": title[:180],
            "url": f"https://item.jd.com/{sku}.html",
            "price": price,
            "price_evidence": proof,
            "store": "",
            "spec": "",
            "stock": "未核实",
            "source": "search_result",
            "evidence": evidence[:400],
        }
    return list(listings.values())


def extract_product_record(observation: dict, listing: dict | None = None) -> dict:
    """Detail-page facts only. Missing price/spec stay unverified."""
    sku = sku_from_url(str(observation.get("url") or (listing or {}).get("url") or ""))
    title = _product_title(observation, listing)
    text = page_text(observation)
    spec = _page_spec(observation, title, listing)
    price, proof = _visible_price(text) or (None, "")
    store = _first_match(r"(?:店铺|自营旗舰店|旗舰店)[:：]?\s*([^\n]{2,40})", text)
    stock = "有货" if re.search(r"有货|现货", text) else "未核实"
    if re.search(r"无货|缺货|暂不销售", text):
        stock = "缺货"
    return {
        "id": f"listing_{sku}" if sku else (listing or {}).get("id") or "listing_unknown",
        "sku": sku,
        "title": title[:180],
        "url": f"https://item.jd.com/{sku}.html" if sku else str((listing or {}).get("url") or ""),
        "price": price,
        "price_evidence": proof,
        "store": store or str((listing or {}).get("store") or ""),
        "spec": spec[:120],
        "stock": stock,
        "source": "product_page",
        "evidence": text[:800],
        "brand": _first_match(r"品牌[:：]\s*([^\n]{1,40})", text),
        "model": _first_match(r"型号[:：]\s*([^\n]{1,40})", text),
    }


def compact_judge_record(record: dict) -> dict:
    return {key: record[key] for key in (
        "id", "sku", "title", "spec", "price", "price_evidence", "store", "stock", "url"
    ) if record.get(key) not in (None, "")}


def _listing_title(element: dict) -> str:
    for candidate in (element.get("name"), *(
            line.strip() for line in str(element.get("group_text") or "").splitlines()),
                      element.get("parent_name")):
        title = str(candidate or "").strip()
        if len(compact(title)) >= 6 and not _promo_title(title):
            return title[:180]
    return ""


def _product_title(observation: dict, listing: dict | None = None) -> str:
    text = page_text(observation)
    page = str(observation.get("title") or "").strip()
    listed = str((listing or {}).get("title") or "").strip()
    if _usable_title(page) and (not _usable_title(listed) or _promo_title(listed)):
        return page[:180]
    if _usable_title(listed) and compact(listed) in compact(text):
        return listed[:180]
    if _usable_title(page):
        return page[:180]
    for element in observation.get("elements", []):
        name = str(element.get("name") or "").strip()
        if _usable_title(name) and not _promo_title(name):
            return name[:180]
    return (page or listed)[:180]


def _page_spec(observation: dict, title: str, listing: dict | None = None) -> str:
    selected = observation.get("selected_spec") or observation.get("selected_specs") or ""
    if isinstance(selected, (list, tuple)):
        selected = " ".join(str(value) for value in selected)
    spec = str(selected or (listing or {}).get("spec") or "").strip()
    if spec:
        return spec[:120]
    # Books and landing SKUs often have no selected chip. Use a contiguous
    # title fragment already on the page; never invent “标准装”.
    for candidate in (title, observation.get("title")):
        fragment = _title_identity(candidate)
        if fragment:
            return fragment[:120]
    return ""


def _title_identity(title: object) -> str:
    cleaned = re.sub(r"【图片.*?】.*$", "", str(title or "")).strip()
    cleaned = re.sub(r"\s*[-—]\s*京东\s*$", "", cleaned).strip()
    return cleaned if len(compact(cleaned)) >= 6 else ""


def _usable_title(text: str) -> bool:
    return len(compact(text)) >= 6


def _promo_title(text: str) -> bool:
    label = compact(text)
    return bool(label and _PROMO_TITLE.search(label) and len(label) < 20)


def _visible_price(text: str) -> tuple[float, str] | None:
    for match in re.finditer(r"((?:京东价|价格|售价)\s*[￥¥]\s*[\d,.]+|[￥¥]\s*[\d,.]+)", str(text or "")):
        proof = match.group(1)
        if re.search(r"起|定金|订金|预付款|每期|月供|补贴后|券后|返后|以旧换新|原价|划线价|参考价|市场价", proof):
            continue
        try:
            price = amount(re.search(r"[￥¥]\s*[\d,.]+", proof).group())
        except (ValueError, AttributeError):
            continue
        if valid_price_evidence(price, proof) or re.search(r"[￥¥]", proof):
            return price, proof.strip()
    return None


def _first_match(pattern: str, text: str) -> str:
    match = re.search(pattern, text)
    return match.group(1).strip() if match else ""
