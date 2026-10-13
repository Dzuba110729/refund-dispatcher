"""Запуск:
  python -m refund_dispatcher submit --email anna@example.com --text "Хочу вернуть кроссовки, заказ 1001"
  python -m refund_dispatcher bot

Нужны переменные: ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_ALLOWED_USER_IDS (через запятую).
Заказы и платежи пока заглушки с демо-данными (см. demo_orders).
"""
import argparse
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

from langgraph.checkpoint.sqlite import SqliteSaver

from .channels import ConsoleChannel
from .data import FakeOrders, FakePayments, Item, Order
from .graph import Deps, build_graph
from .llm import AnthropicLlm
from .service import Dispatcher
from .store import Store
from .telegram import ManagerBot, TelegramApi, TelegramChannel, run_forever


def demo_orders() -> FakeOrders:
    now = datetime.now(timezone.utc)
    return FakeOrders([Order(
        id="1001", customer_email="anna@example.com", status="delivered",
        delivered_at=now - timedelta(days=3), paid_kop=120000, payment_id="pay-1001",
        items=[Item(sku="S1", name="Кроссовки", price_kop=120000)])])


def need(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(f"Не задана переменная {name}")
    return v


def build(db_path: str) -> tuple[Dispatcher, TelegramApi, set[int]]:
    api = TelegramApi(need("TELEGRAM_BOT_TOKEN"))
    chat_id = int(need("TELEGRAM_CHAT_ID"))
    allowed = {int(x) for x in need("TELEGRAM_ALLOWED_USER_IDS").split(",")}
    console = ConsoleChannel()
    deps = Deps(orders=demo_orders(), payments=FakePayments(), llm=AnthropicLlm(),
                manager=TelegramChannel(api, chat_id, int(os.environ.get("TELEGRAM_ESCALATION_CHAT_ID", chat_id))),
                customer=console, support=console, store=Store(db_path),
                clock=lambda: datetime.now(timezone.utc), sleep=time.sleep)
    saver = SqliteSaver(sqlite3.connect(db_path, check_same_thread=False))
    return Dispatcher(deps, build_graph(deps, saver)), api, allowed


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # токен бота виден в адресах запросов
    ap = argparse.ArgumentParser(prog="refund_dispatcher")
    ap.add_argument("--db", default="refund_dispatcher.sqlite3")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("submit")
    s.add_argument("--email", required=True)
    s.add_argument("--text", required=True)
    s.add_argument("--photos", type=int, default=0)
    sub.add_parser("bot")
    args = ap.parse_args()

    dispatcher, api, allowed = build(args.db)
    if args.cmd == "submit":
        print("Заявка:", dispatcher.submit(args.text, args.email, args.photos))
    else:
        run_forever(ManagerBot(api, dispatcher, allowed), api, dispatcher)


if __name__ == "__main__":
    main()
