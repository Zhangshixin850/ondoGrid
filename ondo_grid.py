# -*- coding: utf-8 -*-
"""Ondo v3.0 多币种中性网格；Ubuntu Python 3.10+，通过 .env 配置。"""

import json
import logging
import math
import os
import re
import sys
import time
import urllib.request
import uuid
from decimal import Decimal
from itertools import zip_longest

from dotenv import load_dotenv
from ondo_exchange import OndoAPIError, OndoExchange, OndoFatalError

logger = logging.getLogger(__name__)
order_logger = logging.getLogger("ondo_exchange")


class ConsoleFormatter(logging.Formatter):
    """详细订单信息仅写文件；控制台使用业务摘要。"""

    def format(self, record):
        message = getattr(record, "console_message", record.getMessage())
        return f"{self.formatTime(record, '%H:%M:%S')} | {record.levelname:<7} | {message}"


class GridTrader:
    def __init__(self, exchange, index):
        self.exchange = exchange
        self.index = index
        symbol = os.environ[f"SYMBOL{index}"].strip().strip("'\"")
        market = exchange.market(symbol)
        self.symbol = market["symbol"]
        for key in ("gridStep", "orderSize", "lowLimit", "highLimit", "baseline", "left", "maxposi"):
            value = float(os.environ[f"{key}{index}"])
            if not math.isfinite(value):
                raise ValueError(f"{key}{index} must be finite")
            setattr(self, key, value)
        self.gridLines = int(os.environ[f"gridLines{index}"])
        self.max_loss = float(os.environ[f"MAX_LOSS{index}"])
        if not math.isfinite(self.max_loss):
            raise ValueError(f"MAX_LOSS{index} must be finite")
        if self.gridStep <= 0 or self.orderSize <= 0 or self.gridLines <= 0 or self.gridLines % 2:
            raise ValueError(f"Grid {index}: positive step/size and positive even gridLines required")
        if not 0 < self.lowLimit < self.highLimit or self.maxposi <= 0:
            raise ValueError(f"Grid {index}: invalid price limits or maxposi")
        tick = Decimal(market["precision"]["price"])
        if Decimal(str(self.gridStep)) % tick:
            raise ValueError(f"gridStep{index} must be a positive multiple of {tick}")
        self.amount = exchange.amount_to_precision(self.symbol, self.orderSize)
        if Decimal(self.amount) <= 0:
            raise ValueError(f"orderSize{index} is below the size increment")
        self.All_orders = {}
        self.pending_orders = []
        self.switc = True
        self.counter = 0
        self.myNewPrice = 0.0
        self.last_fill_price = 0.0
        self.position = 0.0
        self.pnl = 0.0

    def place_grid_orders(self):
        # 先验证整张网格；撤单成功后才替换本地状态。
        pending = []
        half = self.gridLines // 2
        for i in range(1, half + 1):
            for side, price in (("buy", self.myNewPrice - i * self.gridStep),
                                ("sell", self.myNewPrice + i * self.gridStep)):
                price = self.exchange.price_to_precision(self.symbol, price)
                if Decimal(price) <= 0:
                    raise ValueError(f"{self.symbol}: grid prices must be positive")
                pending.append({"symbol": self.symbol, "side": side, "price": price,
                                "amount": self.amount, "edge": int(i == half)})
        return pending

    def poller(self, orders):
        open_ids = {str(order["id"]) for order in orders}
        last_price = self.last_fill_price or self.myNewPrice
        # 沿用原策略：本进程订单从完整挂单快照消失即按成交处理。
        filled_ids = sorted(set(self.All_orders) - open_ids,
                            key=lambda oid: (abs(self.All_orders[oid][1] - last_price),
                                             self.All_orders[oid][1], oid))
        edge_ids = [oid for oid in filled_ids if self.All_orders[oid][0]]
        if edge_ids:
            self.last_fill_price = float(self.All_orders[edge_ids[-1]][1])
            self.myNewPrice = self.last_fill_price
            self.switc = True
            logger.info(f"{self.symbol} | 边缘成交，准备以 {self.myNewPrice:g} 重建网格")
            return
        queued = {item.get("old_id") for item in self.pending_orders}
        for order_id in filled_ids:
            if order_id in queued:
                continue
            _, old_price, old_side = self.All_orders[order_id]
            self.pending_orders.append({
                "symbol": self.symbol, "amount": self.amount, "edge": 0,
                "side": "sell" if old_side == "buy" else "buy",
                "price": old_price + self.gridStep if old_side == "buy" else old_price - self.gridStep,
                "old_id": order_id, "old_price": old_price,
            })


class AccountTrader:
    def __init__(self, exchange):
        self.exchange = exchange
        self.name = os.getenv("NAME", "")
        self.check_interval = float(os.getenv("CHECK_INTERVAL", "0.8"))
        self.mmr_limit = float(os.getenv("MMR", "0.2"))
        self.leverage_limit = float(os.getenv("leverage", "5"))
        self.critical_error_limit = int(os.getenv("CRITICAL_ERROR_LIMIT", "3"))
        if any(not math.isfinite(v) or v <= 0 for v in
               (self.check_interval, self.mmr_limit, self.leverage_limit)) or self.critical_error_limit <= 0:
            raise ValueError("Invalid account limits or polling configuration")
        self.cancel_on_exit = os.getenv("CANCEL_ON_EXIT", "true").strip().lower() == "true"
        self.cancel_on_risk = os.getenv("CANCEL_ON_RISK", "true").strip().lower() == "true"
        self.notify_webhook = os.getenv("NOTIFY_WEBHOOK", "").strip().strip("'\"")
        self.exchange.load_markets()
        indices = sorted(int(match.group(1)) for key in os.environ
                         if (match := re.fullmatch(r"SYMBOL([1-9][0-9]*)", key)))
        self.grids = {}
        for index in indices:
            if not os.environ[f"SYMBOL{index}"].strip().strip("'\""):
                continue
            grid = GridTrader(exchange, index)
            if grid.symbol in self.grids:
                raise ValueError(f"Duplicate configured market: {grid.symbol}")
            self.grids[grid.symbol] = grid
        if not self.grids:
            raise ValueError("Configure at least SYMBOL1 and its numbered grid parameters")
        self.myloop = 0
        self.critical_errors = 0
        self.balance = {}

    def notify(self, message):
        if not self.notify_webhook:
            return
        try:
            payload = json.dumps({"msgtype": "text", "text": {"content": message}}, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(self.notify_webhook, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=5) as response:
                response.read()
        except Exception as exc:
            logger.warning(f"notify failed: {exc}", extra={"console_message": "通知发送失败，详情见文件日志"})

    def cancel_open_orders(self, reason, symbols=None, orders=None):
        symbols = set(self.grids) if symbols is None else set(symbols)
        logger.warning(f"撤销挂单 | {reason} | {', '.join(sorted(symbols))}")
        self.notify(f"{self.name} 撤销挂单: {reason}, {', '.join(sorted(symbols))}")
        if orders is None:
            orders = self.exchange.fetch_open_orders()
        selected = [order for order in orders if order["symbol"] in symbols]
        errors = []
        for offset in range(0, len(selected), 20):
            batch = selected[offset:offset + 20]
            try:
                self.exchange.cancel_orders(None, batch)
                order_logger.info(f"Canceled batch: {[(o['symbol'], o['id']) for o in batch]}")
            except Exception as exc:
                errors.append(str(exc))
            if len(batch) == 20 and offset + 20 < len(selected):
                time.sleep(0.4)
        if selected:
            remaining = [order for order in self.exchange.fetch_open_orders() if order["symbol"] in symbols]
            if remaining or errors:
                raise RuntimeError(f"Cancellation incomplete: remaining={len(remaining)}, errors={errors}")

    def critical(self, message, exc=None, exit_after=False):
        self.critical_errors += 1
        detail = f"{message}: {exc}" if exc else message
        logger.error(f"{detail} (critical_errors={self.critical_errors}/{self.critical_error_limit})",
                     extra={"console_message": f"{message} | 连续错误 {self.critical_errors}/{self.critical_error_limit}"})
        self.notify(f"{self.name} {message}")
        if exit_after or self.critical_errors >= self.critical_error_limit:
            if self.cancel_on_risk:
                try:
                    self.cancel_open_orders("风控停止")
                except Exception as cancel_error:
                    logger.error(f"cancel after critical failed: {cancel_error}",
                                 extra={"console_message": "风控撤单未完成，请检查文件日志与账户挂单"})
                    self.notify(f"{self.name} 风控撤单未完成")
            sys.exit(1)

    def accountInfo(self, orders):
        balance = self.exchange.fetch_balance()
        ratio, leverage, margin = (float(balance[key]) for key in ("marginRatio", "leverage", "marginBalance"))
        if not all(math.isfinite(v) for v in (ratio, leverage, margin)) or ratio < 0 or leverage < 0:
            raise ValueError("Invalid account risk metrics")
        if not isinstance(balance.get("underLiquidation"), bool):
            raise ValueError("Missing or invalid underLiquidation")
        self.balance = balance
        logger.info(f"账户 | MMR {ratio:.2%}/{self.mmr_limit:.2%} | 杠杆 {leverage:.2f}/{self.leverage_limit:g}x | 保证金 {margin:.2f}")
        if ratio >= self.mmr_limit or leverage >= self.leverage_limit or margin <= 0 or balance["underLiquidation"]:
            self.critical("账户风控触发，停止全部网格", exit_after=True)
        positions = self.exchange.fetch_positions(list(self.grids))
        mark_prices = None
        for symbol, grid in self.grids.items():
            book = self.exchange.fetch_order_book(symbol, limit=5)
            bid = book["bids"][0][0] if book.get("bids") else 0
            ask = book["asks"][0][0] if book.get("asks") else 0
            if bid and ask:
                price = (bid + ask) / 2
            else:
                if mark_prices is None:
                    mark_prices = self.exchange.fetch_mark_prices()
                detail = mark_prices[self.exchange.market(symbol)["id"]]
                price = float(detail.get("markPrice") or detail.get("price") or 0)
            grid.myNewPrice = float(self.exchange.price_to_precision(symbol, price))
            if not math.isfinite(grid.myNewPrice) or grid.myNewPrice <= 0:
                raise ValueError(f"{symbol}: invalid current price")
            grid.position = sum(float(pos["info"]["positionAmt"]) for pos in positions if pos["symbol"] == symbol)
            grid.pnl = sum(float(pos["unrealizedPnl"]) for pos in positions if pos["symbol"] == symbol)
            if not math.isfinite(grid.position) or not math.isfinite(grid.pnl):
                raise ValueError(f"{symbol}: invalid position or PnL")
            count = sum(order["symbol"] == symbol for order in orders)
            if (count > grid.gridLines + 4 or grid.myNewPrice < grid.lowLimit
                    or grid.myNewPrice > grid.highLimit or grid.pnl < grid.max_loss
                    or abs(grid.position) > grid.maxposi):
                self.critical(f"{symbol} 风控触发，停止全部网格", exit_after=True)

    def rebuild_grids(self, orders):
        prepared = {symbol: grid.place_grid_orders() for symbol, grid in self.grids.items() if grid.switc}
        if not prepared:
            return
        self.cancel_open_orders("重建网格", prepared, orders)
        for symbol, pending in prepared.items():
            grid = self.grids[symbol]
            grid.All_orders.clear()
            grid.pending_orders = pending
            grid.switc = False
            logger.info(f"{symbol} | 新网格 {len(pending)} 单 | 中心 {grid.myNewPrice:g}")

    def submit_orders(self):
        # 跨币种轮流取单，保留各币种内部顺序；仅遍历本轮队列快照。
        queues = [list(grid.pending_orders) for grid in self.grids.values() if not grid.switc]
        pending = [item for row in zip_longest(*queues) for item in row if item is not None]
        for offset in range(0, len(pending), 20):
            batch = pending[offset:offset + 20]
            for item in batch:
                item["clientOrderId"] = "grid_" + uuid.uuid4().hex
            try:
                result = self.exchange.create_orders(None, batch)
                added = {order["clientOrderId"]: order for order in result["addedOrders"]}
                counts = {}
                for item in batch:
                    order = added.get(item["clientOrderId"])
                    if order is None:
                        continue
                    grid = self.grids[item["symbol"]]
                    grid.All_orders[order["id"]] = [item["edge"], order["price"], order["side"]]
                    if "old_id" in item:
                        del grid.All_orders[item["old_id"]]
                        grid.last_fill_price = item["old_price"]
                        grid.myNewPrice = grid.last_fill_price
                        grid.counter += 1
                    grid.pending_orders.remove(item)
                    counts[grid.symbol] = counts.get(grid.symbol, 0) + 1
                    order_logger.info(f"Placed {grid.symbol} order={order['id']} client={item['clientOrderId']} {order['side']} {order['amount']} @ {order['price']}")
                summary = f"批量下单 | 成功 {len(added)}/{len(batch)} | " + ", ".join(f"{symbol}: {n}" for symbol, n in counts.items())
                logger.info(summary)
                if result["failedOrders"]:
                    logger.warning(f"Batch failures: {result['failedOrders']}",
                                   extra={"console_message": f"下单失败 {len(result['failedOrders'])} 单，留待下轮重试"})
            except OndoAPIError as exc:
                logger.warning(f"Ondo batch placement failed: {exc}",
                               extra={"console_message": f"批量下单失败 {len(batch)} 单，留待下轮重试"})
            if len(batch) == 20 and offset + 20 < len(pending):
                time.sleep(0.4)

    def display(self, orders):
        rows = ["币种          参考价      仓位      目标差额     浮盈亏    买/卖挂单  待处理  成交"]
        for symbol, grid in self.grids.items():
            target = (grid.baseline - (grid.last_fill_price or grid.myNewPrice)) * grid.orderSize / grid.gridStep + grid.left
            buys = sum(order["symbol"] == symbol and order["side"] == "buy" for order in orders)
            sells = sum(order["symbol"] == symbol and order["side"] == "sell" for order in orders)
            rows.append(f"{symbol:<12} {grid.myNewPrice:>9g} {grid.position:>9g} {grid.position - target:>11.5g} "
                        f"{grid.pnl:>10.2f} {buys:>4}/{sells:<4} {len(grid.pending_orders):>5} {grid.counter:>6}")
        logger.info("快照（仓位/浮盈亏每20轮刷新；差额负值买、正值卖）\n" + "\n".join(rows))

    def run(self):
        logger.info(f"Ondo v3.0 | {self.name} | {', '.join(self.grids)} | 轮询 {self.check_interval:g}s")
        self.notify(f"{self.name} v3.0 started: {', '.join(self.grids)}")
        while True:
            try:
                orders = self.exchange.fetch_open_orders()
                if not isinstance(orders, list):
                    raise RuntimeError("Missing account open orders snapshot")
                if self.myloop % 20 == 0:
                    self.accountInfo(orders)
                    self.display(orders)
                for symbol, grid in self.grids.items():
                    count = sum(order["symbol"] == symbol for order in orders)
                    if not grid.switc and not grid.pending_orders and count < 3:
                        self.critical(f"{symbol} 挂单过少，停止全部网格", exit_after=True)
                self.rebuild_grids(orders)
                for symbol, grid in self.grids.items():
                    grid.poller([order for order in orders if order["symbol"] == symbol])
                self.submit_orders()
                self.critical_errors = 0
                self.myloop += 1
                time.sleep(self.check_interval)
            except KeyboardInterrupt:
                logger.info("用户停止全部网格")
                self.notify(f"{self.name} stopped by user")
                if self.cancel_on_exit:
                    try:
                        self.cancel_open_orders("用户停止")
                    except Exception as exc:
                        logger.error(f"cancel on exit failed: {exc}",
                                     extra={"console_message": "退出撤单未完成，详情见文件日志"})
                        self.notify(f"{self.name} 退出撤单未完成")
                sys.exit(0)
            except OndoFatalError as exc:
                self.critical("订单状态无法安全恢复", exc, exit_after=True)
            except Exception as exc:
                self.critical("主循环异常，本轮停止交易", exc)
                time.sleep(5)


if __name__ == "__main__":
    load_dotenv()
    logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    logger.propagate = False
    file_handler = logging.FileHandler(f"{os.getenv('NAME', '')}_ondo_log.txt", mode="a", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s"))
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(ConsoleFormatter())
    logger.handlers = [file_handler, console_handler]
    exchange_logger = logging.getLogger("ondo_exchange")
    exchange_logger.setLevel(logger.level)
    exchange_logger.handlers = [file_handler]
    exchange_logger.propagate = False
    exchange = OndoExchange.from_env()
    try:
        AccountTrader(exchange).run()
    finally:
        exchange.close()

