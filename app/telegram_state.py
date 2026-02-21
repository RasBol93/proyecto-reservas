from typing import Any, Dict, List, Tuple

from app.menu import load_menu_index
from app.utils import normalize


CLIENT_STATE: Dict[str, Dict[str, Any]] = {}


def _state_key(tenant_id: str, chat_id: int) -> str:
    return f"{tenant_id}:{chat_id}"


def get_client_state(tenant_id: str, chat_id: int) -> Dict[str, Any]:
    key = _state_key(tenant_id, chat_id)
    if key not in CLIENT_STATE:
        CLIENT_STATE[key] = {
            "step": "HOME",
            "cart": [],
            "pending_sku": None,
            "selected_cat": None,
            "customer_name": "",
            "requested_time": "",
        }
    return CLIENT_STATE[key]


def cart_add(state: Dict[str, Any], sku: str, qty: int) -> None:
    sku = str(sku).strip()
    qty = int(qty)
    for it in state["cart"]:
        if it["sku"] == sku:
            it["qty"] += qty
            return
    state["cart"].append({"sku": sku, "qty": qty})


def cart_clear(state: Dict[str, Any]) -> None:
    state["cart"] = []
    state["pending_sku"] = None


def cart_text_and_total(state: Dict[str, Any], menu_idx: Dict[str, Dict[str, Any]]) -> Tuple[str, float]:
    if not state["cart"]:
        return ("Tu carrito está vacío.", 0.0)
    lines = ["🛒 Carrito:"]
    total = 0.0
    for it in state["cart"]:
        sku = it["sku"]
        qty = it["qty"]
        name = menu_idx.get(sku, {}).get("name", sku)
        price = float(menu_idx.get(sku, {}).get("price", 0))
        subtotal = price * qty
        total += subtotal
        lines.append(f"- {name} ({sku}) x{qty} = {subtotal:.2f} BOB")
    lines.append(f"\nTotal: {total:.2f} BOB")
    return ("\n".join(lines), round(total, 2))
