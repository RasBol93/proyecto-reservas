import argparse
import json
import random
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

try:
    from gspread.exceptions import WorksheetNotFound
except Exception:  # pragma: no cover
    WorksheetNotFound = Exception  # type: ignore

from app.menu import load_menu_index
from app.sheets import detect_header_row, get_gspread_client, open_spreadsheet_by_key
from app.survey_core import SURVEY_RESPONSES_HEADERS, SURVEY_RESPONSES_WS
from app.tenants import get_tenant_or_404
from app.utils import normalize


SEED_TAG = "seed_demo_analytics_2026_01_01_2026_05_21"
RANDOM_SEED = 20260521
DEFAULT_TENANT_ID = "resto_demo"
DEFAULT_START_DATE = "2026-01-01"
DEFAULT_END_DATE = "2026-05-21"
DEFAULT_ORDERS_PER_DAY = 6
DEFAULT_TENANT_TZ = "America/La_Paz"

ORDERS_WORKSHEET_CANDIDATES = ["ORDERS", "Orders", "orders"]
ALLOWED_ORDER_SOURCES = (("webapp", 0.70), ("admin_telegram", 0.30))
QUESTION_SET = [
    ("1", 1, "¿Cómo calificarías el servicio?"),
    ("2", 2, "¿Cómo calificarías el sabor?"),
    ("3", 3, "¿Cómo calificarías la variedad?"),
    ("4", 4, "¿Qué tanto nos recomendarías?"),
    ("q1", 5, "¿En general qué piensas de nosotros?"),
]
STAR_DISTRIBUTION = {
    1: 0.20,
    2: 0.10,
    3: 0.30,
    4: 0.30,
    5: 0.10,
}
CUSTOMER_CLASS_TARGETS = {
    1: 0.30,
    2: 0.45,
    3: 0.20,
    4: 0.05,
}
FALLBACK_CATALOG = [
    {"sku": "H01", "name": "Hamburguesa clásica tiburon", "price": 20.0, "category": "Hamburguesas"},
    {"sku": "H02", "name": "Hamburguesa Doble", "price": 32.0, "category": "Hamburguesas"},
    {"sku": "H03", "name": "Hamburguesa vegetal", "price": 28.0, "category": "Hamburguesas"},
    {"sku": "p_1774482430", "name": "Hambupollo", "price": 22.0, "category": "Hamburguesas"},
    {"sku": "B01", "name": "Coca Cola (lata)", "price": 8.0, "category": "Bebidas"},
    {"sku": "B02", "name": "Agua simple", "price": 7.0, "category": "Bebidas"},
    {"sku": "P01", "name": "Papas Fritas", "price": 6.0, "category": "Acompañamientos"},
    {"sku": "S01", "name": "Salsa Extra", "price": 3.0, "category": "Acompañamientos"},
    {"sku": "D01", "name": "Brownie", "price": 15.0, "category": "Postres"},
    {"sku": "p_1775509993", "name": "Frutillas con crema", "price": 13.0, "category": "Postres"},
]
FIRST_NAMES = [
    "Renato", "Ximena", "Carlos", "María", "Fernanda", "Sofía", "Luis", "Camila", "Valeria", "Diego",
    "Carla", "Mateo", "Paola", "Jorge", "Brenda", "Adriana", "Marcos", "Andrea", "Ricardo", "Elena",
    "Natalia", "José", "Luciana", "Hugo", "Melissa", "Daniela", "Alvaro", "Fabiola", "Kevin", "Noelia",
]
LAST_NAMES = [
    "Rojas", "Flores", "Quispe", "Mamani", "Lopez", "Guzman", "Vargas", "Suarez", "Mendoza", "Rivera",
    "Soria", "Arce", "Salazar", "Paredes", "Montaño", "Arias", "Cespedes", "Nina", "Choque", "Torrez",
]


@dataclass
class SeedCustomer:
    customer_id: str
    name: str
    contact: str


def _safe_text(value: Any) -> str:
    try:
        return str(value or "").strip()
    except Exception:
        return ""


def _parse_cli_date(value: str) -> date:
    try:
        return datetime.strptime(str(value or "").strip(), "%Y-%m-%d").date()
    except Exception as exc:
        raise SystemExit(f"Invalid --start-date/--end-date value '{value}'. Use YYYY-MM-DD.") from exc


def _dates_inclusive(start_date: date, end_date: date) -> List[date]:
    if end_date < start_date:
        raise SystemExit("end-date must be >= start-date.")
    out: List[date] = []
    cursor = start_date
    while cursor <= end_date:
        out.append(cursor)
        cursor += timedelta(days=1)
    return out


def _month_key(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def _month_label(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _iter_months(start_date: date, end_date: date) -> List[Tuple[int, int]]:
    months: List[Tuple[int, int]] = []
    year = start_date.year
    month = start_date.month
    while (year, month) <= (end_date.year, end_date.month):
        months.append((year, month))
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
    return months


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _col_to_a1(col_1based: int) -> str:
    result = ""
    n = int(col_1based)
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


def _range_a1(rows: int, cols: int) -> str:
    return f"A1:{_col_to_a1(cols)}{rows}"


def _normalize_header_map(headers_raw: Sequence[Any]) -> Dict[str, int]:
    mapping: Dict[str, int] = {}
    for idx, header in enumerate(headers_raw):
        clean = normalize(header)
        if clean and clean not in mapping:
            mapping[clean] = idx
    return mapping


def _row_nonempty(row: Sequence[Any]) -> bool:
    return any(_safe_text(cell) for cell in row)


def _pad_rows(rows: List[List[str]], width: int) -> List[List[str]]:
    padded: List[List[str]] = []
    for row in rows:
        current = list(row)
        if len(current) < width:
            current.extend([""] * (width - len(current)))
        else:
            current = current[:width]
        padded.append(current)
    return padded


def _open_orders_worksheet(spreadsheet):
    for title in ORDERS_WORKSHEET_CANDIDATES:
        try:
            return spreadsheet.worksheet(title)
        except WorksheetNotFound:
            continue
        except Exception as exc:
            message = str(exc or "").lower()
            if "not found" in message and "worksheet" in message:
                continue
            raise
    raise RuntimeError("ORDERS worksheet not found (accepted names: ORDERS, Orders, orders).")


def _open_or_create_survey_worksheet(spreadsheet):
    try:
        return spreadsheet.worksheet(SURVEY_RESPONSES_WS)
    except WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=SURVEY_RESPONSES_WS, rows=5000, cols=max(len(SURVEY_RESPONSES_HEADERS) + 2, 16))
        header_row = [str(x) for x in SURVEY_RESPONSES_HEADERS]
        ws.update(_range_a1(1, len(header_row)), [header_row], value_input_option="RAW")
        return ws
    except Exception as exc:
        message = str(exc or "").lower()
        if "not found" in message and "worksheet" in message:
            ws = spreadsheet.add_worksheet(title=SURVEY_RESPONSES_WS, rows=5000, cols=max(len(SURVEY_RESPONSES_HEADERS) + 2, 16))
            header_row = [str(x) for x in SURVEY_RESPONSES_HEADERS]
            ws.update(_range_a1(1, len(header_row)), [header_row], value_input_option="RAW")
            return ws
        raise


def _read_sheet_state(ws, required_headers: Sequence[str]) -> Dict[str, Any]:
    values = ws.get_all_values()
    if not values:
        raise RuntimeError(f"Worksheet '{getattr(ws, 'title', '')}' is empty.")
    header_row_1based = detect_header_row(values, required_headers=list(required_headers), max_scan=10)
    if header_row_1based < 1 or header_row_1based > len(values):
        raise RuntimeError(f"Invalid header row in worksheet '{getattr(ws, 'title', '')}'.")
    headers_raw = list(values[header_row_1based - 1])
    headers_norm = [normalize(h) for h in headers_raw]
    idx_map = _normalize_header_map(headers_raw)
    prefix_rows = [list(row) for row in values[:header_row_1based]]
    body_rows = [list(row) for row in values[header_row_1based:] if _row_nonempty(row)]
    return {
        "ws": ws,
        "values": values,
        "header_row_1based": header_row_1based,
        "headers_raw": headers_raw,
        "headers_norm": headers_norm,
        "idx_map": idx_map,
        "prefix_rows": prefix_rows,
        "body_rows": body_rows,
        "max_cols_existing": max((len(r) for r in values), default=len(headers_raw)),
    }


def _assert_required_columns(idx_map: Dict[str, int], required_headers: Sequence[str], sheet_name: str) -> None:
    missing = [h for h in required_headers if normalize(h) not in idx_map]
    if missing:
        raise RuntimeError(f"Worksheet '{sheet_name}' is missing required columns: {', '.join(missing)}")


def _sheet_row_to_record(headers_raw: Sequence[str], row: Sequence[Any]) -> Dict[str, str]:
    record: Dict[str, str] = {}
    for idx, header in enumerate(headers_raw):
        key = normalize(header)
        if not key:
            continue
        record[key] = _safe_text(row[idx] if idx < len(row) else "")
    return record


def _is_seed_order_row(record: Dict[str, str]) -> bool:
    order_id = record.get("order_id", "")
    notes = record.get("notes", "")
    source = record.get("source", "")
    return (
        order_id.startswith(SEED_TAG)
        or notes == SEED_TAG
        or source == "seed_dashboard"
    )


def _is_seed_survey_row(record: Dict[str, str]) -> bool:
    response_id = record.get("response_id", "")
    order_id = record.get("order_id", "")
    notes = record.get("notes", "")
    source = record.get("source", "")
    seed_tag = record.get("seed_tag", "")
    coupon_code = record.get("coupon_code", "")
    return (
        response_id.startswith(SEED_TAG)
        or order_id.startswith(SEED_TAG)
        or notes == SEED_TAG
        or seed_tag == SEED_TAG
        or source == "seed_dashboard"
        or coupon_code == SEED_TAG
    )


def _build_row_for_headers(headers_raw: Sequence[str], record: Dict[str, Any]) -> List[str]:
    row: List[str] = []
    for header in headers_raw:
        value = record.get(normalize(header), "")
        if isinstance(value, (dict, list)):
            row.append(_json_dump(value))
        elif value is None:
            row.append("")
        else:
            row.append(str(value))
    return row


def _rewrite_sheet(ws, final_rows: List[List[str]], original_row_count: int, width: int) -> None:
    target_rows = max(len(final_rows), original_row_count)
    padded = _pad_rows(final_rows, width)
    while len(padded) < target_rows:
        padded.append([""] * width)
    ws.update(_range_a1(target_rows, width), padded, value_input_option="RAW")


def _load_catalog(orders_sh) -> List[Dict[str, Any]]:
    try:
        menu_idx = load_menu_index(orders_sh, force=False)
    except Exception:
        menu_idx = {}

    items: List[Dict[str, Any]] = []
    for sku, item in (menu_idx or {}).items():
        if not sku:
            continue
        if bool(item.get("is_promo")):
            continue
        try:
            price = float(item.get("price") or 0.0)
        except Exception:
            price = 0.0
        if price <= 0:
            continue
        name = _safe_text(item.get("name") or sku)
        category = _safe_text(item.get("category") or "Otros")
        items.append({
            "sku": str(sku).strip(),
            "name": name,
            "price": round(price, 2),
            "category": category,
        })

    if len(items) >= 6:
        return items

    fallback_by_sku = {item["sku"]: dict(item) for item in FALLBACK_CATALOG}
    current = {item["sku"] for item in items}
    for sku, item in fallback_by_sku.items():
        if sku not in current:
            items.append(dict(item))
    return items


def _category_kind(category: str) -> str:
    value = normalize(category)
    if "hamb" in value or "burger" in value:
        return "burgers"
    if "bebid" in value or "drink" in value or "refresco" in value:
        return "drinks"
    if "acompa" in value or "papa" in value or "salsa" in value:
        return "sides"
    if "postr" in value or "dulce" in value or "frutilla" in value or "brownie" in value:
        return "desserts"
    return "other"


def _catalog_with_weights(catalog: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    weighted: List[Dict[str, Any]] = []
    category_offsets: Dict[str, int] = defaultdict(int)
    for item in catalog:
        kind = _category_kind(str(item.get("category") or ""))
        category_offsets[kind] += 1
        position = category_offsets[kind]
        weight = 1.0
        if kind == "burgers":
            weight = 8.0 if position == 1 else 6.0
        elif kind == "drinks":
            weight = 4.0 if position == 1 else 3.0
        elif kind == "sides":
            weight = 5.0 if position == 1 else 3.5
        elif kind == "desserts":
            weight = 3.0 if position == 1 else 2.5
        else:
            weight = 2.0
        enriched = dict(item)
        enriched["weight"] = weight
        enriched["kind"] = kind
        weighted.append(enriched)
    return weighted


def _pick_primary(by_kind: Dict[str, List[Dict[str, Any]]], kind: str, fallback: List[Dict[str, Any]]) -> Dict[str, Any]:
    items = by_kind.get(kind) or []
    if items:
        return items[0]
    return fallback[0]


def _build_templates(catalog: List[Dict[str, Any]]) -> Dict[int, List[List[str]]]:
    by_kind: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in catalog:
        by_kind[str(item.get("kind") or "other")].append(item)

    burgers = by_kind.get("burgers") or catalog[:3]
    drinks = by_kind.get("drinks") or catalog[:2]
    sides = by_kind.get("sides") or catalog[:2]
    desserts = by_kind.get("desserts") or catalog[:2]
    fallback = catalog

    burger1 = _pick_primary(by_kind, "burgers", fallback)
    burger2 = burgers[1] if len(burgers) > 1 else burger1
    burger3 = burgers[2] if len(burgers) > 2 else burger1
    side1 = _pick_primary(by_kind, "sides", fallback)
    side2 = sides[1] if len(sides) > 1 else side1
    drink1 = _pick_primary(by_kind, "drinks", fallback)
    drink2 = drinks[1] if len(drinks) > 1 else drink1
    dessert1 = _pick_primary(by_kind, "desserts", fallback)

    templates = {
        1: [
            [burger1["sku"]],
            [burger2["sku"]],
            [burger3["sku"]],
            [drink1["sku"]],
            [dessert1["sku"]],
        ],
        2: [
            [burger1["sku"], side1["sku"]],
            [burger1["sku"], drink1["sku"]],
            [burger2["sku"], drink1["sku"]],
            [burger3["sku"], drink2["sku"]],
            [dessert1["sku"], drink1["sku"]],
        ],
        3: [
            [burger1["sku"], side1["sku"], drink1["sku"]],
            [burger2["sku"], side1["sku"], drink1["sku"]],
            [burger3["sku"], side2["sku"], drink2["sku"]],
            [burger1["sku"], dessert1["sku"], drink1["sku"]],
        ],
        4: [
            [burger1["sku"], burger2["sku"], side1["sku"], drink1["sku"]],
            [burger2["sku"], side1["sku"], side2["sku"], drink1["sku"]],
            [burger1["sku"], side1["sku"], drink1["sku"], dessert1["sku"]],
        ],
        5: [
            [burger1["sku"], burger2["sku"], side1["sku"], side2["sku"], drink1["sku"]],
            [burger1["sku"], burger3["sku"], side1["sku"], drink1["sku"], dessert1["sku"]],
        ],
        6: [
            [burger1["sku"], burger2["sku"], burger3["sku"], side1["sku"], drink1["sku"], dessert1["sku"]],
        ],
        7: [
            [burger1["sku"], burger2["sku"], burger3["sku"], side1["sku"], side2["sku"], drink1["sku"], dessert1["sku"]],
        ],
    }
    return templates


def _weighted_choice(rng: random.Random, options: Sequence[Tuple[Any, float]]) -> Any:
    total = sum(max(0.0, float(weight)) for _, weight in options)
    if total <= 0:
        return options[0][0]
    pick = rng.random() * total
    cursor = 0.0
    for value, weight in options:
        cursor += max(0.0, float(weight))
        if pick <= cursor:
            return value
    return options[-1][0]


def _weighted_sample_unique(rng: random.Random, items: Sequence[Dict[str, Any]], k: int) -> List[Dict[str, Any]]:
    pool = list(items)
    out: List[Dict[str, Any]] = []
    target = max(0, min(int(k), len(pool)))
    while pool and len(out) < target:
        picked = _weighted_choice(rng, [(item, float(item.get("weight") or 1.0)) for item in pool])
        out.append(picked)
        pool = [item for item in pool if item.get("sku") != picked.get("sku")]
    return out


def _pick_order_line_count(rng: random.Random) -> int:
    bucket = _weighted_choice(rng, [
        ("1", 0.55),
        ("2", 0.20),
        ("3", 0.15),
        ("4", 0.07),
        ("big", 0.03),
    ])
    if bucket == "big":
        return rng.randint(5, 7)
    return int(bucket)


def _pick_line_qty(rng: random.Random, item: Dict[str, Any]) -> int:
    kind = str(item.get("kind") or "other")
    if kind == "sides":
        return _weighted_choice(rng, [(1, 0.72), (2, 0.23), (3, 0.05)])
    if kind == "drinks":
        return _weighted_choice(rng, [(1, 0.85), (2, 0.14), (3, 0.01)])
    return _weighted_choice(rng, [(1, 0.82), (2, 0.15), (3, 0.03)])


def _build_order_items(
    rng: random.Random,
    catalog: List[Dict[str, Any]],
    templates: Dict[int, List[List[str]]],
    catalog_by_sku: Dict[str, Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], float]:
    line_count = min(_pick_order_line_count(rng), len(catalog))
    use_template = line_count in templates and templates[line_count] and rng.random() < 0.68

    selected_skus: List[str]
    if use_template:
        selected_skus = list(rng.choice(templates[line_count]))
    else:
        selected_skus = [item["sku"] for item in _weighted_sample_unique(rng, catalog, line_count)]

    items: List[Dict[str, Any]] = []
    snapshot: List[Dict[str, Any]] = []
    total_amount = 0.0
    for sku in selected_skus:
        item = catalog_by_sku[sku]
        qty = int(_pick_line_qty(rng, item))
        unit_price = round(float(item["price"]), 2)
        line_total = round(unit_price * qty, 2)
        items.append({"sku": sku, "qty": qty})
        snapshot.append({
            "sku": sku,
            "name": str(item["name"]),
            "qty": qty,
            "unit_price": unit_price,
            "line_total": line_total,
            "category": str(item["category"]),
        })
        total_amount += line_total
    return items, snapshot, round(total_amount, 2)


def _generate_phone(rng: random.Random, used_phones: set[str]) -> str:
    while True:
        first = rng.choice(["6", "7"])
        phone = first + "".join(str(rng.randint(0, 9)) for _ in range(7))
        if phone not in used_phones:
            used_phones.add(phone)
            return phone


def _generate_customer_name(rng: random.Random) -> str:
    return f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"


def _solve_monthly_bucket_counts(total_orders: int) -> Dict[int, int]:
    best_counts: Optional[Dict[int, int]] = None
    best_score: Optional[float] = None
    for n4 in range(0, (total_orders // 4) + 1):
        remaining_after_n4 = total_orders - (4 * n4)
        for n3 in range(0, (remaining_after_n4 // 3) + 1):
            remaining_after_n3 = remaining_after_n4 - (3 * n3)
            for n2 in range(0, (remaining_after_n3 // 2) + 1):
                n1 = remaining_after_n3 - (2 * n2)
                if n1 < 0:
                    continue
                customer_count = n1 + n2 + n3 + n4
                if customer_count <= 0:
                    continue
                counts = {1: n1, 2: n2, 3: n3, 4: n4}
                score = 0.0
                for visits, target in CUSTOMER_CLASS_TARGETS.items():
                    actual = counts[visits] / customer_count
                    score += abs(actual - target)
                score += abs(customer_count - (total_orders / 2.0)) * 0.001
                if best_score is None or score < best_score:
                    best_score = score
                    best_counts = counts
    if best_counts is None:
        raise RuntimeError("Unable to solve monthly customer distribution.")
    return best_counts


def _select_month_customers(
    rng: random.Random,
    global_customers: List[SeedCustomer],
    used_phones: set[str],
    customer_count: int,
) -> List[SeedCustomer]:
    selected: List[SeedCustomer] = []
    selected_ids: set[str] = set()
    reuse_target = min(len(global_customers), max(0, round(customer_count * 0.28)))

    if reuse_target > 0 and global_customers:
        reusable = list(global_customers)
        rng.shuffle(reusable)
        for customer in reusable:
            if len(selected) >= reuse_target:
                break
            if customer.customer_id in selected_ids:
                continue
            selected.append(customer)
            selected_ids.add(customer.customer_id)

    while len(selected) < customer_count:
        customer_id = f"cust_{len(global_customers) + 1:05d}"
        customer = SeedCustomer(
            customer_id=customer_id,
            name=_generate_customer_name(rng),
            contact=_generate_phone(rng, used_phones),
        )
        global_customers.append(customer)
        selected.append(customer)
        selected_ids.add(customer.customer_id)

    rng.shuffle(selected)
    return selected


def _generate_day_order_times(day: date, orders_per_day: int, tenant_tz: str, rng: random.Random) -> List[datetime]:
    tz = ZoneInfo(tenant_tz)
    windows = [
        ((11, 0), (13, 30), 0.25),
        ((14, 0), (17, 0), 0.45),
        ((18, 0), (22, 0), 0.30),
    ]
    out: List[datetime] = []
    for _ in range(orders_per_day):
        start_tuple, end_tuple = _weighted_choice(
            rng,
            [((start, end), weight) for start, end, weight in windows],
        )
        start_minutes = start_tuple[0] * 60 + start_tuple[1]
        end_minutes = end_tuple[0] * 60 + end_tuple[1]
        minute_of_day = rng.randint(start_minutes, end_minutes)
        hour = minute_of_day // 60
        minute = minute_of_day % 60
        out.append(datetime(day.year, day.month, day.day, hour, minute, 0, tzinfo=tz))
    out.sort()
    return out


def _build_star_bag(total_responses: int, rng: random.Random) -> List[int]:
    bag: List[int] = []
    remaining = total_responses
    keys = sorted(STAR_DISTRIBUTION.keys())
    for idx, star in enumerate(keys):
        if idx == len(keys) - 1:
            count = remaining
        else:
            count = int(round(total_responses * STAR_DISTRIBUTION[star]))
            remaining -= count
        bag.extend([star] * count)
    rng.shuffle(bag)
    return bag


def _survey_local_date(dt_local: datetime) -> str:
    return dt_local.strftime("%Y-%m-%d")


def _iso_local(dt_local: datetime) -> str:
    return dt_local.isoformat()


def _build_orders_and_survey_rows(
    *,
    tenant_id: str,
    tenant_tz: str,
    start_date: date,
    end_date: date,
    orders_per_day: int,
) -> Dict[str, Any]:
    rng = random.Random(RANDOM_SEED)
    days = _dates_inclusive(start_date, end_date)
    total_orders = len(days) * int(orders_per_day)
    total_survey_rows = total_orders * len(QUESTION_SET)
    star_bag = _build_star_bag(total_survey_rows, rng)
    star_index = 0

    catalog = _catalog_with_weights(_load_catalog(_OPEN_CONTEXT["orders_sh"]))
    catalog_by_sku = {str(item["sku"]): item for item in catalog}
    templates = _build_templates(catalog)

    days_by_month: Dict[str, List[date]] = defaultdict(list)
    for day in days:
        days_by_month[_month_key(day)].append(day)

    global_customers: List[SeedCustomer] = []
    used_phones: set[str] = set()
    orders_rows: List[Dict[str, Any]] = []
    survey_rows: List[Dict[str, Any]] = []
    monthly_customer_visit_counts: Counter[int] = Counter()

    order_seq = 0
    for year, month in _iter_months(start_date, end_date):
        month_key = _month_label(year, month)
        month_days = list(days_by_month.get(month_key) or [])
        if not month_days:
            continue

        month_order_slots: List[datetime] = []
        for day in month_days:
            month_order_slots.extend(_generate_day_order_times(day, orders_per_day, tenant_tz, rng))
        month_order_slots.sort()

        bucket_counts = _solve_monthly_bucket_counts(len(month_order_slots))
        monthly_customer_visit_counts.update(bucket_counts)
        customer_count = sum(bucket_counts.values())
        month_customers = _select_month_customers(rng, global_customers, used_phones, customer_count)

        visit_counts: List[int] = []
        for visits, count in sorted(bucket_counts.items()):
            visit_counts.extend([visits] * count)
        rng.shuffle(visit_counts)

        customer_visits: List[SeedCustomer] = []
        for customer, visits in zip(month_customers, visit_counts):
            customer_visits.extend([customer] * visits)
        rng.shuffle(customer_visits)

        if len(customer_visits) != len(month_order_slots):
            raise RuntimeError(
                f"Monthly planning mismatch for {month_key}: visits={len(customer_visits)} slots={len(month_order_slots)}"
            )

        for created_local, customer in zip(month_order_slots, customer_visits):
            order_seq += 1
            payment_local = created_local + timedelta(minutes=rng.randint(3, 18))
            survey_local = payment_local + timedelta(minutes=rng.randint(2, 12))
            source = _weighted_choice(rng, list(ALLOWED_ORDER_SOURCES))
            items, items_snapshot, total_amount = _build_order_items(rng, catalog, templates, catalog_by_sku)
            order_id = f"{SEED_TAG}__ord_{order_seq:06d}"
            response_id = f"{SEED_TAG}__resp_{order_seq:06d}"

            orders_rows.append({
                "order_id": order_id,
                "created_at": _iso_local(created_local),
                "tenant_id": tenant_id,
                "customer_name": customer.name,
                "customer_contact": customer.contact,
                "customer_telegram_chat_id": "",
                "items": items,
                "items_snapshot": items_snapshot,
                "currency": "BOB",
                "pricing_version": "seed_v1",
                "notes": SEED_TAG,
                "delivery_type": "pickup",
                "requested_time": created_local.strftime("%H:%M"),
                "status": "PAID",
                "source": source,
                "total_amount": round(total_amount, 2),
                "payment_proof_file_id": "",
                "payment_confirmed_at": _iso_local(payment_local),
                "payment_proof_type": "seed",
                "payment_proof_caption": "Pago simulado para pruebas",
            })

            for question_id, question_order, question_text in QUESTION_SET:
                star_value = star_bag[star_index]
                star_index += 1
                survey_rows.append({
                    "response_id": response_id,
                    "created_at": _iso_local(survey_local),
                    "survey_date": _survey_local_date(survey_local),
                    "submitted_at": _iso_local(survey_local),
                    "tenant_id": tenant_id,
                    "order_id": order_id,
                    "customer_phone": customer.contact,
                    "customer_contact": customer.contact,
                    "customer_name": customer.name,
                    "question_id": question_id,
                    "question_order": str(question_order),
                    "question_text": question_text,
                    "answer_type": "stars",
                    "stars": str(star_value),
                    "answer_value": str(star_value),
                    "text_answer": "",
                    "coupon_code": "",
                    "source": "seed_dashboard",
                    "notes": SEED_TAG,
                    "seed_tag": SEED_TAG,
                })

    if order_seq != total_orders:
        raise RuntimeError(f"Generated orders mismatch: expected {total_orders}, got {order_seq}")
    if len(survey_rows) != total_survey_rows:
        raise RuntimeError(f"Generated survey rows mismatch: expected {total_survey_rows}, got {len(survey_rows)}")

    return {
        "orders_rows": orders_rows,
        "survey_rows": survey_rows,
        "total_orders": total_orders,
        "total_survey_rows": total_survey_rows,
        "customer_visit_buckets": dict(monthly_customer_visit_counts),
        "star_distribution": dict(Counter(int(row["answer_value"]) for row in survey_rows)),
    }


def _prepare_orders_write_payload(sheet_state: Dict[str, Any], generated_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    sheet_name = getattr(sheet_state["ws"], "title", "ORDERS")
    idx_map = dict(sheet_state["idx_map"])
    _assert_required_columns(
        idx_map,
        ["order_id", "created_at", "tenant_id", "customer_name", "customer_contact", "items", "status", "total_amount"],
        sheet_name,
    )
    existing_seed_rows = 0
    kept_rows: List[List[str]] = []
    for row in sheet_state["body_rows"]:
        record = _sheet_row_to_record(sheet_state["headers_raw"], row)
        if _is_seed_order_row(record):
            existing_seed_rows += 1
            continue
        kept_rows.append(list(row))

    seed_rows = [_build_row_for_headers(sheet_state["headers_raw"], row) for row in generated_rows]
    final_rows = [list(r) for r in sheet_state["prefix_rows"]] + kept_rows + seed_rows
    max_cols_final = max(
        sheet_state["max_cols_existing"],
        len(sheet_state["headers_raw"]),
        max((len(r) for r in final_rows), default=len(sheet_state["headers_raw"])),
    )
    return {
        "sheet_name": sheet_name,
        "existing_seed_rows": existing_seed_rows,
        "kept_rows": kept_rows,
        "seed_rows": seed_rows,
        "final_rows": final_rows,
        "max_cols_final": max_cols_final,
        "original_row_count": max(len(sheet_state["values"]), len(final_rows)),
    }


def _prepare_survey_write_payload(sheet_state: Dict[str, Any], generated_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    sheet_name = getattr(sheet_state["ws"], "title", SURVEY_RESPONSES_WS)
    idx_map = dict(sheet_state["idx_map"])
    _assert_required_columns(
        idx_map,
        ["response_id", "created_at", "tenant_id", "customer_name", "question_id", "question_text", "answer_type", "answer_value"],
        sheet_name,
    )
    existing_seed_rows = 0
    kept_rows: List[List[str]] = []
    for row in sheet_state["body_rows"]:
        record = _sheet_row_to_record(sheet_state["headers_raw"], row)
        if _is_seed_survey_row(record):
            existing_seed_rows += 1
            continue
        kept_rows.append(list(row))

    seed_rows = [_build_row_for_headers(sheet_state["headers_raw"], row) for row in generated_rows]
    final_rows = [list(r) for r in sheet_state["prefix_rows"]] + kept_rows + seed_rows
    max_cols_final = max(
        sheet_state["max_cols_existing"],
        len(sheet_state["headers_raw"]),
        max((len(r) for r in final_rows), default=len(sheet_state["headers_raw"])),
    )
    return {
        "sheet_name": sheet_name,
        "existing_seed_rows": existing_seed_rows,
        "kept_rows": kept_rows,
        "seed_rows": seed_rows,
        "final_rows": final_rows,
        "max_cols_final": max_cols_final,
        "original_row_count": max(len(sheet_state["values"]), len(final_rows)),
    }


def _print_samples(title: str, rows: Sequence[Dict[str, Any]], limit: int) -> None:
    print(f"\n{title}:")
    for idx, row in enumerate(rows[:limit], start=1):
        print(f"  [{idx}] {json.dumps(row, ensure_ascii=False)}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Seed synthetic dashboard analytics data into Google Sheets for a demo tenant.")
    parser.add_argument("--tenant-id", default=DEFAULT_TENANT_ID)
    parser.add_argument("--start-date", default=DEFAULT_START_DATE)
    parser.add_argument("--end-date", default=DEFAULT_END_DATE)
    parser.add_argument("--orders-per-day", type=int, default=DEFAULT_ORDERS_PER_DAY)
    parser.add_argument("--apply", action="store_true", help="Actually write rows to Google Sheets.")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing. Default mode if --apply is not set.")
    parser.add_argument("--clear-existing-seed", action="store_true", help="Replace existing rows previously generated by this seed.")
    parser.add_argument("--allow-non-demo", action="store_true", help="Allow writing to a tenant other than resto_demo.")
    return parser


def _validate_args(args: argparse.Namespace) -> Tuple[date, date]:
    tenant_id = _safe_text(args.tenant_id)
    if tenant_id != DEFAULT_TENANT_ID and not bool(args.allow_non_demo):
        raise SystemExit("Refusing to operate on a non-demo tenant without --allow-non-demo.")

    if int(args.orders_per_day or 0) <= 0:
        raise SystemExit("--orders-per-day must be > 0.")

    start_date = _parse_cli_date(args.start_date)
    end_date = _parse_cli_date(args.end_date)
    return start_date, end_date


def _open_tenant_orders_sheet(tenant_id: str):
    gc = get_gspread_client()
    tenant = get_tenant_or_404(tenant_id, gc=gc)
    orders_sheet_id = _safe_text(tenant.get("orders_sheet_id"))
    if not orders_sheet_id:
        raise SystemExit(f"Tenant '{tenant_id}' is missing orders_sheet_id.")
    spreadsheet = open_spreadsheet_by_key(gc, orders_sheet_id)
    tenant_tz = _safe_text(tenant.get("timezone")) or DEFAULT_TENANT_TZ
    return tenant, spreadsheet, tenant_tz


_OPEN_CONTEXT: Dict[str, Any] = {}


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    start_date, end_date = _validate_args(args)
    tenant_id = _safe_text(args.tenant_id)

    tenant, spreadsheet, tenant_tz = _open_tenant_orders_sheet(tenant_id)
    orders_ws = _open_orders_worksheet(spreadsheet)
    survey_ws = _open_or_create_survey_worksheet(spreadsheet)
    _OPEN_CONTEXT["orders_sh"] = spreadsheet

    orders_state = _read_sheet_state(orders_ws, ["order_id", "created_at", "status"])
    survey_state = _read_sheet_state(survey_ws, ["response_id", "created_at", "tenant_id", "question_id", "answer_type", "answer_value"])

    generated = _build_orders_and_survey_rows(
        tenant_id=_safe_text(tenant.get("tenant_id") or tenant_id),
        tenant_tz=tenant_tz,
        start_date=start_date,
        end_date=end_date,
        orders_per_day=int(args.orders_per_day),
    )

    orders_payload = _prepare_orders_write_payload(orders_state, generated["orders_rows"])
    survey_payload = _prepare_survey_write_payload(survey_state, generated["survey_rows"])

    existing_seed_orders = int(orders_payload["existing_seed_rows"])
    existing_seed_surveys = int(survey_payload["existing_seed_rows"])
    if (existing_seed_orders > 0 or existing_seed_surveys > 0) and not bool(args.clear_existing_seed):
        raise SystemExit(
            f"Existing seed rows detected (orders={existing_seed_orders}, survey={existing_seed_surveys}). "
            "Re-run with --clear-existing-seed to replace them."
        )

    total_days = len(_dates_inclusive(start_date, end_date))
    distinct_contacts = len({row["customer_contact"] for row in generated["orders_rows"]})
    print("Seed preview")
    print("------------")
    print(f"Tenant: {tenant_id}")
    print(f"Timezone: {tenant_tz}")
    print(f"Date range: {start_date.isoformat()} -> {end_date.isoformat()}")
    print(f"Days: {total_days}")
    print(f"Orders/day: {args.orders_per_day}")
    print(f"Orders to generate: {generated['total_orders']}")
    print(f"Survey responses to generate: {generated['total_survey_rows']}")
    print(f"Distinct synthetic contacts: {distinct_contacts}")
    print(f"Monthly customer visit buckets (aggregated): {json.dumps(generated['customer_visit_buckets'], ensure_ascii=False, sort_keys=True)}")
    print(f"Survey stars distribution: {json.dumps(generated['star_distribution'], ensure_ascii=False, sort_keys=True)}")
    print(f"Existing seed rows in ORDERS: {existing_seed_orders}")
    print(f"Existing seed rows in Survey_Responses: {existing_seed_surveys}")
    _print_samples("Sample orders", generated["orders_rows"], 3)
    _print_samples("Sample survey responses", generated["survey_rows"], 5)

    if not bool(args.apply):
        print("\nDry-run only. No rows were written.")
        return 0

    _rewrite_sheet(
        orders_state["ws"],
        orders_payload["final_rows"],
        int(orders_payload["original_row_count"]),
        int(orders_payload["max_cols_final"]),
    )
    _rewrite_sheet(
        survey_state["ws"],
        survey_payload["final_rows"],
        int(survey_payload["original_row_count"]),
        int(survey_payload["max_cols_final"]),
    )

    print("\nSeed applied successfully.")
    print(f"ORDERS rows written: {len(orders_payload['seed_rows'])}")
    print(f"Survey_Responses rows written: {len(survey_payload['seed_rows'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
