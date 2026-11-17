import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()

# ==========================================================
# CONFIGURATION (load credentials from an untracked .env file)
# ==========================================================
TG_API_ID = int(os.getenv("TG_API_ID") or "0")
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_TARGET_CHAT = os.getenv("TG_TARGET_CHAT", "")
TG_SESSION_NAME = os.getenv("TG_SESSION_NAME", "telegram_session")
TG_SESSION_STRING = os.getenv("TG_SESSION_STRING", "")
STATE_FILE = os.getenv("STATE_FILE", "last_processed_signal_id.txt")

COINDCX_API_KEY = os.getenv("COINDCX_API_KEY", "")
COINDCX_API_SECRET = os.getenv("COINDCX_API_SECRET", "")

VM_HOST = os.getenv("OCI_VM_HOST", "")
VM_PORT = int(os.getenv("OCI_VM_PORT") or "22")
VM_USER = os.getenv("OCI_VM_USER", "")
SOCKS_HOST = os.getenv("SOCKS_HOST", "127.0.0.1")
SOCKS_PORT = int(os.getenv("SOCKS_PORT") or "1080")
SSH_KEY_BASE64 = os.getenv("OCI_SSH_PRIVATE_KEY_B64", "")
SSH_KNOWN_HOSTS_FILE = os.path.expanduser(
    os.getenv("SSH_KNOWN_HOSTS_FILE", "~/.ssh/known_hosts")
)
DRY_RUN = os.getenv("DRY_RUN", "true").strip().lower() not in {"0", "false", "no"}
SSH_STRICT_HOST_KEY_CHECKING = (
    os.getenv("SSH_STRICT_HOST_KEY_CHECKING", "true").strip().lower()
    not in {"0", "false", "no"}
)

# Trading Parameters (BTC Only)
TARGET_PAIR = "B-BTC_USDT"
LEVERAGE = 27
TP_POINTS = 300.0
MIN_BTC_QUANTITY = Decimal("0.001")

BASE_URL = "https://api.coindcx.com"
USER_AGENT = "RichieRich-AutoBTC/1.0"
PROXIES = {
    "http": f"socks5h://{SOCKS_HOST}:{SOCKS_PORT}",
    "https": f"socks5h://{SOCKS_HOST}:{SOCKS_PORT}",
}


# ==========================================================
# 1. TELEGRAM SIGNAL FETCHER & PARSER
# ==========================================================
def parse_signal_text(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None

    cleaned_text = text.replace("\xa0", " ")
    main_section = cleaned_text.split("Alternative Trades")[0]
    clean_block = main_section.replace("**", "").replace("*", "")

    # Ensure it is a Bitcoin signal
    if "BITCOIN" not in clean_block.upper() and "BTC" not in clean_block.upper():
        return None

    # 1. Direction (SHORT or LONG)
    dir_match = re.search(r"Direction\s*:\s*(SHORT|LONG)", clean_block, re.IGNORECASE)
    direction = dir_match.group(1).upper() if dir_match else None

    # 2. Extract Entry Range bounds (e.g. $64,600–$64,750)
    range_match = re.search(
        r"Entry Range\s*:\s*\$?([\d,]+(?:\.\d+)?)\s*[-–—]\s*\$?([\d,]+(?:\.\d+)?)",
        clean_block,
        re.IGNORECASE,
    )
    range_low = float(range_match.group(1).replace(",", "")) if range_match else None
    range_high = float(range_match.group(2).replace(",", "")) if range_match else None

    # 3. Limit Entry Price (falls back to upper range bound if "Limit:" is absent)
    limit_match = re.search(r"Entry Range.*?Limit\s*:\s*\$?([\d,]+(?:\.\d+)?)", clean_block, re.IGNORECASE)
    if limit_match:
        entry_val = float(limit_match.group(1).replace(",", ""))
    elif range_high is not None:
        entry_val = range_high
    else:
        entry_val = None

    if not direction or entry_val is None:
        return None

    # Ensure range bounds are properly ordered (low <= high), fallback to entry_val if absent
    if range_low is not None and range_high is not None:
        r_min, r_max = min(range_low, range_high), max(range_low, range_high)
    else:
        r_min, r_max = entry_val, entry_val

    tp1_val = (entry_val - TP_POINTS) if direction == "SHORT" else (entry_val + TP_POINTS)

    # Optional metadata for logging
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
# 2. SSH SOCKS5 TUNNEL MANAGEMENT
# ==========================================================
def is_port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def write_ssh_key_tempfile() -> str:
    if not SSH_KEY_BASE64:
        raise RuntimeError("OCI_SSH_PRIVATE_KEY_B64 is required to start the SSH tunnel.")
    try:
        pem_bytes = base64.b64decode(SSH_KEY_BASE64, validate=True)
    except binascii.Error as exc:
        raise ValueError("OCI_SSH_PRIVATE_KEY_B64 must contain valid base64.") from exc
    fd, path = tempfile.mkstemp(prefix="oci_key_", suffix=".key")
    with os.fdopen(fd, "wb") as f:
        f.write(pem_bytes.replace(b"\r\n", b"\n"))

    if os.name == "nt":
        username = os.environ.get("USERNAME", "")
        if username:
            subprocess.run(
                ["icacls", path, "/inheritance:r", "/grant:r", f"{username}:R"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
    else:
        os.chmod(path, 0o600)
    return path


class OracleSshTunnel:
    def __init__(self):
        self.proc: Optional[subprocess.Popen] = None
        self.key_path: Optional[str] = None
        self.owns_tunnel = False

    def __enter__(self):
        if is_port_open(SOCKS_HOST, SOCKS_PORT):
            print(f"[*] SOCKS5 proxy already listening on {SOCKS_HOST}:{SOCKS_PORT}, reusing it.")
            return self

        print(f"[*] Starting SSH SOCKS5 tunnel -> {VM_USER}@{VM_HOST}:{VM_PORT}...")
        self.key_path = write_ssh_key_tempfile()

        cmd = [
            "ssh",
            "-i", self.key_path,
            "-N",
            "-D", f"{SOCKS_HOST}:{SOCKS_PORT}",
            "-p", str(VM_PORT),
            "-o", "IdentitiesOnly=yes",
            "-o",
            f"StrictHostKeyChecking={'yes' if SSH_STRICT_HOST_KEY_CHECKING else 'no'}",
            "-o",
            f"UserKnownHostsFile={SSH_KNOWN_HOSTS_FILE if SSH_STRICT_HOST_KEY_CHECKING else '/dev/null'}",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=15",
            "-o", "ConnectTimeout=20",
            f"{VM_USER}@{VM_HOST}",
        ]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.owns_tunnel = True

        deadline = time.time() + 25
        try:
            while time.time() < deadline:
                if self.proc.poll() is not None:
                    _, stderr = self.proc.communicate()
                    raise RuntimeError(f"SSH exited early ({self.proc.returncode}): {stderr.strip()}")
                if is_port_open(SOCKS_HOST, SOCKS_PORT):
                    print(f"[+] SSH tunnel active on {SOCKS_HOST}:{SOCKS_PORT}")
                    return self
                time.sleep(0.3)
            raise TimeoutError(f"Timed out waiting for SOCKS5 port {SOCKS_HOST}:{SOCKS_PORT}")
        except Exception:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            if self.key_path and os.path.exists(self.key_path):
                os.remove(self.key_path)
                self.key_path = None
            raise

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.owns_tunnel and self.proc is not None and self.proc.poll() is None:
            print("[*] Closing SSH tunnel...")
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

        if self.key_path and os.path.exists(self.key_path):
            try:
                os.remove(self.key_path)
            except OSError:
                pass


# ==========================================================
# 3. COINDCX API HELPERS, POSITION & ORDER EVALUATION
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
    url = f"{BASE_URL}{path}"
    if method.upper() == "GET":
        return requests.get(url, data=body, headers=headers, proxies=PROXIES, timeout=15)
    return requests.post(url, data=body, headers=headers, proxies=PROXIES, timeout=15)


def verify_exit_ip() -> str:
    resp = requests.get("https://api.ipify.org?format=json", proxies=PROXIES, timeout=12)
    resp.raise_for_status()
    return resp.json().get("ip", "")


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


def cancel_order_by_id(order_id: str) -> None:
    payload = {
        "timestamp": int(round(time.time() * 1000)),
        "id": order_id.strip(),
    }
    resp = signed_request("POST", "/exchange/v1/derivatives/futures/orders/cancel", payload)
    print(f"[*] Cancel Order ({order_id}) HTTP {resp.status_code}: {resp.text}")
    if not (200 <= resp.status_code < 300):
        raise RuntimeError(f"Cancel failed ({resp.status_code}): {resp.text}")


def evaluate_btc_exposure(signal_data: Dict[str, Any]) -> tuple[str, str, Optional[Dict[str, Any]]]:
    """
    Evaluates current BTCUSDT INR positions and open orders against the incoming signal.
    Returns (action, reason, existing_order_dict):
      - ("SKIP", reason, None)    -> Active position exists, existing order is inside Entry Range,
                                     or price condition did not meet modify threshold.
      - ("MODIFY", reason, order) -> Unfilled open order exists in the SAME direction, its current
                                     limit price is OUTSIDE [range_low, range_high], AND:
                                     * SHORT: new limit price < existing limit price
                                     * LONG:  new limit price > existing limit price
      - ("PLACE", reason, None)   -> No active position and no open order.
    """
    # 1. Check active positions
    pos_payload = {
        "timestamp": int(round(time.time() * 1000)),
        "page": "1",
        "size": "50",
        "margin_currency_short_name": ["INR"],
    }
    pos_resp = signed_request("POST", "/exchange/v1/derivatives/futures/positions", pos_payload)
    if not (200 <= pos_resp.status_code < 300):
        raise RuntimeError(f"Positions check failed (HTTP {pos_resp.status_code}): {pos_resp.text}")

    positions = pos_resp.json()
    if not isinstance(positions, list):
        raise ValueError("Unexpected response while checking futures positions.")
    for p in positions:
        if p.get("pair") == TARGET_PAIR:
            active_pos = float(p.get("active_pos", 0) or 0)
            if active_pos != 0:
                return "SKIP", f"Active BTC position found (size: {active_pos})", None

    # 2. Check open / untriggered limit orders
    open_btc_orders: List[Dict[str, Any]] = []
    for side in ("buy", "sell"):
        ord_payload = {
            "timestamp": int(round(time.time() * 1000)),
            "status": "open,partially_filled,untriggered",
            "side": side,
            "page": "1",
            "size": "50",
            "margin_currency_short_name": ["INR"],
        }
        ord_resp = signed_request("POST", "/exchange/v1/derivatives/futures/orders", ord_payload)
        if not (200 <= ord_resp.status_code < 300):
            raise RuntimeError(f"Open orders check failed (HTTP {ord_resp.status_code}): {ord_resp.text}")
        orders = ord_resp.json()
        if not isinstance(orders, list):
            raise ValueError("Unexpected response while checking open futures orders.")
        for order in orders:
            if order.get("pair") == TARGET_PAIR:
                open_btc_orders.append(order)

    if not open_btc_orders:
        return "PLACE", "No active BTC position or open order found", None

    # Evaluate the existing open order against the new signal
    existing_order = open_btc_orders[0]
    existing_side = str(existing_order.get("side", "")).lower()  # "buy" or "sell"
    existing_price = float(existing_order.get("price", 0) or 0)
    existing_status = str(existing_order.get("status", "")).lower()

    new_direction = signal_data["direction"]
    expected_side = "buy" if new_direction == "LONG" else "sell"
    new_price = float(signal_data["entry"])
    range_low = float(signal_data["range_low"])
    range_high = float(signal_data["range_high"])

    # Do not modify partially filled orders
    if existing_status == "partially_filled":
        return "SKIP", f"Existing order {existing_order.get('id')} is already partially filled", None

    # Must match the same direction (LONG->buy, SHORT->sell)
    if existing_side != expected_side:
        return (
            "SKIP",
            f"Existing open order ({existing_order.get('id')}) is {existing_side.upper()}, "
            f"opposite of new {new_direction} signal",
            None,
        )

    # Rule 1: If existing open order limit price is already inside Entry Range, do NOT update
    if range_low <= existing_price <= range_high:
        return (
            "SKIP",
            f"Existing {new_direction} order limit ({existing_price:,.2f}) is already inside "
            f"signal Entry Range ({range_low:,.2f} - {range_high:,.2f})",
            None,
        )

    # Rule 2 (only when existing order price is NOT in range):
    # - SHORT: modify if new limit price is reduced (new_price < existing_price)
    # - LONG:  modify if new limit price is increased (new_price > existing_price)
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


def calculate_min_btc_quantity(
    inr_balance: float,
    conversion_rate: float,
    limit_price: float,
    leverage: int = LEVERAGE,
) -> float:
    """Uses the fixed 0.001 BTC minimum lot and verifies its required margin."""
    qty = MIN_BTC_QUANTITY

    # Verify required INR margin is available in wallet
    required_usdt_margin = (float(qty) * limit_price) / leverage
    required_inr_margin = required_usdt_margin * conversion_rate

    print(
        f"[*] Sizing: Qty={qty} BTC | Est. Margin Required=₹{required_inr_margin:.2f} "
        f"(Available: ₹{inr_balance:.2f})"
    )

    if inr_balance < required_inr_margin:
        raise ValueError(
            f"Insufficient INR balance (₹{inr_balance:.2f}) for minimum BTC order "
            f"(requires ~₹{required_inr_margin:.2f} at {leverage}x)."
        )

    return float(qty)


def place_btc_limit_order(signal_data: Dict[str, Any]) -> Dict[str, Any]:
    direction = signal_data["direction"]
    limit_price = signal_data["entry"]
    tp_price = signal_data["tp1"]
    side = "buy" if direction == "LONG" else "sell"

    inr_balance = get_futures_inr_balance()
    conversion_rate = get_inr_usdt_conversion()
    quantity = calculate_min_btc_quantity(
        inr_balance=inr_balance,
        conversion_rate=conversion_rate,
        limit_price=limit_price,
        leverage=LEVERAGE,
    )

    # Note: stop_loss_price intentionally omitted per instructions
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
# 4. MAIN EXECUTION FLOW
# ==========================================================
def validate_config() -> None:
    if not DRY_RUN and not SSH_STRICT_HOST_KEY_CHECKING:
        raise RuntimeError(
            "Live trading requires SSH host-key verification. Set "
            "SSH_STRICT_HOST_KEY_CHECKING=true and configure the verified OCI host key."
        )

    required = {
        "TG_API_ID": TG_API_ID,
        "TG_API_HASH": TG_API_HASH,
        "TG_TARGET_CHAT": TG_TARGET_CHAT,
        "OCI_VM_HOST": VM_HOST,
        "OCI_VM_USER": VM_USER,
        "OCI_SSH_PRIVATE_KEY_B64": SSH_KEY_BASE64,
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
    if SSH_STRICT_HOST_KEY_CHECKING and not os.path.isfile(SSH_KNOWN_HOSTS_FILE):
        raise RuntimeError(
            f"SSH known-hosts file not found: {SSH_KNOWN_HOSTS_FILE}. "
            "Add the VM host key before connecting."
        )


def run_auto_trader():
    validate_config()
    print("=== BTC AUTO-TRADER STARTED ===")
    if DRY_RUN:
        print("[*] DRY_RUN is enabled; orders will not be placed or canceled.")
    if not SSH_STRICT_HOST_KEY_CHECKING:
        print("[!] SSH host-key verification is disabled; use only for a non-trading connectivity test.")

    # Step 1: Fetch latest signal from Telegram
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

    # Step 2: Skip if this exact Telegram message was already processed
    last_id = get_last_processed_id()
    if last_id == signal["message_id"]:
        print(f"[*] Signal Msg ID {last_id} was already processed earlier. Ignoring.")
        return

    # Step 3: Open SSH Tunnel and evaluate CoinDCX state
    with OracleSshTunnel():
        exit_ip = verify_exit_ip()
        print(f"[+] Verified Outbound IP via Oracle VM: {exit_ip}")

        if DRY_RUN:
            print("[*] Dry run: SSH tunnel test complete; CoinDCX was not contacted.")
            return

        action, reason, existing_order = evaluate_btc_exposure(signal)

        if action == "SKIP":
            print(f"[*] Skipping signal -> {reason}")
            save_last_processed_id(signal["message_id"])
            return

        if action == "MODIFY" and existing_order:
            print(f"[*] Modifying order -> {reason}")
            cancel_order_by_id(existing_order["id"])
            time.sleep(0.5)  # Brief pause so margin unlocks cleanly before re-placing

        # Step 4: Place Limit Order with TP (no SL)
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