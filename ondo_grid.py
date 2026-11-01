# -*- coding: utf-8 -*-
"""Ondo 永续中性网格；Ubuntu Python 3.10+，通过 .env 配置。"""

import json
import logging
import math
import os
import sys
import time
import urllib.request
import uuid
from decimal import Decimal

from dotenv import load_dotenv
from ondo_exchange import OndoAPIError, OndoExchange, OndoFatalError


logger = logging.getLogger(__name__)


class GridTrader:
    def __init__(self, exchange):
        self.exchange = exchange
        self.name = os.getenv("NAME", "")
        symbol = os.getenv("SYMBOL", "").strip().strip("'\"")
        if not symbol:
            raise ValueError("Missing SYMBOL in .env, e.g. ETH or ETH-USD.P")
        self.exchange.load_markets()
        market = self.exchange.market(symbol)
        self.symbol = market["symbol"]

        self.gridStep = float(os.getenv("gridStep", 0))
        self.gridLines = int(os.getenv("gridLines", 0))
        self.orderSize = float(os.getenv("orderSize", 0))
        self.lowLimit = float(os.getenv("lowLimit", 0))
        self.highLimit = float(os.getenv("highLimit", 0))
        self.baseline = float(os.getenv("baseline", 0))
        self.left = float(os.getenv("left", 0))
        self.maxposi = float(os.getenv("maxposi", 0))
        self.max_loss = float(os.getenv("MAX_LOSS", -8000))
        self.check_interval = float(os.getenv("CHECK_INTERVAL", 0.8))
        self.cancel_on_exit = os.getenv("CANCEL_ON_EXIT", "true").strip().lower() == "true"
        self.cancel_on_risk = os.getenv("CANCEL_ON_RISK", "true").strip().lower() == "true"
        self.notify_webhook = os.getenv("NOTIFY_WEBHOOK", "").strip().strip("'\"")
        self.critical_error_limit = int(os.getenv("CRITICAL_ERROR_LIMIT", 3))
        self.critical_errors = 0

        if not all(math.isfinite(value) for value in (
            self.gridStep, self.orderSize, self.lowLimit, self.highLimit,
            self.baseline, self.left, self.maxposi, self.max_loss, self.check_interval,
        )):
            raise ValueError("Grid configuration must contain finite numbers")
        if self.gridStep <= 0 or self.orderSize <= 0 or self.gridLines <= 0 or self.gridLines % 2:
            raise ValueError("gridStep/orderSize must be positive; gridLines must be positive and even")
        if not 0 < self.lowLimit < self.highLimit or self.maxposi <= 0:
            raise ValueError("Require 0 < lowLimit < highLimit and maxposi > 0")
        if self.check_interval <= 0 or self.critical_error_limit <= 0:
            raise ValueError("CHECK_INTERVAL and CRITICAL_ERROR_LIMIT must be positive")
        tick = Decimal(market["precision"]["price"])
        if Decimal(str(self.gridStep)) % tick:
            raise ValueError(f"gridStep must be a positive multiple of {tick}")
        self.amount = self.exchange.amount_to_precision(self.symbol, self.orderSize)
        if Decimal(self.amount) <= 0:
            raise ValueError("orderSize is below the Ondo size increment")

        # 仅跟踪本进程订单；baseline/left 只计算日志目标仓位，不自动调仓。
        self.All_orders = {}
        self.pending_orders = []
        self.switc = True
        self.myloop = 0
        self.counter = 0
        self.myNewPrice = 0.0
        self.last_fill_price = 0.0
        logger.info(f"Ondo connected, symbol={self.symbol}, gridStep={self.gridStep}, orderSize={self.amount}")
        self.notify(f"{self.name} started: Ondo {self.symbol}, gridStep={self.gridStep}, orderSize={self.amount}")

    def notify(self, message):
        if not self.notify_webhook:
            return
        try:
            payload = json.dumps({"msgtype": "text", "text": {"content": message}}, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(self.notify_webhook, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=5) as response:
                response.read()
        except Exception as exc:
            logger.warning(f"notify failed: {exc}")

    def cancel_open_orders(self, reason):
        # 与原策略相同：撤当前交易对全部挂单，包括其他进程创建的订单。
        logger.warning(f"Canceling open orders: {reason}")
        self.notify(f"{self.name} cancel open orders: {self.symbol}, reason={reason}")
        orders = self.exchange.fetch_open_orders(self.symbol)
        order_ids = [order["id"] for order in orders]
        errors = []
        for offset in range(0, len(order_ids), 20):
            try:
                self.exchange.cancel_orders(self.symbol, order_ids[offset:offset + 20])
            except Exception as exc:
                # 一批失败仍尝试撤其他批次，最终检查是否有残留挂单。
                errors.append(str(exc))
        remaining = self.exchange.fetch_open_orders(self.symbol)
        if remaining or errors:
            raise RuntimeError(f"Cancellation incomplete: remaining={len(remaining)}, errors={errors}")

    def critical(self, message, exc=None, exit_after=False):
        self.critical_errors += 1
        detail = f"{message}: {exc}" if exc else message
        logger.error(f"{detail} (critical_errors={self.critical_errors}/{self.critical_error_limit})")
        self.notify(f"{self.name} critical: {detail}")
        if exit_after or self.critical_errors >= self.critical_error_limit:
            if self.cancel_on_risk:
                try:
                    self.cancel_open_orders("critical error limit reached")
                except Exception as cancel_error:
                    logger.error(f"cancel after critical failed: {cancel_error}")
                    self.notify(f"{self.name} cancel failed after critical: {cancel_error}")
            sys.exit(1)

    def accountInfo(self):
        book = self.exchange.fetch_order_book(self.symbol, limit=5)
        bid = book["bids"][0][0] if book.get("bids") else 0
        ask = book["asks"][0][0] if book.get("asks") else 0
        if bid and ask:
            self.myNewPrice = float(self.exchange.price_to_precision(self.symbol, (bid + ask) / 2))
        else:
            self.myNewPrice = float(self.exchange.fetch_ticker(self.symbol).get("mark") or 0)
        if not math.isfinite(self.myNewPrice) or self.myNewPrice <= 0:
            raise RuntimeError("Current price is empty or invalid")

        size, pnl, liquidation = 0.0, 0.0, 0
        for pos in self.exchange.fetch_positions([self.symbol]):
            if pos["symbol"] == self.symbol:
                size += float(pos["info"]["positionAmt"])
                pnl += float(pos["unrealizedPnl"])
                liquidation = pos.get("liquidationPrice") or liquidation
        orders = self.exchange.fetch_open_orders(self.symbol)
        prices = [float(order["price"]) for order in orders if order.get("price")]
        total_amount = sum(float(order.get("amount") or 0) for order in orders)
        target_price = self.last_fill_price or self.myNewPrice
        target = (self.baseline - target_price) * self.orderSize / self.gridStep + self.left
        logger.info(
            f"{self.name} symbol={self.symbol} price={self.myNewPrice} targetPrice={target_price} position={size} "
            f"target={round(target, 5)} diff={round(size - target, 5)}（-买+卖） pnl={pnl:.4f} "
            f"openOrders={len(orders)} maxOrder={max(prices) if prices else 0} "
            f"minOrder={min(prices) if prices else 0} liquidation={liquidation} "
            f"openAmount={total_amount:.8f} loop={self.myloop}"
        )
        if (
            len(orders) > self.gridLines + 4
            or self.myNewPrice < self.lowLimit or self.myNewPrice > self.highLimit
            or pnl < self.max_loss or abs(size) > self.maxposi
        ):
            self.critical(
                f"Risk control triggered: orders={len(orders)}, price={self.myNewPrice}, position={size}, pnl={pnl}",
                exit_after=True,
            )

    def submit_orders(self):
        # 每轮只遍历队列快照一次；失败项留在队列，下轮使用新客户端 ID 重试。
        pending = list(self.pending_orders)
        for offset in range(0, len(pending), 20):
            batch = pending[offset:offset + 20]
            for item in batch:
                item["clientOrderId"] = "grid_" + uuid.uuid4().hex
                item["amount"] = self.amount
            try:
                result = self.exchange.create_orders(self.symbol, batch)
            except OndoAPIError as exc:
                logger.warning(f"Ondo batch placement failed, retry next poll: {exc}")
                continue
            added = {order["clientOrderId"]: order for order in result["addedOrders"]}
            for item in batch:
                order = added.get(item["clientOrderId"])
                if order is None:
                    continue
                self.All_orders[order["id"]] = [item["edge"], order["price"], order["side"]]
                old_id = item.get("old_id")
                if old_id is not None:
                    del self.All_orders[old_id]
                    self.last_fill_price = item["old_price"]
                    self.myNewPrice = self.last_fill_price
                    self.counter += 1
                    logger.info(f"Filled id={old_id}, placed {order['side']} id={order['id']} price={order['price']}, count={self.counter}")
                else:
                    logger.info(f"Placed {order['side']} id={order['id']} amount={order['amount']} price={order['price']}")
                self.pending_orders.remove(item)
            if result["failedOrders"]:
                details = [
                    f"{item['order']['clientOrderId']}: {item.get('errorCode', '')} {item.get('error', '')}"
                    for item in result["failedOrders"]
                ]
                logger.warning("Ondo batch placement failed, retry next poll: " + "; ".join(details))

    def place_grid_orders(self):
        if self.myNewPrice == 0:
            self.accountInfo()
        half = self.gridLines // 2
        pending = []
        # 先验证整张网格，再撤旧单，避免价格非法时只铺出部分网格。
        for i in range(1, half + 1):
            for side, price in (
                ("buy", self.myNewPrice - i * self.gridStep),
                ("sell", self.myNewPrice + i * self.gridStep),
            ):
                price = self.exchange.price_to_precision(self.symbol, price)
                if Decimal(price) <= 0:
                    raise ValueError("Grid prices must be positive")
                pending.append({"side": side, "price": price, "edge": int(i == half)})
        self.cancel_open_orders("grid reset")
        self.All_orders.clear()
        self.pending_orders = pending
        self.switc = False
        logger.info(f"Grid prepared: {len(pending)} orders queued")

    def poller(self):
        # 查询失败向主循环报告，不将失败响应当作空挂单列表。
        orders = self.exchange.fetch_open_orders(self.symbol)
        if orders is None:
            raise RuntimeError("Ondo returned no open orders response")
        if not self.pending_orders and len(orders) < self.gridLines // 3:
            self.critical(
                f"Risk control triggered: too few open orders, orders={len(orders)}, minimum={self.gridLines // 3}",
                exit_after=True,
            )
        open_ids = {str(order["id"]) for order in orders}
        last_price = self.last_fill_price or self.myNewPrice
        # 沿用原 Ondo 逻辑：本进程订单从挂单列表消失即按成交处理。
        filled_ids = sorted(
            set(self.All_orders) - open_ids,
            key=lambda order_id: (abs(self.All_orders[order_id][1] - last_price), self.All_orders[order_id][1], order_id),
        )
        edge_ids = [order_id for order_id in filled_ids if self.All_orders[order_id][0]]
        if edge_ids:
            order_id = edge_ids[-1]
            self.last_fill_price = float(self.All_orders[order_id][1])
            self.myNewPrice = self.last_fill_price
            self.switc = True
            logger.info(f"Edge order filled id={order_id}, reset grid at {self.myNewPrice}")
            return
        queued_old_ids = {item.get("old_id") for item in self.pending_orders}
        for order_id in filled_ids:
            if order_id in queued_old_ids:
                continue
            _, old_price, old_side = self.All_orders[order_id]
            side = "sell" if old_side == "buy" else "buy"
            price = old_price + self.gridStep if old_side == "buy" else old_price - self.gridStep
            self.pending_orders.append({
                "side": side, "price": price, "edge": 0,
                "old_id": order_id, "old_price": old_price,
            })
        self.submit_orders()

    def run(self):
        logger.info(f"Program started: symbol={self.symbol}, gridStep={self.gridStep}, orderSize={self.amount}")
        while True:
            placing_grid = False
            try:
                if self.myloop % 20 == 0:
                    self.accountInfo()
                if self.switc:
                    placing_grid = True
                    self.place_grid_orders()
                    placing_grid = False
                self.poller()
                self.critical_errors = 0
                self.myloop += 1
                time.sleep(self.check_interval)
            except KeyboardInterrupt:
                logger.info("Stopped by user")
                self.notify(f"{self.name} stopped by user")
                if self.cancel_on_exit:
                    try:
                        self.cancel_open_orders("user stop")
                    except Exception as exc:
                        logger.error(f"cancel on exit failed: {exc}")
                        self.notify(f"{self.name} cancel on exit failed: {exc}")
                sys.exit(0)
            except OndoFatalError as exc:
                self.critical("Ondo order cannot be safely recovered", exc, exit_after=True)
            except Exception as exc:
                if placing_grid:
                    self.switc = True
                self.critical("Main loop error", exc)
                time.sleep(5)


if __name__ == "__main__":
    load_dotenv()
    logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    logger.propagate = False
    file_handler = logging.FileHandler(f"{os.getenv('NAME', '')}_ondo_log.txt", mode="a", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s"))
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    exchange_logger = logging.getLogger("ondo_exchange")
    exchange_logger.setLevel(logger.level)
    exchange_logger.handlers = list(logger.handlers)
    exchange_logger.propagate = False
    exchange = OndoExchange.from_env()
    try:
        GridTrader(exchange).run()
    finally:
        exchange.close()
