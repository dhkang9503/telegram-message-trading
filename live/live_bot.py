from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
from dotenv import load_dotenv
from telethon import TelegramClient, events

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Telegram / llama.cpp
SESSION_NAME = os.getenv("TELEGRAM_SESSION_NAME", "telegram_session")
CHANNEL_ID = int(os.getenv("TELEGRAM_CHANNEL_ID", "-1002170337088"))
POST_AUTHOR_FILTER = (os.getenv("TELEGRAM_POST_AUTHOR") or "").strip()
LLAMA_URL = os.getenv("LLAMA_URL", "http://127.0.0.1:8080/completion")
LLAMA_TIMEOUT_SECONDS = float(os.getenv("LLAMA_TIMEOUT_SECONDS", "30"))
RECONCILE_INTERVAL_SECONDS = float(os.getenv("RECONCILE_INTERVAL_SECONDS", "2"))

# Fixed trading rules
BITGET_BASE_URL = os.getenv("BITGET_BASE_URL", "https://api.bitget.com").rstrip("/")
SYMBOL = "BTCUSDT"
PRODUCT_TYPE = "USDT-FUTURES"
MARGIN_COIN = "USDT"
MARGIN_MODE = "crossed"
POSITION_MODE = "one_way_mode"
LEVERAGE = Decimal("98")
INITIAL_MARGIN_EQUITY_RATIO = Decimal("0.0125")
PRICE_RESTORE_MAX_GAP_RATIO = Decimal(os.getenv("PRICE_RESTORE_MAX_GAP_RATIO", "0.03"))
FAILED_STOP_CORRECTION_TTL_SECONDS = int(os.getenv("FAILED_STOP_CORRECTION_TTL_SECONDS", "300"))
FAILED_STOP_CORRECTION_MAX_MESSAGE_GAP = int(os.getenv("FAILED_STOP_CORRECTION_MAX_MESSAGE_GAP", "3"))
STOP_CORRECTION_CUE_RE = re.compile(r"(?:죄송|잘못|정정|오타|실수|아니다|아니고)")
STOP_CORRECTION_NUMBER_RE = re.compile(r"(?<!\d)(\d{1,4})(?!\d)")
SOURCE_PRICE_TOKEN_RE = re.compile(r"(?<![\d,])(\d[\d,]*(?:\.\d+)?)(?![\d,]|\.\d)")

STATE_DIR = BASE_DIR / "state"
LOG_DIR = BASE_DIR / "logs"
STATE_PATH = STATE_DIR / "current_state.json"
TRADE_HISTORY_PATH = LOG_DIR / "trade_history.jsonl"
EVENT_LOG_PATH = LOG_DIR / "live_events.jsonl"
STATE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

SYSTEM_PROMPT = (
    "너는 특정 BTCUSDT 리딩 채널의 한국어 메시지를 구조화된 거래 액션 JSON으로 "
    "변환하는 파서다. 현재 즉시 실행할 확정 지시만 추출하고 추측하지 않는다. "
    "'하실 분들', '원하시면', '못 하신 분들', '대응 안 되시는 분들', "
    "'평단 좋으신 분들'처럼 수신자의 선택이나 상태에 따라 적용 여부가 달라지면 "
    "명령형이어도 actions는 빈 배열이다. 조건·가정·권고·미래 계획·개인 행동만 "
    "나타내는 문장도 actions는 빈 배열이다. 대상 제한 없이 지금 실행하라는 행동이 "
    "명확한 경우에만 액션을 추출한다. 채널 표현 '비트 자유롭게'는 CLOSE_ALL이다. "
    "애매하면 actions는 빈 배열이다. 출력은 actions 배열을 가진 JSON 하나만 반환한다."
)
ALLOWED_ACTIONS = {
    "OPEN_LONG", "OPEN_SHORT", "OPEN_REENTRY", "ADD", "SET_STOP", "SET_TP",
    "CLOSE_HALF", "CLOSE_ADDS", "CLOSE_ALL", "CANCEL_ADD", "CANCEL_STOP",
}
TRADE_LOCK = asyncio.Lock()
INFERENCE_LOCK = asyncio.Lock()


class BotError(RuntimeError):
    pass


class BitgetAPIError(BotError):
    def __init__(self, code: str, message: str, payload: Any = None):
        super().__init__(f"Bitget API error {code}: {message}")
        self.code = code
        self.payload = payload


def prioritize_cancel_actions(
    actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Move cancellation actions ahead without changing either group's order."""
    if len(actions) < 2:
        return list(actions)
    return sorted(
        actions,
        key=lambda action: not str(action.get("type", "")).startswith("CANCEL_"),
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable '{name}' is required.")
    return value


def D(value: Any, default: str = "0") -> Decimal:
    return Decimal(default) if value is None or value == "" else Decimal(str(value))


def initial_margin_from_account(
    account: dict[str, Any],
) -> tuple[Decimal, Decimal]:
    raw_equity = account.get("usdtEquity")
    try:
        equity = D(raw_equity)
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise BotError(
            f"Invalid account usdtEquity for initial sizing: {raw_equity!r}"
        ) from exc
    if equity <= 0:
        raise BotError(
            f"Account usdtEquity must be positive for initial sizing: {raw_equity!r}"
        )
    return equity, equity * INITIAL_MARGIN_EQUITY_RATIO


def ds(value: Decimal) -> str:
    return format(value.normalize(), "f")


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def append_jsonl(path: Path, data: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def empty_position() -> dict[str, Any]:
    return {
        "side": None,
        "initial_qty": "0",
        "added_qty": "0",
        "total_qty": "0",
        "margin_size_usdt": "0",
        "entry_price": None,
        "break_even_price": None,
        "opened_at": None,
        "updated_at": now_iso(),
    }


def default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "position": empty_position(),
        "pending": {
            "entry_order": None,
            "add_orders": [],
            "stop_order": None,
            "tp_order": None,
            "failed_stop": None,
        },
        "stopped_position": None,
        "processed_message_ids": [],
        "last_telegram_message_id": None,
        "updated_at": now_iso(),
    }


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))
            if self.data.get("version") != 1:
                raise BotError(f"Unsupported state version: {self.data.get('version')}")
            self._ensure_schema()
            self.save()
        else:
            self.data = default_state()
            self.save()

    def _ensure_schema(self) -> None:
        """Add new optional fields without breaking an existing version-1 state file."""
        self.data.setdefault("stopped_position", None)
        position = self.data.setdefault("position", empty_position())
        position.setdefault("margin_size_usdt", "0")
        pending = self.data.setdefault("pending", {})
        pending.setdefault("entry_order", None)
        pending.setdefault("add_orders", [])
        pending.setdefault("stop_order", None)
        pending.setdefault("tp_order", None)
        pending.setdefault("failed_stop", None)
        self.data.setdefault("processed_message_ids", [])
        self.data.setdefault("last_telegram_message_id", None)

    def save(self) -> None:
        self.data["updated_at"] = now_iso()
        atomic_json(self.path, self.data)

    def is_processed(self, message_id: int) -> bool:
        return message_id in self.data["processed_message_ids"]

    def claim_message(self, message_id: int) -> None:
        ids = self.data["processed_message_ids"]
        if message_id not in ids:
            ids.append(message_id)
        self.data["processed_message_ids"] = ids[-1000:]
        self.data["last_telegram_message_id"] = message_id
        self.save()


@dataclass(frozen=True)
class ContractConfig:
    min_trade_num: Decimal
    min_trade_usdt: Decimal
    size_step: Decimal
    price_step: Decimal
    max_leverage: Decimal

    def floor_size(self, value: Decimal) -> Decimal:
        units = (value / self.size_step).to_integral_value(rounding=ROUND_DOWN)
        return max(Decimal("0"), units * self.size_step)

    def floor_price(self, value: Decimal) -> Decimal:
        if value <= 0:
            raise BotError(f"Invalid price: {value}")
        units = (value / self.price_step).to_integral_value(rounding=ROUND_DOWN)
        return units * self.price_step


class BitgetClient:
    def __init__(self):
        self.api_key = require_env("BITGET_API_KEY")
        self.api_secret = require_env("BITGET_API_SECRET")
        self.passphrase = require_env("BITGET_API_PASSPHRASE")
        self.http = httpx.AsyncClient(base_url=BITGET_BASE_URL, timeout=15.0)

    async def close(self) -> None:
        await self.http.aclose()

    def _signature(self, ts: str, method: str, path: str, query: str, body: str) -> str:
        prehash = ts + method.upper() + path + (("?" + query) if query else "") + body
        digest = hmac.new(self.api_secret.encode(), prehash.encode(), hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        body: Optional[dict[str, Any]] = None,
        private: bool = True,
    ) -> Any:
        params = params or {}
        query = urlencode([(k, str(v)) for k, v in params.items() if v is not None])
        body_text = "" if method.upper() == "GET" else json.dumps(body or {}, separators=(",", ":"), ensure_ascii=False)
        headers = {"Content-Type": "application/json", "locale": "en-US"}
        if private:
            ts = str(int(time.time() * 1000))
            headers.update({
                "ACCESS-KEY": self.api_key,
                "ACCESS-SIGN": self._signature(ts, method, path, query, body_text),
                "ACCESS-PASSPHRASE": self.passphrase,
                "ACCESS-TIMESTAMP": ts,
            })
        response = await self.http.request(
            method,
            path,
            params=params or None,
            content=body_text.encode() if body_text else None,
            headers=headers,
        )
        try:
            payload = response.json()
        except ValueError:
            response.raise_for_status()
            raise BotError(
                f"Bitget returned non-JSON response: HTTP {response.status_code}"
            )
        if not isinstance(payload, dict):
            response.raise_for_status()
            raise BotError(f"Bitget returned unexpected payload: {payload!r}")
        if response.is_error or str(payload.get("code")) != "00000":
            code = str(payload.get("code") or f"HTTP_{response.status_code}")
            message = str(payload.get("msg") or response.reason_phrase)
            raise BitgetAPIError(code, message, payload)
        return payload.get("data")

    async def contract_config(self) -> ContractConfig:
        rows = await self.request(
            "GET", "/api/v2/mix/market/contracts",
            params={"productType": PRODUCT_TYPE, "symbol": SYMBOL}, private=False,
        )
        if not rows:
            raise BotError("BTC contract config is empty")
        row = rows[0]
        price_step = D(row["priceEndStep"]) * (Decimal("10") ** -int(row["pricePlace"]))
        return ContractConfig(
            min_trade_num=D(row["minTradeNum"]),
            min_trade_usdt=D(row["minTradeUSDT"]),
            size_step=D(row["sizeMultiplier"]),
            price_step=price_step,
            max_leverage=D(row["maxLever"]),
        )

    async def ticker(self) -> dict[str, Any]:
        rows = await self.request(
            "GET", "/api/v2/mix/market/ticker",
            params={"productType": PRODUCT_TYPE, "symbol": SYMBOL}, private=False,
        )
        if not rows:
            raise BotError("BTC ticker is empty")
        return rows[0]

    async def account(self) -> dict[str, Any]:
        return await self.request(
            "GET", "/api/v2/mix/account/account",
            params={"symbol": SYMBOL, "productType": PRODUCT_TYPE, "marginCoin": MARGIN_COIN},
        )

    async def position(self) -> Optional[dict[str, Any]]:
        rows = await self.request(
            "GET", "/api/v2/mix/position/single-position",
            params={"symbol": SYMBOL, "productType": PRODUCT_TYPE, "marginCoin": MARGIN_COIN},
        )
        active = [row for row in rows if D(row.get("total")) > 0]
        if len(active) > 1:
            raise BotError(f"Expected at most one BTC position, got {len(active)}")
        return active[0] if active else None

    async def pending_orders(self) -> list[dict[str, Any]]:
        data = await self.request(
            "GET", "/api/v2/mix/order/orders-pending",
            params={"symbol": SYMBOL, "productType": PRODUCT_TYPE, "limit": "100"},
        )
        return list(((data or {}).get("entrustedList") or []))

    async def pending_plans(self) -> list[dict[str, Any]]:
        data = await self.request(
            "GET", "/api/v2/mix/order/orders-plan-pending",
            params={"symbol": SYMBOL, "planType": "profit_loss", "productType": PRODUCT_TYPE, "limit": "100"},
        )
        return list(((data or {}).get("entrustedList") or []))

    async def plan_history(self, order_id: str) -> list[dict[str, Any]]:
        data = await self.request(
            "GET", "/api/v2/mix/order/orders-plan-history",
            params={
                "orderId": order_id,
                "planType": "profit_loss",
                "symbol": SYMBOL,
                "productType": PRODUCT_TYPE,
                "limit": "100",
            },
        )
        return list(((data or {}).get("entrustedList") or []))

    async def set_position_mode(self) -> Any:
        return await self.request(
            "POST", "/api/v2/mix/account/set-position-mode",
            body={"productType": PRODUCT_TYPE, "posMode": POSITION_MODE},
        )

    async def set_margin_mode(self) -> Any:
        return await self.request(
            "POST", "/api/v2/mix/account/set-margin-mode",
            body={"symbol": SYMBOL, "productType": PRODUCT_TYPE, "marginCoin": MARGIN_COIN, "marginMode": MARGIN_MODE},
        )

    async def set_leverage(self) -> Any:
        return await self.request(
            "POST", "/api/v2/mix/account/set-leverage",
            body={"symbol": SYMBOL, "productType": PRODUCT_TYPE, "marginCoin": MARGIN_COIN, "leverage": ds(LEVERAGE)},
        )

    async def place_order(
        self,
        *,
        side: str,
        size: Decimal,
        order_type: str,
        client_oid: str,
        price: Optional[Decimal] = None,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "symbol": SYMBOL,
            "productType": PRODUCT_TYPE,
            "marginMode": MARGIN_MODE,
            "marginCoin": MARGIN_COIN,
            "size": ds(size),
            "side": side,
            "orderType": order_type,
            "clientOid": client_oid,
            "reduceOnly": "YES" if reduce_only else "NO",
        }
        if order_type == "limit":
            if price is None:
                raise BotError("Limit order requires a price")
            body.update({"price": ds(price), "force": "gtc"})
        return await self.request("POST", "/api/v2/mix/order/place-order", body=body)

    async def cancel_order(self, order_id: str) -> Any:
        return await self.request(
            "POST", "/api/v2/mix/order/cancel-order",
            body={"symbol": SYMBOL, "productType": PRODUCT_TYPE, "marginCoin": MARGIN_COIN, "orderId": order_id},
        )

    async def flash_close(self) -> Any:
        return await self.request(
            "POST", "/api/v2/mix/order/close-positions",
            body={"symbol": SYMBOL, "productType": PRODUCT_TYPE},
        )

    async def place_plan(self, *, plan_type: str, hold_side: str, trigger_price: Decimal, client_oid: str) -> dict[str, Any]:
        return await self.request(
            "POST", "/api/v2/mix/order/place-tpsl-order",
            body={
                "marginCoin": MARGIN_COIN,
                "productType": PRODUCT_TYPE,
                "symbol": SYMBOL,
                "planType": plan_type,
                "triggerPrice": ds(trigger_price),
                "triggerType": "mark_price",
                "executePrice": "0",
                "holdSide": hold_side,
                "clientOid": client_oid,
            },
        )

    async def cancel_plan(self, order_id: str, plan_type: str) -> Any:
        return await self.request(
            "POST", "/api/v2/mix/order/cancel-plan-order",
            body={
                "orderIdList": [{"orderId": order_id, "clientOid": ""}],
                "symbol": SYMBOL,
                "productType": PRODUCT_TYPE,
                "marginCoin": MARGIN_COIN,
                "planType": plan_type,
            },
        )


def normalize_message(message: Any) -> str:
    text = (message.text or "").replace("\r", " ").replace("\n", " ").strip()
    return f"{text}" if text and message.photo is not None else text


def normalize_trading_shorthand(text: str) -> str:
    replacements = {
        "물ㅂㅈ": "물 ㅂㅈ",
    }
    return replacements.get(text, text)


def author_matches(post_author: Optional[str]) -> bool:
    if not POST_AUTHOR_FILTER:
        return True
    return bool(post_author and post_author.casefold() == POST_AUTHOR_FILTER.casefold())


def prompt_for(text: str) -> str:
    return (
        "<|im_start|>system\n" + SYSTEM_PROMPT + "<|im_end|>\n"
        "<|im_start|>user\n" + text + "<|im_end|>\n"
        "<|im_start|>assistant\n<think>\n\n</think>\n\n"
    )


def parse_actions(raw: str) -> list[dict[str, Any]]:
    cleaned = raw.replace("[end of text]", "").strip()
    match = re.search(r'\{\s*"actions"\s*:\s*\[.*?\]\s*\}', cleaned, re.DOTALL)
    if not match:
        raise BotError(f"actions JSON not found: {cleaned!r}")
    obj = json.loads(match.group(0))
    if set(obj.keys()) != {"actions"} or not isinstance(obj["actions"], list):
        raise BotError(f"Invalid model JSON: {obj!r}")
    out = []
    for action in obj["actions"]:
        if not isinstance(action, dict):
            raise BotError(f"Invalid action object: {action!r}")
        action_type = action.get("type")
        if action_type not in ALLOWED_ACTIONS:
            raise BotError(f"Invalid action type: {action_type}")
        keys = set(action)
        if action_type == "OPEN_REENTRY":
            if keys not in ({"type", "price"}, {"type", "price", "side"}):
                raise BotError(f"Invalid OPEN_REENTRY object: {action!r}")
            side = action.get("side")
            if side not in {None, "long", "short"}:
                raise BotError(f"Invalid OPEN_REENTRY side: {side!r}")
        else:
            if keys != {"type", "price"}:
                raise BotError(f"Invalid action object: {action!r}")
            side = None
        if action.get("price") is not None and not isinstance(action["price"], (int, float)):
            raise BotError(f"Invalid price: {action['price']!r}")
        normalized = {"type": action_type, "price": action.get("price")}
        if action_type == "OPEN_REENTRY":
            normalized["side"] = side
        out.append(normalized)
    return out


async def infer(http: httpx.AsyncClient, text: str) -> dict[str, Any]:
    payload = {
        "prompt": prompt_for(text),
        "n_predict": 48,
        "temperature": 0.0,
        "top_k": 1,
        "top_p": 1.0,
        "repeat_penalty": 1.0,
        "stop": ["<|im_end|>", "<|endoftext|>"],
        "stream": False,
    }
    async with INFERENCE_LOCK:
        start = time.perf_counter()
        response = await http.post(LLAMA_URL, json=payload)
        elapsed = time.perf_counter() - start
    response.raise_for_status()
    raw = str(response.json().get("content", "")).strip()
    return {"raw_output": raw, "actions": parse_actions(raw), "inference_seconds": elapsed}


def extract_source_price_tokens(text: str) -> list[str]:
    """Return numeric source tokens without discarding leading zeroes."""
    return [
        match.group(1).replace(",", "")
        for match in SOURCE_PRICE_TOKEN_RE.finditer(text)
    ]


def _has_significant_leading_zero(token: str) -> bool:
    integer_part = token.split(".", 1)[0]
    return len(integer_part) > 1 and integer_part.startswith("0")


def align_action_prices_to_source(
    source_text: str,
    actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Preserve source token width for uniquely matched priced actions."""
    available_tokens = extract_source_price_tokens(source_text)
    aligned = [dict(action) for action in actions]

    for action in aligned:
        price = action.get("price")
        if price is None:
            continue
        price_value = D(price)
        matches = [
            (index, token)
            for index, token in enumerate(available_tokens)
            if D(token) == price_value
        ]
        if len(matches) != 1:
            raise BotError(
                "Could not uniquely align action price to source text: "
                f"action={action!r}, source={source_text!r}, matches={matches!r}"
            )
        index, token = matches[0]
        del available_tokens[index]
        if _has_significant_leading_zero(token):
            action["price"] = token
    return aligned


def restore_btc_price(raw_price: Any, reference: Decimal, config: ContractConfig) -> Decimal:
    raw = D(raw_price)
    if isinstance(raw_price, str):
        raw_text = raw_price.strip().replace(",", "")
        parts = raw_text.split(".")
        if (
            len(parts) > 2
            or not parts[0].isdigit()
            or (len(parts) == 2 and not parts[1].isdigit())
        ):
            raise BotError(f"Invalid price token: {raw_price!r}")
        integer_text = parts[0]
    else:
        integer_text = str(abs(int(raw)))

    zero_fragment = (
        isinstance(raw_price, str)
        and raw == 0
        and len(integer_text) > 1
        and set(integer_text) == {"0"}
    )
    if raw < 0 or (raw == 0 and not zero_fragment):
        raise BotError(f"Price must be positive: {raw}")
    if raw >= 10000:
        restored = raw
    else:
        digits = len(integer_text)
        modulus = Decimal(10) ** digits
        base = (reference // modulus) * modulus
        candidates = [
            price
            for price in (
                base + raw - modulus,
                base + raw,
                base + raw + modulus,
            )
            if price > 0
        ]
        restored = min(candidates, key=lambda price: abs(price - reference))
    gap = abs(restored - reference) / reference
    if gap > PRICE_RESTORE_MAX_GAP_RATIO:
        raise BotError(
            f"Restored price too far from market: raw={raw}, restored={restored}, reference={reference}, gap={gap:.2%}"
        )
    return config.floor_price(restored)


def stop_action_validation_error(
    action: dict[str, Any],
    reference: Decimal,
    position_side: str,
    config: ContractConfig,
) -> Optional[str]:
    """Return a rejection reason when a SET_STOP cannot safely execute."""
    if action.get("type") != "SET_STOP":
        return None
    raw_price = action.get("price")
    if raw_price is None:
        return "SET_STOP requires an explicit price"
    try:
        trigger = restore_btc_price(raw_price, reference, config)
    except BotError as exc:
        return str(exc)
    if position_side == "long":
        if trigger >= reference:
            return f"Long stop {trigger} must be below mark price {reference}"
    elif position_side == "short":
        if trigger <= reference:
            return f"Short stop {trigger} must be above mark price {reference}"
    else:
        return f"Invalid position side for stop validation: {position_side}"
    return None


def validate_and_deduplicate_stop_actions(
    actions: list[dict[str, Any]],
    reference: Decimal,
    position_side: str,
    config: ContractConfig,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Validate every SET_STOP, then keep only the last valid one."""
    validated: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for action in actions:
        error = stop_action_validation_error(
            action, reference, position_side, config
        )
        if error is not None:
            rejected.append({"action": action, "reason": error})
        else:
            validated.append(action)

    last_stop_index = next(
        (
            index
            for index in range(len(validated) - 1, -1, -1)
            if validated[index].get("type") == "SET_STOP"
        ),
        None,
    )
    normalized: list[dict[str, Any]] = []
    deduplicated: list[dict[str, Any]] = []
    for index, action in enumerate(validated):
        if action.get("type") == "SET_STOP" and index != last_stop_index:
            deduplicated.append({
                "action": action,
                "reason": "superseded_by_later_valid_set_stop",
            })
        else:
            normalized.append(action)
    return normalized, rejected, deduplicated


def extract_stop_correction_fragment(text: str) -> Optional[str]:
    if not STOP_CORRECTION_CUE_RE.search(text):
        return None
    tokens = STOP_CORRECTION_NUMBER_RE.findall(text.replace(",", ""))
    return tokens[0] if len(tokens) == 1 else None


def build_stop_prefix_correction(
    failed_price: Decimal,
    fragment: str,
    reference: Decimal,
    position_side: str,
    config: ContractConfig,
) -> Decimal:
    failed_integer = failed_price.to_integral_value()
    if failed_price != failed_integer:
        raise BotError(f"Failed stop price is not an integer: {failed_price}")
    failed_digits = str(abs(int(failed_integer)))
    if not fragment.isdigit() or len(fragment) >= len(failed_digits):
        raise BotError(
            f"Invalid stop correction fragment: fragment={fragment!r}, failed={failed_price}"
        )
    candidate = config.floor_price(
        Decimal(int(fragment + failed_digits[len(fragment):]))
    )
    if candidate <= 0 or candidate == failed_price:
        raise BotError(
            f"Stop correction did not produce a new positive price: {candidate}"
        )
    gap = abs(candidate - reference) / reference
    if gap > PRICE_RESTORE_MAX_GAP_RATIO:
        raise BotError(
            f"Corrected stop too far from market: candidate={candidate}, "
            f"reference={reference}, gap={gap:.2%}"
        )
    if abs(candidate - reference) >= abs(failed_price - reference):
        raise BotError(
            f"Corrected stop is not closer to market: failed={failed_price}, "
            f"candidate={candidate}, reference={reference}"
        )
    if position_side == "long" and candidate >= reference:
        raise BotError(
            f"Long stop correction must be below market: {candidate} >= {reference}"
        )
    if position_side == "short" and candidate <= reference:
        raise BotError(
            f"Short stop correction must be above market: {candidate} <= {reference}"
        )
    if position_side not in {"long", "short"}:
        raise BotError(f"Invalid position side for stop correction: {position_side}")
    return candidate


def client_oid(message_id: int, index: int, kind: str) -> str:
    return f"tg{message_id}-{index}-{kind}-{uuid.uuid4().hex[:5]}"[:40]


class TradingEngine:
    def __init__(self, api: BitgetClient, state: StateStore, config: ContractConfig):
        self.api = api
        self.state = state
        self.config = config

    def log(self, event: str, **data: Any) -> None:
        append_jsonl(TRADE_HISTORY_PATH, {"timestamp": now_iso(), "event": event, **data})

    async def setup(self) -> None:
        if self.config.max_leverage < LEVERAGE:
            raise BotError(f"BTC max leverage {self.config.max_leverage} is below 98")
        position = await self.api.position()
        pending = await self.api.pending_orders()
        account = await self.api.account()
        if account.get("posMode") != POSITION_MODE:
            if position or pending:
                raise BotError("Cannot switch to one-way mode while a position/order exists")
            await self.api.set_position_mode()
        account = await self.api.account()
        if account.get("marginMode") != MARGIN_MODE:
            if position or pending:
                raise BotError("Cannot switch to crossed margin while a position/order exists")
            await self.api.set_margin_mode()
        await self.api.set_leverage()
        account = await self.api.account()
        if account.get("posMode") != POSITION_MODE:
            raise BotError(f"Position mode verification failed: {account.get('posMode')}")
        if account.get("marginMode") != MARGIN_MODE:
            raise BotError(f"Margin mode verification failed: {account.get('marginMode')}")
        if D(account.get("crossedMarginLeverage")) != LEVERAGE:
            raise BotError(f"Leverage verification failed: {account.get('crossedMarginLeverage')}")
        await self.reconcile()
        self.log("ACCOUNT_READY", usdt_equity=account.get("usdtEquity"), leverage="98")

    async def reconcile(self) -> None:
        actual = await self.api.position()
        orders = await self.api.pending_orders()
        plans = await self.api.pending_plans()
        previous_position = self.state.data["position"].copy()
        previous_stop = self.state.data["pending"].get("stop_order")
        previous_stop = previous_stop.copy() if previous_stop else None
        became_flat = actual is None and D(previous_position.get("total_qty")) > 0

        # If a stop/TP/manual close flattened the position, cancel all bot opening
        # orders before they can unexpectedly reopen it.
        if became_flat:
            for row in orders:
                if str(row.get("clientOid", "")).startswith("tg"):
                    await self.api.cancel_order(str(row["orderId"]))
            orders = await self.api.pending_orders()

            executed_stop = await self._find_executed_stop(previous_stop)
            if executed_stop:
                self._remember_stopped_position(previous_position, previous_stop, executed_stop)
            else:
                self._clear_reentry("position_flattened_without_executed_stop")

        self._reconcile_orders(orders)
        self._reconcile_plans(plans)
        self._reconcile_position(actual)
        self.state.save()

    async def _find_executed_stop(
        self, stop: Optional[dict[str, Any]], attempts: int = 5,
    ) -> Optional[dict[str, Any]]:
        if not stop or not stop.get("order_id"):
            return None
        order_id = str(stop["order_id"])
        for attempt in range(attempts):
            try:
                rows = await self.api.plan_history(order_id)
            except Exception as exc:
                self.log(
                    "STOP_HISTORY_LOOKUP_FAILED",
                    order_id=order_id,
                    attempt=attempt + 1,
                    error=f"{type(exc).__name__}: {exc}",
                )
                rows = []
            for row in rows:
                if (
                    str(row.get("orderId")) == order_id
                    and str(row.get("planStatus")) == "executed"
                    and str(row.get("planType")) in {"loss_plan", "pos_loss"}
                ):
                    return row
            if attempt + 1 < attempts:
                await asyncio.sleep(0.35)
        return None

    def _remember_stopped_position(
        self,
        previous_position: dict[str, Any],
        previous_stop: Optional[dict[str, Any]],
        executed_stop: dict[str, Any],
    ) -> None:
        margin = D(previous_position.get("margin_size_usdt"))
        if margin <= 0:
            total_qty = D(previous_position.get("total_qty"))
            entry_price = D(previous_position.get("entry_price"))
            if total_qty > 0 and entry_price > 0:
                margin = total_qty * entry_price / LEVERAGE
        if margin <= 0:
            self.log(
                "STOP_EXECUTED_WITHOUT_MARGIN_SNAPSHOT",
                previous_position=previous_position,
                stop_order=previous_stop,
                stop_history=executed_stop,
            )
            self._clear_reentry("executed_stop_without_valid_margin")
            return

        stopped_at = now_iso()
        if executed_stop.get("uTime"):
            try:
                stopped_at = datetime.fromtimestamp(
                    int(executed_stop["uTime"]) / 1000, tz=timezone.utc,
                ).isoformat()
            except (TypeError, ValueError):
                pass
        record = {
            "side": previous_position.get("side"),
            "total_margin_usdt": ds(margin),
            "total_qty": str(previous_position.get("total_qty") or "0"),
            "entry_price": previous_position.get("entry_price"),
            "stop_order_id": str((previous_stop or {}).get("order_id") or ""),
            "stop_client_oid": str((previous_stop or {}).get("client_oid") or ""),
            "stop_trigger_price": (previous_stop or {}).get("price"),
            "stop_execute_order_id": str(executed_stop.get("executeOrderId") or ""),
            "stopped_at": stopped_at,
            "available_for_reentry": True,
            "consumed_at": None,
            "consumed_by_message_id": None,
            "consumed_reason": None,
        }
        self.state.data["stopped_position"] = record
        self.log("STOP_LOSS_POSITION_SAVED", stopped_position=record.copy())

    def _clear_reentry(self, reason: str) -> None:
        stopped = self.state.data.get("stopped_position")
        if stopped and stopped.get("available_for_reentry"):
            stopped["available_for_reentry"] = False
            stopped["consumed_at"] = now_iso()
            stopped["consumed_reason"] = reason
            self.log("STOP_REENTRY_INVALIDATED", reason=reason, stopped_position=stopped.copy())

    def _consume_reentry(self, message_id: int, reason: str) -> None:
        stopped = self.state.data.get("stopped_position")
        if not stopped or not stopped.get("available_for_reentry"):
            return
        stopped["available_for_reentry"] = False
        stopped["consumed_at"] = now_iso()
        stopped["consumed_by_message_id"] = message_id
        stopped["consumed_reason"] = reason
        self.state.save()
        self.log("STOP_REENTRY_CONSUMED", reason=reason, stopped_position=stopped.copy())

    def _reconcile_orders(self, rows: list[dict[str, Any]]) -> None:
        unknown = [r for r in rows if not str(r.get("clientOid", "")).startswith("tg")]
        if unknown:
            raise BotError("Untracked pending BTC order exists on Bitget; cancel it manually")
        live_ids = {str(r.get("orderId")) for r in rows}
        pending = self.state.data["pending"]
        if pending["entry_order"] and str(pending["entry_order"]["order_id"]) not in live_ids:
            pending["entry_order"] = None
        pending["add_orders"] = [x for x in pending["add_orders"] if str(x["order_id"]) in live_ids]
        for row in rows:
            oid = str(row["orderId"])
            coid = str(row.get("clientOid", ""))
            record = {
                "order_id": oid,
                "client_oid": coid,
                "qty": str(row.get("size", "0")),
                "price": str(row.get("price", "0")),
                "created_at": now_iso(),
            }
            if "-entry-" in coid and pending["entry_order"] is None:
                pending["entry_order"] = record
            elif "-add-" in coid and not any(str(x["order_id"]) == oid for x in pending["add_orders"]):
                pending["add_orders"].append(record)

    def _reconcile_plans(self, rows: list[dict[str, Any]]) -> None:
        stops = [r for r in rows if str(r.get("planType")) in {"loss_plan", "pos_loss"}]
        tps = [r for r in rows if str(r.get("planType")) in {"profit_plan", "pos_profit"}]
        if len(stops) > 1 or len(tps) > 1:
            raise BotError("Multiple BTC stop-loss or take-profit plans exist")
        pending = self.state.data["pending"]
        pending["stop_order"] = self._plan_record(stops[0]) if stops else None
        pending["tp_order"] = self._plan_record(tps[0]) if tps else None

    @staticmethod
    def _plan_record(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "order_id": str(row.get("orderId")),
            "client_oid": str(row.get("clientOid", "")),
            "price": str(row.get("triggerPrice", "")),
            "plan_type": str(row.get("planType")),
            "created_at": now_iso(),
        }

    def _reconcile_position(self, actual: Optional[dict[str, Any]]) -> None:
        old = self.state.data["position"]
        tracked = D(old.get("total_qty"))
        initial = D(old.get("initial_qty"))
        added = D(old.get("added_qty"))
        if actual is None:
            if tracked > 0:
                self.log("POSITION_BECAME_FLAT", previous=old.copy())
            self.state.data["position"] = empty_position()
            self.state.data["pending"]["stop_order"] = None
            self.state.data["pending"]["tp_order"] = None
            self.state.data["pending"]["failed_stop"] = None
            return
        total = D(actual.get("total"))
        side = str(actual.get("holdSide"))
        if old.get("side") and old["side"] != side:
            raise BotError(f"Position side mismatch: state={old['side']} Bitget={side}")
        if tracked == 0:
            initial, added = total, Decimal("0")
            self.log("POSITION_ADOPTED", side=side, qty=ds(total))
        elif total > tracked:
            delta = total - tracked
            if initial == 0:
                initial += delta
            else:
                added += delta
            self.log("POSITION_INCREASE_RECONCILED", delta=ds(delta))
        elif total < tracked:
            reduction = tracked - total
            from_added = min(added, reduction)
            added -= from_added
            initial = max(Decimal("0"), initial - (reduction - from_added))
            self.log("POSITION_REDUCTION_RECONCILED", actual_total=ds(total))
        if initial + added != total:
            added = max(Decimal("0"), total - initial)
        opened_at = old.get("opened_at")
        if not opened_at and actual.get("cTime"):
            opened_at = datetime.fromtimestamp(int(actual["cTime"]) / 1000, tz=timezone.utc).isoformat()
        self.state.data["position"] = {
            "side": side,
            "initial_qty": ds(initial),
            "added_qty": ds(added),
            "total_qty": ds(total),
            "margin_size_usdt": ds(D(actual.get("marginSize"))),
            "entry_price": actual.get("openPriceAvg"),
            "break_even_price": actual.get("breakEvenPrice"),
            "opened_at": opened_at,
            "updated_at": now_iso(),
        }

    async def reference_price(self) -> Decimal:
        ticker = await self.api.ticker()
        price = D(ticker.get("markPrice") or ticker.get("lastPr"))
        if price <= 0:
            raise BotError(f"Bad ticker: {ticker}")
        return price

    def require_position(self) -> dict[str, Any]:
        pos = self.state.data["position"]
        if D(pos.get("total_qty")) <= 0 or pos.get("side") not in {"long", "short"}:
            raise BotError("No open position")
        return pos

    def _clear_failed_stop(self, reason: str, *, log_event: bool = True) -> None:
        failed = self.state.data["pending"].get("failed_stop")
        if failed is None:
            return
        self.state.data["pending"]["failed_stop"] = None
        self.state.save()
        if log_event:
            self.log("FAILED_STOP_CLEARED", reason=reason, failed_stop=failed)

    def _record_failed_stop(
        self,
        *,
        trigger: Decimal,
        raw_price: Any,
        message_id: int,
        index: int,
        position_side: str,
        error: Exception,
    ) -> None:
        record = {
            "failed_price": ds(trigger),
            "raw_price": None if raw_price is None else str(raw_price),
            "message_id": message_id,
            "action_index": index,
            "position_side": position_side,
            "failed_at": now_iso(),
            "error_type": type(error).__name__,
            "error_code": getattr(error, "code", None),
            "error": str(error),
        }
        self.state.data["pending"]["failed_stop"] = record
        self.state.save()
        self.log("STOP_PLAN_REJECTED", failed_stop=record.copy())

    def _failed_stop_is_fresh(
        self, failed: dict[str, Any], message_id: int
    ) -> bool:
        failed_message_id = int(failed.get("message_id") or 0)
        message_gap = message_id - failed_message_id
        if message_gap <= 0 or message_gap > FAILED_STOP_CORRECTION_MAX_MESSAGE_GAP:
            return False
        try:
            failed_at = datetime.fromisoformat(str(failed["failed_at"]))
        except (KeyError, TypeError, ValueError):
            return False
        age = (datetime.now(timezone.utc) - failed_at).total_seconds()
        return 0 <= age <= FAILED_STOP_CORRECTION_TTL_SECONDS

    async def maybe_failed_stop_correction(
        self, source_text: str, message_id: int
    ) -> Optional[dict[str, Any]]:
        failed = self.state.data["pending"].get("failed_stop")
        if not failed:
            return None
        if not self._failed_stop_is_fresh(failed, message_id):
            self._clear_failed_stop("correction_window_expired")
            return None
        fragment = extract_stop_correction_fragment(source_text)
        if fragment is None:
            return None
        pos = self.require_position()
        if pos["side"] != failed.get("position_side"):
            self._clear_failed_stop("position_side_changed")
            return None
        reference = await self.reference_price()
        try:
            candidate = build_stop_prefix_correction(
                D(failed.get("failed_price")),
                fragment,
                reference,
                str(pos["side"]),
                self.config,
            )
        except BotError as exc:
            self.log(
                "FAILED_STOP_CORRECTION_REJECTED",
                message_id=message_id,
                message=source_text,
                fragment=fragment,
                failed_stop=failed.copy(),
                error=str(exc),
            )
            return None
        return {
            "action": {"type": "SET_STOP", "price": int(candidate)},
            "failed_price": str(failed["failed_price"]),
            "fragment": fragment,
            "candidate_price": ds(candidate),
            "reference_price": ds(reference),
            "failed_message_id": int(failed["message_id"]),
        }

    async def wait_change(self, before: Decimal, increase: bool) -> None:
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            pos = await self.api.position()
            total = D(pos.get("total")) if pos else Decimal("0")
            if (increase and total > before) or ((not increase) and total < before):
                return

    async def cancel_adds(self) -> list[str]:
        cancelled = []
        for item in list(self.state.data["pending"]["add_orders"]):
            await self.api.cancel_order(str(item["order_id"]))
            cancelled.append(str(item["order_id"]))
        self.state.data["pending"]["add_orders"] = []
        self.state.save()
        return cancelled

    async def cancel_entry(self) -> Optional[str]:
        item = self.state.data["pending"]["entry_order"]
        if not item:
            return None
        await self.api.cancel_order(str(item["order_id"]))
        self.state.data["pending"]["entry_order"] = None
        self.state.save()
        return str(item["order_id"])

    async def cancel_plan_slot(self, slot: str) -> Optional[str]:
        item = self.state.data["pending"][slot]
        if not item:
            return None
        plan_type = str(item.get("plan_type") or ("pos_loss" if slot == "stop_order" else "pos_profit"))
        await self.api.cancel_plan(str(item["order_id"]), plan_type)
        self.state.data["pending"][slot] = None
        self.state.save()
        return str(item["order_id"])

    async def place_opening_order(
        self,
        *,
        kind: str,
        side: str,
        qty: Decimal,
        raw_price: Any,
        message_id: int,
        index: int,
        resolved_price: Optional[Decimal] = None,
        entry_sizing: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        qty = self.config.floor_size(qty)
        if qty < self.config.min_trade_num:
            raise BotError(f"Order qty {qty} below minTradeNum {self.config.min_trade_num}")
        order_type, price = "market", None
        if raw_price is not None:
            if resolved_price is None:
                price = restore_btc_price(
                    raw_price, await self.reference_price(), self.config
                )
            else:
                price = self.config.floor_price(resolved_price)
            order_type = "limit"
        coid = client_oid(message_id, index, kind)
        before = D(self.state.data["position"]["total_qty"])
        result = await self.api.place_order(
            side=side, size=qty, order_type=order_type, client_oid=coid, price=price, reduce_only=False,
        )
        record = {
            "order_id": str(result.get("orderId")), "client_oid": coid, "qty": ds(qty),
            "price": ds(price) if price is not None else None, "order_type": order_type, "created_at": now_iso(),
        }
        if entry_sizing:
            record["sizing_source"] = entry_sizing["source"]
            record["margin_usdt"] = entry_sizing["margin_usdt"]
            for key in (
                "account_usdt_equity",
                "equity_ratio",
                "sizing_price",
            ):
                if key in entry_sizing:
                    record[key] = entry_sizing[key]

        # Consume or invalidate the old SL-reentry allowance immediately after
        # Bitget accepts a new entry order. This prevents a crash/replay from
        # reusing the same stopped-position margin twice.
        if kind == "entry":
            if entry_sizing and entry_sizing.get("use_reentry"):
                self._consume_reentry(message_id, "reentry_order_accepted")
            else:
                self._clear_reentry("superseded_by_new_entry")
                self.state.save()

        if order_type == "limit":
            slot = self.state.data["pending"]
            if kind == "entry":
                slot["entry_order"] = record
            else:
                slot["add_orders"].append(record)
            self.state.save()
        else:
            await self.wait_change(before, True)
            await self.reconcile()
        self.log("ORDER_PLACED", kind=kind, side=side, **record)
        return record

    async def open(
        self,
        action_type: str,
        raw_price: Any,
        message_id: int,
        index: int,
        reentry_side: Optional[str] = None,
    ) -> dict[str, Any]:
        if D(self.state.data["position"]["total_qty"]) > 0:
            raise BotError("OPEN rejected: position already exists")
        if self.state.data["pending"]["entry_order"]:
            raise BotError("OPEN rejected: entry order already pending")

        stopped = self.state.data.get("stopped_position")
        reentry_available = bool(
            stopped
            and stopped.get("available_for_reentry")
            and stopped.get("side") in {"long", "short"}
            and stopped.get("stop_order_id")
            and D(stopped.get("total_margin_usdt")) > 0
        )
        use_reentry = action_type == "OPEN_REENTRY"

        if use_reentry:
            if not reentry_available:
                raise BotError("REENTRY rejected: no verified SL-closed position is available")
            position_side = str(stopped["side"])
            if reentry_side is not None and reentry_side != position_side:
                raise BotError(
                    "REENTRY rejected: requested side does not match the SL-closed position"
                )
        elif action_type == "OPEN_LONG":
            if reentry_side is not None:
                raise BotError("OPEN_LONG rejected: reentry side is not allowed")
            position_side = "long"
        elif action_type == "OPEN_SHORT":
            if reentry_side is not None:
                raise BotError("OPEN_SHORT rejected: reentry side is not allowed")
            position_side = "short"
        else:
            raise BotError(f"Unsupported open action: {action_type}")

        account_equity: Optional[Decimal] = None
        if use_reentry:
            margin = D(stopped.get("total_margin_usdt"))
            sizing_source = "previous_sl_margin_usdt"
        else:
            account_equity, margin = initial_margin_from_account(
                await self.api.account()
            )
            sizing_source = "account_usdt_equity_1_25_percent"

        reference = await self.reference_price()
        entry_price: Optional[Decimal] = None
        sizing_price = reference
        if raw_price is not None:
            entry_price = restore_btc_price(raw_price, reference, self.config)
            sizing_price = entry_price

        notional = margin * LEVERAGE
        if notional < self.config.min_trade_usdt:
            raise BotError(
                f"{sizing_source} gives {notional} USDT notional, "
                f"below minimum {self.config.min_trade_usdt}"
            )
        qty = self.config.floor_size(notional / sizing_price)
        entry_sizing = {
            "source": sizing_source,
            "margin_usdt": ds(margin),
            "sizing_price": ds(sizing_price),
            "use_reentry": use_reentry,
        }
        if account_equity is not None:
            entry_sizing["account_usdt_equity"] = ds(account_equity)
            entry_sizing["equity_ratio"] = ds(INITIAL_MARGIN_EQUITY_RATIO)
        return await self.place_opening_order(
            kind="entry",
            side="buy" if position_side == "long" else "sell",
            qty=qty,
            raw_price=raw_price,
            message_id=message_id,
            index=index,
            resolved_price=entry_price,
            entry_sizing=entry_sizing,
        )
    async def add(self, raw_price: Any, message_id: int, index: int) -> dict[str, Any]:
        if self.state.data["pending"]["entry_order"]:
            raise BotError("ADD rejected: initial entry limit order still has an unfilled remainder")
        if self.state.data["pending"]["add_orders"]:
            raise BotError("ADD rejected: another ADD limit order is still pending")
        pos = self.require_position()
        qty = D(pos["total_qty"])
        return await self.place_opening_order(
            kind="add", side="buy" if pos["side"] == "long" else "sell", qty=qty,
            raw_price=raw_price, message_id=message_id, index=index,
        )

    async def reduce(self, qty: Decimal, reason: str, message_id: int, index: int) -> dict[str, Any]:
        pos = self.require_position()
        total = D(pos["total_qty"])
        qty = min(total, self.config.floor_size(qty))
        if qty < self.config.min_trade_num:
            raise BotError(f"Close qty {qty} below minTradeNum {self.config.min_trade_num}")
        await self.cancel_adds()
        await self.cancel_entry()
        coid = client_oid(message_id, index, "close")
        result = await self.api.place_order(
            side="sell" if pos["side"] == "long" else "buy",
            size=qty, order_type="market", client_oid=coid, reduce_only=True,
        )
        await self.wait_change(total, False)
        await self.reconcile()
        self.log("POSITION_REDUCED", reason=reason, qty=ds(qty), order_id=str(result.get("orderId")), client_oid=coid)
        return {"order_id": str(result.get("orderId")), "qty": ds(qty)}

    async def close_all(self, message_id: int, index: int, reason: str = "CLOSE_ALL") -> dict[str, Any]:
        has_position = D(self.state.data["position"]["total_qty"]) > 0
        pending_entry = self.state.data["pending"]["entry_order"] is not None
        pending_add = bool(self.state.data["pending"]["add_orders"])
        await self.cancel_adds()
        await self.cancel_entry()
        if not has_position:
            if pending_entry or pending_add:
                self.log("OPENING_ORDERS_CANCELLED_WHILE_FLAT", reason=reason)
                return {"flat": True, "opening_orders_cancelled": True}
            raise BotError("CLOSE_ALL rejected: no position or pending opening order")
        for slot in ("stop_order", "tp_order"):
            try:
                await self.cancel_plan_slot(slot)
            except BitgetAPIError as exc:
                self.log("PLAN_CANCEL_BEFORE_CLOSE_FAILED", slot=slot, error=str(exc))
        before = D(self.state.data["position"]["total_qty"])
        result = await self.api.flash_close()
        await self.wait_change(before, False)
        await self.reconcile()
        self.log("POSITION_CLOSED_ALL", reason=reason, result=result)
        return {"result": result}

    async def set_plan(self, stop: bool, raw_price: Any, message_id: int, index: int) -> dict[str, Any]:
        pos = self.require_position()
        if stop:
            self._clear_failed_stop("superseded_by_set_stop", log_event=False)
        if raw_price is None:
            if stop:
                raise BotError("SET_STOP requires an explicit price")
            trigger = self.config.floor_price(D(pos.get("break_even_price")))
            source = "break_even"
        else:
            trigger = restore_btc_price(raw_price, await self.reference_price(), self.config)
            source = "message"
        slot = "stop_order" if stop else "tp_order"
        plan_type = "pos_loss" if stop else "pos_profit"
        await self.cancel_plan_slot(slot)
        coid = client_oid(message_id, index, "sl" if stop else "tp")
        try:
            result = await self.api.place_plan(
                plan_type=plan_type,
                hold_side="buy" if pos["side"] == "long" else "sell",
                trigger_price=trigger,
                client_oid=coid,
            )
        except BitgetAPIError as exc:
            if stop:
                self._record_failed_stop(
                    trigger=trigger,
                    raw_price=raw_price,
                    message_id=message_id,
                    index=index,
                    position_side=str(pos["side"]),
                    error=exc,
                )
            raise
        except httpx.HTTPStatusError as exc:
            if stop and 400 <= exc.response.status_code < 500:
                self._record_failed_stop(
                    trigger=trigger,
                    raw_price=raw_price,
                    message_id=message_id,
                    index=index,
                    position_side=str(pos["side"]),
                    error=exc,
                )
            raise
        record = {
            "order_id": str(result.get("orderId")), "client_oid": coid, "price": ds(trigger),
            "plan_type": plan_type, "source": source, "created_at": now_iso(),
        }
        self.state.data["pending"][slot] = record
        if stop:
            self.state.data["pending"]["failed_stop"] = None
        self.state.save()
        self.log("POSITION_PLAN_SET", slot=slot, **record)
        return record

    async def execute(
        self,
        action: dict[str, Any],
        message_id: int,
        index: int,
        source_text: str,
    ) -> dict[str, Any]:
        await self.reconcile()
        kind, price = action["type"], action["price"]
        if kind in {"OPEN_LONG", "OPEN_SHORT", "OPEN_REENTRY"}:
            return await self.open(kind, price, message_id, index, action.get("side"))
        if kind == "ADD":
            return await self.add(price, message_id, index)
        if kind == "SET_STOP":
            return await self.set_plan(True, price, message_id, index)
        if kind == "SET_TP":
            return await self.set_plan(False, price, message_id, index)
        if kind == "CLOSE_HALF":
            pos = self.require_position()
            half = self.config.floor_size(D(pos["total_qty"]) / 2)
            if half < self.config.min_trade_num:
                return await self.close_all(message_id, index, "CLOSE_HALF_DUST_TO_FULL")
            return await self.reduce(half, "CLOSE_HALF", message_id, index)
        if kind == "CLOSE_ADDS":
            pos = self.require_position()
            added = D(pos["added_qty"])
            if added <= 0:
                raise BotError("CLOSE_ADDS rejected: no tracked added quantity")
            return await self.reduce(added, "CLOSE_ADDS", message_id, index)
        if kind == "CLOSE_ALL":
            return await self.close_all(message_id, index)
        if kind == "CANCEL_ADD":
            cancelled = await self.cancel_adds()
            self.log("PENDING_ADDS_CANCELLED", order_ids=cancelled)
            return {"cancelled_order_ids": cancelled}
        if kind == "CANCEL_STOP":
            oid = await self.cancel_plan_slot("stop_order")
            self._clear_failed_stop("cancel_stop_action")
            self.log("STOP_CANCELLED", order_id=oid)
            return {"cancelled_order_id": oid}
        raise BotError(f"Unhandled action: {kind}")


async def check_only() -> None:
    api = BitgetClient()
    try:
        config = await api.contract_config()
        state = StateStore(STATE_PATH)
        engine = TradingEngine(api, state, config)
        await engine.setup()
        account = await api.account()
        print(json.dumps({
            "status": "ok",
            "symbol": SYMBOL,
            "usdt_equity": account.get("usdtEquity"),
            "margin_mode": account.get("marginMode"),
            "position_mode": account.get("posMode"),
            "leverage": account.get("crossedMarginLeverage"),
            "position": await api.position(),
            "mark_price": (await api.ticker()).get("markPrice"),
            "contract": {
                "min_trade_num": ds(config.min_trade_num),
                "min_trade_usdt": ds(config.min_trade_usdt),
                "size_step": ds(config.size_step),
                "price_step": ds(config.price_step),
                "max_leverage": ds(config.max_leverage),
            },
        }, ensure_ascii=False, indent=2))
    finally:
        await api.close()


async def run_bot() -> None:
    api_id = int(require_env("TELEGRAM_API_ID"))
    api_hash = require_env("TELEGRAM_API_HASH")
    phone = require_env("TELEGRAM_PHONE")
    api = BitgetClient()
    telegram: Optional[TelegramClient] = None
    llama: Optional[httpx.AsyncClient] = None
    reconcile_task: Optional[asyncio.Task[None]] = None
    try:
        config = await api.contract_config()
        state = StateStore(STATE_PATH)
        engine = TradingEngine(api, state, config)
        await engine.setup()

        telegram = TelegramClient(str(BASE_DIR / SESSION_NAME), api_id, api_hash)
        await telegram.start(phone=phone)
        entity = await telegram.get_entity(CHANNEL_ID)
        llama = httpx.AsyncClient(timeout=LLAMA_TIMEOUT_SECONDS)

        print(f"LIVE bot started: {getattr(entity, 'title', CHANNEL_ID)}")
        print(f"Bitget {SYMBOL} | crossed | one-way | 98x")
        print(f"Author filter: {POST_AUTHOR_FILTER or '(none)'}")
        print(f"State: {STATE_PATH}")
        print(f"Reconcile interval: {RECONCILE_INTERVAL_SECONDS:g}s")

        async def periodic_reconcile() -> None:
            while True:
                await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)
                try:
                    async with TRADE_LOCK:
                        await engine.reconcile()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    engine.log("PERIODIC_RECONCILE_FAILED", error=error)
                    print(json.dumps({
                        "timestamp": now_iso(),
                        "event": "PERIODIC_RECONCILE_FAILED",
                        "error": error,
                    }, ensure_ascii=False, indent=2))

        async def handler(event: Any) -> None:
            message = event.message
            if not author_matches(message.post_author):
                return
            source_text = normalize_message(message)
            if not source_text:
                return
            text = normalize_trading_shorthand(source_text)
            record: dict[str, Any] = {
                "received_at": now_iso(),
                "message_date": message.date.isoformat() if message.date else None,
                "telegram_message_id": message.id,
                "post_author": message.post_author,
                "message": source_text,
                "ok": False,
            }
            if text != source_text:
                record["normalized_message"] = text
            try:
                parsed = await infer(llama, text)
                record.update(parsed)
                async with TRADE_LOCK:
                    if state.is_processed(message.id):
                        record.update({"ok": True, "skipped": "duplicate_message_id"})
                    else:
                        # Bring local state up to date before applying a fallback or
                        # executing any action. This also detects manual closes.
                        await engine.reconcile()

                        actions = align_action_prices_to_source(
                            source_text, list(parsed["actions"])
                        )
                        if not actions:
                            correction = await engine.maybe_failed_stop_correction(
                                source_text, message.id
                            )
                            if correction:
                                actions = [correction["action"]]
                                record["fallback"] = "FAILED_SET_STOP_PREFIX_CORRECTION"
                                record["stop_correction"] = {
                                    key: value
                                    for key, value in correction.items()
                                    if key != "action"
                                }
                                engine.log(
                                    "FAILED_STOP_CORRECTION_INFERRED",
                                    message_id=message.id,
                                    message=source_text,
                                    **record["stop_correction"],
                                )
                        if (
                            not actions
                            and "자유" in source_text
                            and D(state.data["position"].get("total_qty")) > 0
                        ):
                            actions = [{"type": "CLOSE_ALL", "price": None}]
                            record["fallback"] = "EMPTY_ACTIONS_WITH_FREE_KEYWORD_TO_CLOSE_ALL"
                            engine.log(
                                "MODEL_EMPTY_CLOSE_ALL_FALLBACK",
                                message_id=message.id,
                                message=source_text,
                                keyword="자유",
                            )

                        if any(
                            action.get("type") == "SET_STOP"
                            for action in actions
                        ):
                            reference = await engine.reference_price()
                            position_side = str(engine.require_position()["side"])
                            (
                                actions,
                                rejected_actions,
                                deduplicated_actions,
                            ) = validate_and_deduplicate_stop_actions(
                                actions, reference, position_side, config
                            )
                            if rejected_actions:
                                record["rejected_actions"] = rejected_actions
                            if deduplicated_actions:
                                record["deduplicated_actions"] = deduplicated_actions

                        actions = prioritize_cancel_actions(actions)
                        record["actions"] = actions

                        # Deliberately claim before orders: losing one signal after a
                        # crash is safer than replaying it and doubling a 98x order.
                        state.claim_message(message.id)
                        executions = []
                        for index, action in enumerate(actions):
                            try:
                                result = await engine.execute(action, message.id, index, source_text)
                                executions.append({"action": action, "ok": True, "result": result})
                            except Exception as exc:
                                executions.append({"action": action, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
                                break
                        record["executions"] = executions
                        record["ok"] = all(x["ok"] for x in executions)
            except Exception as exc:
                record["error"] = f"{type(exc).__name__}: {exc}"
            append_jsonl(EVENT_LOG_PATH, record)
            print(json.dumps(record, ensure_ascii=False, indent=2))

        reconcile_task = asyncio.create_task(periodic_reconcile())
        telegram.add_event_handler(handler, events.NewMessage(chats=entity))
        await telegram.run_until_disconnected()
    finally:
        if reconcile_task:
            reconcile_task.cancel()
            try:
                await reconcile_task
            except asyncio.CancelledError:
                pass
        if llama:
            await llama.aclose()
        if telegram:
            await telegram.disconnect()
        await api.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Configure/verify Bitget and exit without listening")
    args = parser.parse_args()
    asyncio.run(check_only() if args.check else run_bot())


if __name__ == "__main__":
    main()
