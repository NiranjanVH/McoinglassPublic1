import asyncio
import hashlib
import hmac
import json
import math
import os
import re
import sys
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()

# ==========================================================
# CONFIGURATION
# ==========================================================
TG_API_ID = int(os.getenv("TG_API_ID") or "0")
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_TARGET_CHAT = os.getenv("TG_TARGET_CHAT", "")
TG_SESSION_NAME = os.getenv("TG_SESSION_NAME", "telegram_session")
TG_SESSION_STRING = os.getenv("TG_SESSION_STRING", "")
STATE_FILE = os.getenv("STATE_FILE", "last_processed_signal_id.txt")

COINDCX_API_KEY = os.getenv("COINDCX_API_KEY", "")
COINDCX_API_SECRET = os.getenv("COINDCX_API_SECRET", "")

# GCP Static IP Proxy URL
PROXY_URL = os.getenv("GCP_PROXY_URL", "")
DRY_RUN = os.getenv("DRY_RUN", "true").strip().lower() not in {"0", "false", "no"}

# Trading Parameters (BTC Only)
TARGET_PAIR = "B-BTC_USDT"
LEVERAGE = 27
TP_POINTS = 300.0
BALANCE_ALLOCATION_PCT = 80.0             # Uses 80% of your INR wallet balance for margin
MIN_ALLOWED_BTC_QUANTITY = Decimal("0.001") # Lowest step size precision limit

USER_AGENT = "RichieRich-AutoBTC/1.0"


# ==========================================================
# 1. TELEGRAM SIGNAL FETCHER & PARSER
# ==========================================================
def parse_signal_text(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None

    cleaned_text = text.replace("\xa0", " ")
    main_section = cleaned_text.split("Alternative Trades")[0]
    clean_block = main_section.replace("**", "").replace("*", "")

    if "BITCOIN" not in clean_block.upper() and "BTC" not in clean_block.upper():
        return None

    dir_match = re.search(r"Direction\s*:\s*(SHORT|LONG)", clean_block, re.IGNORECASE)
    direction = dir_match.group(1).upper() if dir_match else None

    range_match = re.search(
        r"Entry Range\s*:\s*\$?([\d,]+(?:\.\d+)?)\s*[-–—]\s*\$?([\d,]+(?:\.\d+)?)",
        clean_block,
        re.IGNORECASE,
    )
    range_low = float(range_match.group(1).replace(",", "")) if range_match else None
    range_high = float(range_match.group(2).replace(",", "")) if range_match else None

    limit_match = re.search(r"Entry Range.*?Limit\s*:\s*\$?([\d,]+(?:\.\d+)?)", clean_block, re.IGNORECASE)
    if limit_match:
        entry_val = float(limit_match.group(1).replace(",", ""))
    elif range_high is not None:
        entry_val = range_high
    else:
        entry_val = None

    if not direction or entry_val is None:
        return None

    if range_low is not None and range_high is not None:
        r_min, r_max = min(range_low, range_high), max(range_low, range_high)
    else:
        r_min, r_max = entry_val, entry_val

    tp1_val = (entry_val - TP_POINTS) if direction == "SHORT" else (entry_val + TP_POINTS)

    sl_match = re.search(r"Stop Loss\s*:\s*\$?([\d,]+(?:\.\d+)?)", clean_block, re.IGNORECASE)
    prob_match = re.search(r"Probability\s*:\s*([\d]+%)", clean_block, re.IGNORECASE)
    conf_match = re.search(r"Confidence\s*:\s*([\d]+%)", clean_block, re.IGNORECASE)

    return {
        "direction": direction,
        "entry": entry_val,
        "range_low": r_min,
        "range_high": r_max,
        "tp1": tp1_val,
        "sl_ignored": sl_match.group(1) if sl_match else "N/A",
        "probability": prob_match.group(1) if prob_match else "N/A",
        "confidence": conf_match.group(1) if conf_match else "N/A",
    }


def confirm_signal_direction(signals: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if len(signals) < 2:
        print("[!] Fewer than two valid BTC signals were found; cannot confirm direction.")
        return None

    latest, previous = signals[0], signals[1]
    if latest["direction"] != previous["direction"]:
        print(
            f"[!] Latest BTC signal directions disagree "
            f"({latest['direction']} in Msg {latest['message_id']}, "
            f"{previous['direction']} in Msg {previous['message_id']}); skipping."
        )
        return None

    print(
        f"[+] Direction confirmed by latest two BTC signals: {latest['direction']} "
        f"(Msgs {latest['message_id']} and {previous['message_id']})."
    )
    return latest


async def fetch_latest_btc_signal() -> Optional[Dict[str, Any]]:
    if TG_SESSION_STRING:
        try:
            session = StringSession(TG_SESSION_STRING.strip())
        except ValueError as exc:
            raise ValueError(
                "TG_SESSION_STRING is invalid. Set it to the exact value returned by "
                "client.session.save() after completing Telegram login; do not use a "
                ".session filename, API hash, or surrounding label."
            ) from exc
    else:
        session = TG_SESSION_NAME
    async with TelegramClient(session, TG_API_ID, TG_API_HASH) as client:
        signals = []
        async for message in client.iter_messages(TG_TARGET_CHAT):
            parsed = parse_signal_text(message.text)
            if parsed:
                parsed["message_id"] = message.id
                parsed["message_date"] = str(message.date)
                signals.append(parsed)
                if len(signals) == 2:
                    break
    return confirm_signal_direction(signals)


def get_last_processed_id() -> Optional[int]:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return int(f.read().strip())
        except ValueError:
            return None
    return None


def save_last_processed_id(msg_id: int) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(str(msg_id))


# ==========================================================
# 2. COINDCX API HELPERS VIA GCP STATIC IP PROXY
# ==========================================================
def sign_payload(secret: str, payload: str) -> str:
    return hmac.new(
        secret.strip().encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def signed_request(method: str, path: str, payload_dict: Dict[str, Any]) -> requests.Response:
    body = json.dumps(payload_dict, separators=(",", ":"))
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "X-AUTH-APIKEY": COINDCX_API_KEY.strip(),
        "X-AUTH-SIGNATURE": sign_payload(COINDCX_API_SECRET, body),
    }
    url = f"{PROXY_URL}{path}"
    if method.upper() == "GET":
        return requests.get(url, data=body, headers=headers, timeout=15)
    return requests.post(url, data=body, headers=headers, timeout=15)


def get_futures_inr_balance() -> float:
    timestamp = int(round(time.time() * 1000))
    resp = signed_request("GET", "/exchange/v1/derivatives/futures/wallets", {"timestamp": timestamp})
    if resp.status_code in (404, 405):
        resp = signed_request("POST", "/exchange/v1/users/balances", {"timestamp": timestamp})
    if not (200 <= resp.status_code < 300):
        raise RuntimeError(f"Wallet fetch failed (HTTP {resp.status_code}): {resp.text}")

    for w in resp.json():
        currency = (w.get("currency_short_name") or w.get("currency") or "").upper()
        if currency == "INR":
            return float(w.get("balance", 0.0))
    raise ValueError("INR futures wallet entry not found.")


def get_inr_usdt_conversion() -> float:
    timestamp = int(round(time.time() * 1000))
    resp = signed_request("GET", "/api/v1/derivatives/futures/data/conversions", {"timestamp": timestamp})
    if not (200 <= resp.status_code < 300):
        raise RuntimeError(f"INR/USDT conversion fetch failed (HTTP {resp.status_code}): {resp.text}")
    for conv in resp.json():
        margin = str(conv.get("margin_currency_short_name", "")).upper()
        target = str(conv.get("target_currency_short_name", "")).upper()
        if margin == "INR" and target == "USDT":
            rate = float(conv.get("conversion_price", 0.0))
            if rate > 0:
                return rate
    raise ValueError("INR/USDT conversion rate not found.")


def list_open_orders(side: Optional[str] = None, status: str = "open,partially_filled,untriggered") -> List[Dict[str, Any]]:
    sides = [side] if side else ["buy", "sell"]
    all_orders: List[Dict[str, Any]] = []
    for s in sides:
        payload = {
            "timestamp": int(round(time.time() * 1000)),
            "status": status,
            "side": s,
            "page": "1",
            "size": "50",
            "margin_currency_short_name": ["INR"],
        }
        resp = signed_request("POST", "/exchange/v1/derivatives/futures/orders", payload)
        if 200 <= resp.status_code < 300 and isinstance(resp.json(), list):
            all_orders.extend(resp.json())
    return all_orders


def cancel_order_by_id(order_id: str) -> None:
    payload = {
        "timestamp": int(round(time.time() * 1000)),
        "id": order_id.strip(),
    }
    resp = signed_request("POST", "/exchange/v1/derivatives/futures/orders/cancel", payload)
    print(f"[*] Cancel Order ({order_id}) HTTP {resp.status_code}: {resp.text}")
    if not (200 <= resp.status_code < 300):
        raise RuntimeError(f"Cancel failed ({resp.status_code}): {resp.text}")


def list_positions() -> List[Dict[str, Any]]:
    payload = {
        "timestamp": int(round(time.time() * 1000)),
        "page": "1",
        "size": "50",
        "margin_currency_short_name": ["INR"],
    }
    resp = signed_request("POST", "/exchange/v1/derivatives/futures/positions", payload)
    if not (200 <= resp.status_code < 300):
        raise RuntimeError(f"Positions check failed (HTTP {resp.status_code}): {resp.text}")
    data = resp.json()
    return data if isinstance(data, list) else []


def close_position_conditional_auto() -> List[Dict[str, Any]]:
    print("[*] Checking active positions and open orders...")
    positions = list_positions()
    if not positions:
        return []

    open_orders = list_open_orders()
    results = []

    for target_pos in positions:
        if target_pos.get("pair") != TARGET_PAIR:
            continue
        
        active_pos = float(target_pos.get("active_pos", 0) or 0)
        if active_pos == 0:
            continue

        side = str(target_pos.get("side", "")).lower()
        quantity = float(target_pos.get("quantity") or target_pos.get("total_quantity") or 0.0)
        if quantity <= 0:
            continue

        pair_open_orders = [o for o in open_orders if o.get("pair") == TARGET_PAIR and o.get("id")]
        closing_side = "sell" if side == "buy" else "buy"

        # Extract price strictly from the existing TP order
        closing_price = 0.0
        for ord_item in pair_open_orders:
            p_val = float(ord_item.get("price", 0.0) or 0.0)
            if p_val > 0:
                closing_price = p_val
                break

        if closing_price <= 0:
            try:
                t_resp = requests.get(f"{PROXY_URL}/exchange/v1/derivatives/futures/data/active_instruments", timeout=10)
                if 200 <= t_resp.status_code < 300:
                    for inst in t_resp.json():
                        if inst.get("pair") == TARGET_PAIR:
                            closing_price = float(inst.get("last_price") or inst.get("price") or 0.0)
                            break
            except Exception:
                pass

        if closing_price <= 0:
            closing_price = float(target_pos.get("average_price") or target_pos.get("entry_price") or 64150.0)

        payload = {
            "timestamp": int(round(time.time() * 1000)),
            "order": {
                "fromOrderForm": False,
                "pair": TARGET_PAIR,
                "order_type": "limit_order",
                "price": closing_price,
                "side": closing_side,
                "total_quantity": quantity,
                "leverage": 1,
                "margin_currency_short_name": "INR",
                "notification": "email_notification",
                "time_in_force": "good_till_cancel"
            }
        }

        print(f"[*] Submitting opposing Close Position Limit Order at TP price ({closing_price:,.2f}) for {TARGET_PAIR}...")
        resp = signed_request("POST", "/exchange/v1/derivatives/futures/orders/create", payload)
        print(f"[*] Close Order HTTP {resp.status_code}: {resp.text}")

        if not (200 <= resp.status_code < 300):
            raise RuntimeError(f"Close position order failed ({resp.status_code}): {resp.text}")

        results.append(resp.json())

        print(f"[*] Cancelling associated TP / open orders for {TARGET_PAIR}...")
        for ord_item in pair_open_orders:
            oid = ord_item.get("id")
            try:
                cancel_order_by_id(oid)
            except Exception as e:
                print(f"[!] Warning: Could not cancel order {oid}: {e}")

    return results


def evaluate_btc_exposure(signal_data: Dict[str, Any]) -> tuple[str, str, Optional[Dict[str, Any]]]:
    positions = list_positions()
    for p in positions:
        if p.get("pair") == TARGET_PAIR:
            active_pos = float(p.get("active_pos", 0) or 0)
            if active_pos != 0:
                return "SKIP", f"Active BTC position found (size: {active_pos})", None

    open_btc_orders = list_open_orders()
    pair_open_orders = [o for o in open_btc_orders if o.get("pair") == TARGET_PAIR]

    if not pair_open_orders:
        return "PLACE", "No active BTC position or open order found", None

    existing_order = pair_open_orders[0]
    existing_side = str(existing_order.get("side", "")).lower()
    existing_price = float(existing_order.get("price", 0) or 0)
    existing_status = str(existing_order.get("status", "")).lower()

    new_direction = signal_data["direction"]
    expected_side = "buy" if new_direction == "LONG" else "sell"
    new_price = float(signal_data["entry"])
    range_low = float(signal_data["range_low"])
    range_high = float(signal_data["range_high"])

    if existing_status == "partially_filled":
        return "SKIP", f"Existing order {existing_order.get('id')} is already partially filled", None

    if existing_side != expected_side:
        return (
            "SKIP",
            f"Existing open order ({existing_order.get('id')}) is {existing_side.upper()}, "
            f"opposite of new {new_direction} signal",
            None,
        )

    if range_low <= existing_price <= range_high:
        return (
            "SKIP",
            f"Existing {new_direction} order limit ({existing_price:,.2f}) is already inside "
            f"signal Entry Range ({range_low:,.2f} - {range_high:,.2f})",
            None,
        )

    if new_direction == "SHORT" and new_price < existing_price:
        return (
            "MODIFY",
            f"Existing SHORT limit ({existing_price:,.2f}) is outside range ({range_low:,.2f}-{range_high:,.2f}) "
            f"and reduced to {new_price:,.2f}",
            existing_order,
        )
    elif new_direction == "LONG" and new_price > existing_price:
        return (
            "MODIFY",
            f"Existing LONG limit ({existing_price:,.2f}) is outside range ({range_low:,.2f}-{range_high:,.2f}) "
            f"and increased to {new_price:,.2f}",
            existing_order,
        )

    return (
        "SKIP",
        f"Existing {new_direction} open order at {existing_price:,.2f} kept "
        f"(outside range {range_low:,.2f}-{range_high:,.2f}, but new limit {new_price:,.2f} did not meet modify condition)",
        None,
    )


def calculate_dynamic_btc_quantity(
    inr_balance: float,
    conversion_rate: float,
    limit_price: float,
    leverage: int = LEVERAGE,
    allocation_pct: float = BALANCE_ALLOCATION_PCT,
) -> float:
    """Calculates quantity dynamically using 80% of wallet balance, rounded down to 0.001 step size."""
    allocated_inr = inr_balance * (allocation_pct / 100.0)
    margin_usdt = allocated_inr / conversion_rate
    position_value_usdt = margin_usdt * leverage
    raw_qty = position_value_usdt / limit_price

    step = Decimal("0.001")
    floored_units = Decimal(str(math.floor(raw_qty / float(step))))
    quantity = (floored_units * step).quantize(step, rounding=ROUND_DOWN)

    required_usdt_margin = (float(quantity) * limit_price) / leverage
    required_inr_margin = required_usdt_margin * conversion_rate

    print(
        f"[*] Sizing ({allocation_pct}% balance allocation): Qty={quantity} BTC | "
        f"Est. Margin Required=₹{required_inr_margin:.2f} (Available Balance: ₹{inr_balance:.2f})"
    )

    if quantity < MIN_ALLOWED_BTC_QUANTITY:
        raise ValueError(
            f"Calculated quantity ({quantity} BTC) is below exchange minimum step size ({MIN_ALLOWED_BTC_QUANTITY} BTC)."
        )

    if inr_balance < required_inr_margin:
        raise ValueError(
            f"Insufficient INR balance (₹{inr_balance:.2f}) for calculated order size "
            f"(requires ~₹{required_inr_margin:.2f} at {leverage}x)."
        )

    return float(quantity)


def place_btc_limit_order(signal_data: Dict[str, Any]) -> Dict[str, Any]:
    direction = signal_data["direction"]
    limit_price = signal_data["entry"]
    tp_price = signal_data["tp1"]
    side = "buy" if direction == "LONG" else "sell"

    inr_balance = get_futures_inr_balance()
    conversion_rate = get_inr_usdt_conversion()
    
    quantity = calculate_dynamic_btc_quantity(
        inr_balance=inr_balance,
        conversion_rate=conversion_rate,
        limit_price=limit_price,
        leverage=LEVERAGE,
    )

    payload = {
        "timestamp": int(round(time.time() * 1000)),
        "order": {
            "side": side,
            "pair": TARGET_PAIR,
            "order_type": "limit_order",
            "price": limit_price,
            "total_quantity": quantity,
            "leverage": LEVERAGE,
            "margin_currency_short_name": "INR",
            "notification": "email_notification",
            "take_profit_price": tp_price,
        },
    }

    print(f"[*] Submitting {direction} Limit Order for {TARGET_PAIR}:\n{json.dumps(payload, indent=2)}")
    resp = signed_request("POST", "/exchange/v1/derivatives/futures/orders/create", payload)
    print(f"[*] Create Order HTTP {resp.status_code}: {resp.text}")

    if not (200 <= resp.status_code < 300):
        raise RuntimeError(f"Exchange error ({resp.status_code}): {resp.text}")

    data = resp.json()
    order_obj = data[0] if isinstance(data, list) and data else data
    if isinstance(order_obj, dict) and order_obj.get("status") == "error":
        raise RuntimeError(order_obj.get("message", "Order creation failed"))

    return {
        "order_id": order_obj.get("id") or order_obj.get("order_id") or "",
        "pair": TARGET_PAIR,
        "direction": direction,
        "side": side,
        "limit_price": limit_price,
        "take_profit_price": tp_price,
        "quantity": quantity,
        "leverage": LEVERAGE,
    }


# ==========================================================
# 3. MAIN EXECUTION FLOW
# ==========================================================
def validate_config() -> None:
    required = {
        "TG_API_ID": TG_API_ID,
        "TG_API_HASH": TG_API_HASH,
        "TG_TARGET_CHAT": TG_TARGET_CHAT,
        "GCP_PROXY_URL": PROXY_URL,
    }
    if not DRY_RUN:
        required.update(
            {
                "COINDCX_API_KEY": COINDCX_API_KEY,
                "COINDCX_API_SECRET": COINDCX_API_SECRET,
            }
        )
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(f"Missing required configuration: {', '.join(missing)}")


def run_auto_trader():
    validate_config()
    print("=== BTC AUTO-TRADER STARTED ===")
    if DRY_RUN:
        print("[*] DRY_RUN is enabled; orders will not be placed or canceled.")

    if not DRY_RUN:
        try:
            closed_results = close_position_conditional_auto()
            if closed_results:
                print(f"[+] Successfully closed active position with a limit order at TP price and cleared old orders: {closed_results}")
        except Exception as e:
            print(f"[!] Note during position check: {e}")

    signal = asyncio.run(fetch_latest_btc_signal())
    if not signal:
        print("[!] No valid Bitcoin trade signal found in the latest message. Exiting.")
        return

    print(
        f"[+] Latest Signal (Msg ID {signal['message_id']}): "
        f"{signal['direction']} | Range: {signal['range_low']:,.2f}-{signal['range_high']:,.2f} | "
        f"Limit: {signal['entry']:,.2f} | TP1: {signal['tp1']:,.2f} | "
        f"Prob: {signal['probability']} | Conf: {signal['confidence']}"
    )

    last_id = get_last_processed_id()
    if last_id == signal["message_id"]:
        print(f"[*] Signal Msg ID {last_id} was already processed earlier. Ignoring.")
        return

    if DRY_RUN:
        print("[*] Dry run: Signal parsed successfully; CoinDCX was not contacted.")
        return

    action, reason, existing_order = evaluate_btc_exposure(signal)

    if action == "SKIP":
        print(f"[*] Skipping signal -> {reason}")
        save_last_processed_id(signal["message_id"])
        return

    if action == "MODIFY" and existing_order:
        print(f"[*] Modifying order -> {reason}")
        cancel_order_by_id(existing_order["id"])
        time.sleep(0.5)

    order_result = place_btc_limit_order(signal)
    save_last_processed_id(signal["message_id"])
    print(f"[+] ORDER {'MODIFIED' if action == 'MODIFY' else 'PLACED'} SUCCESSFULLY:")
    print(json.dumps(order_result, indent=2))


if __name__ == "__main__":
    try:
        run_auto_trader()
    except Exception as e:
        print(f"[ERROR] {e}")
        sys.exit(1)
