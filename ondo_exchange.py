import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from urllib.parse import urlsplit


logger = logging.getLogger(__name__)


class OndoFatalError(RuntimeError):
    """订单状态无法安全恢复，需要停止策略。"""


class OndoAPIError(RuntimeError):
    def __init__(self, message, code="", uncertain=False, status=None):
        super().__init__(message)
        self.code = code
        self.uncertain = uncertain
        self.status = status


class OndoExchange:
    id = "ondo"

    def __init__(
        self, api_key_id, api_secret,
        api_url="https://api.ondoperps.xyz", timeout=10,
    ):
        try:
            import requests
        except ImportError as exc:
            raise RuntimeError(
                "requests is not installed; use requirements-ondo.txt"
            ) from exc

        if not api_key_id or not api_secret:
            raise ValueError("Missing ONDO_API_KEY_ID or ONDO_API_SECRET")
        url = urlsplit(api_url)
        if (
            url.scheme != "https"
            or url.netloc not in (
                "api.ondoperps.xyz", "api.ondoperps-sandbox.xyz"
            )
            or url.path not in ("", "/")
            or url.query or url.fragment
        ):
            raise ValueError("ONDO_API_URL must be an official Ondo API URL")
        if not 0 < timeout < float("inf"):
            raise ValueError("ONDO_TIMEOUT must be positive and finite")

        self.api_key_id = api_key_id
        self._api_secret = api_secret
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self._requests = requests
        self._session = requests.Session()
        self._markets = {}
        self._closed = False

    @classmethod
    def from_env(cls):
        api_key_id = os.getenv("ONDO_API_KEY_ID", "").strip().strip("'\"")
        api_secret = os.getenv("ONDO_API_SECRET", "").strip().strip("'\"")
        api_url = os.getenv(
            "ONDO_API_URL", "https://api.ondoperps.xyz"
        ).strip().strip("'\"")
        timeout = float(os.getenv("ONDO_TIMEOUT", "10").strip().strip("'\""))
        return cls(api_key_id, api_secret, api_url, timeout)

    def _request(self, method, path, params=None, body=None, private=True):
        if self._closed:
            raise RuntimeError("Ondo client is closed")

        method = method.upper()
        body_text = (
            json.dumps(body, separators=(",", ":")) if body is not None else ""
        )
        request = self._requests.Request(
            method, self.api_url + path, params=params,
            data=body_text.encode("utf-8") if body is not None else None,
            headers={"Content-Type": "application/json"},
        )
        prepared = self._session.prepare_request(request)
        if private:
            timestamp = str(int(time.time() * 1000))
            # 使用最终发送的 URI 和正文，避免编码或空格导致签名不一致。
            message = timestamp + method.upper() + prepared.path_url + body_text
            signature = hmac.new(
                self._api_secret.encode(), message.encode(), hashlib.sha256
            ).hexdigest()
            prepared.headers.update({
                "ONDO-KEY-ID": self.api_key_id,
                "ONDO-TIMESTAMP": timestamp,
                "ONDO-SIGN": signature,
            })

        settings = self._session.merge_environment_settings(
            prepared.url, {}, None, None, None
        )
        try:
            response = self._session.send(
                prepared, timeout=self.timeout, allow_redirects=False, **settings
            )
        except self._requests.RequestException as exc:
            raise OndoAPIError(
                f"Ondo {method} request failed: {type(exc).__name__}",
                uncertain=method == "POST",
            ) from None

        with response:
            status = response.status_code
            # 429 明确表示请求被限流，不把它误判为可能已下单。
            if status == 429:
                raise OndoAPIError(
                    "Ondo HTTP 429: too_many_requests", code="too_many_requests",
                    status=status,
                )
            try:
                payload = response.json()
            except ValueError:
                raise OndoAPIError(
                    f"Ondo returned invalid JSON (HTTP {status})",
                    uncertain=method == "POST", status=status,
                ) from None
            if (
                not 200 <= status < 300
                or not isinstance(payload, dict)
                or payload.get("success") is not True
            ):
                code, message = "", "invalid response"
                if isinstance(payload, dict):
                    code = payload.get("error_code", "")
                    message = payload.get("error", "request failed")
                detail = f"Ondo HTTP {status}: {code} {message}"
                for secret in (self.api_key_id, self._api_secret):
                    detail = detail.replace(secret, "<redacted>")
                raise OndoAPIError(
                    detail, code=code, status=status,
                    uncertain=method == "POST" and (
                        status >= 500 or 300 <= status < 400
                        or not isinstance(payload, dict)
                        or payload.get("success") is not False
                    ),
                )
            return payload

    def load_markets(self):
        response = self._request("GET", "/v1/markets")
        markets = {}
        for detail in response["result"]["perps"]["tradingPairs"]:
            market_id = detail["market"]
            if (
                not market_id.endswith(".P") or detail.get("disabled")
                or detail.get("tradingState", "TRADING") != "TRADING"
            ):
                continue
            amount_step = Decimal(detail["baseIncrement"])
            price_step = Decimal(detail["quoteIncrement"])
            if any(not step.is_finite() or step <= 0
                   for step in (amount_step, price_step)):
                raise ValueError(f"Invalid Ondo increments for {market_id}")
            pair = detail.get("pair") or {}
            base = (pair.get("base") or market_id.rsplit("-", 1)[0]).upper()
            symbol = market_id.upper()
            if symbol in markets:
                raise ValueError(f"Ambiguous Ondo symbol {symbol!r}")
            markets[symbol] = {
                "id": market_id,
                "symbol": symbol,
                "base": base,
                "quote": pair.get("quote") or "USD",
                "swap": True,
                "active": True,
                "contractSize": 1,
                "precision": {
                    "amount": str(amount_step), "price": str(price_step),
                },
                "limits": {
                    "amount": {"min": float(amount_step), "max": None},
                    "cost": {"min": None, "max": None},
                },
                "info": detail,
            }
        if not markets:
            raise RuntimeError("Ondo returned no active perpetual markets")
        self._markets = markets
        return markets

    def market(self, symbol):
        if not self._markets:
            self.load_markets()
        symbol = symbol.strip().upper()
        if symbol in self._markets:
            return self._markets[symbol]
        # 裸币名与旧 CCXT 配置只是别名，策略和返回数据均使用原生 ID。
        candidates = [
            market for market in self._markets.values()
            if symbol in (market["base"], f'{market["base"]}/USDT:USDT')
        ]
        if len(candidates) != 1:
            raise ValueError(
                f"Ondo perpetual market {symbol!r} "
                f"{'is ambiguous' if candidates else 'was not found'}; "
                "use an active native market ID such as ETH-USD.P"
            )
        return candidates[0]

    def amount_to_precision(self, symbol, amount):
        step = Decimal(self.market(symbol)["precision"]["amount"])
        value = Decimal(str(amount))
        if not value.is_finite() or value < 0:
            raise ValueError("Ondo amount must be non-negative and finite")
        value = (value / step).to_integral_value(rounding=ROUND_DOWN) * step
        return format(value.quantize(step), "f")

    def price_to_precision(self, symbol, price):
        step = Decimal(self.market(symbol)["precision"]["price"])
        value = Decimal(str(price))
        if not value.is_finite() or value < 0:
            raise ValueError("Ondo price must be non-negative and finite")
        value = (value / step).to_integral_value(rounding=ROUND_HALF_UP) * step
        return format(value.quantize(step), "f")

    def fetch_order_book(self, symbol, limit=5):
        market = self.market(symbol)
        response = self._request(
            "GET", "/v1/perps/depth",
            params={"market": market["id"], "depth": max(1, min(int(limit), 100))},
            private=False,
        )
        book = response["result"]
        bids = [[float(p), float(q)] for p, q in book["bids"] if Decimal(q) > 0]
        asks = [[float(p), float(q)] for p, q in book["asks"] if Decimal(q) > 0]
        bids.sort(key=lambda level: level[0], reverse=True)
        asks.sort(key=lambda level: level[0])
        return {"bids": bids, "asks": asks}

    def fetch_ticker(self, symbol):
        market = self.market(symbol)
        detail = self.fetch_mark_prices()[market["id"]]
        mark = float(detail.get("markPrice") or detail.get("price") or 0)
        return {"mark": mark, "last": None, "close": mark}

    def fetch_mark_prices(self):
        return self._request("GET", "/v1/perps/mark_prices", private=False)["result"]

    def fetch_balance(self):
        return self._request("GET", "/v1/perps/balance")["result"]

    def fetch_positions(self, symbols=None):
        if not self._markets:
            self.load_markets()
        response = self._request("GET", "/v1/perps/positions")
        requested = symbols if symbols is not None else list(self._markets)
        symbols_by_market = {}
        for symbol in requested:
            market = self.market(symbol)
            symbols_by_market[market["id"]] = market["symbol"]
        positions = []
        for position in response["result"]:
            symbol = symbols_by_market.get(position["market"])
            if symbol is None:
                continue
            contracts = abs(Decimal(position["netQuantity"]))
            side = position["direction"]
            if side not in {"long", "short", "neutral"}:
                raise ValueError("Unknown Ondo position direction")
            size = -contracts if side == "short" else contracts
            positions.append({
                "symbol": symbol,
                "contracts": float(contracts),
                "side": side,
                "unrealizedPnl": float(position["unrealizedPnl"]),
                "liquidationPrice": float(position.get("liquidationPrice") or 0),
                "info": {"positionAmt": str(size)},
            })
        return positions

    def _parse_order(self, order):
        order_id = str(order["orderId"] or "")
        amount = Decimal(order["size"])
        filled = Decimal(order["filledSize"])
        if (
            not order_id or not amount.is_finite() or not filled.is_finite()
            or amount <= 0 or not 0 <= filled <= amount
        ):
            raise ValueError("Invalid Ondo order ID or quantity")
        status = order["status"]
        if status == "canceled" and filled > 0:
            raise OndoFatalError(
                f"Ondo order {order_id} canceled after filling {filled}/{amount}; "
                "stop instead of replacing the full quantity"
            )
        if status == "fullyfilled":
            if filled != amount:
                raise ValueError("Inconsistent Ondo fullyfilled quantity")
            status = "filled"
        return {
            "id": order_id,
            "clientOrderId": order.get("clientOrderId"),
            "symbol": self.market(order["market"])["symbol"],
            "price": float(order["price"] or 0),
            "amount": float(amount),
            "filled": float(filled),
            "remaining": float(amount - filled),
            "side": order["side"],
            "status": status,
            "info": order,
        }

    def fetch_open_orders(self, symbol=None):
        market = self.market(symbol) if symbol is not None else None
        params = {"status": "open", "limit": 1000}
        if market is not None:
            params["market"] = market["id"]
        orders, cursors = {}, set()
        while True:
            page = self._request("GET", "/v1/perps/orders", params=params)
            if not isinstance(page["result"], list):
                raise ValueError("Invalid Ondo open orders response")
            for raw in page["result"]:
                if market is not None and raw["market"] != market["id"]:
                    raise ValueError("Ondo returned orders for another market")
                if raw["status"] in {"open", "pending", "untriggered"}:
                    order = self._parse_order(raw)
                    orders[order["id"]] = order
            cursor = (page.get("pageInfo") or {}).get("nextCursor")
            if not cursor:
                return list(orders.values())
            if cursor in cursors:
                raise RuntimeError("Ondo returned a repeated pagination cursor")
            cursors.add(cursor)
            params["cursor"] = cursor

    def fetch_fills(self, start_time, end_time):
        """共享查询指定时间窗内的全部成交；分页完成后才返回。"""
        params = {"startTime": start_time, "endTime": end_time, "limit": 1000}
        fills, cursors = [], set()
        while True:
            page = self._request("GET", "/v1/perps/fills", params=params)
            if not isinstance(page.get("result"), list):
                raise ValueError("Invalid Ondo fills response")
            fills.extend(page["result"])
            page_info = page.get("pageInfo") or {}
            if not isinstance(page_info, dict):
                raise ValueError("Invalid Ondo fills pagination")
            cursor = page_info.get("nextCursor")
            if not cursor:
                return fills
            if not isinstance(cursor, str) or cursor in cursors:
                raise ValueError("Invalid or repeated Ondo fills cursor")
            cursors.add(cursor)
            params["cursor"] = cursor

    def create_orders(self, symbol, orders):
        """提交一批最多 20 笔普通 GTC 单，返回成功项及明确失败/无法确认项。"""
        if not orders:
            return {"addedOrders": [], "failedOrders": []}
        if len(orders) > 20:
            raise ValueError("Ondo batch placement supports at most 20 orders")
        requests = {}
        for item in orders:
            market = self.market(item["symbol"] if symbol is None else symbol)
            amount = self.amount_to_precision(market["symbol"], item["amount"])
            price = self.price_to_precision(market["symbol"], item["price"])
            if item["side"] not in {"buy", "sell"} or min(Decimal(amount), Decimal(price)) <= 0:
                raise ValueError("Ondo orders require a valid side, positive amount and price")
            client_id = item.get("clientOrderId") or "grid_" + uuid.uuid4().hex
            if client_id in requests:
                raise ValueError("Duplicate clientOrderId in batch")
            requests[client_id] = {
                "market": market["id"], "type": "limit", "side": item["side"],
                "size": amount, "price": price, "clientOrderId": client_id,
                "timeInForce": "GTC", "postOnly": False, "reduceOnly": False,
            }

        # POST 只发一次；未知结果仅用 GET 确认，禁止整批重发。
        try:
            response = self._request(
                "POST", "/v1/perps/orders/batch", body={"orders": list(requests.values())}
            )
        except OndoAPIError as exc:
            if not exc.uncertain:
                raise
            logger.warning(f"Ondo batch placement result uncertain: {exc}; confirming by clientOrderId")
            response = {"result": {"addedOrders": [], "failedOrders": []}}

        added, failed = {}, {}
        result = response.get("result")
        if not isinstance(result, dict):
            logger.warning(
                f"Ondo batch response invalid: field=result, type={type(result).__name__}, "
                f"request_count={len(requests)}; confirming unconfirmed orders by clientOrderId"
            )
            result = {}
        collections = {}
        for field in ("addedOrders", "failedOrders"):
            items = result.get(field)
            # 集合未提供或返回 null 时按空集合处理，其他类型不能悄悄忽略。
            if items is None:
                items = []
            elif not isinstance(items, list):
                logger.warning(
                    f"Ondo batch response invalid: field={field}, type={type(items).__name__}, "
                    f"request_count={len(requests)}; confirming unconfirmed orders by clientOrderId"
                )
                items = []
            collections[field] = items

        # 逐项处理；仅将格式异常或结果冲突的订单交给后续查询，保留其余有效结果。
        invalid_clients, order_clients = set(), {}
        for field, items in collections.items():
            for index, item in enumerate(items):
                client_id = None
                try:
                    if not isinstance(item, dict):
                        raise ValueError("Expected an order result object")
                    raw = item if field == "addedOrders" else item["order"]
                    if not isinstance(raw, dict):
                        raise ValueError("Expected order to be an object")
                    client_id = raw.get("clientOrderId")
                    if not isinstance(client_id, str) or client_id not in requests:
                        raise ValueError("Missing or unexpected clientOrderId")
                    if client_id in added or client_id in failed or client_id in invalid_clients:
                        raise ValueError("Repeated or conflicting clientOrderId")
                    request = requests[client_id]
                    if field == "failedOrders":
                        failed[client_id] = item
                        continue
                    order = self._parse_order(raw)
                    other_client = order_clients.get(order["id"])
                    if other_client is not None and other_client != client_id:
                        invalid_clients.add(other_client)
                        added.pop(other_client, None)
                        failed.pop(other_client, None)
                        logger.warning(
                            f"Ondo batch order ID conflict: client_ids={other_client},{client_id}; "
                            "confirming both orders by clientOrderId"
                        )
                        raise ValueError("Order ID is shared by multiple clientOrderIds")
                    order_clients[order["id"]] = client_id
                    if (
                        order["symbol"] != self.market(request["market"])["symbol"] or order["side"] != request["side"]
                        or Decimal(raw["size"]) != Decimal(request["size"])
                        or Decimal(raw["price"]) != Decimal(request["price"])
                        or order["status"] not in {"open", "pending", "filled", "canceled"}
                    ):
                        raise ValueError("Batch order did not match the requested order")
                    if order["status"] == "canceled":
                        failed[client_id] = {
                            "order": request, "errorCode": "order_canceled",
                            "error": f'Canceled without fills: {raw.get("cancelReason") or "unspecified"}',
                        }
                    else:
                        added[client_id] = order
                except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                    known_client = isinstance(client_id, str) and client_id in requests
                    if known_client:
                        invalid_clients.add(client_id)
                        added.pop(client_id, None)
                        failed.pop(client_id, None)
                    logger.warning(
                        f"Ondo batch item invalid: field={field}, index={index}, "
                        f"type={type(item).__name__}, client_id={client_id if known_client else 'unknown'}, "
                        f"added_count={len(collections['addedOrders'])}, failed_count={len(collections['failedOrders'])}: "
                        f"{type(exc).__name__}: {exc}; confirming unconfirmed orders by clientOrderId"
                    )

        for client_id, request in requests.items():
            if client_id in added or client_id in failed:
                continue
            last_error = "No matching order in batch response"
            for attempt in range(3):
                if attempt:
                    time.sleep(0.5)
                try:
                    response = self._request("GET", f"/v1/perps/orders/client:{client_id}")
                    raw = response["result"]
                    order = self._parse_order(raw)
                    if (
                        order["clientOrderId"] == client_id
                        and order["symbol"] == self.market(request["market"])["symbol"]
                        and order["side"] == request["side"]
                        and Decimal(raw["size"]) == Decimal(request["size"])
                        and Decimal(raw["price"]) == Decimal(request["price"])
                        and order["status"] in {"open", "pending", "filled", "canceled"}
                        and order["id"] not in {value["id"] for value in added.values()}
                    ):
                        if order["status"] == "canceled":
                            failed[client_id] = {
                                "order": request, "errorCode": "order_canceled",
                                "error": f'Canceled without fills: {raw.get("cancelReason") or "unspecified"}',
                            }
                        else:
                            added[client_id] = order
                        break
                    raise ValueError("Ondo confirmation did not match the requested order")
                except (OndoAPIError, KeyError, TypeError, ValueError, InvalidOperation) as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    logger.warning(f"Confirm Ondo order client_id={client_id}, attempt={attempt + 1}/3 failed: {last_error}")
            if client_id not in added and client_id not in failed:
                failed[client_id] = {
                    "order": request, "errorCode": "order_unconfirmed",
                    "error": f"Cannot confirm order; retry next poll; last error: {last_error}",
                }
        return {"addedOrders": list(added.values()), "failedOrders": list(failed.values())}

    def cancel_orders(self, symbol, order_ids):
        """symbol=None 时接收含 id/symbol 的订单，支持跨市场撤单。"""
        expected = {}
        for item in order_ids:
            order_id = str(item["id"] if isinstance(item, dict) else item)
            market = self.market(item["symbol"] if symbol is None else symbol)
            if order_id in expected and expected[order_id] != market["id"]:
                raise ValueError("Conflicting cancellation markets")
            expected[order_id] = market["id"]
        order_ids = list(expected)
        if not order_ids:
            return
        if len(order_ids) > 20:
            raise ValueError("Use at most 20 cancellations per batch")
        response = self._request(
            "DELETE", "/v1/perps/orders/batch", params={"orderIDs": ",".join(order_ids)}
        )
        result = response.get("result") or {}
        confirmed = set()
        for order in result.get("successfulCancels", []):
            if order.get("market") == expected.get(str(order.get("orderId"))) and order.get("status") == "canceled":
                confirmed.add(str(order["orderId"]))
        failures = {
            str(item["orderId"]): item for item in result.get("failedCancels", [])
        }
        unresolved = []
        for order_id in order_ids:
            if order_id in confirmed:
                continue
            response = self._request("GET", f"/v1/perps/orders/{order_id}")
            order = response["result"]
            if (
                str(order["orderId"]) == order_id and order["market"] == expected[order_id]
                and order["status"] in {"canceled", "fullyfilled"}
            ):
                continue
            detail = failures.get(order_id, {})
            unresolved.append(f'{order_id}: {detail.get("errorCode") or detail.get("error") or "not canceled"}')
        if unresolved:
            raise OndoAPIError("Ondo batch cancellation failed: " + "; ".join(unresolved))

    def close(self):
        if not self._closed:
            self._session.close()
            self._closed = True
