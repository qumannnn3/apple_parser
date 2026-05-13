import logging
import os
import re
import time
import asyncio
import concurrent.futures
import contextvars
import copy
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections.abc import MutableMapping

import requests

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PROXY_URL = os.environ.get("PROXY_URL", "")

VINTED_REGIONS = {
    "at": "www.vinted.at",
    "be": "www.vinted.be",
    "com": "www.vinted.com",
    "co.uk": "www.vinted.co.uk",
    "cz": "www.vinted.cz",
    "de": "www.vinted.de",
    "dk": "www.vinted.dk",
    "ee": "www.vinted.ee",
    "es": "www.vinted.es",
    "fi": "www.vinted.fi",
    "fr": "www.vinted.fr",
    "gr": "www.vinted.gr",
    "hr": "www.vinted.hr",
    "hu": "www.vinted.hu",
    "ie": "www.vinted.ie",
    "it": "www.vinted.it",
    "pl": "www.vinted.pl",
    "lt": "www.vinted.lt",
    "lu": "www.vinted.lu",
    "lv": "www.vinted.lv",
    "nl": "www.vinted.nl",
    "pt": "www.vinted.pt",
    "ro": "www.vinted.ro",
    "se": "www.vinted.se",
    "si": "www.vinted.si",
    "sk": "www.vinted.sk",
}
DEFAULT_VINTED_REGION_CODES = set()
CATALOG_IDS = [1, 3, 5, 9, 7, 12]
MAX_AGE_HOURS = 24

try:
    MERCARI_MIN_MARKET_SAMPLES = max(1, int(os.environ.get("MERCARI_MIN_MARKET_SAMPLES", "1")))
except ValueError:
    MERCARI_MIN_MARKET_SAMPLES = 1

try:
    MERCARI_MAX_MARKET_RATIO = float(os.environ.get("MERCARI_MAX_MARKET_RATIO", "0.90"))
except ValueError:
    MERCARI_MAX_MARKET_RATIO = 0.90

BAD_WORDS = [
    "pieluchy", "pampers", "baby", "dziecko", "dla dzieci", "подгузники", "детское",
    "nosidelko", "fotelik", "wozek", "kocyk", "smoczek", "lozeczko",
    "underwear", "socks", "bielizna", "majtki", "skarpety", "rajstopy",
    "biustonosz", "bokserki", "stringi", "figi",
    "kask", "rower", "hulajnoga", "rolki", "narty", "deska",
    "telefon", "laptop", "tablet", "konsola",
    "perfumy", "krem", "szampon",
    "ksiazka", "zabawka", "puzzle", "klocki",
    "posciel", "poduszka", "koldra", "recznik", "zaslona",
]

DEEP_FASHION_BLOCKED_WORDS = [
    "novelty", "ノベルティ", "sample", "gift", "promo", "limited gift", "付録",
    "mirror", "ミラー", "鏡", "basket", "バスケット", "籠", "かご",
    "cosmetic", "makeup", "perfume", "perfumes", "fragrance", "cologne",
    "parfum", "eau de toilette", "eau de parfum", "edt", "edp",
    "aroma", "aromat", "духи", "парфюм", "аромат",
    "化粧", "メイク", "ポーチのみ", "case only",
    "tableware", "plate", "cup", "mug", "bottle", "glass", "皿", "カップ", "マグ",
    "interior", "home", "room", "blanket", "pillow", "towel", "rug",
    "キッチン", "インテリア", "タオル", "ブランケット", "クッション",
    "baby", "kids", "child", "children", "ベビー", "キッズ", "子供",
]

UNWANTED_ITEM_TERMS = [
    "loafer", "loafers", "penny loafer", "penny loafers", "sport loafer", "sport loafers",
    "derby", "derbies", "derby shoes", "dress shoes", "formal shoes", "oxford shoes",
    "monk strap", "monk-strap", "brogue", "brogues", "moccasin", "moccasins",
    "pump", "pumps", "heel", "heels", "high heels", "stiletto", "stilettos",
    "sandal", "sandals", "flat shoes", "ballet flats", "ballerina", "mules",
    "glasses", "sunglasses", "eyeglasses", "eyewear", "optical", "frames",
    "blouse", "blouses",
    "туфли", "лоферы", "лофер", "дерби", "оксфорды", "каблуки", "босоножки",
    "сандалии", "балетки", "очки", "солнцезащитные очки", "блузка", "блуза",
    "mokasyny", "mokasyn", "lofery", "pantofle", "czolenka", "czółenka",
    "szpilki", "obcasy", "sandaly", "sandały", "okulary", "bluzka",
    "ローファー", "ダービー", "革靴", "オックスフォード", "パンプス", "ヒール",
    "サンダル", "バレエシューズ", "メガネ", "眼鏡", "サングラス", "ブラウス",
    "로퍼", "더비", "구두", "옥스포드", "펌프스", "힐", "샌들", "플랫슈즈",
    "안경", "선글라스", "블라우스",
]

DEEP_FASHION_SIZE_PATTERN = re.compile(
    r"(?<![a-z0-9])("
    r"xxs|xs|s|m|l|xl|xxl|xxxl|"
    r"it\s?\d{2}|eu\s?\d{2}|jp\s?\d{1,2}|us\s?\d{1,2}|"
    r"\d{2}(?:\.\d)?\s?cm"
    r")(?![a-z0-9])",
    re.IGNORECASE,
)

ALL_BRANDS = [
    "stone island", "balenciaga", "raf simons", "bape", "aape",
    "gucci", "chanel", "jeremy scott", "undercover", "comme des garcons",
    "yohji yamamoto", "vetements", "palm angels", "maison margiela",
    "givenchy", "burberry", "supreme", "amiri", "acne studios", "alyx",
    "tornado mart", "14th addiction", "project g/r", "hysteric glamour",
    "dolce&gabbana", "number nine", "grailz project", "y-3", "lgb",
    "ed hardy", "mcm", "true religion", "guiseppe zanotti", "arcteryx",
    "rick owens", "evisu", "saint laurent", "neighborhood", "prada",
    "dior", "jaded london", "diesel", "alpha industries", "glory boyz",
    "ralph lauren", "louis vuitton", "phillipp plein", "versace",
    "rock revival", "armani", "mastermind", "alexander mqueen",
    "cav empt", "buffalo bobs", "billionaire boys club", "acronym",
    "swear", "vivienne westwood", "balmain", "issey miyake",
    "if six was nine", "20471120", "cp company", "laoboutin",
    "robin jeans", "гоша рубчинский", "ferragamo", "salem",
    "marcelo burlon", "erd", "chrome hearts", "isabel marant",
    "mihara yasuhiro", "carol cristian poell", "alice hollywood",
    "moncler", "valentino", "hysterics", "helmut lang",
    "maison martin margiela", "dsquared2",
]

BRAND_ALIASES = {
    "stone island": ["stoneisland", "stone isl", "ストーンアイランド"],
    "balenciaga": ["バレンシアガ", "발렌시아가"],
    "raf simons": ["rafsimons", "ラフシモンズ"],
    "bape": ["a bathing ape", "abathingape", "エイプ", "베이프"],
    "aape": ["aape by a bathing ape"],
    "gucci": ["グッチ", "구찌"],
    "chanel": ["シャネル", "샤넬"],
    "jeremy scott": ["jeremyscott"],
    "undercover": ["under cover", "undercoverism", "アンダーカバー", "アンダーカバーイズム"],
    "comme des garcons": ["comme des garçons", "comme des garcon", "cdg", "コムデギャルソン", "꼼데가르송"],
    "yohji yamamoto": ["yohji", "ヨウジヤマモト", "요지 야마모토"],
    "vetements": ["vetement", "ヴェトモン", "베트멍"],
    "palm angels": ["palmangels"],
    "maison margiela": ["margiela", "maison martin margiela", "martin margiela", "마르지엘라"],
    "givenchy": ["ジバンシィ", "ジバンシー"],
    "burberry": ["バーバリー", "버버리"],
    "supreme": ["シュプリーム", "슈프림"],
    "amiri": ["アミリ", "아미리"],
    "acne studios": ["acne", "アクネ", "아크네"],
    "alyx": ["1017 alyx 9sm", "alyx studio"],
    "tornado mart": ["tornadomart", "トルネードマート"],
    "14th addiction": ["fourteenth addiction", "14thaddiction"],
    "project g/r": ["project gr", "project g r", "projectgr"],
    "hysteric glamour": ["hysterics", "hysteric", "ヒステリックグラマー"],
    "dolce&gabbana": ["dolce gabbana", "dolce and gabbana", "d&g", "ドルチェ&ガッバーナ"],
    "number nine": ["number (n)ine", "number n ine", "numbernine", "ナンバーナイン"],
    "grailz project": ["grailz", "grailzproject"],
    "y-3": ["y3", "yohji adidas", "ワイスリー"],
    "lgb": ["le grand bleu", "ルグランブルー"],
    "ed hardy": ["edhardy"],
    "true religion": ["truereligion", "トゥルーレリジョン"],
    "guiseppe zanotti": ["giuseppe zanotti", "giuseppezanotti", "zanotti", "ジュゼッペザノッティ"],
    "arcteryx": ["arc'teryx", "arc teryx", "arc-teryx", "veilance", "アークテリクス"],
    "rick owens": ["rickowens", "drkshdw", "リックオウエンス", "릭 오웬스"],
    "evisu": ["エヴィス", "エビス"],
    "saint laurent": ["ysl", "yves saint laurent", "saintlaurent", "サンローラン"],
    "neighborhood": ["nbhd", "ネイバーフッド"],
    "dior": ["christian dior", "ディオール", "디올"],
    "alpha industries": ["alpha"],
    "glory boyz": ["gloryboyz", "glo gang", "gbe"],
    "ralph lauren": ["polo ralph lauren"],
    "louis vuitton": ["lv", "ルイヴィトン", "루이비통"],
    "phillipp plein": ["philipp plein", "philip plein", "plein"],
    "mastermind": ["mastermind japan", "mastermind world", "マスターマインド"],
    "alexander mqueen": ["alexander mcqueen", "mcqueen"],
    "cav empt": ["cavempt", "cav empt", "c.e", "c.e.", "c.e cavempt", "シーイー", "キャブエンプト"],
    "billionaire boys club": ["bbc", "bbc ice cream", "bbc icecream", "bbcicecream", "icecream", "ビリオネアボーイズクラブ", "ビリオネア・ボーイズ・クラブ"],
    "vivienne westwood": ["vivienne", "ヴィヴィアン"],
    "issey miyake": ["issey", "イッセイミヤケ"],
    "if six was nine": ["ifsixwasnine", "if six was9", "ifsixwas9"],
    "cp company": ["c.p. company", "c.p company", "c.p.company", "cpcompany", "cp.company", "シーピーカンパニー"],
    "laoboutin": ["louboutin", "christian louboutin", "ルブタン"],
    "robin jeans": ["robin's jeans", "robins jeans"],
    "гоша рубчинский": ["gosha rubchinskiy", "gosha rubchinsky", "gosha rubchinskiy", "ゴーシャラブチンスキー"],
    "ferragamo": ["salvatore ferragamo"],
    "marcelo burlon": ["marcelo burlon county of milan", "county of milan"],
    "erd": ["enfants riches deprimes", "enfants riches déprimés"],
    "chrome hearts": ["クロムハーツ", "크롬하츠"],
    "mihara yasuhiro": ["maison mihara yasuhiro", "mmy"],
    "carol cristian poell": ["carol christian poell", "ccp"],
    "hysterics": ["hysteric glamour", "ヒステリックグラマー"],
    "maison martin margiela": ["maison margiela", "martin margiela", "margiela", "마르지엘라"],
    "moncler": ["モンクレール", "몽클레어"],
    "dsquared2": ["dsquared", "d squared2", "d squared"],
}

try:
    MAX_BRAND_QUERY_VARIANTS = max(1, int(os.environ.get("MAX_BRAND_QUERY_VARIANTS", "4")))
except ValueError:
    MAX_BRAND_QUERY_VARIANTS = 4

USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
]

def _user_state_file_path():
    default_path = "/data/user_profiles.json" if Path("/data").exists() else "user_profiles.json"
    raw = os.environ.get("BOT_USER_STATE_FILE", default_path)
    path = Path(raw)
    if path.is_absolute():
        return path
    return Path(__file__).resolve().parent / path


USER_STATE_FILE = _user_state_file_path()
_current_user_id = contextvars.ContextVar("current_user_id", default=None)
_profiles_lock = threading.RLock()


def _new_state():
    return {
    "chat_id": None,
    "chat_ids": set(),
    "awaiting": None,
    "current_market": None,
    "brands_page": 0,
    "brands_query": "",
    "brands_active_only": False,
    "active_brands": set(ALL_BRANDS),
    "custom_emoji_ids": {},
    "active_vinted_regions": set(DEFAULT_VINTED_REGION_CODES),
    "vinted_regions_page": 0,
    "vinted_running": False,
    "vinted_min": 10,
    "vinted_max": 500,
    "vinted_min_age_hours": 0,
    "vinted_max_age_hours": MAX_AGE_HOURS,
    "vinted_keywords": [],
    "vinted_interval": 300,
    "vinted_run_id": 0,
    "vinted_seen": set(),
    "vinted_stats": {"found": 0, "cycles": 0},
    "_vinted_ts_field": None,
    "_vinted_debug_done": False,
    "mercari_running": False,
    "mercari_min": 1000,
    "mercari_max": 50000,
    "mercari_min_age_hours": 0,
    "mercari_max_age_hours": MAX_AGE_HOURS,
    "mercari_keywords": [],
    "mercari_interval": 300,
    "mercari_run_id": 0,
    "mercari_seen": set(),
    "mercari_stats": {"found": 0, "cycles": 0},
    "fruits_running": False,
    "fruits_min": 10000,
    "fruits_max": 1000000,
    "fruits_min_age_hours": 0,
    "fruits_max_age_hours": MAX_AGE_HOURS,
    "fruits_keywords": [],
    "fruits_interval": 300,
    "fruits_run_id": 0,
    "fruits_seen": set(),
    "fruits_stats": {"found": 0, "cycles": 0},
    "grailed_running": False,
    "grailed_min": 10,
    "grailed_max": 500,
    "grailed_min_age_hours": 0,
    "grailed_max_age_hours": MAX_AGE_HOURS,
    "grailed_keywords": [],
    "grailed_interval": 300,
    "grailed_run_id": 0,
    "grailed_seen": set(),
    "grailed_stats": {"found": 0, "cycles": 0},
    "apple_vinted_running": False,
    "apple_vinted_min": 20,
    "apple_vinted_max": 2000,
    "apple_vinted_min_age_hours": 0,
    "apple_vinted_max_age_hours": MAX_AGE_HOURS,
    "apple_vinted_keywords": [],
    "apple_vinted_desc_filter": [],
    "apple_vinted_interval": 300,
    "apple_vinted_run_id": 0,
    "apple_vinted_seen": set(),
    "apple_vinted_stats": {"found": 0, "cycles": 0},
    }


_default_state = _new_state()
_user_states = {}


_PERSISTED_KEYS = {
    "chat_id",
    "chat_ids",
    "current_market",
    "brands_page",
    "brands_query",
    "brands_active_only",
    "active_brands",
    "custom_emoji_ids",
    "active_vinted_regions",
    "vinted_regions_page",
    "vinted_min",
    "vinted_max",
    "vinted_min_age_hours",
    "vinted_max_age_hours",
    "vinted_keywords",
    "vinted_seen",
    "mercari_min",
    "mercari_max",
    "mercari_min_age_hours",
    "mercari_max_age_hours",
    "mercari_keywords",
    "mercari_seen",
    "fruits_min",
    "fruits_max",
    "fruits_min_age_hours",
    "fruits_max_age_hours",
    "fruits_keywords",
    "fruits_seen",
    "grailed_min",
    "grailed_max",
    "grailed_min_age_hours",
    "grailed_max_age_hours",
    "grailed_keywords",
    "grailed_seen",
    "apple_vinted_min",
    "apple_vinted_max",
    "apple_vinted_min_age_hours",
    "apple_vinted_max_age_hours",
    "apple_vinted_keywords",
    "apple_vinted_desc_filter",
    "apple_vinted_seen",
}


def _serialize_value(value):
    if isinstance(value, set):
        return sorted(str(item) for item in value if item is not None)
    return copy.deepcopy(value)


def _apply_saved_state(target, saved):
    if not isinstance(saved, dict):
        return
    for key in _PERSISTED_KEYS:
        if key not in saved:
            continue
        value = saved[key]
        if key == "chat_ids":
            converted = set()
            for item in value or []:
                try:
                    converted.add(int(item))
                except (TypeError, ValueError):
                    continue
            value = converted
        elif key in ("active_brands", "active_vinted_regions"):
            value = set(value or [])
        elif key in ("vinted_seen", "mercari_seen", "fruits_seen", "grailed_seen", "apple_vinted_seen"):
            value = set(str(item) for item in (value or []) if item is not None)
        if key == "active_vinted_regions":
            value = {code for code in value if code in VINTED_REGIONS}
        target[key] = value


def _load_user_states():
    if not USER_STATE_FILE.exists():
        return
    try:
        raw = json.loads(USER_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logging.getLogger("parser").warning("Could not read user profiles %s: %s", USER_STATE_FILE, exc)
        return
    if not isinstance(raw, dict):
        return
    with _profiles_lock:
        for user_id, saved in raw.items():
            profile = _new_state()
            _apply_saved_state(profile, saved)
            _user_states[str(user_id)] = profile


def _save_user_states():
    with _profiles_lock:
        data = {
            user_id: {
                key: _serialize_value(profile.get(key))
                for key in _PERSISTED_KEYS
                if key in profile
            }
            for user_id, profile in _user_states.items()
        }
    try:
        USER_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        USER_STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        logging.getLogger("parser").warning("Could not save user profiles %s: %s", USER_STATE_FILE, exc)


def _get_profile(user_id):
    key = str(user_id)
    with _profiles_lock:
        if key not in _user_states:
            _user_states[key] = _new_state()
        return _user_states[key]


def set_current_user(user_id, chat_id=None):
    if user_id is None:
        return None
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return None
    _current_user_id.set(str(user_id))
    profile = _get_profile(user_id)
    if chat_id is not None:
        register_chat_id(chat_id)
    return profile


def save_current_user_state():
    if _current_user_id.get() is not None:
        _save_user_states()


def current_user_id():
    return _current_user_id.get()


def _active_state():
    user_id = _current_user_id.get()
    if user_id is None:
        return _default_state
    return _get_profile(user_id)


class StateProxy(MutableMapping):
    def __getitem__(self, key):
        return _active_state()[key]

    def __setitem__(self, key, value):
        _active_state()[key] = value

    def __delitem__(self, key):
        del _active_state()[key]

    def __iter__(self):
        return iter(_active_state())

    def __len__(self):
        return len(_active_state())

    def __contains__(self, key):
        return key in _active_state()

    def get(self, key, default=None):
        return _active_state().get(key, default)

    def setdefault(self, key, default=None):
        return _active_state().setdefault(key, default)

    def update(self, *args, **kwargs):
        return _active_state().update(*args, **kwargs)


state = StateProxy()
_load_user_states()

log = logging.getLogger("parser")
MSK_TZ = timezone(timedelta(hours=3), "MSK")
_eur_rate_cache = {"rate": None, "ts": 0}
_fx_rate_cache = {}
_telegram_loop = None


def set_telegram_loop(loop):
    global _telegram_loop
    _telegram_loop = loop


def run_telegram_coroutine(coro, timeout=60):
    loop = _telegram_loop
    if loop is None or loop.is_closed():
        coro.close()
        log.warning("Telegram send skipped: main event loop is not available")
        return False
    try:
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        future.result(timeout=timeout)
        return True
    except concurrent.futures.TimeoutError:
        log.warning("Telegram send timed out after %ss", timeout)
    except Exception as e:
        log.warning("Telegram send failed on main event loop: %s", e)
    return False


def register_chat_id(chat_id):
    if chat_id is None:
        return
    try:
        chat_id = int(chat_id)
    except (TypeError, ValueError):
        return
    state["chat_id"] = chat_id
    state.setdefault("chat_ids", set()).add(chat_id)


def notification_chat_ids():
    ids = set(state.get("chat_ids") or [])
    if state.get("chat_id") is not None:
        ids.add(state["chat_id"])
    return sorted(ids)


def is_market_run_current(market, run_id):
    return bool(
        state.get(f"{market}_running")
        and run_id is not None
        and state.get(f"{market}_run_id", 0) == run_id
    )


def sleep_while_market_running(market, run_id, seconds):
    end = time.time() + float(seconds)
    while is_market_run_current(market, run_id) and time.time() < end:
        time.sleep(min(1.0, end - time.time()))


def _seen_key(item_id, namespace=None):
    item_id = str(item_id or "").strip()
    if not item_id:
        return ""
    namespace = str(namespace or "").strip()
    return f"{namespace}:{item_id}" if namespace else item_id


def mark_item_seen(market, item_id, namespace=None, max_items=5000):
    key = _seen_key(item_id, namespace)
    if not key:
        return False

    seen_key = f"{market}_seen"
    legacy = str(item_id or "").strip()

    with _profiles_lock:
        profile = _active_state()
        seen = profile.setdefault(seen_key, set())
        if not isinstance(seen, set):
            seen = set(str(item) for item in (seen or []) if item is not None)
            profile[seen_key] = seen

        if key in seen or legacy in seen or item_id in seen:
            return False

        seen.add(key)
        if len(seen) > max_items:
            keep = list(seen)[-max_items:]
            profile[seen_key] = set(keep)

    save_current_user_state()
    return True


def has_item_seen(market, item_id, namespace=None):
    key = _seen_key(item_id, namespace)
    if not key:
        return False
    seen = state.get(f"{market}_seen") or set()
    legacy = str(item_id or "").strip()
    return key in seen or legacy in seen or item_id in seen


def download_image_bytes(url, referer=None, timeout=15):
    if not url:
        return None
    url = str(url).strip()
    if url.startswith("//"):
        url = f"https:{url}"
    if not (url.startswith("http://") or url.startswith("https://")):
        return None

    try:
        response = requests.get(
            url,
            headers={
                "User-Agent": USER_AGENTS[int(time.time()) % len(USER_AGENTS)],
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                **({"Referer": referer} if referer else {}),
            },
            timeout=timeout,
            allow_redirects=True,
        )
        if response.status_code != 200:
            log.warning("Image download bad status %s url=%s", response.status_code, url)
            return None

        content = response.content or b""
        content_type = (response.headers.get("content-type") or "").lower()
        looks_like_image = content.startswith((b"\xff\xd8", b"\x89PNG", b"GIF8", b"RIFF"))
        if not content_type.startswith("image/") and not looks_like_image:
            log.warning("Image download wrong content-type %s url=%s", content_type, url)
            return None
        if len(content) < 1000:
            log.warning("Image download too small url=%s size=%s", url, len(content))
            return None
        return content
    except Exception as e:
        log.warning("Image download failed: %s url=%s", e, url)
        return None


def get_jpy_to_eur() -> float:
    now = time.time()
    if _eur_rate_cache["rate"] and now - _eur_rate_cache["ts"] < 3600:
        return _eur_rate_cache["rate"]
    try:
        r = requests.get("https://api.frankfurter.app/latest?from=JPY&to=EUR", timeout=10)
        rate = float(r.json()["rates"]["EUR"])
        _eur_rate_cache["rate"] = rate
        _eur_rate_cache["ts"] = now
        log.info("Курс JPY->EUR обновлен: %.5f", rate)
        return rate
    except Exception as e:
        log.warning("Не удалось получить курс JPY->EUR: %s", e)
        return 0.0062


def get_fx_rate(from_currency: str, to_currency: str) -> float:
    src = from_currency.upper()
    dst = to_currency.upper()
    if src == dst:
        return 1.0
    key = (src, dst)
    now = time.time()
    cached = _fx_rate_cache.get(key)
    if cached and now - cached["ts"] < 6 * 3600:
        return cached["rate"]
    fallback = {("EUR", "PLN"): 4.23, ("PLN", "EUR"): 1 / 4.23}.get(key, 1.0)
    try:
        r = requests.get(
            "https://api.frankfurter.app/latest",
            params={"from": src, "to": dst},
            timeout=10,
        )
        rate = float(r.json()["rates"][dst])
        _fx_rate_cache[key] = {"rate": rate, "ts": now}
        return rate
    except Exception as e:
        log.warning("Не удалось получить курс %s->%s: %s", src, dst, e)
        return fallback


def vinted_domain_currency(domain: str) -> str:
    return "PLN" if domain.endswith(".pl") else "EUR"


def vinted_price_bounds(domain: str) -> tuple[float, float, str]:
    currency = vinted_domain_currency(domain)
    rate = get_fx_rate("EUR", currency)
    return state["vinted_min"] * rate, state["vinted_max"] * rate, currency


def vinted_price_to_eur(price: float, currency: str) -> float:
    return float(price) * get_fx_rate(currency or "EUR", "EUR")


def _try_parse_ts(val) -> float | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        dt = val if val.tzinfo else val.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    if hasattr(val, "seconds"):
        try:
            return _try_parse_ts(float(val.seconds))
        except (TypeError, ValueError):
            pass
    if hasattr(val, "timestamp") and callable(val.timestamp):
        try:
            return _try_parse_ts(float(val.timestamp()))
        except (TypeError, ValueError, OSError):
            pass
    if isinstance(val, (int, float)):
        ts = float(val)
        if 1577836800000 < ts < 1893456000000:
            ts /= 1000
        return ts if 1577836800 < ts < 1893456000 else None
    if isinstance(val, str):
        val = val.strip()
        if not val:
            return None
        try:
            return _try_parse_ts(float(val))
        except ValueError:
            pass
        try:
            v = val.replace(" UTC", "+00:00").replace("Z", "+00:00").replace(" ", "T")
            dt = datetime.fromisoformat(v)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            pass
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(val[:19], fmt).replace(tzinfo=timezone.utc).timestamp()
            except Exception:
                pass
    return None


def format_msk_timestamp(ts) -> str:
    parsed = _try_parse_ts(ts)
    if not parsed:
        return "не указано"
    return datetime.fromtimestamp(parsed, tz=timezone.utc).astimezone(MSK_TZ).strftime("%d-%m-%Y в %H:%M МСК")


def _age_label(hours):
    hours = float(hours)
    return f"{int(hours)}ч" if hours == int(hours) else f"{hours:g}ч"


def age_range_label(min_hours, max_hours):
    min_hours = float(min_hours or 0)
    max_hours = float(max_hours or 0)
    if min_hours <= 0:
        return f"до {_age_label(max_hours)}"
    return f"{_age_label(min_hours)}–{_age_label(max_hours)}"


def _eur_label(value):
    return f"{float(value):g}"


def vinted_price_range_label():
    return f"{_eur_label(state['vinted_min'])}–{_eur_label(state['vinted_max'])}€"


def mercari_price_range_label():
    return f"{int(state['mercari_min']):,}–{int(state['mercari_max']):,}¥"


def fruits_price_range_label():
    return f"{int(state['fruits_min']):,}–{int(state['fruits_max']):,}₩"


def grailed_price_range_label():
    return f"${_eur_label(state['grailed_min'])}–${_eur_label(state['grailed_max'])}"


def parse_keywords(text):
    raw = str(text or "").strip()
    if raw.lower() in ("", "-", "нет", "none", "clear", "off", "выкл"):
        return []
    parts = re.split(r"[,;\n]+", raw)
    result = []
    seen = set()
    for part in parts:
        keyword = re.sub(r"\s+", " ", part).strip()
        if not keyword:
            continue
        key = keyword.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(keyword)
    return result[:20]


def keywords_label(market):
    keywords = state.get(f"{market}_keywords", [])
    if not keywords:
        return "только бренд"
    text = ", ".join(keywords)
    return text if len(text) <= 90 else text[:87] + "..."


def _dedupe_texts(values):
    result = []
    seen = set()
    for value in values:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def brand_aliases(brand):
    return _dedupe_texts(BRAND_ALIASES.get(str(brand or "").lower().strip(), []))


def brand_query_variants(brand):
    return _dedupe_texts([brand, *brand_aliases(brand)])[:MAX_BRAND_QUERY_VARIANTS]


def brand_match_terms(brand):
    return _dedupe_texts([brand, *brand_aliases(brand)])


def has_brand_disclaimer(text, brand):
    text = re.sub(r"\s+", " ", str(text or "").lower()).strip()
    if not text:
        return False

    markers_after_brand = (
        r"style",
        r"inspired",
        r"inspiration",
        r"look\s*alike",
        r"lookalike",
        r"dupe",
        r"replica",
        r"fake",
        r"bootleg",
        r"unbranded",
        r"w\s+stylu",
        r"в\s+стиле",
    )
    marker_alt = "|".join(markers_after_brand)

    for term in brand_match_terms(brand):
        term = re.sub(r"\s+", " ", str(term or "").lower()).strip()
        if not term:
            continue
        escaped = re.escape(term).replace(r"\ ", r"\s+")
        if re.search(rf"(?<![a-z0-9]){escaped}[-\s]+(?:{marker_alt})(?![a-z0-9])", text):
            return True
        if re.search(rf"(?<![a-z0-9])(?:inspired\s+by|в\s+стиле|w\s+stylu)[-\s]+{escaped}(?![a-z0-9])", text):
            return True

    return False


def _keyword_contains_brand(keyword, brand):
    keyword_l = re.sub(r"\s+", " ", str(keyword or "").lower()).strip()
    if not keyword_l:
        return False
    compact_keyword = keyword_l.replace(" ", "")
    for brand_text in brand_match_terms(brand):
        brand_l = re.sub(r"\s+", " ", str(brand_text or "").lower()).strip()
        if brand_l and (brand_l in keyword_l or brand_l.replace(" ", "") in compact_keyword):
            return True
    return False


def _keyword_mentions_other_brand(keyword, brand):
    return any(
        other != brand and _keyword_contains_brand(keyword, other)
        for other in ALL_BRANDS
    )


def _keyword_without_brand(keyword, brand):
    result = str(keyword or "").strip()
    if not result:
        return result
    for brand_text in sorted(brand_match_terms(brand), key=len, reverse=True):
        pattern = re.escape(brand_text).replace(r"\ ", r"\s+")
        result = re.sub(pattern, " ", result, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", result).strip(" ,;:-")


def market_search_queries(brand, market):
    keywords = state.get(f"{market}_keywords", [])
    if not keywords:
        return [(query_brand, "") for query_brand in brand_query_variants(brand)]
    queries = []
    for keyword in keywords:
        if _keyword_mentions_other_brand(keyword, brand):
            continue
        if _keyword_contains_brand(keyword, brand):
            queries.append((keyword, _keyword_without_brand(keyword, brand)))
            continue
        for query_brand in brand_query_variants(brand):
            queries.append((f"{query_brand} {keyword}", keyword))
    result = []
    seen = set()
    for query, match_keyword in queries:
        key = (query.lower().strip(), match_keyword.lower().strip())
        if key in seen:
            continue
        seen.add(key)
        result.append((query, match_keyword))
    return result


def parse_age_range(text):
    nums = [n.replace(",", ".") for n in re.findall(r"\d+(?:[.,]\d+)?", text or "")]
    if not nums:
        raise ValueError
    if len(nums) == 1:
        min_hours = 0.0
        max_hours = float(nums[0])
    else:
        min_hours = float(nums[0])
        max_hours = float(nums[1])
    if min_hours < 0 or max_hours <= 0 or min_hours >= max_hours:
        raise ValueError
    return min_hours, max_hours


def _parse_price_number(raw):
    s = re.sub(r"[^\d,.\s]", "", str(raw or "")).replace(" ", "")
    if not s:
        raise ValueError
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        chunks = s.split(",")
        if len(chunks) > 1 and len(chunks[0]) <= 3 and all(len(c) == 3 for c in chunks[1:]):
            s = "".join(chunks)
        else:
            s = s.replace(",", ".")
    elif "." in s:
        chunks = s.split(".")
        if len(chunks) > 1 and len(chunks[0]) <= 3 and all(len(c) == 3 for c in chunks[1:]):
            s = "".join(chunks)
    return float(s)


def parse_price_range(text, *, is_int=False):
    raw = str(text or "").strip()
    parts = [p for p in re.split(r"\s*(?:-|–|—|до|to)\s*", raw, maxsplit=1, flags=re.IGNORECASE) if p.strip()]
    if len(parts) < 2:
        parts = re.findall(r"\d+(?:[.,]\d+)?", raw)
    if len(parts) < 2:
        raise ValueError
    min_price = _parse_price_number(parts[0])
    max_price = _parse_price_number(parts[1])
    if min_price <= 0 or max_price <= 0 or min_price >= max_price:
        raise ValueError
    if is_int:
        return int(min_price), int(max_price)
    return min_price, max_price


def publish_age_hours(ts):
    parsed = _try_parse_ts(ts)
    if not parsed:
        return None
    return (time.time() - parsed) / 3600


def age_in_range(ts, min_hours, max_hours):
    age = publish_age_hours(ts)
    if age is None:
        return None
    if age < -1:
        return False
    return float(min_hours) <= age <= float(max_hours)


def translate_to_ru(text: str) -> str:
    if not text or not text.strip():
        return text
    cyr = sum(1 for c in text if "\u0400" <= c <= "\u04FF")
    if cyr / max(len(text), 1) > 0.4:
        return text
    try:
        r = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": "ru", "dt": "t", "q": text},
            timeout=8,
        )
        data = r.json()
        return "".join(part[0] for part in data[0] if part[0]).strip() or text
    except Exception:
        return text


def _obj_get(obj, *names, default=None):
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _contains_term(text, term):
    term = str(term).lower().strip()
    if not term:
        return False
    if re.fullmatch(r"[a-z0-9][a-z0-9 .&'/-]*[a-z0-9]", term):
        boundary = r"[a-z0-9.]" if "." in term else r"[a-z0-9]"
        pattern = rf"(?<!{boundary})" + re.escape(term) + rf"(?!{boundary})"
        return re.search(pattern, text) is not None
    return term in text


def _has_any_term(text, terms):
    return any(_contains_term(text, term) for term in terms)


def is_unwanted_item_text(text):
    return _has_any_term(str(text or "").lower(), UNWANTED_ITEM_TERMS)


KEYWORD_ALIASES = {
    "track": [
        "\u30c8\u30e9\u30c3\u30af",
        "\u30c8\u30e9\u30c3\u30af\u30b9\u30cb\u30fc\u30ab\u30fc",
        "\u30c8\u30e9\u30c3\u30af\u30b7\u30e5\u30fc\u30ba",
        "\u30c8\u30e9\u30c3\u30af\u30c8\u30ec\u30fc\u30ca\u30fc",
        "\u30c8\u30e9\u30c3\u30af1",
        "\u30c8\u30e9\u30c3\u30af2",
    ],
    "sneaker": ["sneakers", "\u30b9\u30cb\u30fc\u30ab\u30fc", "\u30b7\u30e5\u30fc\u30ba"],
    "sneakers": ["sneaker", "\u30b9\u30cb\u30fc\u30ab\u30fc", "\u30b7\u30e5\u30fc\u30ba"],
    "shoe": ["shoes", "\u30b7\u30e5\u30fc\u30ba", "\u9774"],
    "shoes": ["shoe", "\u30b7\u30e5\u30fc\u30ba", "\u9774"],
}


def keyword_matches_text(text, keyword):
    if not keyword:
        return True
    text = str(text or "").lower()
    keyword = str(keyword or "").lower().strip()
    terms = [keyword, *KEYWORD_ALIASES.get(keyword, [])]
    return _has_any_term(text, terms)
