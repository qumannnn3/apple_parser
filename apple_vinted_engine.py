import asyncio
import html
import random
import re
import time
from io import BytesIO

from shared import (
    USER_AGENTS,
    VINTED_REGIONS,
    age_in_range,
    format_msk_timestamp,
    get_fx_rate,
    has_item_seen,
    is_market_run_current,
    keyword_matches_text,
    log,
    mark_item_seen,
    notification_chat_ids,
    publish_age_hours,
    run_telegram_coroutine,
    sleep_while_market_running,
    state,
    translate_to_ru,
    vinted_domain_currency,
    vinted_price_to_eur,
    _has_any_term,
    _try_parse_ts,
)
from vinted_platform import (
    decode_response,
    download_vinted_photo,
    get_vinted_photo_url,
    get_vinted_session,
    init_vinted,
    _get_nested,
)


APPLE_VINTED_REGIONS = ("ee", "lt", "lv", "pl")
APPLE_VINTED_DOMAINS = [VINTED_REGIONS[code] for code in APPLE_VINTED_REGIONS]
APPLE_VINTED_MARKET_PRICE_MAX_EUR = 5000
APPLE_VINTED_MAX_MARKET_RATIO = 0.90
APPLE_VINTED_MIN_MARKET_SAMPLES = 1
APPLE_VINTED_OLD_ITEM_STOP_STREAK = 8

APPLE_SEARCH_QUERIES = [
    "apple iphone",
    "apple ipad",
    "apple macbook",
    "airpods",
    "apple watch",
    "apple imac",
    "apple mac mini",
    "apple pencil",
]

APPLE_PRODUCT_TERMS = [
    "iphone", "ipad", "macbook", "mac book", "airpods", "airpod", "air pods", "aipods",
    "apple watch", "iwatch", "imac", "mac mini", "mac studio", "apple pencil",
    "айфон", "айпад", "макбук", "аирподс", "эйрподс", "эпл вотч",
]

APPLE_CARRIER_JUNK_TERMS = [
    "bag", "bags", "backpack", "briefcase", "clutch", "cosmetic bag", "messenger bag",
    "shoulder bag", "crossbody", "cross body", "tote", "totebag", "duffle", "rucksack",
    "laptop bag", "laptop backpack", "laptop sleeve", "folio",
    "plecak", "torba", "torebka", "aktowka", "aktówka", "teczka", "kosmetyczka",
    "rankine", "rankinė", "kuprinė", "deklas", "dėklas",
    "rygsæk", "taske", "skuldertaske", "laukku", "brasna", "geanta",
]

APPLE_BAD_CONDITION_TERMS = [
    "for parts", "parts only", "spares", "repair", "broken", "faulty",
    "not working", "does not work", "icloud locked", "i cloud locked",
    "activation lock", "locked", "blacklist", "blacklisted", "blocked",
    "clone", "replica", "fake", "dummy", "display model",
    "на запчасти", "не работает", "сломан", "ремонт", "заблокирован",
    "icloud", "айклауд", "копия", "реплика", "муляж",
    "uszkodzony", "nie dziala", "nie działa", "blokada icloud", "zablokowany",
]


# Battery-health words in EN/RU/PL/LT/LV/EE/FI/DE.
# These are treated as a reason to skip an iPhone completely when battery health is under 90%.
APPLE_BATTERY_HEALTH_TERMS = [
    "battery", "battery health", "battery capacity", "battery percentage", "bh",
    "batterie", "akku", "akkukunto", "akun kunto", "akutase",
    "bateria", "kondycja baterii", "pojemnosc baterii", "pojemność baterii",
    "baterija", "baterijos bukle", "baterijos būklė", "akumuliatorius",
    "akumuliatoriaus bukle", "akumuliatoriaus būklė", "talpa",
    "baterija", "baterijas stavoklis", "baterijas stāvoklis", "akumulators",
    "батарея", "аккумулятор", "акб", "ёмкость", "емкость", "износ",
]

APPLE_ACCESSORY_TERMS = [
    "case", "cover", "screen protector", "protector", "tempered glass", "glass",
    "charger", "charging", "cable", "adapter", "dock", "stand", "holder",
    "strap", "band", "watch band", "watch strap", "keyboard", "mouse", "trackpad",
    "sleeve", "skin", "wallet", "lanyard", "pouch",
    "etui", "obudowa", "pokrowiec", "szklo", "szkЕ‚o", "ladowarka", "Е‚adowarka",
    "kabel", "pasek", "bransoleta", "uchwyt", "podstawka", "klawiatura", "mysz",
    "deklas", "dД—klas", "laidas", "ikroviklis", "apyranke", "apyrankД—",
    "macins", "ladetajs", "kabelis", "siksna", "turetajs",
]


def _is_iphone_text(text):
    return bool(re.search(r"\biphone\b|айфон", text or "", re.IGNORECASE))


def _has_low_battery_health(text):
    """Detect iPhone battery-health mentions below 90% in many languages.

    Listings with battery health under 90% are rejected.
    """
    text = str(text or "").lower()
    if not _is_iphone_text(text):
        return False
    if not _has_any_term(text, APPLE_BATTERY_HEALTH_TERMS):
        return False
    for match in re.finditer(r"(?<!\d)([1-8]?\d)(?:[.,]\d+)?\s*%", text):
        try:
            value = int(match.group(1))
        except ValueError:
            continue
        if 1 <= value < 90:
            start = max(0, match.start() - 80)
            end = min(len(text), match.end() + 80)
            window = text[start:end]
            if _has_any_term(window, APPLE_BATTERY_HEALTH_TERMS):
                return True
    return False


def _strip_battery_health_context(text):
    """Remove short low-battery-health fragments before bad-condition checks.

    Remove low battery fragments.
    """
    text = str(text or "")
    if not _has_low_battery_health(text):
        return text
    cleaned = text
    for term in sorted(APPLE_BATTERY_HEALTH_TERMS, key=len, reverse=True):
        term_re = re.escape(term)
        cleaned = re.sub(rf".{0,60}{term_re}.{0,60}(?<!\d)([1-8]?\d)(?:[.,]\d+)?\s*%.{0,40}", " ", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(rf".{0,40}(?<!\d)([1-8]?\d)(?:[.,]\d+)?\s*%.{0,60}{term_re}.{0,60}", " ", cleaned, flags=re.IGNORECASE)
    return cleaned


def _first_regex_group(pattern, text):
    match = re.search(pattern, text, re.IGNORECASE)
    return match.group(1).lower() if match else ""


def _normalize_model_token(value):
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _device_or_accessory(item):
    return "accessory" if _has_any_term(_apple_text_blob(item), APPLE_ACCESSORY_TERMS) else "device"


def _iphone_model_signature(text):
    number = _first_regex_group(r"\biphone\s*(1[1-7]|x|xs|xr|se|8|7|6s?|5s?)\b", text)
    if not number and "apple" in text and not _has_any_term(text, ["macbook", "mac book", "ipad", "airpods", "air pods", "apple watch", "iwatch"]):
        match = re.search(r"\b(1[1-7])\s*(pro\s*max|pro|plus|mini)?\b", text, re.IGNORECASE)
        if match:
            variant = _normalize_model_token(match.group(2) or "")
            return _normalize_model_token(f"{match.group(1)} {variant}")
    if not number:
        return ""
    start = text.find(number)
    tail = text[start + len(number): start + len(number) + 40] if start >= 0 else ""
    variant = ""
    if re.search(r"\bpro\s*max\b", tail):
        variant = "pro max"
    elif re.search(r"\bpro\b", tail):
        variant = "pro"
    elif re.search(r"\bplus\b", tail):
        variant = "plus"
    elif re.search(r"\bmini\b", tail):
        variant = "mini"
    return _normalize_model_token(f"{number} {variant}")


def _ipad_model_signature(text):
    if "ipad" not in text:
        return ""
    family = ""
    for value in ("pro", "air", "mini"):
        if re.search(rf"\bipad\s+{value}\b|\b{value}\s+ipad\b", text):
            family = value
            break
    size = _first_regex_group(r"\b(1[0-3](?:[.,]\d)?|9(?:[.,]\d)?)\s*(?:inch|in|\"|cala)\b", text)
    gen = _first_regex_group(r"\b(\d{1,2})(?:st|nd|rd|th)?\s*(?:gen|generation|generacji)\b", text)
    return _normalize_model_token(" ".join(part for part in (family, size, gen) if part)) or "ipad"


def _macbook_model_signature(text):
    if "macbook" not in text and "mac book" not in text:
        return ""
    family = "pro" if re.search(r"\bpro\b", text) else "air" if re.search(r"\bair\b", text) else ""
    size = _first_regex_group(r"\b(1[1-6])\s*(?:inch|in|\"|cala)\b", text)
    chip = _first_regex_group(r"\b(m[1-4](?:\s*(?:pro|max|ultra))?)\b", text)
    year = _first_regex_group(r"\b(20[1-2]\d)\b", text)
    return _normalize_model_token(" ".join(part for part in (family, size, chip, year) if part)) or "macbook"


def _watch_model_signature(text):
    if "apple watch" not in text and "iwatch" not in text:
        return ""
    if re.search(r"\bultra\s*2\b", text):
        return "ultra 2"
    if re.search(r"\bultra\b", text):
        return "ultra"
    se = "se" if re.search(r"\bse\b", text) else ""
    series = _first_regex_group(r"\b(?:series|s)\s*(\d{1,2})\b", text)
    size = _first_regex_group(r"\b(3[8-9]|4[0-9])\s*mm\b", text)
    return _normalize_model_token(" ".join(part for part in (se or series, size) if part)) or "watch"


def _airpods_model_signature(text):
    if (
        "airpods" not in text
        and "air pods" not in text
        and "airpod" not in text
        and "aipods" not in text
        and "аирподс" not in text
        and "эйрподс" not in text
    ):
        return ""
    if re.search(r"\bmax\b", text):
        return "max"
    is_pro = re.search(r"\bpro\b", text)
    is_second = re.search(
        r"\b(?:2|ii|2nd|second)\b|2\s*[.#-]?\s*(?:gen|gener|generation|generacji|generacja|generacie|generacia)",
        text,
        re.IGNORECASE,
    )
    if is_pro and is_second:
        return "pro 2"
    if re.search(r"\bpro\b", text):
        return "pro"
    gen = _first_regex_group(r"\b([1-4])(?:st|nd|rd|th)?\s*(?:gen|generation|generacji)\b", text)
    return _normalize_model_token(gen) or "airpods"


def apple_vinted_market_signature(item):
    text = _apple_text_blob(item)
    kind = apple_vinted_product_kind(item)
    accessory = _device_or_accessory(item)
    model = ""
    if kind == "iphone":
        model = _iphone_model_signature(text)
    elif kind == "ipad":
        model = _ipad_model_signature(text)
    elif kind == "macbook":
        model = _macbook_model_signature(text)
    elif kind == "watch":
        model = _watch_model_signature(text)
    elif kind == "airpods":
        model = _airpods_model_signature(text)
    elif kind in ("imac", "mac", "pencil"):
        model = kind
    return _normalize_model_token(f"{kind}:{model or 'unknown'}:{accessory}") if kind else ""


def _to_price(value):
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def calculate_market_price(items, target_item, price_getter, id_getter, item_filter, kind_getter, min_samples=1):
    target_id = str(id_getter(target_item) or "")
    target_kind = kind_getter(target_item)
    prices = []

    for item in items or []:
        if str(id_getter(item) or "") == target_id:
            continue
        try:
            if not item_filter(item):
                continue
        except Exception:
            continue
        if target_kind and kind_getter(item) != target_kind:
            continue
        price = _to_price(price_getter(item))
        if price is not None:
            prices.append(price)

    if len(prices) < max(1, int(min_samples)):
        return None

    prices.sort()
    mid = len(prices) // 2
    median = prices[mid] if len(prices) % 2 else (prices[mid - 1] + prices[mid]) / 2
    average = sum(prices) / len(prices)
    market_price = round((median * 0.7) + (average * 0.3))
    return {"price": market_price, "count": len(prices)}


def _apple_text_blob(item):
    parts = []
    for key in ("title", "brand_title", "size_title", "status", "description", "catalog_title"):
        val = item.get(key) if isinstance(item, dict) else ""
        if val:
            parts.append(str(val))
    for path in ("item_box.accessibility_label", "photo.accessibility_label"):
        val = _get_nested(item, path)
        if val:
            parts.append(str(val))
    return " ".join(parts).lower()


def _apple_url_item_id(item):
    url = str(item.get("url") or "") if isinstance(item, dict) else ""
    match = re.search(r"/items/(\d+)", url)
    return match.group(1) if match else ""


def _apple_item_fingerprint(item):
    title = re.sub(r"\s+", " ", str(item.get("title") or "").lower()).strip()
    title = re.sub(r"[^a-z0-9а-яё]+", " ", title).strip()
    seller = _get_nested(item, "user.id") or _get_nested(item, "user.login") or _get_nested(item, "user.username") or ""
    price = apple_vinted_price_eur(item)
    signature = apple_vinted_market_signature(item)
    if not title or not signature:
        return ""
    return f"fp:{seller}:{signature}:{title}:{price:.2f}"


def _apple_primary_seen_key(item):
    item_id = str(item.get("id") or "").strip() if isinstance(item, dict) else ""
    if item_id:
        return item_id
    url_id = _apple_url_item_id(item)
    if url_id:
        return f"url:{url_id}"
    return _apple_item_fingerprint(item)


def _apple_seen_aliases(item, domain):
    aliases = set()
    item_id = str(item.get("id") or "").strip() if isinstance(item, dict) else ""
    url_id = _apple_url_item_id(item)
    fingerprint = _apple_item_fingerprint(item)

    for value in (item_id, f"url:{url_id}" if url_id else "", fingerprint):
        value = str(value or "").strip()
        if not value:
            continue
        aliases.add(value)
        aliases.add(f"{domain}:{value}")
    return aliases


def _apple_item_seen(item, domain, runtime_seen):
    aliases = _apple_seen_aliases(item, domain)
    if aliases & runtime_seen:
        return True

    seen = state.get("apple_vinted_seen") or set()
    seen_strings = {str(value) for value in seen}
    if aliases & seen_strings:
        return True

    item_id = str(item.get("id") or "").strip() if isinstance(item, dict) else ""
    if item_id and any(value.endswith(f":{item_id}") for value in seen_strings):
        return True

    primary_key = _apple_primary_seen_key(item)
    return bool(primary_key and has_item_seen("apple_vinted", primary_key))


def _apple_price_bounds(domain):
    currency = vinted_domain_currency(domain)
    rate = get_fx_rate("EUR", currency)
    return state["apple_vinted_min"] * rate, state["apple_vinted_max"] * rate


def _apple_market_price_bounds(domain):
    currency = vinted_domain_currency(domain)
    rate = get_fx_rate("EUR", currency)
    return 1 * rate, APPLE_VINTED_MARKET_PRICE_MAX_EUR * rate


def fetch_apple_vinted(query, domain, retry=True, price_min=None, price_max=None):
    session = get_vinted_session(domain)
    session.headers["User-Agent"] = random.choice(USER_AGENTS)
    try:
        currency = vinted_domain_currency(domain)
        if price_min is None or price_max is None:
            price_min, price_max = _apple_price_bounds(domain)
        params = [
            ("search_text", query),
            ("page", 1),
            ("per_page", 48),
            ("order", "newest_first"),
            ("price_from", f"{float(price_min):.2f}"),
            ("price_to", f"{float(price_max):.2f}"),
            ("currency", currency),
        ]
        response = session.get(
            f"https://{domain}/api/v2/catalog/items",
            params=params,
            timeout=20,
        )
        if response.status_code == 200:
            items = decode_response(response).get("items", [])
            if items:
                log.info("Apple Vinted %s '%s' -> %s items", domain, query, len(items))
            return items
        if response.status_code == 401 and retry:
            init_vinted(domain)
            return fetch_apple_vinted(query, domain, retry=False, price_min=price_min, price_max=price_max)
        if response.status_code in (403, 429):
            log.error("Apple Vinted BAN %s %s", response.status_code, domain)
            return "BAN"
        log.warning("Apple Vinted response %s %s query=%r body=%s", response.status_code, domain, query, response.text[:200])
        return []
    except Exception as e:
        log.warning("fetch_apple_vinted %s '%s': %s", domain, query, e)
        return []


def parse_apple_vinted_ts(item):
    candidates = [
        "created_at_ts",
        "updated_at_ts",
        "activation_ts",
        "created_at",
        "updated_at",
        "active_at",
        "last_push_up_at",
        "photo.high_resolution.timestamp",
        "photo.timestamp",
        "photos.0.high_resolution.timestamp",
        "photos.0.timestamp",
    ]
    for key in candidates:
        ts = _try_parse_ts(_get_nested(item, key))
        if ts:
            return ts
    return None


def apple_vinted_product_kind(item):
    text = _apple_text_blob(item)
    if _iphone_model_signature(text):
        return "iphone"
    pairs = [
        ("iphone", ["iphone", "айфон"]),
        ("ipad", ["ipad", "айпад"]),
        ("macbook", ["macbook", "mac book", "макбук"]),
        ("airpods", ["airpods", "airpod", "air pods", "aipods", "аирподс", "эйрподс"]),
        ("watch", ["apple watch", "iwatch", "эпл вотч"]),
        ("imac", ["imac"]),
        ("mac", ["mac mini", "mac studio"]),
        ("pencil", ["apple pencil"]),
    ]
    for kind, terms in pairs:
        if _has_any_term(text, terms):
            return kind
    return ""


def apple_vinted_matches_keyword(item, keyword):
    keyword = str(keyword or "").lower().strip()
    if _has_any_term(keyword, ["airpods", "air pods", "airpod", "aipods", "аирподс", "эйрподс"]):
        return apple_vinted_product_kind(item) == "airpods"
    return keyword_matches_text(_apple_text_blob(item), keyword)


def apple_vinted_matches_desc_filter(item):
    """Return True if item matches the description/title filter.

    This is an inclusion filter: if terms are set, the item is kept only
    when at least one term is found in the title or description.
    """
    desc_filter = state.get("apple_vinted_desc_filter") or []
    if not desc_filter:
        return True
    text = f"{item.get('title') or ''} {item.get('description') or ''}".lower()
    return any(keyword_matches_text(text, term) for term in desc_filter)


def is_relevant_apple_vinted_item(item):
    text = _apple_text_blob(item)
    if not _has_any_term(text, APPLE_PRODUCT_TERMS) and not apple_vinted_product_kind(item):
        return False
    if _has_any_term(text, APPLE_CARRIER_JUNK_TERMS):
        return False
    bad_condition_text = _strip_battery_health_context(text)
    if _has_any_term(bad_condition_text, APPLE_BAD_CONDITION_TERMS):
        return False
    return bool(apple_vinted_product_kind(item))


def apple_vinted_price_eur(item):
    price_data = item.get("price", {}) or {}
    try:
        amount = float(price_data.get("amount", 0))
    except (TypeError, ValueError):
        return 0
    return vinted_price_to_eur(amount, price_data.get("currency_code", "EUR"))


def apple_vinted_market_price_eur(items, target_item, keyword=None):
    target_price = apple_vinted_price_eur(target_item)
    target_signature = apple_vinted_market_signature(target_item)
    target_is_accessory = target_signature.endswith(":accessory")
    min_sample_price = max(1, target_price * (0.20 if target_is_accessory else 0.35))
    max_sample_price = target_price * (4.0 if target_is_accessory else 3.0) if target_price else None

    def market_sample_filter(item):
        if not is_relevant_apple_vinted_item(item):
            return False
        if keyword and not apple_vinted_matches_keyword(item, keyword):
            return False
        price = apple_vinted_price_eur(item)
        if price < min_sample_price:
            return False
        if max_sample_price and price > max_sample_price:
            return False
        return True

    return calculate_market_price(
        items,
        target_item,
        price_getter=apple_vinted_price_eur,
        id_getter=lambda item: item.get("id"),
        item_filter=market_sample_filter,
        kind_getter=apple_vinted_market_signature,
        min_samples=APPLE_VINTED_MIN_MARKET_SAMPLES,
    )


def _queries():
    keywords = state.get("apple_vinted_keywords") or []
    values = keywords or APPLE_SEARCH_QUERIES
    result = []
    seen = set()
    for value in values:
        query = re.sub(r"\s+", " ", str(value or "")).strip()
        if _has_any_term(query.lower(), ["airpods", "air pods", "airpod", "aipods", "аирподс", "эйрподс"]):
            query = "airpods"
        key = query.lower()
        if query and key not in seen:
            seen.add(key)
            result.append(query)
    return result


def format_apple_vinted_message(item, domain, title, title_ru, price, curr, link, ts_d, market_line):
    title_safe = html.escape(str(title_ru or title))
    link_safe = html.escape(str(link), quote=True)
    posted = format_msk_timestamp(ts_d)
    try:
        price_eur = vinted_price_to_eur(price, curr)
        price_line = f"{price:g} {html.escape(str(curr))}"
        if str(curr).upper() != "EUR":
            price_line += f" (~{price_eur:.0f} евро)"
    except Exception:
        price_line = f"{price:g} {html.escape(str(curr))}"

    details = [item.get("brand_title"), item.get("status")]
    details_line = html.escape(" / ".join(str(x) for x in details if x))
    meta = f"{details_line}\n\n" if details_line else ""
    country = domain.rsplit(".", 1)[-1].upper()
    return (
        f"<b>Apple Vinted {country}</b>\n"
        f"<b>{title_safe}</b>\n"
        f"{meta}"
        f"<b>Цена:</b> {price_line}{market_line}\n"
        f"<b>Публикация:</b> {posted}\n\n"
        f"<a href='{link_safe}'>Открыть объявление</a>"
    )


async def _send_apple_vinted_item(bot_app, photo_data, msg, run_id):
    if not is_market_run_current("apple_vinted", run_id):
        return
    chat_ids = notification_chat_ids()
    if not chat_ids or not bot_app:
        return

    async def send_all():
        for chat_id in chat_ids:
            if not is_market_run_current("apple_vinted", run_id):
                return
            if photo_data:
                try:
                    photo_file = BytesIO(photo_data)
                    photo_file.name = "apple_vinted.jpg"
                    await bot_app.bot.send_photo(
                        chat_id=chat_id,
                        photo=photo_file,
                        caption=msg,
                        parse_mode="HTML",
                    )
                    continue
                except Exception as e:
                    log.warning("Apple Vinted send_photo failed for chat %s: %s", chat_id, e)

            try:
                await bot_app.bot.send_message(
                    chat_id=chat_id,
                    text=msg,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception as e:
                log.warning("Apple Vinted send_message failed for chat %s: %s", chat_id, e)

    run_telegram_coroutine(send_all())


def apple_vinted_loop(bot_app):
    run_id = state.get("apple_vinted_run_id", 0)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for domain in APPLE_VINTED_DOMAINS:
        init_vinted(domain)
        sleep_while_market_running("apple_vinted", run_id, 1)

    log.info("Apple Vinted monitoring started: %s", ", ".join(APPLE_VINTED_DOMAINS))
    sent_in_this_run = set()

    while is_market_run_current("apple_vinted", run_id):
        state["apple_vinted_stats"]["cycles"] += 1
        for query in _queries():
            if not is_market_run_current("apple_vinted", run_id):
                break
            keyword = query if state.get("apple_vinted_keywords") else ""

            for domain in APPLE_VINTED_DOMAINS:
                if not is_market_run_current("apple_vinted", run_id):
                    break

                price_min, price_max = _apple_price_bounds(domain)
                items = fetch_apple_vinted(query, domain, price_min=price_min, price_max=price_max)
                if items == "BAN":
                    sleep_while_market_running("apple_vinted", run_id, random.randint(60, 120))
                    continue

                market_items = None
                old_item_streak = 0

                for item in items or []:
                    if not is_market_run_current("apple_vinted", run_id):
                        break
                    seen_key = _apple_primary_seen_key(item)
                    seen_aliases = _apple_seen_aliases(item, domain)
                    if not seen_key or _apple_item_seen(item, domain, sent_in_this_run):
                        continue
                    title = item.get("title", "?")

                    if not is_relevant_apple_vinted_item(item):
                        log.info("SKIP Apple Vinted tech filter: %s", title[:60])
                        continue
                    if keyword and not apple_vinted_matches_keyword(item, keyword):
                        log.info("SKIP Apple Vinted keyword '%s': %s", keyword, title[:60])
                        continue
                    if not apple_vinted_matches_desc_filter(item):
                        log.info("SKIP Apple Vinted desc_filter: %s", title[:60])
                        continue

                    ts_d = parse_apple_vinted_ts(item)
                    if ts_d is None:
                        log.info("SKIP Apple Vinted no publish time id=%s '%s'", seen_key, title[:60])
                        continue
                    age_ok = age_in_range(
                        ts_d,
                        state["apple_vinted_min_age_hours"],
                        state["apple_vinted_max_age_hours"],
                    )
                    if age_ok is False:
                        age_hours = publish_age_hours(ts_d)
                        age_label = f"{age_hours:.1f}h" if age_hours is not None else "unknown"
                        log.info("SKIP Apple Vinted age %s: %s", age_label, title[:60])
                        if age_hours is not None and age_hours > float(state["apple_vinted_max_age_hours"]):
                            old_item_streak += 1
                            if old_item_streak >= APPLE_VINTED_OLD_ITEM_STOP_STREAK:
                                break
                        continue
                    old_item_streak = 0

                    price_data = item.get("price", {}) or {}
                    try:
                        price = float(price_data.get("amount", 0))
                    except (ValueError, TypeError):
                        continue
                    curr = price_data.get("currency_code", "EUR")
                    price_eur = vinted_price_to_eur(price, curr)
                    if not (state["apple_vinted_min"] <= price_eur <= state["apple_vinted_max"]):
                        continue

                    if market_items is None:
                        mp_min, mp_max = _apple_market_price_bounds(domain)
                        market_items = fetch_apple_vinted(query, domain, price_min=mp_min, price_max=mp_max)
                        if market_items == "BAN":
                            market_items = items
                        market_items = market_items or items

                    market_line = ""
                    market = apple_vinted_market_price_eur(market_items, item, keyword)
                    if market:
                        market_eur = float(market["price"])
                        market_count = int(market["count"])
                        if market_eur >= price_eur * 0.50:
                            if price_eur < market_eur:
                                diff = round((1 - price_eur / market_eur) * 100)
                                relation = f"ниже на {diff}%"
                            elif price_eur > market_eur:
                                diff = round((price_eur / market_eur - 1) * 100)
                                relation = f"выше на {diff}%"
                            else:
                                relation = "примерно рынок"
                            market_line = f"\n<b>Рынок:</b> ~{market_eur:.0f} евро, {relation} · {market_count} сравн."
                        else:
                            log.info(
                                "IGNORE Apple Vinted unreliable market %.2f/%.2f %s: %s",
                                price_eur,
                                market_eur,
                                apple_vinted_market_signature(item),
                                title[:60],
                            )
                    else:
                        log.info(
                            "IGNORE Apple Vinted no market sample %s: %s",
                            apple_vinted_market_signature(item),
                            title[:60],
                        )

                    url = item.get("url", "")
                    link = f"https://{domain}{url}" if url.startswith("/") else url
                    title_ru = translate_to_ru(title)
                    photo_url = get_vinted_photo_url(item)
                    photo_data = download_vinted_photo(domain, photo_url)
                    if not is_market_run_current("apple_vinted", run_id):
                        break

                    msg = format_apple_vinted_message(
                        item, domain, title, title_ru, price, curr, link, ts_d, market_line
                    )
                    if not mark_item_seen("apple_vinted", seen_key):
                        continue
                    sent_in_this_run.update(seen_aliases)
                    state["apple_vinted_stats"]["found"] += 1
                    log.info("FOUND Apple Vinted: %s - %.2f %s", title, price, curr)
                    loop.run_until_complete(_send_apple_vinted_item(bot_app, photo_data, msg, run_id))

                sleep_while_market_running("apple_vinted", run_id, random.uniform(8, 15))

        if is_market_run_current("apple_vinted", run_id):
            sleep_while_market_running("apple_vinted", run_id, state["apple_vinted_interval"])

    loop.close()
