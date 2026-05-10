import asyncio
import html
import random
import re
import time
from io import BytesIO

from market_price import calculate_market_price
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
    pairs = [
        ("iphone", ["iphone", "айфон"]),
        ("ipad", ["ipad", "айпад"]),
        ("macbook", ["macbook", "mac book", "макбук"]),
        ("airpods", ["airpods", "air pods", "аирподс", "эйрподс"]),
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
    return keyword_matches_text(_apple_text_blob(item), keyword)


def is_relevant_apple_vinted_item(item):
    text = _apple_text_blob(item)
    if not _has_any_term(text, APPLE_PRODUCT_TERMS):
        return False
    if _has_any_term(text, APPLE_CARRIER_JUNK_TERMS):
        return False
    if _has_any_term(text, APPLE_BAD_CONDITION_TERMS):
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
    return calculate_market_price(
        items,
        target_item,
        price_getter=apple_vinted_price_eur,
        id_getter=lambda item: item.get("id"),
        item_filter=lambda item: (
            is_relevant_apple_vinted_item(item)
            and (not keyword or apple_vinted_matches_keyword(item, keyword))
        ),
        kind_getter=apple_vinted_product_kind,
        min_samples=APPLE_VINTED_MIN_MARKET_SAMPLES,
    )


def _queries():
    keywords = state.get("apple_vinted_keywords") or []
    values = keywords or APPLE_SEARCH_QUERIES
    result = []
    seen = set()
    for value in values:
        query = re.sub(r"\s+", " ", str(value or "")).strip()
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
            price_line += f" (~{price_eur:.0f} euros)"
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
        f"<b>Price:</b> {price_line}{market_line}\n"
        f"<b>Publication:</b> {posted}\n\n"
        f"<a href='{link_safe}'>Open listing</a>"
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
                    iid = item.get("id")
                    if not iid or has_item_seen("apple_vinted", iid, domain):
                        continue
                    title = item.get("title", "?")

                    if not is_relevant_apple_vinted_item(item):
                        log.info("SKIP Apple Vinted tech filter: %s", title[:60])
                        continue
                    if keyword and not apple_vinted_matches_keyword(item, keyword):
                        log.info("SKIP Apple Vinted keyword '%s': %s", keyword, title[:60])
                        continue

                    ts_d = parse_apple_vinted_ts(item)
                    if ts_d is None:
                        log.info("SKIP Apple Vinted no publish time id=%s '%s'", iid, title[:60])
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

                    market = apple_vinted_market_price_eur(market_items, item, keyword)
                    if not market:
                        log.info("SKIP Apple Vinted no market sample: %s", title[:60])
                        continue

                    market_eur = float(market["price"])
                    market_count = int(market["count"])
                    if price_eur > market_eur * APPLE_VINTED_MAX_MARKET_RATIO:
                        log.info("SKIP Apple Vinted not under market %.2f/%.2f: %s", price_eur, market_eur, title[:60])
                        continue
                    discount = max(0, round((1 - price_eur / market_eur) * 100))
                    market_line = f"\n<b>Market:</b> ~{market_eur:.0f} euros, {discount}% lower · {market_count} comparisons"

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
                    if not mark_item_seen("apple_vinted", iid, domain):
                        continue
                    state["apple_vinted_stats"]["found"] += 1
                    log.info("FOUND Apple Vinted: %s - %.2f %s", title, price, curr)
                    loop.run_until_complete(_send_apple_vinted_item(bot_app, photo_data, msg, run_id))

                sleep_while_market_running("apple_vinted", run_id, random.uniform(8, 15))

        if is_market_run_current("apple_vinted", run_id):
            sleep_while_market_running("apple_vinted", run_id, state["apple_vinted_interval"])

    loop.close()
