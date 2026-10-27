"""Evidence and purchasing constraints; no product prices are hard coded here."""
from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, urlparse


COMPUTER_PARTS = {
    "cpu": "CPU", "motherboard": "主板", "gpu": "显卡", "memory": "内存",
    "storage": "SSD", "psu": "电源", "case": "机箱", "cooler": "散热器",
    "monitor": "显示器",
}
DISALLOWED_LABELS = (
    "提交订单", "去结算", "结算", "立即购买", "立即抢购", "立即支付", "确认支付",
    "删除", "清空", "移出购物车", "移到我的关注", "确认收货", "开通白条", "分期购买",
    "发送消息", "发送给客服", "联系卖家", "联系客户", "支付密码", "领取并购买",
)


def compact(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def page_text(observation: dict) -> str:
    """Keep element labels AND values: editable quantity often only has a value."""
    chunks = [str(observation.get("title", "")), str(observation.get("text", ""))]
    selected = observation.get("selected_spec") or observation.get("selected_specs")
    if selected:
        chunks.append(" ".join(map(str, selected)) if isinstance(selected, (list, tuple)) else str(selected))
    for element in observation.get("elements", []):
        chunks.extend(str(element.get(key, "")) for key in ("name", "value", "parent_name"))
    return "\n".join(chunk for chunk in chunks if chunk)


def amount(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("金额不能是布尔值")
    try:
        parsed = Decimal(str(value).replace(",", "").replace("￥", "").replace("¥", "").strip())
    except (InvalidOperation, ValueError):
        raise ValueError("金额必须为页面上可核实的数字") from None
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError("金额必须大于零且为有限数值")
    return float(parsed.quantize(Decimal("0.01")))


def jd_url(value: object, *, product: bool = False) -> str:
    value = str(value or "").strip()
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not (hostname == "jd.com" or hostname.endswith(".jd.com")):
        raise ValueError("只允许打开京东 jd.com 页面")
    if parsed.username or parsed.password:
        raise ValueError("网址不能包含用户名或密码")
    if product and not re.search(r"/(\d+)\.html(?:$|[?#])", value):
        raise ValueError("候选必须来自有明确 SKU 的京东商品详情页")
    return value


def sku_from_url(url: str) -> str:
    match = re.search(r"/(\d+)\.html(?:$|[?#])", url)
    return match.group(1) if match else ""


def is_multi_sku_listing(title: str) -> bool:
    """True for seller pages that list many CPU/GPU variants in one title."""
    text = str(title or "")
    codes = re.findall(r"(?<![A-Za-z0-9])(\d{4,5}X(?:3D)?|[iI][3579]-\d{4,5}[A-Za-z]*)(?![A-Za-z0-9])", text)
    unique = {item.upper() for item in codes}
    if len(unique) >= 3:
        return True
    return bool(re.search(r"系列", text) and len(unique) >= 2)


def specific_model(category: str, model: str) -> bool:
    """Reject obvious families/spec-only labels; existence still needs JD proof.

    This is a completeness check, not an offline product catalogue. A detailed
    model passing it remains explicitly unverified until read on a product page.
    """
    label = compact(model)
    if len(label) < 2 or re.search(r"级别|同级|同档|左右|待定|待查|未知|不确定|任意|某品牌|主流|[￥¥]|\d+(?:\.\d+)?元", label):
        return False
    if category not in COMPUTER_PARTS:
        return True
    if category == "cpu":
        return bool(re.search(r"\d{3,5}[a-z]*", label))
    if category == "gpu":
        # A GPU chip name alone does not identify the board/vendor edition.
        remainder = re.sub(r"(?:geforce|radeon|rtx|gtx|rx|arc)|\d{3,5}(?:(?:ti|super|xtx|xt))*|显卡|游戏|\d+gb", "", label)
        return bool(re.search(r"\d{3,5}", label) and re.search(r"[a-z\u4e00-\u9fff]", remainder))
    remainder = re.sub(r"(?:ddr[345]|pcie[345](?:\.0)?|nvme|ssd|atx(?:[23]\.\d)?|m[.-]?atx|mini[.-]?itx|itx|rgb)", "", label)
    remainder = re.sub(r"\d+(?:\.\d+)?(?:gb|tb|mhz|mt/s|w|瓦|寸|英寸|hz|k|mm|毫米)|[\d\s+×*()./-]+", "", remainder)
    remainder = re.sub(r"内存条?|套装|固态|硬盘|电源|金牌|全模组|主板|机箱|中塔|全塔|散热器|风冷|水冷|双塔|单塔|显示器|电竞|游戏|高性能|处理器|黑色|白色|标准版|标准装", "", remainder)
    return bool(re.search(r"[a-z\u4e00-\u9fff]", remainder))


def _search_models(category: dict) -> tuple[list[dict], bool]:
    targets, seen = [], set()
    entries = category.get("target_models") or []
    if not isinstance(entries, list):
        raise ValueError("型号备选必须为数组")
    for entry in entries[:3]:
        if not isinstance(entry, dict):
            continue
        target = {key: str(entry.get(key) or "").strip() for key in ("brand", "model", "variant", "search_query", "reason")}
        if (not target["brand"] or not target["variant"]
            or re.search(r"待定|待查|未知|任意|主流|某品牌", target["brand"])
            or not specific_model(category["id"], target["model"])):
            continue
        if re.search(r"[￥¥]|\d+(?:\.\d+)?元", " ".join(target[key] for key in ("brand", "model", "variant", "search_query"))):
            continue
        # A vague supplied query cannot undo the complete proposed identity.
        if not all(compact(target[key]) in compact(target["search_query"]) for key in ("brand", "model", "variant")):
            target["search_query"] = " ".join(target[key] for key in ("brand", "model", "variant"))
        if len(target["search_query"]) > 160 or any("\n" in target[key] for key in ("brand", "model", "variant", "search_query")):
            continue
        signature = compact(target["brand"] + target["model"] + target["variant"])
        if signature in seen:
            continue
        seen.add(signature)
        if re.search(r"[￥¥]|\d+(?:\.\d+)?元", target["reason"]):
            target["reason"] = "作为搜索备选，规格和价格待页面核实。"
        targets.append(target)
    return targets, not bool(targets)


def _budget_share(value: object, budget: float | None) -> float | None:
    if value in (None, ""):
        return None
    try:
        share = float(value)
    except (ValueError, TypeError):
        raise ValueError("类别预算比例必须为 0 至 1 的数字") from None
    if isinstance(value, bool) or not math.isfinite(share) or share < 0:
        raise ValueError("类别预算比例必须为 0 至 1 的数字")
    if share > 1 and budget:
        share /= budget  # Compatibility with old/model outputs using yuan.
    if share > 1:
        raise ValueError("类别预算分配超过总预算，或没有总预算可用于换算")
    return share


def normalize_plan(data: dict, prompt: str) -> dict:
    if not isinstance(data, dict):
        raise ValueError("模型返回的需求计划不是对象")
    # Whitelisting prevents model-generated price/stock fields from becoming
    # product facts in a plan. Only page-grounded candidates may have prices.
    plan = {key: data.get(key) for key in ("scenario", "summary", "purpose", "budget", "needs_monitor",
                                         "assumptions", "reused_components", "categories") if key in data}
    plan["version"] = 2
    plan["scenario"] = str(plan.get("scenario", "general"))
    if plan["scenario"] not in {"computer", "books", "general"}:
        raise ValueError("无法识别的购物场景")
    if re.search(r"(?:包括|包含|含|带上|需要|要个|要一个)\s*(?:一[台个])?显示器", prompt) and not re.search(r"(?:不含|不需要|不要|不包括|已有|已经有)\s*显示器", prompt):
        plan["needs_monitor"] = True
    budget = plan.get("budget")
    plan["budget"] = amount(budget) if budget not in (None, "") else None
    # Preserve an explicit budget from the request instead of a model's guess.
    match = re.search(r"预算\s*(?:为|是|在|不超过|约|大约)?\s*[￥¥]?\s*((?:\d{1,3}(?:[,，]\d{3})+|\d+)(?:\.\d+)?)\s*([万wW])?(?:\s*(\d{1,4}))?", prompt)
    if match:
        numeric_budget = float(re.sub(r"[,，]", "", match.group(1))) * (10000 if match.group(2) else 1)
        if match.group(2) and match.group(3):
            numeric_budget += int(match.group(3)) * 10 ** (4 - len(match.group(3)))
        plan["budget"] = amount(numeric_budget)
    categories = plan.get("categories") or []
    if not isinstance(categories, list) or not categories:
        raise ValueError("购物计划缺少商品类别")
    cleaned = []
    seen = set()
    for category in categories:
        if not isinstance(category, dict):
            raise ValueError("商品类别格式错误")
        cid = str(category.get("id", "")).strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,35}", cid) or cid in seen:
            raise ValueError("商品类别 ID 为空、重复或格式错误")
        seen.add(cid)
        targets, needs_lookup = _search_models({**category, "id": cid})
        share = _budget_share(category.get("budget_share"), plan["budget"])
        cleaned.append({"id": cid, "name": str(category.get("name") or cid),
                        "query": targets[0]["search_query"] if targets else str(category.get("query") or category.get("name") or cid),
                        "required": bool(category.get("required", True)), "budget_share": share,
                        "budget_allocation": round(plan["budget"] * share, 2) if plan["budget"] and share is not None else None,
                        "target_models": targets, "needs_page_model_lookup": needs_lookup})
    if plan["scenario"] == "computer":
        proposed_reuse = set(plan.get("reused_components") or [])
        aliases = {"cpu": ("cpu", "处理器"), "motherboard": ("主板",), "gpu": ("显卡",),
                   "memory": ("内存",), "storage": ("硬盘", "ssd", "固态"), "psu": ("电源",),
                   "case": ("机箱",), "cooler": ("散热", "风冷", "水冷"), "monitor": ("显示器", "屏幕")}
        clauses = re.split(r"[，,。；;\n]", prompt.lower())
        reused = set()
        for cid in proposed_reuse:
            if cid not in aliases:
                continue
            for clause in clauses:
                if (any(alias in clause for alias in aliases[cid])
                    and re.search(r"已有|已经有|我有|复用|不需要|不要|不用|保留", clause)
                    and not re.search(r"没有|还没有|还需要|仍需要", clause)):
                    reused.add(cid)
                    break
        # Existing entries need the same completeness requirement as added ones.
        for category in cleaned:
            if category["id"] in COMPUTER_PARTS:
                category["required"] = category["id"] not in reused and (category["id"] != "monitor" or bool(plan.get("needs_monitor")))
        for cid, name in COMPUTER_PARTS.items():
            if cid == "monitor" and not plan.get("needs_monitor", False):
                continue
            if cid not in seen and cid not in reused:
                cleaned.append({"id": cid, "name": name, "query": name, "required": True, "budget_share": None,
                                "budget_allocation": None, "target_models": [], "needs_page_model_lookup": True})
        plan["reused_components"] = sorted(reused)
    plan["categories"] = cleaned
    plan["summary"] = str(plan.get("summary") or prompt)
    plan["purpose"] = str(plan.get("purpose") or "按用户描述")
    plan["assumptions"] = [str(item) for item in (plan.get("assumptions") or [])]
    plan["assumptions"].append("型号备选仅为未核实搜索建议；商品存在性、已选规格、库存及售价须在京东页面核实。类别预算分配不是商品报价。")
    for category in cleaned:
        if category["needs_page_model_lookup"]:
            plan["assumptions"].append(f"{category['name']}：完整型号待页面查明，当前仅按参数搜索。")
    plan["assumptions"] = list(dict.fromkeys(plan["assumptions"]))
    plan["needs_monitor"] = bool(plan.get("needs_monitor", False))
    return plan


def price_is_visible(price: float, text: str) -> bool:
    for match in re.finditer(r"(?<!\d)(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?(?!\d)", text):
        try:
            if Decimal(match.group().replace(",", "")) == Decimal(str(price)):
                return True
        except InvalidOperation:
            pass
    return False


def valid_price_evidence(price: float, proof: str) -> bool:
    return (bool(re.search(r"[￥¥元]|京东价|价格|售价", proof)) and price_is_visible(price, proof)
            and not re.search(r"起|定金|订金|预付款|每期|月供|补贴后|券后|返后|以旧换新|原价|划线价|参考价|市场价", proof))


def _normalize_selected_spec(observation: dict) -> str:
    selected = observation.get("selected_spec") or observation.get("selected_specs")
    if isinstance(selected, (list, tuple)):
        selected = " ".join(str(value) for value in selected)
    if isinstance(selected, dict):
        selected = " ".join(str(value) for value in selected.values())
    return str(selected or "").strip()


def spec_identity_tokens(spec: str) -> list[str]:
    """Keep model codes, capacities, and 2+ character Chinese variant words."""
    return re.findall(
        r"[A-Za-z]+[0-9][A-Za-z0-9]*|[0-9]+[A-Za-z]+|[0-9]{2,}[\u4e00-\u9fff]*|[0-9]+[\u4e00-\u9fff]+|[\u4e00-\u9fff]{2,}",
        str(spec or ""),
    )


def selected_spec_conflicts(spec: str, selected: str) -> bool:
    """Reject only when extracted selection names a different concrete variant."""
    spec_text, selected_text = compact(spec), compact(selected)
    if not spec_text or not selected_text:
        return False
    for left, right in (("盒装", "散片"), ("盒装", "散装")):
        if (compact(left) in spec_text and compact(right) in selected_text) or (
                compact(right) in spec_text and compact(left) in selected_text):
            return True
    spec_caps = set(re.findall(r"(\d+)gb", spec_text))
    selected_caps = set(re.findall(r"(\d+)gb", selected_text))
    return bool(spec_caps and selected_caps and spec_caps != selected_caps)


def selected_spec_matches(spec: str, observation: dict, title: str = "") -> bool:
    """Option text appearing somewhere is insufficient proof of selection.

    Prefer browser-extracted selected controls. If the site exposes no selected
    option, the SKU title or an explicit '已选' label must identify the variant.
    A landing-SKU title may still identify 盒装/型号 even when the DOM also
    marks an unrelated chip such as 官方标配.
    """
    selected = _normalize_selected_spec(observation)
    selected_labels = [str(e.get("name") or e.get("value") or "") for e in observation.get("elements", [])
                       if e.get("selected") is True or e.get("checked") is True]
    selected_text = selected or " ".join(selected_labels)
    page_title = str(observation.get("title") or "")
    identity = compact(f"{title} {page_title}")
    if selected_text and compact(spec) in compact(selected_text):
        return True
    if selected_spec_conflicts(spec, selected_text):
        return False
    for line in page_text(observation).splitlines():
        if re.search(r"已选(?:择)?\s*[:：]?", line) and compact(spec) in compact(line):
            return True
    if identity and compact(spec) in identity:
        return True
    tokens = spec_identity_tokens(spec)
    return bool(tokens and identity and all(compact(token) in identity for token in tokens))


def grounded_candidate(data: dict, observation: dict, category: str, *, scenario: str | None = None) -> dict:
    """Reject invented title/spec/price and keep the original observable evidence."""
    url = jd_url(observation.get("url"), product=True)
    if is_multi_sku_listing(observation.get("title") or ""):
        raise ValueError("当前是多规格合卖页，不是单一型号详情。请返回搜索打开只含一个具体型号的商品页。")
    supplied_url = data.get("url")
    if supplied_url and sku_from_url(jd_url(supplied_url, product=True)) != sku_from_url(url):
        raise ValueError("候选链接与当前观察的商品 SKU 不一致")
    text = page_text(observation)
    title = str(data.get("title", "")).strip()
    spec = str(data.get("spec", "")).strip()
    if len(compact(title)) < 6 or compact(title) not in compact(text):
        raise ValueError("商品名称必须逐字来自当前页面，至少 6 个字符")
    brand = str(data.get("brand") or "").strip()
    model = str(data.get("model") or "").strip()
    model_evidence = str(data.get("model_evidence") or "").strip()
    is_book = scenario == "books" and category not in COMPUTER_PARTS
    if category in COMPUTER_PARTS or brand or model:
        if not brand or not specific_model(category, model):
            if is_book:
                raise ValueError("需要当前商品页面上的出版社及完整书名，不能用店铺名或推荐商品代替")
            raise ValueError("需要页面上的完整品牌及具体型号，不能用系列级别、容量或功率代替型号")
        # Repeated names in recommendations are insufficient. Identity must be
        # in this exact title or explicitly labelled product parameters.
        browser_title = str(observation.get("title") or "").strip()
        parsed_url = urlparse(url)
        canonical_title = (
            observation.get("source") == "dom"
            and parsed_url.hostname == "item.jd.com"
            and re.fullmatch(r"/\d+\.html", parsed_url.path) is not None
            and all(compact(value) in compact(browser_title) for value in (title, brand, model))
        )
        if canonical_title:
            # document.title belongs to the same observed SKU. Store its exact
            # text instead of a model's stitched or paraphrased evidence.
            model_evidence = browser_title
        elif not model_evidence:
            model_evidence = title
        if compact(model_evidence) not in compact(text):
            if is_book:
                raise ValueError("书名/出版社缺少当前页面原文证据；请逐字引用当前商品标题或明确的出版社/书名参数")
            raise ValueError("完整型号缺少当前页面原文证据")
        identity_labels = (("品牌", "品牌", brand), ("型号", "型号", model))
        if is_book:
            # Book category IDs describe learning stages, not product types.
            # Accept explicit bibliographic parameters, never a shop name alone.
            identity_labels = (("出版社/品牌", r"(?<!\w)(?:出版社|品牌)", brand),
                               ("书名/型号", r"(?<!\w)(?:书名|型号)", model))
        for label, parameter_pattern, value in identity_labels:
            if canonical_title or compact(value) in compact(title):
                continue
            if not re.search(parameter_pattern + r"\s*[:：]\s*" + re.escape(value), model_evidence, re.I):
                raise ValueError(f"{label}必须来自当前商品标题或明确的{label}参数，不能引用推荐商品")
    if not spec:
        raise ValueError("商品规格必须引用当前页面，不能推测")
    if compact(spec) not in compact(text):
        title_identity = compact(f"{title} {observation.get('title') or ''}")
        tokens = spec_identity_tokens(spec)
        if not (tokens and title_identity and all(compact(token) in title_identity for token in tokens)):
            raise ValueError("商品规格必须引用当前页面，不能推测")
    if not selected_spec_matches(spec, observation, title):
        raise ValueError("页面未证实该规格已被选中；可选规格文本不能作为已选规格")
    price = amount(data.get("price"))
    price_evidence = str(data.get("price_evidence", "")).strip()
    if not price_evidence or compact(price_evidence) not in compact(text) or not price_is_visible(price, price_evidence):
        raise ValueError("价格缺少可核实的页面原文；请打开详情读到真实售价")
    if not valid_price_evidence(price, price_evidence):
        raise ValueError("条件价、起售价或定金不能作为确定购买价格，请读取选中规格的无条件售价")
    quantity = data.get("quantity", 1)
    if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 20:
        raise ValueError("商品数量必须为 1 至 20 的整数")
    compat = {}
    compat_evidence = {}
    for key, value in (data.get("compatibility") or {}).items():
        proof = str((data.get("compatibility_evidence") or {}).get(key, ""))
        if proof and compact(proof) in compact(text) and compact(value) in compact(proof):
            compat[str(key)] = value
            compat_evidence[str(key)] = proof
    store = str(data.get("store") or "未展示")
    if store != "未展示" and compact(store) not in compact(text):
        store = "未展示"
    rating = str(data.get("rating") or "未展示")
    if rating != "未展示" and compact(rating) not in compact(text):
        rating = "未展示"
    promotions = str(data.get("promotions") or "未展示")
    if promotions != "未展示" and compact(promotions) not in compact(text):
        promotions = "未展示"
    return {
        "id": f"{category}_{sku_from_url(url)}", "category": category, "title": title,
        "url": url, "sku": sku_from_url(url), "price": price, "quantity": quantity,
        "brand": brand, "model": model, "model_evidence": model_evidence,
        "spec": spec, "store": store, "rating": rating, "promotions": promotions,
        "self_operated": ("自营" in text) if data.get("self_operated") is True else None,
        "compatibility": compat, "compatibility_evidence": compat_evidence,
        "reason": str(data.get("reason") or ""), "recommended": False,
        "evidence": {"obs_id": observation.get("obs_id"), "url": url, "title": title,
                     "spec": spec, "price": price_evidence, "brand": brand, "model": model_evidence,
                     "kind": "live_dom_text" if observation.get("source") == "dom" else "live_uia_text"},
    }


def validate_selection(plan: dict, candidates: list[dict], selected_ids: list[str]) -> dict:
    errors, warnings = [], []
    by_id = {item["id"]: item for item in candidates}
    if not isinstance(selected_ids, list) or not selected_ids:
        return {"total": 0, "errors": ["尚未选择商品"], "warnings": [], "compatible": False}
    if len(set(selected_ids)) != len(selected_ids):
        errors.append("同一个候选商品被重复选择")
    selected = [by_id[cid] for cid in selected_ids if cid in by_id]
    if len(selected) != len(selected_ids):
        errors.append("选择中存在未经过页面核实的商品")
    skus = [item.get("sku") for item in selected if item.get("sku")]
    if len(set(skus)) != len(skus):
        errors.append("同一商品 SKU 出现在多个类别中，需确认套装内容并避免重复购买")
    total = Decimal("0")
    by_category = {}
    for item in selected:
        by_category.setdefault(item["category"], []).append(item)
        try:
            total += Decimal(str(amount(item["price"]))) * int(item.get("quantity", 1))
            if not item.get("evidence") or not item.get("spec"):
                errors.append(f"{item['title']} 缺少来源或规格证据")
            if (plan.get("version") == 2 and item.get("category") in COMPUTER_PARTS
                and (not item.get("brand") or not specific_model(item["category"], item.get("model", ""))
                     or not item.get("model_evidence"))):
                errors.append(f"{item['title']} 缺少页面核实的完整品牌型号，请补查详情后再确认")
        except (ValueError, KeyError, TypeError):
            errors.append("商品价格或数量无效")
    for category in plan.get("categories", []):
        count = len(by_category.get(category["id"], []))
        if category.get("required", True) and count == 0:
            errors.append(f"缺少必需商品：{category['name']}")
        if plan.get("scenario") == "computer" and count > 1:
            errors.append(f"{category['name']} 选择了多个商品，请保留一个明确方案")
    budget = plan.get("budget")
    if budget and total > Decimal(str(budget)):
        errors.append(f"总价 {total:.2f} 元超过预算 {float(budget):.2f} 元")
    if plan.get("scenario") == "computer":
        parts = {key: values[0] for key, values in by_category.items() if len(values) == 1}

        def compare(left, lk, right, rk, label, numeric=False, supported_set=False):
            if left not in parts or right not in parts:
                return
            a = parts[left].get("compatibility", {}).get(lk)
            b = parts[right].get("compatibility", {}).get(rk)
            if a in (None, "") or b in (None, ""):
                warnings.append(f"待核实：{label}（商品页面尚未提供足够参数）")
                return
            if numeric:
                try:
                    okay = float(a) >= float(b) and math.isfinite(float(a)) and math.isfinite(float(b))
                except (TypeError, ValueError):
                    warnings.append(f"待核实：{label}（参数格式无法比较）")
                    return
            elif supported_set:
                supported = re.sub(r"LGA\s+(?=\d)", "LGA", str(b), flags=re.I)
                okay = compact(a) in [compact(token) for token in re.split(r"[/,，、;；|\s]+", supported) if token]
            else:
                okay = compact(a) == compact(b)
            if not okay:
                errors.append(f"兼容性冲突：{label}（{a} / {b}）")

        compare("cpu", "socket", "motherboard", "socket", "CPU 与主板插槽")
        compare("motherboard", "memory_type", "memory", "memory_type", "主板与内存代际")
        compare("cpu", "socket", "cooler", "socket", "CPU 与散热器扣具", supported_set=True)
        compare("psu", "watts", "gpu", "recommended_psu_watts", "电源功率与显卡要求", True)
        compare("case", "max_gpu_length_mm", "gpu", "length_mm", "机箱显卡限长", True)
        compare("case", "max_cooler_height_mm", "cooler", "height_mm", "机箱散热器限高", True)
        if "case" in parts and "motherboard" in parts:
            supported = parts["case"].get("compatibility", {}).get("motherboard_formats")
            board = parts["motherboard"].get("compatibility", {}).get("form_factor")
            if not supported or not board:
                warnings.append("待核实：机箱与主板尺寸")
            elif compact(board) not in [compact(x) for x in re.split(r"[,，、;/| ]+", str(supported))]:
                errors.append(f"兼容性冲突：机箱支持 {supported}，主板为 {board}")
        warnings.append("还需核实 BIOS 支持、电源接口及显卡供电连接；页面未提供的信息不会视为已通过。")
    return {"total": float(total), "budget": budget, "errors": errors,
            "warnings": list(dict.fromkeys(warnings)), "compatible": not errors and not warnings}


def _approved_gate_link(href: str, approved_sku: str, approved_quantity: int) -> bool:
    """The only mutation URL exception: one approved SKU and exact quantity."""
    try:
        url = urlparse(href)
        if (url.scheme != "https" or url.hostname != "cart.jd.com"
            or url.username or url.password or url.port not in {None, 443}
            or url.path != "/gate.action" or url.params or url.fragment):
            return False
        pairs = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True)
        if len(pairs) != 3 or {key for key, _ in pairs} != {"pid", "pcount", "ptype"}:
            return False
        params = dict(pairs)
        return (bool(re.fullmatch(r"[0-9]+", approved_sku)) and params["pid"] == approved_sku
                and type(approved_quantity) is int and 1 <= approved_quantity <= 20
                and params["pcount"] == str(approved_quantity) and params["ptype"] == "1")
    except (TypeError, ValueError):
        return False


def guard_action(action: dict, observation: dict, *, allow_add=False, approved_sku="", allow_quantity=False,
                 approved_quantity=1) -> dict:
    """Only visible observed controls can be clicked; never arbitrary shell or JS."""
    kind = str(action.get("action") or action.get("kind") or "")
    if kind not in {"click", "type", "press", "scroll", "back"}:
        raise ValueError("动作不在允许列表中")
    if action.get("obs_id") != observation.get("obs_id"):
        raise ValueError("页面已变化，请依据最新观察重新定位")
    clean = {"action": kind, "obs_id": observation["obs_id"]}
    if kind in {"click", "type"}:
        if (urlparse(observation.get("url", "")).hostname or "").lower() == "cart.jd.com":
            raise ValueError("购物车只允许读取和滚动，不修改或删除原有商品")
        element = next((e for e in observation.get("elements", []) if str(e.get("id")) == str(action.get("element_id"))), None)
        if not element or element.get("visible") is False or element.get("enabled") is False:
            raise ValueError("目标控件不在当前可见页面中或不可用")
        label = " ".join(str(element.get(key) or "") for key in ("name", "value", "click_owner_name"))
        if compact(element.get("name")) in {"+", "-", "＋", "－", "增加", "减少"}:
            raise ValueError("不点击用途不明确的加减按钮")
        href = str(element.get("href") or "")
        explicit_add = any(compact(element.get(key)) in {"加入购物车", "加购物车", "加购", "加入购物袋", "addtocart"}
                           for key in ("name", "click_owner_name"))
        authorized_add = (kind == "click" and allow_add is True and explicit_add and approved_sku
                          and sku_from_url(observation.get("url", "")) == approved_sku)
        gate_exception = authorized_add and _approved_gate_link(href, approved_sku, approved_quantity)
        if href and re.search(r"(?:cart|order|trade|pay|api)\.[^/]+/|(?:add|delete|remove|submit|checkout)[^/]*\.(?:action|html)", href, re.I) and not gate_exception:
            raise ValueError("该链接可能修改购物车或进入交易流程")
        if any(token in label for token in DISALLOWED_LABELS):
            raise ValueError("此控件涉及结算、支付、删除或对外沟通，执行器拒绝操作")
        if re.search(r"加入购物车|加购物车|加购|加入购物袋|add\s*to\s*cart", label, re.I):
            if not allow_add or not approved_sku or sku_from_url(observation.get("url", "")) != approved_sku:
                raise ValueError("当前商品未获加购授权")
        clean["element_id"] = element["id"]
        if kind == "type":
            value = str(action.get("text", ""))
            if len(value) > 400 or re.search(r"[\r\n\x00-\x08]", value):
                raise ValueError("输入文字过长或包含控制字符")
            if re.search(r"地址|address|location|搜索或输入网址", label, re.I):
                raise ValueError("地址栏导航须通过京东域名校验接口")
            is_search = bool(re.search(r"搜索|search|关键词", label, re.I))
            quantity_target = (element.get("input_purpose") == "quantity"
                               or bool(re.search(r"数量|quantity|qty|件数", str(element.get("name", "")), re.I)))
            is_quantity = (allow_quantity is True and bool(approved_sku)
                           and sku_from_url(observation.get("url", "")) == approved_sku
                           and str(element.get("role", "")).lower() in {"edit", "textbox", "combobox", "spinner", "spinbutton"}
                           and quantity_target and type(approved_quantity) is int and 1 <= approved_quantity <= 20
                           and bool(re.fullmatch(r"[0-9]{1,2}", value)) and int(value) == approved_quantity)
            if not is_search and not is_quantity:
                raise ValueError("仅可输入搜索词，或在已批准商品详情页设置数量")
            clean["text"] = value
    elif kind == "press":
        key = str(action.get("key", "")).lower()
        # Enter/space on arbitrary focus can submit checkout. Enter is only used by
        # the engine immediately after typing into a confirmed search box.
        if key not in {"esc", "escape", "pageup", "pagedown", "up", "down", "left", "right"}:
            raise ValueError("不允许模型对未知焦点按 Enter、空格或系统快捷键")
        clean["key"] = key
    elif kind == "scroll":
        direction = str(action.get("direction", "down")).lower()
        if direction not in {"up", "down"}:
            raise ValueError("滚动方向无效")
        clean.update(direction=direction, amount=max(1, min(8, int(action.get("amount", 4)))))
    return clean
