"""真实 requests 编码、签名及模拟交易所测试；禁止访问网络或加载真实 .env。"""
import ast
import hashlib
import hmac
import io
import json
import logging
import os
from pathlib import Path
import runpy
import socket
import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import Mock, patch, call
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests
from dotenv import dotenv_values
from ondo_exchange import OndoAPIError, OndoExchange, OndoFatalError
from ondo_grid import AccountTrader, ConsoleFormatter

class Venue:
    def __init__(self):
        self.orders = {}
        self.calls = []
        self.post_count = 0
        self.partial = False
        self.reject_all = False
        self.cancel_on_post = False
        self.partial_cancel_on_post = False
        self.fail_confirm = False
        self.timeout_after_post = False
        self.omit_result = False
        self.batch_result_transform = None
        self.reverse = True
        self.rate_limit_post = 0
        self.keep_open = set()
        self.price = 2000
        self.balance = {"marginRatio": "0.1", "leverage": "2", "marginBalance": "1000", "underLiquidation": False}
        self.page_size = 0
        self.empty_book = False
        self.position = 0
        self.pnl = 0
        self.fills = []
        self.fill_times = {}
        self.fill_page_size = 0
        self.fail_fill_page = None
        self.markets = [{
            "market": "ETH-USD.P", "baseIncrement": "0.001", "quoteIncrement": "0.1",
            "pair": {"base": "ETH", "quote": "USD"},
        }]

    def send(self, request, **kwargs):
        self.calls.append(request)
        path = unquote(urlsplit(request.url).path)
        query = parse_qs(urlsplit(request.url).query)
        body = request.body.decode() if request.body else ""
        if "ONDO-SIGN" in request.headers:
            message = request.headers["ONDO-TIMESTAMP"] + request.method + request.path_url + body
            expected = hmac.new(b"test-secret", message.encode(), hashlib.sha256).hexdigest()
            if request.headers["ONDO-SIGN"] != expected:
                raise AssertionError("Signature must cover the exact URI and body sent")
        status = 200
        page_info = None
        if path == "/v1/markets":
            result = {"perps": {"tradingPairs": self.markets}}
        elif path == "/v1/perps/depth":
            result = {"bids": [[str(self.price - 1), "1"]], "asks": [[str(self.price + 1), "1"]]} if not self.empty_book else {"bids": [], "asks": []}
        elif path == "/v1/perps/mark_prices":
            result = {m["market"]: {"markPrice": str(self.price)} for m in self.markets}
        elif path == "/v1/perps/balance":
            result = self.balance
        elif path == "/v1/perps/positions":
            result = [{
                "market": "ETH-USD.P", "netQuantity": str(abs(self.position)),
                "direction": "short" if self.position < 0 else "long",
                "unrealizedPnl": str(self.pnl), "liquidationPrice": "0",
            }]
        elif path == "/v1/perps/orders/batch" and request.method == "POST":
            self.post_count += 1
            if self.post_count == self.rate_limit_post:
                response = requests.Response()
                response.status_code = 429
                response._content = b"rate limited"  # 429 即便非 JSON 也必须明确识别。
                response._content_consumed = True
                return response
            items = json.loads(body)["orders"]
            if not 1 <= len(items) <= 20:
                raise AssertionError("Invalid placement batch size")
            added, failed = [], []
            for index, item in enumerate(items):
                if self.reject_all or (self.partial and index > 0):
                    failed.append({"order": item, "error": "insufficient margin", "errorCode": "insufficient_margin"})
                    continue
                order_id = str(len(self.orders) + 1)
                order = dict(item, orderId=order_id, status="open", filledSize="0")
                if self.cancel_on_post or self.partial_cancel_on_post:
                    order["status"] = "canceled"
                    order["cancelReason"] = "selfMatchPrevention"
                    if self.partial_cancel_on_post:
                        order["filledSize"] = "0.005"
                self.orders[order_id] = order
                added.append(dict(order))
            if self.timeout_after_post:
                raise requests.Timeout("response lost after acceptance")
            result = {} if self.omit_result else {
                "addedOrders": list(reversed(added)) if self.reverse else added,
                "failedOrders": failed,
            }
            if self.batch_result_transform is not None:
                result = self.batch_result_transform(result)
        elif path == "/v1/perps/orders/batch" and request.method == "DELETE":
            if body:
                raise AssertionError("Cancellation IDs belong in the query, not body")
            ids = query["orderIDs"][0].split(",")
            if not 1 <= len(ids) <= 20:
                raise AssertionError("Invalid cancellation batch size")
            successful, failed = [], []
            for order_id in ids:
                order = self.orders[order_id]
                if order["status"] != "open" or order_id in self.keep_open:
                    failed.append({"orderId": order_id, "errorCode": "order_not_in_cancelable_state"})
                else:
                    order["status"] = "canceled"
                    successful.append(dict(order))
            result = {"successfulCancels": successful, "failedCancels": failed}
        elif path == "/v1/perps/fills":
            start, end = int(query["startTime"][0]), int(query["endTime"][0])
            result = []
            for fill in self.fills:
                # 模拟服务按独立的数值时间筛选，原始字符串留给客户端解析。
                timestamp = (self.fill_times[fill["id"]] if fill["id"] in self.fill_times else
                             round(datetime.fromisoformat(fill["time"]).timestamp() * 1000))
                if start <= timestamp <= end:
                    result.append(dict(fill))
            result.reverse()
            if self.fill_page_size:
                start = int(query.get("cursor", ["0"])[0])
                if start == self.fail_fill_page:
                    raise requests.Timeout("fills page failed")
                end = start + self.fill_page_size
                if end < len(result): page_info = {"nextCursor": str(end)}
                result = result[start:end]
        elif path == "/v1/perps/orders":
            result = [dict(order) for order in self.orders.values() if order["status"] == "open"
                      and ("market" not in query or order["market"] == query["market"][0])]
            if self.page_size:
                start = int(query.get("cursor", ["0"])[0])
                end = start + self.page_size
                if end < len(result): page_info = {"nextCursor": str(end)}
                result = result[start:end]
        elif path.startswith("/v1/perps/orders/"):
            order_id = path.rsplit("/", 1)[1]
            if order_id.startswith("client:"):
                result = None if self.fail_confirm else next((dict(order) for order in self.orders.values() if order["clientOrderId"] == order_id[7:]), None)
            else:
                result = dict(self.orders[order_id]) if order_id in self.orders else None
            if result is None:
                status = 400
        else:
            raise AssertionError(f"Unexpected endpoint: {request.method} {path}")
        response = requests.Response()
        response.status_code = status
        response._content_consumed = True
        response._content = json.dumps(
            {"success": True, "result": result, "pageInfo": page_info} if status == 200 else
            {"success": False, "error_code": "order_not_found", "error": "not found"}
        ).encode()
        return response



class GridTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "SYMBOL1": "ETH", "gridLines1": "60", "gridStep1": "1", "orderSize1": "0.01",
            "lowLimit1": "1000", "highLimit1": "3000", "baseline1": "2000", "left1": "0",
            "maxposi1": "1", "MAX_LOSS1": "-100", "CRITICAL_ERROR_LIMIT": "3",
            "CHECK_INTERVAL": "0.8", "CANCEL_ON_EXIT": "false",
        }
        self.environment = patch.dict(os.environ, self.env, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.sleep_mock = patch("ondo_grid.time.sleep").start()
        self.addCleanup(patch.stopall)
        self.logger = patch("ondo_grid.logger").start()
        self.exchange_logger = patch("ondo_exchange.logger").start()
        patch("socket.socket.connect", side_effect=AssertionError("Tests cannot access the network")).start()
        self.venue = Venue()
        for base in ("BTC", "LTC", "SOL"):
            self.venue.markets.append({
                "market": base + "-USD.P", "baseIncrement": "0.001", "quoteIncrement": "0.1",
                "pair": {"base": base, "quote": "USD"},
            })
        self.exchange = OndoExchange("test-key", "test-secret")
        self.exchange._session.send = Mock(side_effect=self.venue.send)
        self.addCleanup(self.exchange.close)
        self.account = AccountTrader(self.exchange)
        self.grid = self.account.grids["ETH-USD.P"]

    def enable_three(self):
        for i, symbol in ((2, "BTC"), (3, "LTC")):
            for key, value in self.env.items():
                if key.endswith("1"):
                    os.environ[key[:-1] + str(i)] = symbol if key == "SYMBOL1" else value
        self.account = AccountTrader(self.exchange)
        self.grid = self.account.grids["ETH-USD.P"]

    def placed(self):
        orders = self.exchange.fetch_open_orders()
        self.account.accountInfo(orders)
        self.account.rebuild_grids(orders)
        self.account.submit_orders()
        return [c for c in self.venue.calls if c.method == "POST"]

    def run_loops(self, n):
        count = 0
        def sleep(seconds):
            nonlocal count
            if seconds == 0.8:
                count += 1
                if count >= n:
                    raise KeyboardInterrupt
        self.sleep_mock.side_effect = sleep
        with self.assertRaises(SystemExit) as stopped:
            self.account.run()
        self.assertEqual(stopped.exception.code, 0)

    def test_timeout_and_missing_response_confirm_without_reposting(self):
        for flag in ("timeout_after_post", "omit_result"):
            with self.subTest(flag=flag):
                self.venue.orders.clear()
                setattr(self.venue, flag, True)
                before = self.venue.post_count
                result = self.exchange.create_orders("ETH", [
                    {"side": "buy", "amount": "0.01", "price": "1999"},
                    {"side": "sell", "amount": "0.01", "price": "2001"},
                ])
                self.assertEqual(len(result["addedOrders"]), 2)
                self.assertEqual(result["failedOrders"], [])
                self.assertEqual(self.venue.post_count - before, 1)
                setattr(self.venue, flag, False)

    def test_optional_failure_collection_preserves_success_without_queries(self):
        for variant in ("empty", "missing", "null"):
            with self.subTest(variant=variant):
                def transform(result):
                    if variant == "missing":
                        result.pop("failedOrders")
                    else:
                        result["failedOrders"] = None if variant == "null" else []
                    return result
                self.venue.batch_result_transform = transform
                self.venue.calls.clear()
                self.exchange_logger.reset_mock()
                result = self.exchange.create_orders("ETH", [
                    {"side": "buy", "amount": "0.01", "price": "1999"},
                    {"side": "sell", "amount": "0.01", "price": "2001"},
                ])
                self.assertEqual(len(result["addedOrders"]), 2)
                self.assertEqual(result["failedOrders"], [])
                self.assertEqual([call.method for call in self.venue.calls], ["POST"])
                self.exchange_logger.warning.assert_not_called()

    def test_optional_success_collection_preserves_explicit_failures_without_queries(self):
        self.venue.reject_all = True
        for variant in ("empty", "missing", "null"):
            with self.subTest(variant=variant):
                def transform(result):
                    if variant == "missing":
                        result.pop("addedOrders")
                    else:
                        result["addedOrders"] = None if variant == "null" else []
                    return result
                self.venue.batch_result_transform = transform
                self.venue.calls.clear()
                self.exchange_logger.reset_mock()
                result = self.exchange.create_orders("ETH", [{"side": "buy", "amount": "0.01", "price": "1999"}])
                self.assertEqual(result["addedOrders"], [])
                self.assertEqual(result["failedOrders"][0]["errorCode"], "insufficient_margin")
                self.assertEqual([call.method for call in self.venue.calls], ["POST"])
                self.exchange_logger.warning.assert_not_called()

    def test_empty_optional_collections_still_confirm_every_unaccounted_order(self):
        for added in ("empty", "missing", "null"):
            for failed in ("empty", "missing", "null"):
                with self.subTest(added=added, failed=failed):
                    self.venue.batch_result_transform = lambda result: {
                        field: None if variant == "null" else []
                        for field, variant in (("addedOrders", added), ("failedOrders", failed))
                        if variant != "missing"
                    }
                    self.venue.calls.clear()
                    self.exchange_logger.reset_mock()
                    result = self.exchange.create_orders("ETH", [
                        {"side": "buy", "amount": "0.01", "price": "1999"},
                        {"side": "sell", "amount": "0.01", "price": "2001"},
                    ])
                    self.assertEqual(len(result["addedOrders"]), 2)
                    self.assertEqual(result["failedOrders"], [])
                    self.assertEqual([call.method for call in self.venue.calls], ["POST", "GET", "GET"])
                    self.exchange_logger.warning.assert_not_called()

    def test_non_object_batch_result_is_diagnosed_and_confirmed(self):
        for value in (None, [], False, "invalid"):
            with self.subTest(value=value):
                self.venue.batch_result_transform = lambda result: value
                self.venue.calls.clear()
                self.exchange_logger.reset_mock()
                result = self.exchange.create_orders("ETH", [{"side": "buy", "amount": "0.01", "price": "1999"}])
                self.assertEqual(len(result["addedOrders"]), 1)
                self.assertEqual([call.method for call in self.venue.calls], ["POST", "GET"])
                message = self.exchange_logger.warning.call_args_list[0].args[0]
                self.assertIn("field=result", message)
                self.assertIn(f"type={type(value).__name__}", message)

    def test_non_array_collections_are_not_silently_treated_as_empty(self):
        for field in ("addedOrders", "failedOrders"):
            for value in ({}, {"unexpected": []}, "", "invalid", False, True, 0, 2.5):
                with self.subTest(field=field, value=value):
                    def transform(result):
                        result[field] = value
                        return result
                    self.venue.batch_result_transform = transform
                    self.venue.calls.clear()
                    self.exchange_logger.reset_mock()
                    result = self.exchange.create_orders("ETH", [{"side": "buy", "amount": "0.01", "price": "1999"}])
                    self.assertEqual(len(result["addedOrders"]), 1)
                    expected_methods = ["POST", "GET"] if field == "addedOrders" else ["POST"]
                    self.assertEqual([call.method for call in self.venue.calls], expected_methods)
                    message = self.exchange_logger.warning.call_args_list[0].args[0]
                    self.assertIn(f"field={field}", message)
                    self.assertIn(f"type={type(value).__name__}", message)

    def test_invalid_success_item_only_confirms_affected_order(self):
        for variant in ("invalid_price", "missing_client", "null", "string", "list"):
            with self.subTest(variant=variant):
                affected = []
                def transform(result):
                    raw = result["addedOrders"][0]
                    affected.append(raw["clientOrderId"])
                    if variant == "invalid_price":
                        raw["price"] = "bad price"
                    elif variant == "missing_client":
                        raw.pop("clientOrderId")
                    else:
                        result["addedOrders"][0] = {"null": None, "string": "invalid", "list": []}[variant]
                    return result
                self.venue.batch_result_transform = transform
                self.venue.calls.clear()
                self.exchange_logger.reset_mock()
                result = self.exchange.create_orders("ETH", [
                    {"side": "buy", "amount": "0.01", "price": "1999"},
                    {"side": "sell", "amount": "0.01", "price": "2001"},
                ])
                self.assertEqual(len(result["addedOrders"]), 2)
                queries = [call for call in self.venue.calls if call.method == "GET"]
                self.assertEqual(len(queries), 1)
                self.assertTrue(queries[0].url.endswith("client:" + affected[0]))
                message = self.exchange_logger.warning.call_args_list[0].args[0]
                self.assertIn("field=addedOrders", message)
                self.assertIn("added_count=2, failed_count=0", message)

    def test_invalid_failure_item_preserves_other_success_and_failure(self):
        self.venue.partial = True
        def transform(result):
            result["failedOrders"][0].pop("order")
            return result
        self.venue.batch_result_transform = transform
        self.venue.calls.clear()
        result = self.exchange.create_orders("ETH", [
            {"side": "buy", "amount": "0.01", "price": str(price)} for price in (1999, 1998, 1997)
        ])
        self.assertEqual(len(result["addedOrders"]), 1)
        self.assertEqual({item["errorCode"] for item in result["failedOrders"]}, {"insufficient_margin", "order_unconfirmed"})
        submitted = json.loads(self.venue.calls[0].body)["orders"]
        queries = [call for call in self.venue.calls if call.method == "GET"]
        self.assertEqual(len(queries), 3)
        self.assertTrue(all(call.url.endswith("client:" + submitted[1]["clientOrderId"]) for call in queries))

    def test_repeated_client_result_confirms_only_that_client(self):
        def transform(result):
            result["addedOrders"].append(dict(result["addedOrders"][0]))
            return result
        self.venue.batch_result_transform = transform
        self.venue.calls.clear()
        result = self.exchange.create_orders("ETH", [
            {"side": "buy", "amount": "0.01", "price": str(price)} for price in (1999, 1998)
        ])
        self.assertEqual(len(result["addedOrders"]), 2)
        self.assertEqual([call.method for call in self.venue.calls], ["POST", "GET"])

    def test_success_failure_conflict_queries_only_conflicting_client(self):
        self.venue.partial = True
        def transform(result):
            result["failedOrders"].append({"order": dict(result["addedOrders"][0]), "errorCode": "conflict"})
            return result
        self.venue.batch_result_transform = transform
        self.venue.calls.clear()
        result = self.exchange.create_orders("ETH", [
            {"side": "buy", "amount": "0.01", "price": str(price)} for price in (1999, 1998, 1997)
        ])
        self.assertEqual(len(result["addedOrders"]), 1)
        self.assertEqual(len(result["failedOrders"]), 2)
        self.assertTrue(all(item["errorCode"] == "insufficient_margin" for item in result["failedOrders"]))
        self.assertEqual([call.method for call in self.venue.calls], ["POST", "GET"])

    def test_shared_order_id_confirms_both_clients_preserving_unrelated_success(self):
        def transform(result):
            result["addedOrders"][0]["orderId"] = result["addedOrders"][1]["orderId"]
            return result
        self.venue.batch_result_transform = transform
        self.venue.calls.clear()
        result = self.exchange.create_orders("ETH", [
            {"side": "buy", "amount": "0.01", "price": str(price)} for price in (1999, 1998, 1997)
        ])
        self.assertEqual(len({item["id"] for item in result["addedOrders"]}), 3)
        self.assertEqual(result["failedOrders"], [])
        self.assertEqual([call.method for call in self.venue.calls], ["POST", "GET", "GET"])

    def test_canceled_zero_fill_is_explicit_failure_in_batch_and_confirmation(self):
        self.venue.cancel_on_post = True
        for lost_response in (False, True):
            with self.subTest(lost_response=lost_response):
                self.venue.timeout_after_post = lost_response
                result = self.exchange.create_orders("ETH", [{"side": "buy", "amount": "0.01", "price": 1999}])
                self.assertEqual(result["addedOrders"], [])
                self.assertEqual(result["failedOrders"][0]["errorCode"], "order_canceled")
                self.assertIn("selfMatchPrevention", result["failedOrders"][0]["error"])

    def test_empty_batches_do_not_send_requests(self):
        before = len(self.venue.calls)
        self.assertEqual(self.exchange.create_orders("ETH", []), {"addedOrders": [], "failedOrders": []})
        self.exchange.cancel_orders("ETH", [])
        self.assertEqual(len(self.venue.calls), before)

    def test_batch_size_and_precision_checked_before_post(self):
        with self.assertRaises(ValueError):
            self.exchange.create_orders("ETH", [{}] * 21)
        with self.assertRaises(ValueError):
            self.exchange.cancel_orders("ETH", range(21))
        with self.assertRaises(ValueError):
            self.exchange.create_orders("ETH", [{"side": "buy", "amount": "0.0001", "price": 1999}])
        self.assertEqual(self.exchange.amount_to_precision("ETH", "0.0129"), "0.012")
        self.assertEqual(self.exchange.price_to_precision("ETH", "1999.15"), "1999.2")
        self.assertEqual(self.venue.post_count, 0)

    def test_rate_limit_is_explicit_and_does_not_trigger_confirmation_reads(self):
        self.venue.rate_limit_post = 1
        with self.assertRaises(OndoAPIError) as rejected:
            self.exchange.create_orders("ETH", [{"side": "buy", "amount": "0.01", "price": 1999}])
        self.assertEqual(rejected.exception.status, 429)
        self.assertFalse(rejected.exception.uncertain)
        self.assertEqual(self.venue.post_count, 1)
        self.assertFalse(any("client:" in call.url for call in self.venue.calls))

    def test_three_markets_share_reads_and_mix_batches(self):
        self.enable_three()
        self.run_loops(2)
        calls = self.venue.calls
        for path in ("/v1/perps/positions", "/v1/perps/balance", "/v1/markets"):
            # load_markets was called once per AccountTrader construction in setup.
            expected = 2 if path == "/v1/markets" else 1
            self.assertEqual(sum(urlsplit(c.url).path == path for c in calls), expected)
        reads = [c for c in calls if urlsplit(c.url).path == "/v1/perps/orders"]
        self.assertEqual(len(reads), 2)
        self.assertTrue(all("market" not in parse_qs(urlsplit(c.url).query) for c in reads))
        posts = [c for c in calls if c.method == "POST"]
        self.assertEqual(len(posts), 9)
        first = json.loads(posts[0].body)["orders"]
        self.assertEqual([o["market"] for o in first[:3]], list(self.account.grids))
        self.assertEqual(len({o["clientOrderId"] for c in posts for o in json.loads(c.body)["orders"]}), 180)
        for symbol, grid in self.account.grids.items():
            self.assertEqual(len(grid.All_orders), 60)
            self.assertTrue(all(self.venue.orders[oid]["market"] == symbol for oid in grid.All_orders))

    def test_numbered_configuration_and_duplicate_alias_validation(self):
        os.environ["SYMBOL2"] = ""
        self.assertEqual(len(AccountTrader(self.exchange).grids), 1)
        os.environ["SYMBOL2"] = "BTC"
        with self.assertRaises(KeyError):
            AccountTrader(self.exchange)
        self.enable_three()
        os.environ["SYMBOL2"] = "ETH/USDT:USDT"
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            AccountTrader(self.exchange)
        self.assertEqual(self.venue.post_count, 0)

    def test_configuration_invalid_before_mutations(self):
        for key, value in (("gridLines1", "3"), ("gridStep1", "0.05"),
                           ("orderSize1", "0.0001"), ("maxposi1", "0"),
                           ("lowLimit1", "inf"), ("MAX_LOSS1", "nan"),
                           ("MMR", "0"), ("leverage", "nan"), ("CHECK_INTERVAL", "0"),
                           ("interPatch", "-0.1"), ("interPatch", "nan"), ("interPatch", "inf")):
            with self.subTest(key=key), patch.dict(os.environ, {key: value}):
                with self.assertRaises((ValueError, KeyError)):
                    AccountTrader(self.exchange)
        self.assertFalse(any(c.method in {"POST", "DELETE"} for c in self.venue.calls))

    def test_account_pagination_and_repeated_cursor(self):
        self.enable_three()
        self.placed()
        self.venue.page_size = 37
        self.venue.calls.clear()
        orders = self.exchange.fetch_open_orders()
        self.assertEqual(len(orders), 180)
        self.assertEqual(len(self.venue.calls), 5)
        page = {"result": [], "pageInfo": {"nextCursor": "repeated"}}
        with patch.object(self.exchange, "_request", return_value=page):
            with self.assertRaisesRegex(RuntimeError, "repeated"):
                self.exchange.fetch_open_orders()
        with patch.object(self.exchange, "_request", return_value={"result": None}):
            with self.assertRaises(ValueError):
                self.exchange.fetch_open_orders()

    def test_account_risk_thresholds_cancel_only_configured_markets(self):
        self.enable_three()
        self.placed()
        unrelated = self.exchange.create_orders("SOL", [{"side": "buy", "amount": "0.01", "price": "2000"}])
        other_id = unrelated["addedOrders"][0]["id"]
        snapshot = self.exchange.fetch_open_orders()
        for updates in ({"marginRatio": "0.2"}, {"marginRatio": "0.21"},
                        {"leverage": "5"}, {"leverage": "5.1"},
                        {"marginBalance": "0"}, {"marginBalance": "-1"},
                        {"underLiquidation": True}):
            with self.subTest(updates=updates):
                self.venue.balance = dict(marginRatio="0.1", leverage="2", marginBalance="1000", underLiquidation=False, **{})
                self.venue.balance.update(updates)
                with self.assertRaises(SystemExit) as stopped:
                    self.account.accountInfo(snapshot)
                self.assertEqual(stopped.exception.code, 1)
                self.assertEqual(self.venue.orders[other_id]["status"], "open")
        self.assertFalse(any(o["status"] == "open" and o["market"] != "SOL-USD.P" for o in self.venue.orders.values()))

    def test_invalid_account_metrics_never_trade(self):
        for key, value in (("marginRatio", "nan"), ("leverage", "inf"),
                           ("marginBalance", "bad"), ("underLiquidation", "false")):
            with self.subTest(key=key), patch.dict(self.venue.balance, {key: value}):
                with self.assertRaises(ValueError):
                    self.account.accountInfo([])
        for key in ("marginRatio", "leverage", "marginBalance", "underLiquidation"):
            balance = dict(self.venue.balance)
            del balance[key]
            with self.subTest(missing=key), patch.object(self.exchange, "fetch_balance", return_value=balance):
                with self.assertRaises((KeyError, ValueError)):
                    self.account.accountInfo([])
        self.assertEqual(self.venue.post_count, 0)

    def test_account_and_fills_checks_every_fifty_loops(self):
        self.run_loops(101)
        for path in ("/v1/perps/balance", "/v1/perps/positions", "/v1/perps/fills"):
            self.assertEqual(sum(urlsplit(c.url).path == path for c in self.venue.calls), 3)
        self.assertEqual(self.account.myloop, 101)
        self.assertEqual(sum(urlsplit(c.url).path == "/v1/perps/orders" for c in self.venue.calls), 101)
        tables = [c for c in self.logger.info.call_args_list if "交易量(U)" in c.args[0]]
        self.assertEqual(len(tables), 3)

    def test_price_fallback_shared_and_midpoint_preferred(self):
        self.enable_three()
        self.account.accountInfo([])
        self.assertFalse(any("mark_prices" in c.url for c in self.venue.calls))
        self.venue.empty_book = True
        self.account.accountInfo([])
        self.assertEqual(sum("mark_prices" in c.url for c in self.venue.calls), 1)
        self.assertTrue(all(g.myNewPrice == 2000 for g in self.account.grids.values()))

    def test_batch_spacing_for_placement_and_cancellation(self):
        self.assertEqual(self.account.inter_patch, 0.2)
        for interval in (0.2, 0.35, 0):
            with patch.dict(os.environ, {"interPatch": str(interval)}):
                self.account = AccountTrader(self.exchange)
                self.grid = self.account.grids["ETH-USD.P"]
            for total, waits in ((20, 0), (40, 1), (41, 2), (60, 2)):
                with self.subTest(interval=interval, total=total):
                    self.venue.orders.clear()
                    self.grid.All_orders.clear()
                    self.grid.switc = False
                    self.grid.pending_orders = [
                        {"symbol": self.grid.symbol, "side": "buy", "amount": "0.01",
                         "price": 1900 + i, "edge": 0} for i in range(total)
                    ]
                    self.sleep_mock.reset_mock()
                    self.account.submit_orders()
                    self.assertEqual(self.sleep_mock.call_args_list, [call(interval)] * waits)
                    self.assertEqual(len(self.grid.All_orders), total)
                    self.sleep_mock.reset_mock()
                    self.account.cancel_open_orders("spacing")
                    self.assertEqual(self.sleep_mock.call_args_list, [call(interval)] * waits)
                    self.assertEqual(self.exchange.fetch_open_orders(), [])

    def test_mixed_markets_precision_and_confirmation(self):
        self.exchange._markets["BTC-USD.P"]["precision"] = {"amount": "0.01", "price": "1"}
        self.venue.omit_result = True
        result = self.exchange.create_orders(None, [
            {"symbol": "ETH", "side": "buy", "amount": "0.0129", "price": "1999.15"},
            {"symbol": "BTC", "side": "sell", "amount": "0.0129", "price": "1999.15"},
        ])
        by_symbol = {o["symbol"]: o for o in result["addedOrders"]}
        self.assertEqual((by_symbol["ETH-USD.P"]["amount"], by_symbol["ETH-USD.P"]["price"]), (0.012, 1999.2))
        self.assertEqual((by_symbol["BTC-USD.P"]["amount"], by_symbol["BTC-USD.P"]["price"]), (0.01, 1999.0))
        self.exchange.cancel_orders(None, result["addedOrders"])
        self.assertEqual(self.exchange.fetch_open_orders(), [])

    def test_cross_market_mismatched_response_is_confirmed(self):
        def transform(result):
            result["addedOrders"][0]["market"] = "SOL-USD.P"
            return result
        self.venue.batch_result_transform = transform
        result = self.exchange.create_orders(None, [
            {"symbol": "ETH", "side": "buy", "amount": "0.01", "price": "1999"},
            {"symbol": "BTC", "side": "sell", "amount": "0.01", "price": "2001"},
        ])
        self.assertEqual({o["symbol"] for o in result["addedOrders"]}, {"ETH-USD.P", "BTC-USD.P"})
        self.assertEqual(sum("client:" in c.url for c in self.venue.calls), 1)

    def test_partial_failure_retries_only_remaining_and_no_duplicate_queue(self):
        self.venue.partial = True
        self.placed()
        self.assertEqual(len(self.grid.All_orders), 3)
        self.assertEqual(len(self.grid.pending_orders), 57)
        old_ids = set(self.grid.All_orders)
        self.venue.orders[next(iter(old_ids))]["status"] = "fullyfilled"
        self.grid.poller(self.exchange.fetch_open_orders())
        self.grid.poller(self.exchange.fetch_open_orders())
        self.assertEqual(len([item for item in self.grid.pending_orders if "old_id" in item]), 1)
        self.venue.partial = False
        self.account.submit_orders()
        self.assertEqual(len(self.grid.All_orders), 60)
        self.assertEqual(len(self.grid.pending_orders), 0)
        self.assertEqual(self.grid.counter, 1)

    def test_multiple_fills_cross_market_batch_and_reference_order(self):
        self.enable_three()
        self.placed()
        for grid in self.account.grids.values():
            chosen = [oid for oid in grid.All_orders if not grid.All_orders[oid][0]][:2]
            for oid in chosen:
                self.venue.orders[oid]["status"] = "fullyfilled"
            grid.poller([o for o in self.exchange.fetch_open_orders() if o["symbol"] == grid.symbol])
            self.assertEqual([i["side"] for i in grid.pending_orders], ["sell", "buy"])
            self.assertEqual([i["price"] for i in grid.pending_orders], [2000, 2000])
        before = self.venue.post_count
        self.account.submit_orders()
        self.assertEqual(self.venue.post_count - before, 1)
        self.assertTrue(all(g.counter == 2 and g.last_fill_price == 2001 for g in self.account.grids.values()))

    def test_edge_fill_skips_same_market_replacements_but_other_markets_continue(self):
        self.enable_three()
        self.placed()
        normal = next(oid for oid, state in self.grid.All_orders.items() if not state[0])
        edge = next(oid for oid, state in self.grid.All_orders.items() if state[0])
        for oid in [normal, edge]:
            self.venue.orders[oid]["status"] = "fullyfilled"
        btc = self.account.grids["BTC-USD.P"]
        btc_oid = next(oid for oid, state in btc.All_orders.items() if not state[0])
        self.venue.orders[btc_oid]["status"] = "fullyfilled"
        snapshot = self.exchange.fetch_open_orders()
        for symbol, grid in self.account.grids.items():
            grid.poller([o for o in snapshot if o["symbol"] == symbol])
        self.assertTrue(self.grid.switc)
        self.assertEqual(self.grid.pending_orders, [])
        self.account.submit_orders()
        self.assertEqual(btc.counter, 1)
        self.account.rebuild_grids(self.exchange.fetch_open_orders())
        self.assertEqual(len(self.grid.pending_orders), 60)
        self.assertFalse(self.grid.switc)
        self.assertEqual(len(btc.All_orders), 60)

    def test_cancel_failure_preserves_old_state_and_attempts_other_batches(self):
        self.placed()
        self.venue.keep_open.add(next(iter(self.grid.All_orders)))
        self.grid.switc = True
        old_state = dict(self.grid.All_orders)
        old_queue = [{"side": "buy", "price": 2000, "edge": 0, "symbol": self.grid.symbol}]
        self.grid.pending_orders = old_queue
        with self.assertRaisesRegex(RuntimeError, "remaining=1"):
            self.account.rebuild_grids(self.exchange.fetch_open_orders())
        self.assertEqual(self.grid.All_orders, old_state)
        self.assertIs(self.grid.pending_orders, old_queue)
        self.assertTrue(self.grid.switc)
        self.assertEqual(sum(c.method == "DELETE" for c in self.venue.calls), 3)

    def test_invalid_grid_does_not_cancel_existing_orders(self):
        self.placed()
        before = sum(c.method == "DELETE" for c in self.venue.calls)
        self.grid.switc = True
        self.grid.myNewPrice = 1
        with self.assertRaises(ValueError):
            self.account.rebuild_grids(self.exchange.fetch_open_orders())
        self.assertEqual(sum(c.method == "DELETE" for c in self.venue.calls), before)

    def test_cancel_terminal_and_partial_cleanup(self):
        self.placed()
        ids = list(self.grid.All_orders)
        self.venue.orders[ids[0]]["status"] = "fullyfilled"
        self.venue.orders[ids[1]]["status"] = "canceled"
        self.exchange.cancel_orders("ETH", ids[:3])
        self.venue.orders[ids[3]]["filledSize"] = "0.005"
        self.account.cancel_open_orders("cleanup")
        self.assertEqual(self.exchange.fetch_open_orders(), [])

    def test_partial_fill_canceled_during_placement_is_fatal(self):
        self.venue.partial_cancel_on_post = True
        with self.assertRaises(SystemExit) as stopped:
            self.account.run()
        self.assertEqual(stopped.exception.code, 1)
        self.assertEqual(self.venue.post_count, 1)

    def test_429_retains_batch_and_continues_without_confirmation(self):
        self.venue.rate_limit_post = 2
        self.placed()
        self.assertEqual(len(self.grid.All_orders), 40)
        self.assertEqual(len(self.grid.pending_orders), 20)
        self.assertEqual(self.venue.post_count, 3)
        self.assertFalse(any("client:" in c.url for c in self.venue.calls))
        self.account.submit_orders()
        self.assertEqual(len(self.grid.All_orders), 60)

    def test_unconfirmed_replacement_retains_old_then_recovers(self):
        self.placed()
        oid = next(oid for oid, state in self.grid.All_orders.items() if not state[0])
        self.venue.orders[oid]["status"] = "fullyfilled"
        self.grid.poller(self.exchange.fetch_open_orders())
        self.venue.reject_all = True
        self.venue.omit_result = True
        self.venue.fail_confirm = True
        self.account.submit_orders()
        previous_client = self.grid.pending_orders[0]["clientOrderId"]
        self.assertIn(oid, self.grid.All_orders)
        self.grid.poller(self.exchange.fetch_open_orders())
        self.assertEqual(len(self.grid.pending_orders), 1)
        self.venue.reject_all = self.venue.omit_result = self.venue.fail_confirm = False
        self.account.submit_orders()
        self.assertNotIn(oid, self.grid.All_orders)
        self.assertEqual(self.grid.counter, 1)
        latest = json.loads([c for c in self.venue.calls if c.method == "POST"][-1].body)
        self.assertNotEqual(latest["orders"][0]["clientOrderId"], previous_client)

    def test_all_placement_failures_do_not_skip_account_risk(self):
        self.venue.reject_all = True
        self.run_loops(51)
        self.assertEqual(len(self.grid.pending_orders), 60)
        self.assertEqual(self.account.critical_errors, 0)
        self.assertEqual(sum("perps/balance" in c.url for c in self.venue.calls), 2)

    def test_consecutive_read_failures_stop_without_placement(self):
        with patch.object(self.exchange, "fetch_open_orders", side_effect=OndoAPIError("read failed")):
            with self.assertRaises(SystemExit) as stopped:
                self.account.run()
        self.assertEqual(stopped.exception.code, 1)
        self.assertEqual(self.account.critical_errors, 3)
        self.assertEqual(self.venue.post_count, 0)

    def test_missing_snapshot_is_not_treated_as_fills(self):
        self.placed()
        old = dict(self.grid.All_orders)
        with patch.object(self.exchange, "fetch_open_orders", return_value=None):
            with self.assertRaises(SystemExit):
                self.account.run()
        self.assertEqual(self.grid.All_orders, old)
        self.assertEqual(self.grid.counter, 0)

    def test_pending_does_not_bypass_position_or_excess_order_risk(self):
        self.placed()
        self.grid.pending_orders = [{"symbol": self.grid.symbol}]
        with patch.dict(self.venue.balance, {"leverage": "1"}):
            self.venue.position = 2
            with self.assertRaises(SystemExit):
                self.account.accountInfo(self.exchange.fetch_open_orders())
        self.venue.position = 0
        with self.assertRaises(SystemExit):
            self.account.accountInfo([{"symbol": self.grid.symbol}] * 65)

    def test_three_open_orders_continue_but_two_stop_between_account_checks(self):
        self.grid.switc = False
        self.account.myloop = 1
        self.exchange.create_orders("ETH", [
            {"side": "buy", "amount": "0.01", "price": price} for price in (1997, 1998, 1999)
        ])
        self.run_loops(1)
        orders = self.exchange.fetch_open_orders()
        self.assertEqual(len(orders), 3)
        self.assertEqual(self.account.myloop, 2)
        self.assertFalse(any("perps/balance" in c.url for c in self.venue.calls))
        self.exchange.cancel_orders("ETH", [orders[0]["id"]])
        with self.assertRaises(SystemExit) as stopped:
            self.account.run()
        self.assertEqual(stopped.exception.code, 1)
        self.assertEqual(self.exchange.fetch_open_orders(), [])

    def test_single_market_risk_stops_all_and_too_few_resumes_after_queue_drains(self):
        self.enable_three()
        self.placed()
        self.venue.pnl = -101
        with self.assertRaises(SystemExit):
            self.account.accountInfo(self.exchange.fetch_open_orders())
        self.assertEqual(self.exchange.fetch_open_orders(), [])
        self.venue.pnl = 0
        for grid in self.account.grids.values():
            grid.switc = False
        with self.assertRaises(SystemExit):
            self.account.run()

    def test_ctrl_c_cancellation_switch_and_scope(self):
        self.enable_three()
        self.placed()
        self.exchange.create_orders("SOL", [{"side": "buy", "amount": "0.01", "price": "2000"}])
        self.account.cancel_on_exit = True
        self.run_loops(1)
        self.assertEqual({o["symbol"] for o in self.exchange.fetch_open_orders()}, {"SOL-USD.P"})

    def test_console_hides_ids_while_file_records_keep_details(self):
        self.venue.rate_limit_post = 1
        self.placed()
        self.account.critical("主循环异常", RuntimeError("order=SECRET_ORDER_ID client=SECRET_CLIENT_ID"))
        records = []
        for level in ("info", "warning", "error"):
            for invocation in getattr(self.logger, level).call_args_list:
                record = logging.LogRecord("grid", getattr(logging, level.upper()), "", 1, invocation.args[0], (), None)
                for key, value in invocation.kwargs.get("extra", {}).items():
                    setattr(record, key, value)
                records.append(record)
        console = "\n".join(ConsoleFormatter().format(r) for r in records)
        detailed = "\n".join(r.getMessage() for r in records)
        self.assertNotIn("SECRET_ORDER_ID", console)
        self.assertNotIn("SECRET_CLIENT_ID", console)
        self.assertIn("SECRET_ORDER_ID", detailed)

    def test_entrypoint_closes_client_on_startup_failure(self):
        handlers = []
        def file_handler(*args, **kwargs):
            handler = logging.NullHandler()
            handlers.append(handler)
            return handler
        with patch("dotenv.load_dotenv"), patch("logging.FileHandler", side_effect=file_handler), \
             patch("ondo_exchange.OndoExchange.from_env", return_value=self.exchange), \
             patch.dict(os.environ, {"SYMBOL1": ""}):
            with self.assertRaises(ValueError):
                runpy.run_path(str(Path(__file__).resolve().parents[1] / "ondo_grid.py"), run_name="__main__")
        self.assertTrue(self.exchange._closed)
        for name in ("ondo_exchange", "__main__"):
            logging.getLogger(name).handlers.clear()

    def record_fill(self, order_id, size="0.01", price=None, fee="0", maker=True, rebate="0", offset=1000):
        order = self.venue.orders[order_id]
        size, price = Decimal(size), Decimal(str(price if price is not None else order["price"]))
        fill = {"id": f"fill-{len(self.venue.fills) + 1}", "orderId": order_id,
                "clientOrderId": order["clientOrderId"], "market": order["market"], "side": order["side"],
                "price": str(price), "size": str(size), "filledCost": str(size * price),
                "fee": fee, "feeRebate": rebate, "isMaker": maker,
                "time": datetime.fromtimestamp((self.account.stats_started + offset) / 1000, timezone.utc).isoformat()}
        self.venue.fills.append(fill)
        self.venue.fill_times[fill["id"]] = self.account.stats_started + offset
        order["filledSize"] = str(Decimal(order["filledSize"]) + size)
        if Decimal(order["filledSize"]) == Decimal(order["size"]):
            order["status"] = "fullyfilled"
        return fill

    def refresh_stats_at(self, offset=10000):
        with patch("ondo_grid.time.time", return_value=(self.account.stats_started + offset) / 1000):
            self.account.refresh_stats()
        self.assertFalse(self.account.stats_stale)

    def replace_filled(self):
        before = set(self.grid.All_orders)
        self.grid.poller(self.exchange.fetch_open_orders())
        self.account.submit_orders()
        created = set(self.grid.All_orders) - before
        self.assertEqual(len(created), 1)
        return created.pop()

    def test_stats_real_nanosecond_timestamps_update_all_columns(self):
        self.placed()
        self.account.stats_started = int(datetime(2026, 11, 6, 19, tzinfo=timezone.utc).timestamp() * 1000)
        self.account.stats_until = self.account.stats_started
        samples = [
            ("2026-11-06T19:03:13.793836979+00:00", 193793),
            ("2026-11-06T19:03:56.241825909+00:00", 236241),
            ("2026-11-06T19:04:37.657815764+00:00", 277657),
            ("2026-11-06T19:05:25.093803626+00:00", 325093),
            ("2026-11-06T19:06:12.693791391+00:00", 372693),
        ]
        order_id = next(oid for oid, state in self.grid.All_orders.items() if not state[0] and state[2] == "buy")
        for index, (stamp, offset) in enumerate(samples):
            fill = self.record_fill(order_id, fee="0.001", maker=index % 2 == 0, offset=offset)
            fill["time"] = stamp
            if index < 3:
                order_id = self.replace_filled()
            elif index == 3:
                order_id = next(oid for oid, state in self.grid.All_orders.items()
                                if not state[0] and state[2] == "sell" and self.grid.order_stats[oid]["entry"])
        self.refresh_stats_at(400000)
        self.assertEqual(self.grid.stats, dict(volume=Decimal("99.99"), fees=Decimal("0.005"),
                                              profit=Decimal("0.015"), makers=3, takers=2))
        for index, (_, offset) in enumerate(samples, 1):
            self.assertEqual(self.account.stats_fills[f"fill-{index}"]["time"], self.account.stats_started + offset)
        self.account.display(self.exchange.fetch_open_orders())
        self.assertNotIn("统计滞后", self.logger.info.call_args.args[0])

    def test_stats_timestamp_precisions_zones_and_millisecond_boundaries(self):
        self.placed()
        self.account.stats_started = int(datetime(2026, 11, 6, 19, tzinfo=timezone.utc).timestamp() * 1000)
        self.account.stats_until = self.account.stats_started
        order_id = next(iter(self.grid.All_orders))
        for head, zone in (("2026-11-06T19:00:01", "Z"), ("2026-11-06T19:00:01", "+00:00"),
                           ("2026-11-07T00:30:01", "+05:30"), ("2026-11-06T15:00:01", "-04:00")):
            for fraction in ("", "1", "12", "123", "1234", "12345", "123456", "1234567",
                             "12345678", "123456789", "999999999", "000000001"):
                with self.subTest(zone=zone, fraction=fraction):
                    self.venue.fills.clear()
                    self.venue.fill_times.clear()
                    self.account.stats_fills.clear()
                    self.venue.orders[order_id]["filledSize"] = "0"
                    offset = 1000 + int((fraction + "000")[:3])
                    fill = self.record_fill(order_id, size="0.001", offset=offset)
                    fill["time"] = head + ("." + fraction if fraction else "") + zone
                    self.refresh_stats_at(10000)
                    self.assertEqual(self.account.stats_fills[fill["id"]]["time"], self.account.stats_started + offset)
                    self.assertEqual(self.grid.stats["volume"], Decimal("1.999"))
        # 用真实纳秒样例跨过毫秒窗口边界，不能把 .999999999 四舍五入到下一秒。
        self.account.stats_until = self.account.stats_started
        self.account.stats_fills.clear()
        fill["time"] = "2026-11-06T19:00:01.999999999Z"
        self.venue.fill_times[fill["id"]] = self.account.stats_started + 1999
        self.refresh_stats_at(1998)
        self.assertEqual(self.grid.stats["volume"], 0)
        self.refresh_stats_at(1999)
        self.assertEqual(self.account.stats_fills[fill["id"]]["time"], self.account.stats_started + 1999)
        self.refresh_stats_at(2000)
        self.assertEqual(len(self.account.stats_fills), 1)
        self.assertEqual(self.grid.stats["volume"], Decimal("1.999"))

    def test_stats_invalid_timestamp_keeps_previous_snapshot(self):
        self.placed()
        fill = self.record_fill(next(iter(self.grid.All_orders)), size="0.001")
        self.refresh_stats_at()
        previous, watermark = dict(self.grid.stats), self.account.stats_until
        for stamp in ("2026-11-06T19:00:01.123456789", "2026-02-30T19:00:01.123456789Z",
                      "2026-11-06T19:00:01.123456789+25:00", "2026-11-06T19:00:01.badZ",
                      "2026-11-06T19:00:01.1234567890Z"):
            with self.subTest(stamp=stamp), patch.dict(fill, {"time": stamp}), \
                 patch("ondo_grid.time.time", return_value=(self.account.stats_started + 10000) / 1000):
                self.account.refresh_stats()
                self.assertTrue(self.account.stats_stale)
                self.assertEqual(self.grid.stats, previous)
                self.assertEqual(self.account.stats_until, watermark)

    def test_stats_partial_mixed_roles_and_signed_fees(self):
        self.placed()
        oid = next(oid for oid, state in self.grid.All_orders.items() if not state[0] and state[2] == "buy")
        self.record_fill(oid, "0.004", "1999", fee="0.001", maker=False)
        self.record_fill(oid, "0.003", "1999", fee="-0.0002", rebate="0.0001")
        self.record_fill(oid, "0.003", "1999", fee="0.001")
        self.refresh_stats_at()
        self.assertEqual(self.grid.stats, dict(volume=Decimal("19.990"), fees=Decimal("0.0017"),
                                              profit=Decimal("-0.0017"), makers=1, takers=1))
        self.refresh_stats_at(20000)
        self.assertEqual(self.grid.stats["volume"], Decimal("19.990"))
        self.assertEqual(len(self.account.stats_fills), 3)

    def test_stats_round_trips_use_actual_prices_without_reusing_close(self):
        self.placed()
        entry = next(oid for oid, state in self.grid.All_orders.items() if not state[0] and state[2] == "buy")
        self.record_fill(entry, price="1998.8", fee="0.002")
        close = self.replace_filled()
        self.record_fill(close, "0.004", "2000.2", fee="0.001", maker=False, offset=2000)
        self.refresh_stats_at()
        self.assertEqual(self.grid.stats["profit"], Decimal("0.0026"))
        self.record_fill(close, "0.006", "2000.2", fee="0.001", offset=3000)
        next_entry = self.replace_filled()
        self.record_fill(next_entry, price="1999", fee="0.002", offset=4000)
        self.refresh_stats_at(20000)
        self.assertEqual(self.grid.stats["profit"], Decimal("0.008"))
        next_close = self.replace_filled()
        self.record_fill(next_close, price="2000", fee="0.002", offset=5000)
        self.refresh_stats_at(30000)
        self.assertEqual(self.grid.stats["profit"], Decimal("0.016"))
        self.assertEqual(self.grid.stats["volume"], Decimal("79.980"))
        self.assertEqual((self.grid.stats["makers"], self.grid.stats["takers"]), (4, 1))

    def test_stats_short_first_and_late_entry_are_order_independent(self):
        self.placed()
        entry = next(oid for oid, state in self.grid.All_orders.items() if not state[0] and state[2] == "sell")
        opening_fill = self.record_fill(entry, price="2001", fee="0.001")
        close = self.replace_filled()
        self.record_fill(close, price="2000", fee="0.001", offset=2000)
        self.venue.fills.remove(opening_fill)
        self.refresh_stats_at()
        self.assertEqual(self.grid.stats["profit"], Decimal("-0.001"))
        self.venue.fills.append(opening_fill)
        self.refresh_stats_at(20000)
        self.assertEqual(self.grid.stats["profit"], Decimal("0.008"))

    def test_stats_rebuild_keeps_totals_but_never_matches_old_inventory(self):
        self.placed()
        entry = next(oid for oid, state in self.grid.All_orders.items() if not state[0] and state[2] == "buy")
        self.record_fill(entry, price="1999", fee="0.001")
        self.refresh_stats_at()
        self.grid.switc = True
        self.grid.myNewPrice = 2100
        self.account.rebuild_grids(self.exchange.fetch_open_orders())
        self.account.submit_orders()
        new_sell = next(oid for oid, state in self.grid.All_orders.items() if not state[0] and state[2] == "sell")
        self.record_fill(new_sell, price="2101", fee="0.001", offset=2000)
        self.refresh_stats_at(20000)
        self.assertEqual(self.grid.stats["profit"], Decimal("-0.002"))
        self.assertEqual(self.grid.stats["volume"], Decimal("41.00"))
        self.assertIn(entry, self.grid.order_stats)

    def test_stats_account_shared_pagination_and_ignores_manual_fills(self):
        self.enable_three()
        self.placed()
        for grid in self.account.grids.values():
            self.record_fill(next(iter(grid.All_orders)))
        manual = dict(self.venue.fills[0], id="manual", orderId="manual", clientOrderId="manual")
        self.venue.fills.extend([manual, dict(self.venue.fills[0])])
        self.venue.fill_page_size = 2
        self.venue.calls.clear()
        self.refresh_stats_at()
        self.assertEqual(len(self.account.stats_fills), 3)
        self.assertTrue(all(grid.stats["makers"] == 1 for grid in self.account.grids.values()))
        reads = [c for c in self.venue.calls if urlsplit(c.url).path == "/v1/perps/fills"]
        self.assertEqual(len(reads), 3)
        self.assertTrue(all("market" not in parse_qs(urlsplit(c.url).query) for c in reads))
        self.assertEqual(len(self.venue.calls), 3)

    def test_stats_retries_and_unconfirmed_clients_do_not_double_match(self):
        self.placed()
        entry = next(oid for oid, state in self.grid.All_orders.items() if not state[0] and state[2] == "buy")
        self.record_fill(entry, fee="0.001")
        self.grid.poller(self.exchange.fetch_open_orders())
        self.venue.omit_result = self.venue.fail_confirm = True
        before = set(self.venue.orders)
        self.account.submit_orders()
        uncertain = (set(self.venue.orders) - before).pop()
        self.assertNotIn(uncertain, self.grid.order_stats)
        self.record_fill(uncertain, price="2000", fee="0.001", offset=2000)
        self.refresh_stats_at()
        self.assertIn(uncertain, self.grid.order_stats)
        self.assertEqual(self.grid.stats["profit"], Decimal("0.008"))
        self.venue.omit_result = self.venue.fail_confirm = False
        self.account.submit_orders()
        retry = next(oid for oid in self.grid.All_orders if oid not in before)
        self.record_fill(retry, price="2000", fee="0.001", offset=3000)
        self.refresh_stats_at(20000)
        self.assertEqual(self.grid.stats["profit"], Decimal("0.007"))
        self.assertEqual(self.grid.stats["volume"], Decimal("59.99"))

    def test_stats_failure_is_atomic_and_time_window_recovers(self):
        self.placed()
        oid = next(iter(self.grid.All_orders))
        self.record_fill(oid, "0.002")
        self.refresh_stats_at()
        original, watermark = dict(self.grid.stats), self.account.stats_until
        self.record_fill(oid, "0.002", offset=2000)
        self.venue.fill_page_size = 1
        self.venue.fail_fill_page = 1
        with patch("ondo_grid.time.time", return_value=(self.account.stats_started + 80000) / 1000):
            self.account.refresh_stats()
        self.assertTrue(self.account.stats_stale)
        self.assertEqual(self.grid.stats, original)
        self.assertEqual(self.account.stats_until, watermark)
        self.assertEqual(len(self.account.stats_fills), 1)
        self.venue.fail_fill_page = None
        self.refresh_stats_at(80000)
        self.assertEqual(len(self.account.stats_fills), 2)
        self.venue.calls.clear()
        self.refresh_stats_at(90000)
        query = parse_qs(urlsplit(self.venue.calls[0].url).query)
        self.assertEqual(int(query["startTime"][0]), self.account.stats_started + 20000)
        self.assertEqual(int(query["endTime"][0]), self.account.stats_started + 90000)
        self.assertEqual(self.grid.stats["volume"], Decimal("7.996"))

    def test_stats_invalid_data_and_duplicate_conflicts_keep_last_totals(self):
        self.placed()
        oid = next(iter(self.grid.All_orders))
        fill = self.record_fill(oid, "0.002")
        self.refresh_stats_at()
        original, watermark = dict(self.grid.stats), self.account.stats_until
        clock = patch("ondo_grid.time.time", return_value=(self.account.stats_started + 10000) / 1000)
        clock.start()
        self.addCleanup(clock.stop)
        for key, value in (("fee", "nan"), ("isMaker", "false"), ("filledCost", "0"),
                           ("side", "sell"), ("time", "bad time"), ("id", "")):
            with self.subTest(key=key), patch.dict(fill, {key: value}):
                self.account.refresh_stats()
                self.assertTrue(self.account.stats_stale)
                self.assertEqual(self.grid.stats, original)
                self.assertEqual(self.account.stats_until, watermark)
        for response in ({"result": None}, {"result": [], "pageInfo": {"nextCursor": "same"}}):
            with patch.object(self.exchange, "_request", return_value=response):
                self.account.refresh_stats()
            self.assertTrue(self.account.stats_stale)
            self.assertEqual(self.grid.stats, original)
        self.venue.fills.append(dict(fill, filledCost="10"))
        self.account.refresh_stats()
        self.assertTrue(self.account.stats_stale)
        self.assertEqual(self.grid.stats, original)

    def test_stats_errors_do_not_stop_trading_or_increment_critical_errors(self):
        with patch.object(self.exchange, "fetch_fills", side_effect=OndoAPIError("429", status=429)):
            self.run_loops(51)
        self.assertEqual(self.account.myloop, 51)
        self.assertEqual(self.account.critical_errors, 0)
        self.assertTrue(self.account.stats_stale)
        self.assertEqual(len(self.grid.All_orders), 60)
        self.account.display(self.exchange.fetch_open_orders())
        message = self.logger.info.call_args.args[0]
        self.assertIn("统计滞后", message)
        for label in ("交易量(U)", "Maker单", "Taker单", "累计手续费(U)", "网格净利(U)"):
            self.assertIn(label, message)
        self.assertNotIn("grid_", message)

    def test_stats_new_process_does_not_include_old_grid_clients(self):
        self.placed()
        self.record_fill(next(iter(self.grid.All_orders)))
        self.refresh_stats_at()
        self.account = AccountTrader(self.exchange)
        self.grid = self.account.grids["ETH-USD.P"]
        self.refresh_stats_at()
        self.assertEqual(self.grid.stats["volume"], 0)
        self.assertEqual(self.grid.stats["profit"], 0)

    def test_example_config_and_syntax(self):
        root = Path(__file__).resolve().parents[1]
        example = dotenv_values(root / ".env.example")
        self.assertEqual(example["ONDO_API_KEY_ID"], "")
        self.assertEqual(example["ONDO_API_SECRET"], "")
        self.assertEqual(example["interPatch"], "0.2")
        self.assertEqual((example["SYMBOL2"], example["SYMBOL3"]), ("", ""))
        with patch.dict(os.environ, {k: v for k, v in example.items() if v is not None}, clear=True):
            self.assertEqual(len(AccountTrader(self.exchange).grids), 1)
        for name in ("ondo_grid.py", "ondo_exchange.py"):
            ast.parse((root / name).read_text(encoding="utf-8"), feature_version=(3, 10))


if __name__ == "__main__":
    unittest.main()



