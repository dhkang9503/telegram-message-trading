"""Small USD-M REST adapter. No automatic retries of mutating requests."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from decimal import Decimal
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SYMBOL = "BTCUSDT"


class ExchangeError(RuntimeError):
    def __init__(self, code):
        self.code = int(code)
        # Never expose request URLs, signatures, keys or remote exception text.
        super().__init__(f"Binance code {self.code}")


class Binance:
    def __init__(self, key="", secret="", opener=urlopen, clock=time.time):
        self.key, self.secret = key, secret
        self.opener, self.clock = opener, clock

    def request(self, method, path, params=None, signed=True):
        params = dict(params or {})
        headers = {}
        if signed:
            if not self.key or not self.secret:
                raise ValueError("Missing scenario exchange credentials")
            params.update(timestamp=int(self.clock() * 1000), recvWindow=5000)
            headers["X-MBX-APIKEY"] = self.key
        query = urlencode(params)
        if signed:
            signature = hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            query += "&signature=" + signature
        url = "https://fapi.binance.com" + path
        data = None
        if method == "POST":
            data = query.encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif query:
            url += "?" + query
        try:
            with self.opener(Request(url, data=data, headers=headers, method=method), timeout=5) as r:
                return json.load(r)
        except HTTPError as exc:
            try:
                code = json.loads(exc.read()).get("code", exc.code)
            except (ValueError, OSError):
                code = exc.code
            raise ExchangeError(code) from None

    def now(self):
        now = int(self.request("GET", "/fapi/v1/time", signed=False)["serverTime"])
        if abs(now - int(self.clock() * 1000)) > 1000:
            raise ValueError("Exchange clock skew exceeds 1 second")
        return now

    def rules(self):
        result = self.request("GET", "/fapi/v1/exchangeInfo", signed=False)
        row = next(x for x in result["symbols"] if x["symbol"] == SYMBOL)
        if row["status"] != "TRADING" or row["contractType"] != "PERPETUAL":
            raise ValueError("BTCUSDT perpetual is not trading")
        return {x["filterType"]: x for x in row["filters"]}

    def quote(self):
        book = self.request("GET", "/fapi/v1/ticker/bookTicker", {"symbol": SYMBOL}, False)
        mark = self.request("GET", "/fapi/v1/premiumIndex", {"symbol": SYMBOL}, False)
        now = self.now()
        if any(not 0 <= now - int(x["time"]) <= 5000 for x in (book, mark)):
            raise ValueError("Stale execution quote")
        return Decimal(book["bidPrice"]), Decimal(book["askPrice"]), Decimal(mark["markPrice"])

    def balance(self):
        rows = self.request("GET", "/fapi/v3/balance")
        row = next(x for x in rows if x["asset"] == "USDT")
        return Decimal(row["balance"]), Decimal(row["availableBalance"])

    def fee(self):
        return Decimal(self.request("GET", "/fapi/v1/commissionRate", {"symbol": SYMBOL})["takerCommissionRate"])

    def daily_losses(self, start, end):
        # Negative trading cash flows only: profits/rebates do not replenish budget.
        loss = Decimal(0)
        for page in range(1, 21):
            rows = self.request("GET", "/fapi/v1/income", {
                "startTime": start, "endTime": end, "limit": 1000, "page": page})
            for r in rows:
                if r["incomeType"] in {"REALIZED_PNL", "COMMISSION", "FUNDING_FEE"}:
                    if r["asset"] != "USDT":
                        raise ValueError("Non-USDT trading costs need explicit conversion")
                    loss += max(Decimal(0), -Decimal(r["income"]))
            if len(rows) < 1000:
                return loss
        raise ValueError("Daily income history truncated")

    def positions(self):
        return self.request("GET", "/fapi/v3/positionRisk")

    def position(self):
        rows = [r for r in self.positions() if r["symbol"] == SYMBOL and Decimal(r["positionAmt"])]
        if len(rows) > 1 or any(r["positionSide"] != "BOTH" for r in rows):
            raise ValueError("Only dedicated one-way BTC positions supported")
        return rows[0] if rows else {"positionAmt": "0", "entryPrice": "0", "liquidationPrice": "0"}

    def open_orders(self):
        return self.request("GET", "/fapi/v1/openOrders")

    def open_algos(self):
        return self.request("GET", "/fapi/v1/openAlgoOrders")

    def configure(self):
        if self.request("GET", "/fapi/v1/positionSide/dual")["dualSidePosition"]:
            raise ValueError("Use one-way mode in the dedicated account")
        if self.request("GET", "/fapi/v1/multiAssetsMargin")["multiAssetsMargin"]:
            raise ValueError("Use single-asset mode")
        try:
            self.request("POST", "/fapi/v1/marginType", {"symbol": SYMBOL, "marginType": "ISOLATED"})
        except ExchangeError as exc:
            if exc.code != -4046:  # Already isolated.
                raise
        self.request("POST", "/fapi/v1/leverage", {"symbol": SYMBOL, "leverage": 3})

    def order(self, client_id):
        try:
            return self.request("GET", "/fapi/v1/order", {"symbol": SYMBOL, "origClientOrderId": client_id})
        except ExchangeError as exc:
            if exc.code == -2013:
                return None
            raise

    def market(self, client_id, side, qty, reduce=False):
        params = dict(symbol=SYMBOL, side=side, type="MARKET", quantity=str(qty),
                      newClientOrderId=client_id, newOrderRespType="RESULT", positionSide="BOTH")
        if reduce:
            params["reduceOnly"] = "true"
        return self.request("POST", "/fapi/v1/order", params)

    def cancel_order(self, client_id):
        return self.request("DELETE", "/fapi/v1/order", {"symbol": SYMBOL, "origClientOrderId": client_id})

    def algo(self, client_id):
        try:
            return self.request("GET", "/fapi/v1/algoOrder", {"clientAlgoId": client_id})
        except ExchangeError as exc:
            if exc.code == -2013:
                return None
            raise

    def protect(self, client_id, side, kind, price):
        return self.request("POST", "/fapi/v1/algoOrder", dict(
            symbol=SYMBOL, algoType="CONDITIONAL", side=side, positionSide="BOTH",
            type=kind, triggerPrice=str(price), clientAlgoId=client_id,
            closePosition="true", workingType="MARK_PRICE", priceProtect="false"))

    def cancel_algo(self, client_id):
        return self.request("DELETE", "/fapi/v1/algoOrder", {"clientAlgoId": client_id})
