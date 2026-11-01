"""使用真实 requests 编码和模拟服务器验证批量策略，不访问网络或读取 .env。"""

import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import runpy
import sys
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests
from ondo_exchange import OndoAPIError, OndoExchange, OndoFatalError
from ondo_grid import GridTrader


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
        self.position = 0
        self.pnl = 0
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
        if path == "/v1/markets":
            result = {"perps": {"tradingPairs": self.markets}}
        elif path == "/v1/perps/depth":
            result = {"bids": [[str(self.price - 1), "1"]], "asks": [[str(self.price + 1), "1"]]}
        elif path == "/v1/perps/mark_prices":
            result = {"ETH-USD.P": {"markPrice": str(self.price)}}
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
        elif path == "/v1/perps/orders":
            result = [dict(order) for order in self.orders.values() if order["status"] == "open"]
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
            {"success": True, "result": result} if status == 200 else
            {"success": False, "error_code": "order_not_found", "error": "not found"}
        ).encode()
        return response


class GridTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {
            "SYMBOL": "ETH", "gridLines": "60", "gridStep": "1", "orderSize": "0.01",
            "lowLimit": "1000", "highLimit": "3000", "baseline": "2000", "left": "0",
            "maxposi": "1", "MAX_LOSS": "-100", "CRITICAL_ERROR_LIMIT": "3",
        }, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.sleep = patch("ondo_exchange.time.sleep")
        self.sleep_mock = self.sleep.start()
        self.addCleanup(self.sleep.stop)
        log_patch = patch("ondo_grid.logger")
        log_patch.start()
        self.addCleanup(log_patch.stop)
        exchange_log_patch = patch("ondo_exchange.logger")
        self.exchange_logger = exchange_log_patch.start()
        self.addCleanup(exchange_log_patch.stop)
        self.venue = Venue()
        self.exchange = OndoExchange("test-key", "test-secret")
        self.exchange._session.send = Mock(side_effect=self.venue.send)
        self.addCleanup(self.exchange.close)
        self.trader = GridTrader(self.exchange)
        self.trader.myNewPrice = 2000

    def placed(self):
        self.trader.place_grid_orders()
        self.trader.poller()
        return [call for call in self.venue.calls if call.method == "POST"]

    def test_symbols_are_native_everywhere(self):
        for symbol in ("ETH", " eth ", "ETH-USD.P", "ETH/USDT:USDT"):
            self.assertEqual(self.exchange.market(symbol)["symbol"], "ETH-USD.P")
        self.venue.position = -0.2
        self.assertEqual(self.exchange.fetch_positions(["ETH"])[0]["info"]["positionAmt"], "-0.2")
        self.assertEqual(self.trader.check_interval, 0.8)

    def test_unknown_disabled_and_ambiguous_symbols(self):
        with self.assertRaises(ValueError):
            self.exchange.market("UNKNOWN")
        self.venue.markets.append(dict(self.venue.markets[0], market="ETH-USDC.P"))
        self.exchange.load_markets()
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.exchange.market("ETH")
        self.assertEqual(self.exchange.market("ETH-USD.P")["id"], "ETH-USD.P")
        for market in self.venue.markets:
            market["disabled"] = True
        with self.assertRaises(RuntimeError):
            self.exchange.load_markets()

    def test_sixty_grid_orders_use_three_signed_batches(self):
        posts = self.placed()
        self.assertEqual([len(json.loads(call.body)["orders"]) for call in posts], [20, 20, 20])
        self.assertTrue(all(urlsplit(call.url).path == "/v1/perps/orders/batch" for call in posts))
        self.assertEqual(len(self.trader.All_orders), 60)
        self.assertEqual(sum(item[0] for item in self.trader.All_orders.values()), 2)
        self.assertEqual(sum(order["side"] == "buy" for order in self.venue.orders.values()), 30)
        for order_id, (_, price, side) in self.trader.All_orders.items():
            self.assertEqual(float(self.venue.orders[order_id]["price"]), price)
            self.assertEqual(self.venue.orders[order_id]["side"], side)
        self.sleep_mock.assert_not_called()

    def test_multiple_fills_make_one_replacement_batch(self):
        self.placed()
        for order_id in ("1", "2", "3"):
            self.venue.orders[order_id]["status"] = "fullyfilled"
            self.venue.orders[order_id]["filledSize"] = "0.01"
        self.trader.poller()
        self.assertEqual(self.venue.post_count, 4)
        replacements = [self.venue.orders[str(index)] for index in (61, 62, 63)]
        self.assertEqual([(item["side"], float(item["price"])) for item in replacements], [
            ("sell", 2000), ("buy", 2000), ("sell", 1999),
        ])
        self.assertEqual(self.trader.counter, 3)
        self.assertEqual(self.trader.last_fill_price, 1998)
        self.assertEqual(len(self.trader.All_orders), 60)

    def test_edge_fill_precedes_other_replacements(self):
        self.placed()
        self.venue.orders["59"]["status"] = "fullyfilled"
        self.venue.orders["1"]["status"] = "fullyfilled"
        self.trader.poller()
        self.assertTrue(self.trader.switc)
        self.assertEqual(self.trader.myNewPrice, 1970)
        self.assertEqual(self.venue.post_count, 3)
        self.trader.place_grid_orders()
        self.trader.poller()
        self.assertEqual(self.venue.post_count, 6)
        self.assertEqual(len(self.exchange.fetch_open_orders("ETH")), 60)

    def test_partial_failure_records_success_and_retries_only_failed_orders(self):
        self.venue.partial = True
        self.placed()
        self.assertEqual(self.venue.post_count, 3)
        self.assertEqual(len(self.trader.All_orders), 3)
        self.assertEqual(len(self.trader.pending_orders), 57)
        previous_ids = {item["clientOrderId"] for item in self.trader.pending_orders}
        accepted_ids = {item["clientOrderId"] for item in self.venue.orders.values()}
        self.venue.partial = False
        self.trader.poller()
        posts = [call for call in self.venue.calls if call.method == "POST"]
        retried = [item for call in posts[3:] for item in json.loads(call.body)["orders"]]
        self.assertEqual([len(json.loads(call.body)["orders"]) for call in posts[3:]], [20, 20, 17])
        self.assertFalse({item["clientOrderId"] for item in retried} & (previous_ids | accepted_ids))
        self.assertEqual(len(self.venue.orders), 60)
        self.assertEqual(len(self.trader.All_orders), 60)
        self.assertEqual(self.trader.pending_orders, [])

    def test_partial_failure_in_run_continues_and_recovers(self):
        self.venue.partial = True
        self.trader.cancel_on_exit = False
        def next_poll(seconds):
            self.assertEqual(seconds, 0.8)
            if self.venue.partial:
                self.venue.partial = False
            else:
                raise KeyboardInterrupt()
        self.sleep_mock.side_effect = next_poll
        with self.assertRaises(SystemExit) as stopped:
            self.trader.run()
        self.assertEqual(stopped.exception.code, 0)
        self.assertEqual(self.trader.critical_errors, 0)
        self.assertEqual(self.trader.myloop, 2)
        self.assertEqual(len(self.exchange.fetch_open_orders("ETH")), 60)
        self.assertFalse(any(call.method == "DELETE" for call in self.venue.calls))

    def test_failed_replacements_stay_tracked_and_are_not_queued_twice(self):
        self.placed()
        for order_id in ("1", "2", "3"):
            self.venue.orders[order_id].update(status="fullyfilled", filledSize="0.01")
        self.venue.partial = True
        self.trader.poller()
        self.assertNotIn("1", self.trader.All_orders)
        self.assertIn("2", self.trader.All_orders)
        self.assertIn("3", self.trader.All_orders)
        self.assertEqual(self.trader.counter, 1)
        self.assertEqual(self.trader.last_fill_price, 1999)
        self.venue.reject_all = True
        self.venue.orders["4"].update(status="fullyfilled", filledSize="0.01")
        for _ in range(3):
            self.trader.poller()
            self.assertEqual(len(self.trader.pending_orders), 3)
            self.assertEqual({item["old_id"] for item in self.trader.pending_orders}, {"2", "3", "4"})
            self.assertEqual(self.trader.counter, 1)
        self.venue.reject_all = False
        self.venue.partial = False
        self.trader.poller()
        self.assertEqual(self.trader.pending_orders, [])
        self.assertEqual(len(self.trader.All_orders), 60)
        self.assertEqual(self.trader.counter, 4)

    def test_edge_rebuild_discards_old_queue_only_after_cancellation_succeeds(self):
        self.placed()
        self.venue.reject_all = True
        self.venue.orders["1"].update(status="fullyfilled", filledSize="0.01")
        self.trader.poller()
        old_queue = list(self.trader.pending_orders)
        post_count = self.venue.post_count
        self.venue.orders["59"].update(status="fullyfilled", filledSize="0.01")
        self.trader.poller()
        self.assertEqual(self.venue.post_count, post_count)
        self.assertTrue(self.trader.switc)
        self.venue.keep_open.add("2")
        with self.assertRaises(RuntimeError):
            self.trader.place_grid_orders()
        self.assertEqual(self.trader.pending_orders, old_queue)
        self.venue.keep_open.clear()
        self.venue.reject_all = False
        self.trader.place_grid_orders()
        self.assertEqual(len(self.trader.pending_orders), 60)
        self.assertTrue(all("old_id" not in item for item in self.trader.pending_orders))
        self.trader.poller()
        self.assertEqual(self.trader.pending_orders, [])
        self.assertEqual(len(self.exchange.fetch_open_orders("ETH")), 60)
        self.assertEqual(self.trader.counter, 0)

    def test_too_few_orders_check_resumes_after_queue_drains(self):
        self.venue.reject_all = True
        self.placed()
        self.trader.poller()  # 零挂单但有待提交订单，允许继续恢复。
        self.assertEqual(len(self.trader.pending_orders), 60)
        self.venue.reject_all = False
        self.trader.poller()
        for order in self.venue.orders.values():
            order["status"] = "canceled"
        with self.assertRaises(SystemExit):
            self.trader.poller()

    def test_pending_orders_do_not_disable_position_or_excess_order_risk(self):
        self.venue.reject_all = True
        self.placed()
        self.venue.position = 2
        with self.assertRaises(SystemExit):
            self.trader.accountInfo()
        self.venue.position = 0
        self.venue.orders = {
            str(index): {
                "orderId": str(index), "clientOrderId": f"extra_{index}",
                "market": "ETH-USD.P", "side": "buy", "price": "1999",
                "size": "0.01", "filledSize": "0", "status": "open",
            } for index in range(65)
        }
        with self.assertRaises(SystemExit):
            self.trader.accountInfo()
        self.assertEqual(self.exchange.fetch_open_orders("ETH"), [])

    def test_account_risk_still_runs_every_twenty_loops_during_placement_failures(self):
        self.venue.reject_all = True
        self.trader.accountInfo = Mock(wraps=self.trader.accountInfo)
        def next_poll(seconds):
            self.assertEqual(seconds, 0.8)
            if self.trader.myloop == 20:
                self.venue.position = 2
        self.sleep_mock.side_effect = next_poll
        with self.assertRaises(SystemExit) as stopped:
            self.trader.run()
        self.assertEqual(stopped.exception.code, 1)
        self.assertEqual(self.trader.myloop, 20)
        self.assertEqual(self.trader.accountInfo.call_count, 2)

    def test_all_placement_failures_keep_running_without_critical_errors(self):
        self.venue.reject_all = True
        self.trader.cancel_on_exit = False
        self.sleep_mock.side_effect = [None] * 4 + [KeyboardInterrupt()]
        with self.assertRaises(SystemExit):
            self.trader.run()
        self.assertEqual(self.trader.myloop, 5)
        self.assertEqual(self.trader.critical_errors, 0)
        self.assertEqual(self.venue.post_count, 15)
        self.assertEqual(self.trader.All_orders, {})
        self.assertEqual(len(self.trader.pending_orders), 60)
        self.assertTrue(all(call.args == (0.8,) for call in self.sleep_mock.call_args_list))

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

    def test_null_failure_collection_during_grid_and_replacements(self):
        def transform(result):
            result["failedOrders"] = None
            return result
        self.venue.batch_result_transform = transform
        self.placed()
        self.venue.orders["1"].update(status="fullyfilled", filledSize="0.01")
        self.trader.poller()
        self.assertEqual(len(self.trader.All_orders), 60)
        self.assertEqual(self.trader.pending_orders, [])
        self.assertEqual(self.trader.counter, 1)
        self.assertEqual(self.venue.post_count, 4)
        self.assertFalse(any("client:" in call.url for call in self.venue.calls))
        self.exchange_logger.warning.assert_not_called()

    def test_unconfirmed_orders_are_queued_with_confirmation_error_details(self):
        self.exchange._request = Mock(side_effect=[
            OndoAPIError("timeout", uncertain=True),
            OndoAPIError("missing", code="order_not_found"),
            OndoAPIError("missing", code="order_not_found"),
            OndoAPIError("missing", code="order_not_found"),
        ])
        self.trader.pending_orders = [{"side": "buy", "price": 1999, "edge": 0}]
        self.trader.submit_orders()
        self.assertEqual(len(self.trader.pending_orders), 1)
        self.assertEqual(self.trader.critical_errors, 0)
        self.assertEqual([call.args[0] for call in self.exchange._request.call_args_list], ["POST", "GET", "GET", "GET"])
        warnings = [call.args[0] for call in self.exchange_logger.warning.call_args_list]
        self.assertTrue(any("attempt=3/3" in warning and "missing" in warning for warning in warnings))

    def test_unconfirmed_replacement_retries_with_new_client_id_next_poll(self):
        self.placed()
        self.venue.orders["1"].update(status="fullyfilled", filledSize="0.01")
        self.venue.timeout_after_post = True
        self.venue.fail_confirm = True
        self.trader.poller()
        self.assertEqual(len(self.trader.pending_orders), 1)
        first_client_id = self.trader.pending_orders[0]["clientOrderId"]
        self.assertIn("1", self.trader.All_orders)
        self.assertEqual(self.trader.counter, 0)
        self.venue.timeout_after_post = False
        self.venue.fail_confirm = False
        self.trader.poller()
        self.assertEqual(self.trader.pending_orders, [])
        self.assertNotIn("1", self.trader.All_orders)
        self.assertEqual(self.trader.counter, 1)
        self.assertEqual(self.venue.post_count, 5)
        self.assertNotEqual(first_client_id, self.venue.orders["62"]["clientOrderId"])
        # 按用户选择，未知但实际已成功的上一笔会留下额外挂单。
        self.assertEqual(len(self.exchange.fetch_open_orders("ETH")), 61)

    def test_canceled_zero_fill_is_explicit_failure_in_batch_and_confirmation(self):
        self.venue.cancel_on_post = True
        for lost_response in (False, True):
            with self.subTest(lost_response=lost_response):
                self.venue.timeout_after_post = lost_response
                result = self.exchange.create_orders("ETH", [{"side": "buy", "amount": "0.01", "price": 1999}])
                self.assertEqual(result["addedOrders"], [])
                self.assertEqual(result["failedOrders"][0]["errorCode"], "order_canceled")
                self.assertIn("selfMatchPrevention", result["failedOrders"][0]["error"])

    def test_canceled_zero_fill_recovers_next_poll(self):
        self.venue.cancel_on_post = True
        self.placed()
        self.assertEqual(len(self.trader.pending_orders), 60)
        self.assertEqual(self.trader.All_orders, {})
        self.venue.cancel_on_post = False
        self.trader.poller()
        self.assertEqual(self.trader.pending_orders, [])
        self.assertEqual(len(self.exchange.fetch_open_orders("ETH")), 60)

    def test_partial_fill_cancellation_remains_fatal(self):
        self.venue.partial_cancel_on_post = True
        with self.assertRaises(SystemExit) as stopped:
            self.trader.run()
        self.assertEqual(stopped.exception.code, 1)
        self.assertEqual(self.venue.post_count, 1)

    def test_sixty_cancellations_use_three_batches(self):
        self.placed()
        self.trader.cancel_open_orders("test")
        deletes = [call for call in self.venue.calls if call.method == "DELETE"]
        self.assertEqual(len(deletes), 3)
        self.assertTrue(all(len(parse_qs(urlsplit(call.url).query)["orderIDs"][0].split(",")) == 20 for call in deletes))
        self.assertIn("%2C", deletes[0].path_url)
        self.assertEqual(self.exchange.fetch_open_orders("ETH"), [])

    def test_cancel_already_terminal_orders_is_success(self):
        self.placed()
        self.venue.orders["1"]["status"] = "fullyfilled"
        self.venue.orders["2"]["status"] = "canceled"
        self.exchange.cancel_orders("ETH", ["1", "2", "3"])
        self.assertEqual(self.venue.orders["3"]["status"], "canceled")

    def test_partially_filled_cancellation_does_not_block_cleanup(self):
        self.placed()
        self.venue.orders["1"]["filledSize"] = "0.005"
        self.trader.cancel_open_orders("test")
        self.assertEqual(self.exchange.fetch_open_orders("ETH"), [])

    def test_cancel_failure_attempts_other_batches_and_blocks_rebuild(self):
        self.placed()
        self.venue.keep_open.add("1")
        with self.assertRaisesRegex(RuntimeError, "remaining=1"):
            self.trader.place_grid_orders()
        self.assertEqual(len([call for call in self.venue.calls if call.method == "DELETE"]), 3)
        self.assertEqual(self.venue.post_count, 3)

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

    def test_rate_limit_keeps_failed_batch_and_continues_other_batches(self):
        self.venue.rate_limit_post = 2
        self.placed()
        self.assertEqual(self.venue.post_count, 3)
        self.assertEqual(len(self.trader.All_orders), 40)
        self.assertEqual(len(self.trader.pending_orders), 20)
        self.trader.poller()
        self.assertEqual(self.venue.post_count, 4)
        self.assertEqual(len(self.exchange.fetch_open_orders("ETH")), 60)
        self.assertEqual(self.trader.pending_orders, [])
        self.assertFalse(any(call.method == "DELETE" for call in self.venue.calls))

    def test_rate_limit_in_run_keeps_normal_poll_interval(self):
        self.venue.rate_limit_post = 2
        self.trader.cancel_on_exit = False
        self.sleep_mock.side_effect = [None, KeyboardInterrupt()]
        with self.assertRaises(SystemExit) as stopped:
            self.trader.run()
        self.assertEqual(stopped.exception.code, 0)
        self.assertEqual(self.venue.post_count, 4)
        self.assertEqual(len(self.exchange.fetch_open_orders("ETH")), 60)
        self.assertTrue(all(call.args == (0.8,) for call in self.sleep_mock.call_args_list))

    def test_consecutive_read_errors_reach_critical_limit(self):
        self.trader.myloop = 1
        self.trader.switc = False
        self.trader.cancel_on_risk = False
        self.exchange.fetch_open_orders = Mock(side_effect=OndoAPIError("Ondo HTTP 429", status=429))
        with self.assertRaises(SystemExit) as stopped:
            self.trader.run()
        self.assertEqual(stopped.exception.code, 1)
        self.assertEqual(self.trader.critical_errors, 3)

    def test_risk_limits_and_too_few_orders(self):
        for field, value in (("price", 999), ("price", 3001), ("position", 2), ("pnl", -101)):
            with self.subTest(field=field, value=value):
                self.venue.price, self.venue.position, self.venue.pnl = 2000, 0, 0
                setattr(self.venue, field, value)
                with self.assertRaises(SystemExit):
                    self.trader.accountInfo()
        with self.assertRaises(SystemExit):
            self.trader.poller()

    def test_ctrl_c_cancels_when_enabled(self):
        self.placed()
        self.trader.accountInfo = Mock(side_effect=KeyboardInterrupt())
        with self.assertRaises(SystemExit) as stopped:
            self.trader.run()
        self.assertEqual(stopped.exception.code, 0)
        self.assertEqual(self.exchange.fetch_open_orders("ETH"), [])

    def test_invalid_configuration_never_posts(self):
        for config in ({"gridStep": "0.15"}, {"orderSize": "0.0001"}, {"gridLines": "59"}, {"gridStep": "nan"}):
            with self.subTest(config=config), patch.dict(os.environ, config), self.assertRaises(ValueError):
                GridTrader(self.exchange)
        self.assertEqual(self.venue.post_count, 0)

    def test_entrypoint_closes_session_even_on_startup_error(self):
        exchange = Mock()
        exchange.load_markets.side_effect = ValueError("startup failed")
        with patch("ondo_exchange.OndoExchange.from_env", return_value=exchange), \
             patch("dotenv.load_dotenv"), \
             patch("logging.FileHandler", return_value=logging.NullHandler()), \
             self.assertRaisesRegex(ValueError, "startup failed"):
            runpy.run_path(str(Path(__file__).resolve().parents[1] / "ondo_grid.py"), run_name="__main__")
        exchange.close.assert_called_once()


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main()
