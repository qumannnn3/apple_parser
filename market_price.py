import statistics
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Sequence


MIN_REQUIRED_PRICES = 3
MIN_REASONABLE_PRICE = 1
PROFITABLE_DISCOUNT_PERCENT = 20


@dataclass(frozen=True)
class MarketPriceResult:
    market_price: int
    discount_percent: int
    comparable_count: int


def _to_int_price(value) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        price = int(value)
        return price if price >= MIN_REASONABLE_PRICE else None

    if isinstance(value, str):
        cleaned = (
            value.replace(',', '')
            .replace('¥', '')
            .replace('₩', '')
            .replace('€', '')
            .replace('$', '')
            .replace(' ', '')
            .strip()
        )
        digits = ''.join(ch for ch in cleaned if ch.isdigit())
        if not digits:
            return None
        price = int(digits)
        return price if price >= MIN_REASONABLE_PRICE else None

    return None


def _default_price_getter(item: Any):
    if isinstance(item, dict):
        return (
            item.get('price')
            or item.get('price_jpy')
            or item.get('price_krw')
            or item.get('price_eur')
            or item.get('converted_price')
            or item.get('amount')
        )
    return item


def _default_id_getter(item: Any):
    if isinstance(item, dict):
        return (
            item.get('id')
            or item.get('item_id')
            or item.get('product_id')
            or item.get('url')
            or item.get('link')
        )
    return getattr(item, 'id', None) or getattr(item, 'item_id', None)


def _safe_get(item: Any, getter: Optional[Callable[[Any], Any]], fallback: Callable[[Any], Any]):
    if getter:
        try:
            return getter(item)
        except Exception:
            return None
    return fallback(item)


def _same_id(a: Any, b: Any) -> bool:
    return a is not None and b is not None and str(a) == str(b)


def _legacy_target_id(args: tuple[Any, ...], id_getter: Optional[Callable[[Any], Any]]):
    if not args:
        return None
    target_item = args[0]
    if isinstance(target_item, int):
        return None
    return _safe_get(target_item, id_getter, _default_id_getter)


def normalize_market_items(
    items: Iterable,
    *args,
    price_getter: Optional[Callable[[Any], Any]] = None,
    id_getter: Optional[Callable[[Any], Any]] = None,
    item_filter: Optional[Callable[[Any], bool]] = None,
    kind_getter: Optional[Callable[[Any], Any]] = None,
    exclude_id: Any = None,
    **kwargs,
) -> list[int]:
    target_item = args[0] if args and not isinstance(args[0], int) else None
    target_id = exclude_id if exclude_id is not None else _legacy_target_id(args, id_getter)
    target_kind = _safe_get(target_item, kind_getter, lambda _: None) if target_item is not None and kind_getter else None

    prices: list[int] = []

    for item in items or []:
        item_id = _safe_get(item, id_getter, _default_id_getter)
        if _same_id(item_id, target_id):
            continue

        if item_filter:
            try:
                if not item_filter(item):
                    continue
            except Exception:
                continue

        if kind_getter and target_kind:
            try:
                item_kind = kind_getter(item)
            except Exception:
                item_kind = None
            if item_kind and item_kind != target_kind:
                continue

        value = _to_int_price(_safe_get(item, price_getter, _default_price_getter))
        if value is not None:
            prices.append(value)

    return prices


def remove_outliers(prices: Sequence[int]) -> list[int]:
    prices = sorted(int(p) for p in prices if p and p > 0)
    if len(prices) < 4:
        return prices

    q1, _, q3 = statistics.quantiles(prices, n=4, method='inclusive')
    iqr = q3 - q1
    if iqr <= 0:
        return prices

    lower = q1 - 1.5 * iqr
    upper = q3 + 1.5 * iqr
    return [p for p in prices if lower <= p <= upper]


def _market_value(prices: Sequence[int]) -> Optional[int]:
    prices = list(prices)
    if not prices:
        return None

    median_price = statistics.median(prices)
    average_price = statistics.mean(prices)
    return int(round((median_price * 0.7) + (average_price * 0.3)))


def _min_samples(args: tuple[Any, ...], kwargs: dict[str, Any]) -> int:
    value = kwargs.get('min_samples', kwargs.get('min_required', MIN_REQUIRED_PRICES))
    if args and isinstance(args[0], int):
        value = args[0]
    try:
        return max(1, int(value))
    except Exception:
        return MIN_REQUIRED_PRICES


def calculate_market_price(
    items: Iterable,
    *args,
    price_getter: Optional[Callable[[Any], Any]] = None,
    id_getter: Optional[Callable[[Any], Any]] = None,
    item_filter: Optional[Callable[[Any], bool]] = None,
    kind_getter: Optional[Callable[[Any], Any]] = None,
    exclude_id: Any = None,
    return_dict: bool = True,
    **kwargs,
):
    """
    Универсальный расчёт рынка для Mercari / Fruits / Vinted.

    Совместим с текущими вызовами:
    calculate_market_price(items, target_item, brand, price_getter=..., id_getter=...,
                           item_filter=..., kind_getter=..., min_samples=...)

    Возвращает dict: {'price': int, 'count': int}
    Если нужно старое поведение с одним числом: return_dict=False.
    """
    min_samples = _min_samples(args, kwargs)

    prices = normalize_market_items(
        items,
        *args,
        price_getter=price_getter,
        id_getter=id_getter,
        item_filter=item_filter,
        kind_getter=kind_getter,
        exclude_id=exclude_id,
    )

    if len(prices) < min_samples:
        return None

    filtered = remove_outliers(prices)
    if len(filtered) < min_samples:
        return None

    market_price = _market_value(filtered)
    if not market_price:
        return None

    if return_dict:
        return {'price': market_price, 'count': len(filtered)}

    return market_price


def calculate_discount(price: int, market_price: int) -> int:
    current = _to_int_price(price)
    market = _to_int_price(market_price)

    if not current or not market or market <= 0:
        return 0

    return int(round((1 - (current / market)) * 100))


def build_market_result(
    current_price: int,
    comparable_items: Iterable,
    *args,
    price_getter: Optional[Callable[[Any], Any]] = None,
    id_getter: Optional[Callable[[Any], Any]] = None,
    item_filter: Optional[Callable[[Any], bool]] = None,
    kind_getter: Optional[Callable[[Any], Any]] = None,
    exclude_id: Any = None,
    **kwargs,
) -> Optional[MarketPriceResult]:
    market = calculate_market_price(
        comparable_items,
        *args,
        price_getter=price_getter,
        id_getter=id_getter,
        item_filter=item_filter,
        kind_getter=kind_getter,
        exclude_id=exclude_id,
        return_dict=True,
        **kwargs,
    )
    if not market:
        return None

    market_price = int(market['price'])
    return MarketPriceResult(
        market_price=market_price,
        discount_percent=calculate_discount(current_price, market_price),
        comparable_count=int(market['count']),
    )


def is_profitable(
    current_price: int,
    market_price: int,
    min_discount_percent: int = PROFITABLE_DISCOUNT_PERCENT,
) -> bool:
    return calculate_discount(current_price, market_price) >= min_discount_percent


def market_line_jpy(current_price: int, comparable_items: Iterable, *args, **kwargs) -> str:
    result = build_market_result(current_price, comparable_items, *args, **kwargs)
    if not result:
        return ''
    return f'Рынок: ~¥{result.market_price:,}, ниже на {result.discount_percent}%'


def market_line_krw(current_price: int, comparable_items: Iterable, *args, **kwargs) -> str:
    result = build_market_result(current_price, comparable_items, *args, **kwargs)
    if not result:
        return ''
    return f'Рынок: ~₩{result.market_price:,}, ниже на {result.discount_percent}%'


def market_line_eur(current_price: int, comparable_items: Iterable, *args, **kwargs) -> str:
    result = build_market_result(current_price, comparable_items, *args, **kwargs)
    if not result:
        return ''
    return f'Рынок: ~{result.market_price} EUR, ниже на {result.discount_percent}%'
