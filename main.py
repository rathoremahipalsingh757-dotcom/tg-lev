"""Telegram shop bot entry point."""

from __future__ import annotations

import asyncio
import html
import io
import logging
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, InvalidToken, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import Settings, load_settings
from paymento import PaymentoClient, PaymentoError, PaymentoTransportError
from paymento_ipn import PaymentoIPNServer
from sheets import OutOfStockError, SheetsStore

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
BANNER_PATH = Path(__file__).resolve().parent / "LEVi-Shop-Neon-Marketplace-Banner.jpg"
BANNER_FILE_ID_PATH = BANNER_PATH.with_name(".banner_file_id")

CATEGORIES = {
    "reddit": ("Reddit", "📱 Reddit Accounts"),
    "instagram": ("Instagram", "📸 Instagram Accounts"),
    "facebook": ("Facebook", "📘 Facebook Accounts"),
    "telegram": ("Telegram", "✈️ Telegram Accounts"),
}
HOME_TEXT = (
    "<b>🌟 S E N S N O V A 🌟</b>\n"
    "╭━━━━━━━━━━━━━━━━━━╮\n"
    "   🛍️  <b>Digital Shop</b>  🛍️\n"
    "╰━━━━━━━━━━━━━━━━━━╯\n"
    "Welcome! Browse the catalog, check your orders, or see our vouches.\n\n"
    "✨ <i>Choose an option below</i> ✨"
)
MENU = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton(CATEGORIES[key][1], callback_data=f"cat:{key}")
            for key in ("reddit", "instagram")
        ],
        [
            InlineKeyboardButton(CATEGORIES[key][1], callback_data=f"cat:{key}")
            for key in ("facebook", "telegram")
        ],
        [InlineKeyboardButton("👤  My Orders", callback_data="profile")],
        [InlineKeyboardButton("⭐  Vouches", callback_data="vouches")],
        [InlineKeyboardButton("💬  Help & Support", callback_data="help")],
    ]
)
PRICE_OPTIONS_PER_PAGE = 90
BULK_QUANTITY_PRESETS = (5, 10, 20, 30, 40)
MAX_BULK_QUANTITY = 500


def price_keyboard(
    category_key: str, products: list[dict[str, str | int]], page: int
) -> tuple[InlineKeyboardMarkup, list[str]]:
    start = page * PRICE_OPTIONS_PER_PAGE
    page_products = products[start : start + PRICE_OPTIONS_PER_PAGE]
    buttons = []
    page_prices = []
    for index, product in enumerate(page_products):
        label = f"${product['price']} USD — {product['stock']} available"
        if len(label) > 64:
            label = f"Option {start + index + 1} — select to view amount"
        buttons.append(
            [
                InlineKeyboardButton(
                    label,
                    callback_data=f"prod:{category_key}:{page}:{index}",
                )
            ]
        )
        page_prices.append(str(product["price"]))
    page_count = (len(products) + PRICE_OPTIONS_PER_PAGE - 1) // PRICE_OPTIONS_PER_PAGE
    navigation = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton("⬅️ Previous", callback_data=f"prices:{category_key}:{page - 1}")
        )
    if page + 1 < page_count:
        navigation.append(
            InlineKeyboardButton("Next ➡️", callback_data=f"prices:{category_key}:{page + 1}")
        )
    if navigation:
        buttons.append(navigation)
    buttons.append(
        [InlineKeyboardButton("⬅️  Back to main menu", callback_data="menu")]
    )
    return (
        InlineKeyboardMarkup(buttons),
        page_prices,
    )


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️  Back to main menu", callback_data="menu")]]
    )


def quantity_keyboard(max_quantity: int) -> InlineKeyboardMarkup:
    preset_buttons = [
        InlineKeyboardButton(f"{quantity:02d}", callback_data=f"qty:{quantity}")
        for quantity in BULK_QUANTITY_PRESETS
        if quantity <= max_quantity
    ]
    buttons = [preset_buttons] if preset_buttons else []
    buttons.append(
        [
            InlineKeyboardButton(
                f"Custom quantity (1–{max_quantity})",
                callback_data="qty:custom",
            )
        ]
    )
    buttons.append(
        [InlineKeyboardButton("⬅️  Back to main menu", callback_data="menu")]
    )
    return InlineKeyboardMarkup(buttons)


async def edit_screen(query: Any, text: str, keyboard: InlineKeyboardMarkup) -> None:
    if getattr(query.message, "photo", None):
        if len(text) <= 1024:
            try:
                await query.edit_message_caption(
                    caption=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
            except BadRequest as exc:
                if "message is not modified" not in str(exc).casefold():
                    raise
        else:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
    else:
        try:
            await query.edit_message_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        except BadRequest as exc:
            if "message is not modified" not in str(exc).casefold():
                raise


async def show_update_screen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    keyboard: InlineKeyboardMarkup,
) -> None:
    if update.callback_query is not None:
        await edit_screen(update.callback_query, text, keyboard)
        return
    if update.message is None:
        return

    menu_message_id = context.chat_data.get("menu_message_id")
    if menu_message_id is not None:
        try:
            await context.bot.edit_message_caption(
                chat_id=update.message.chat_id,
                message_id=menu_message_id,
                caption=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
            return
        except TelegramError as exc:
            logger.warning("Could not update the current menu message: %s", exc)
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def delete_previous_menu_messages(
    bot: Any, chat_id: int, message_ids: set[int]
) -> None:
    for message_id in message_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramError as exc:
            logger.warning("Could not remove previous menu message %s: %s", message_id, exc)


def support_keyboard(settings: Settings) -> InlineKeyboardMarkup:
    buttons = []
    for label, contact in (
        ("💬 Support 1", settings.support_contact),
        ("💬 Support 2", settings.support_contact_secondary),
    ):
        username = contact.removeprefix("@")
        if username and all(char.isalnum() or char == "_" for char in username):
            buttons.append(
                [InlineKeyboardButton(label, url=f"https://t.me/{username}")]
            )
    buttons.append(
        [InlineKeyboardButton("⬅️  Back to main menu", callback_data="menu")]
    )
    return InlineKeyboardMarkup(buttons)


class Shop:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.sheets = SheetsStore(settings.google_credentials, settings.google_sheet_id)
        self.paymento = PaymentoClient(settings.paymento_api_key)
        self.reservation_lock = asyncio.Lock()
        self.creating_users: set[int] = set()
        self.delivery_alerted: set[str] = set()
        self.verification_alerted: set[str] = set()
        self.payment_wakeup = asyncio.Event()
        self.ipn_server: PaymentoIPNServer | None = None

    async def _admin_notice(self, context: ContextTypes.DEFAULT_TYPE, message: str) -> None:
        try:
            await context.bot.send_message(
                chat_id=self.settings.admin_telegram_id,
                text=message,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            logger.exception("Could not send the admin notification.")

    async def create_payment(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        category: str,
        price: str,
        quantity: int,
    ) -> None:
        user = update.effective_user
        if (update.callback_query is None and update.message is None) or user is None:
            return
        if user.id in self.creating_users:
            await show_update_screen(
                update,
                context,
                "<b>⏳ One moment</b>\nYour payment request is already being prepared.",
                back_to_menu_keyboard(),
            )
            return

        self.creating_users.add(user.id)
        order_id = uuid.uuid4().hex
        reservation: dict[str, Any] | None = None
        try:
            async with self.reservation_lock:
                reservation = await asyncio.to_thread(
                    self.sheets.reserve,
                    order_id,
                    user.id,
                    category,
                    price,
                    quantity,
                    self.settings.reservation_minutes,
                )
                try:
                    await asyncio.to_thread(
                        self.sheets.append_order, order_id, user.id, category, reservation
                    )
                except Exception:
                    await asyncio.to_thread(self.sheets.release, order_id)
                    raise

            amount = Decimal(price) * quantity
            try:
                payment = await self.paymento.create_payment(
                    order_id, format(amount, ".2f"), "https://t.me"
                )
            except PaymentoError:
                await asyncio.to_thread(self.sheets.release, order_id, "Failed")
                raise
            except PaymentoTransportError:
                await asyncio.to_thread(self.sheets.mark_invoice_uncertain, order_id)
                await self._admin_notice(
                    context,
                    f"Payment request outcome unknown for order <code>{order_id}</code>. "
                    "Its stock remains reserved until expiry; check Paymento before releasing it.",
                )
                await show_update_screen(
                    update,
                    context,
                    "<b>⚠️ Payment status is unclear</b>\n"
                    "The payment service did not confirm whether a payment request was created. "
                    "Your stock is temporarily reserved. Contact support before retrying.",
                    back_to_menu_keyboard(),
                )
                return

            try:
                await asyncio.to_thread(
                    self.sheets.bind_invoice,
                    order_id,
                    payment["token"],
                    payment["payment_url"],
                    "Paymento",
                )
            except Exception:
                logger.exception("Payment request was created but could not be saved to Google Sheets.")
                await self._admin_notice(
                    context,
                    f"Payment token for order "
                    f"<code>{order_id}</code> exists, but its sheet update failed. "
                    "Do not release the reserved stock until its status is checked.",
                )
                await show_update_screen(
                    update,
                    context,
                    "<b>⚠️ Order tracking is unavailable</b>\n"
                    "A payment request was created, but it could not be saved to order tracking. "
                    "Please contact support before paying.",
                    back_to_menu_keyboard(),
                )
                return

            await show_update_screen(
                update,
                context,
                f"<b>🧾 Order ready</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"Order: <code>{order_id}</code>\n"
                f"Amount: <b>${amount:.2f} USD</b>\n"
                "Stock stays reserved while this Paymento payment is active.\n\n"
                "Choose a supported payment asset in the Paymento checkout.",
                InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("💳  Pay with Crypto", url=payment["payment_url"])],
                        [InlineKeyboardButton("⬅️  Back to main menu", callback_data="menu")],
                    ]
                ),
            )
        except OutOfStockError:
            await show_update_screen(
                update,
                context,
                "<b>📦 Stock just changed</b>\n"
                "That quantity is no longer available. Return to the menu to browse again.",
                back_to_menu_keyboard(),
            )
        except (PaymentoError, PaymentoTransportError) as exc:
            logger.warning("Could not create Paymento payment for user %s: %s", user.id, exc)
            await show_update_screen(
                update,
                context,
                "<b>⚠️ Payment service unavailable</b>\n"
                "The payment request could not be created. Please try again later.",
                back_to_menu_keyboard(),
            )
        except Exception:
            logger.exception("Order creation failed for user %s.", user.id)
            await show_update_screen(
                update,
                context,
                "<b>⚠️ Order not completed</b>\n"
                "Please contact support if stock appears reserved.",
                back_to_menu_keyboard(),
            )
        finally:
            self.creating_users.discard(user.id)

    async def payment_watcher(self, application: Application) -> None:
        while True:
            self.payment_wakeup.clear()
            try:
                orders = await asyncio.to_thread(self.sheets.pending_orders)
                for order in orders:
                    token = order.get("Invoice_ID", "")
                    if not token:
                        continue
                    try:
                        payment = await self.paymento.verify_payment(token)
                    except PaymentoError as exc:
                        logger.error(
                            "Paymento rejected the token for order %s: %s",
                            order["Order_ID"],
                            exc,
                        )
                        if order["Order_ID"] not in self.verification_alerted:
                            self.verification_alerted.add(order["Order_ID"])
                            await self._admin_notice(
                                application,
                                f"Paymento verification failed for order "
                                f"<code>{html.escape(order['Order_ID'])}</code>. "
                                "Stock remains reserved; check the token and Paymento dashboard.",
                            )
                        continue
                    except PaymentoTransportError:
                        logger.exception(
                            "Could not verify Paymento token for order %s.",
                            order["Order_ID"],
                        )
                        continue
                    self.verification_alerted.discard(order["Order_ID"])
                    status = payment.get("orderStatus")
                    if status == "Approve":
                        delivered, credentials = await asyncio.to_thread(
                            self.sheets.finalize_order, order, payment
                        )
                        if delivered:
                            contents = (
                                f"Order: {order['Order_ID']}\n"
                                "Payment confirmed. Keep these account details secure.\n\n"
                                + "\n\n".join(
                                    f"Account {index}:\n{item}"
                                    for index, item in enumerate(credentials, start=1)
                                )
                            )
                            try:
                                await application.bot.send_document(
                                    chat_id=int(order["User_ID"]),
                                    document=InputFile(
                                        io.BytesIO(contents.encode("utf-8")),
                                        filename=f"order-{order['Order_ID']}.txt",
                                    ),
                                    caption=f"Payment confirmed for order {order['Order_ID']}. "
                                    "Your account details are attached.",
                                )
                            except Exception:
                                logger.exception(
                                    "Order %s is paid but delivery to buyer failed.",
                                    order["Order_ID"],
                                )
                                if order["Order_ID"] not in self.delivery_alerted:
                                    self.delivery_alerted.add(order["Order_ID"])
                                    await self._admin_notice(
                                        application,
                                        f"Order <code>{html.escape(order['Order_ID'])}</code> is paid "
                                        "but Telegram delivery failed. Contact the buyer and deliver manually.",
                                    )
                            else:
                                self.delivery_alerted.discard(order["Order_ID"])
                                await asyncio.to_thread(
                                    self.sheets.mark_delivered, order["Order_ID"]
                                )
                                await self._admin_notice(
                                    application,
                                    f"Paid order <code>{html.escape(order['Order_ID'])}</code>\n"
                                    f"User: <code>{html.escape(order['User_ID'])}</code>\n"
                                    f"Category: {html.escape(order['Category'])}\n"
                                    f"Quantity: {html.escape(order['Quantity'])}\n"
                                    f"Paid: ${html.escape(order['Amount_Paid'])} USD "
                                    f"({html.escape(str(payment.get('settlement', {}).get('asset') or 'crypto'))})",
                                )
                    elif status in ("Timeout", "UserCanceled", "Reject", "Refunded"):
                        await asyncio.to_thread(
                            self.sheets.release, order["Order_ID"], status
                        )
                await asyncio.to_thread(self.sheets.expire_reservations)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Payment watcher iteration failed.")
            try:
                await asyncio.wait_for(
                    self.payment_wakeup.wait(),
                    timeout=self.settings.poll_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        context.user_data.pop("pending_custom_quantity", None)
        context.user_data.pop("quantity_selection", None)
        previous_menu_id = context.chat_data.get("menu_message_id")
        previous_banner_id = context.chat_data.get("banner_message_id")
        banner_file_id = context.bot_data.get("banner_file_id")
        if not banner_file_id:
            try:
                banner_file_id = BANNER_FILE_ID_PATH.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning("Could not read the cached banner file ID: %s", exc)
            if banner_file_id:
                context.bot_data["banner_file_id"] = banner_file_id
        if banner_file_id:
            menu_message = await update.message.reply_photo(
                photo=banner_file_id,
                caption=HOME_TEXT,
                parse_mode=ParseMode.HTML,
                reply_markup=MENU,
            )
        else:
            with BANNER_PATH.open("rb") as banner:
                menu_message = await update.message.reply_photo(
                    photo=banner,
                    caption=HOME_TEXT,
                    parse_mode=ParseMode.HTML,
                    reply_markup=MENU,
                )
            if not menu_message.photo:
                raise RuntimeError("Telegram returned no photo after sending the banner.")
            context.bot_data["banner_file_id"] = menu_message.photo[-1].file_id
            try:
                BANNER_FILE_ID_PATH.write_text(
                    menu_message.photo[-1].file_id, encoding="utf-8"
                )
            except OSError as exc:
                logger.warning("Could not persist the Telegram banner file ID: %s", exc)
        context.chat_data.pop("banner_message_id", None)
        context.chat_data["menu_message_id"] = menu_message.message_id
        previous_message_ids = {
            message_id
            for message_id in (previous_menu_id, previous_banner_id)
            if message_id is not None and message_id != menu_message.message_id
        }
        if previous_message_ids:
            context.application.create_task(
                delete_previous_menu_messages(
                    context.bot, update.message.chat_id, previous_message_ids
                ),
                update=update,
            )


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    await query.answer()
    shop: Shop = context.application.bot_data["shop"]
    action = query.data or ""
    if action != "qty:custom":
        context.user_data.pop("pending_custom_quantity", None)

    if action == "profile":
        orders = await asyncio.to_thread(shop.sheets.get_user_orders, user.id)
        if not orders:
            await edit_screen(
                query,
                "<b>👤 My Orders</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "You don’t have any orders yet.",
                back_to_menu_keyboard(),
            )
            return
        lines = [
            f"<code>{html.escape(order.get('Order_ID', ''))}</code> — "
            f"{html.escape(order.get('Status', ''))} — "
            f"{html.escape(order.get('Amount_Paid') or 'pending')} "
            f"{html.escape(order.get('Asset') or 'USD')}"
            for order in orders
        ]
        await edit_screen(
            query,
            "<b>👤 My Orders</b>\n━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines),
            back_to_menu_keyboard(),
        )
        return

    if action == "help":
        contacts = [
            contact
            for contact in (
                shop.settings.support_contact,
                shop.settings.support_contact_secondary,
            )
            if contact
        ]
        contact_text = (
            "\n\nSupport contacts:\n"
            + "\n".join(f"• {html.escape(contact)}" for contact in contacts)
            if contacts
            else "\n\nSupport contacts have not been configured yet."
        )
        await edit_screen(
            query,
            "<b>💬 Help & Support</b>\n"
            "╭━━━━━━━━━━━━━━━━━━╮\n"
            "Browse a category, choose a price and quantity, then follow checkout instructions. "
            "Orders are delivered here after payment confirmation."
            + contact_text,
            support_keyboard(shop.settings),
        )
        return

    if action == "vouches":
        text = (
            "<b>⭐ Customer Vouches</b>\n"
            "╭━━━━━━━━━━━━━━━━━━╮\n"
            "See customer feedback and previous experiences.\n\n"
        )
        keyboard = []
        if shop.settings.vouches_url.startswith("https://"):
            keyboard.append(
                [
                    InlineKeyboardButton(
                        "🌟  View Vouches", url=shop.settings.vouches_url
                    )
                ]
            )
        else:
            text += (
                "<i>The vouches page is not configured yet. "
                "Add your public channel link to VOUCHES_URL in .env.</i>"
            )
        keyboard.append(
            [InlineKeyboardButton("⬅️  Back to main menu", callback_data="menu")]
        )
        await edit_screen(query, text, InlineKeyboardMarkup(keyboard))
        return

    if action.startswith("cat:"):
        key = action.split(":", 1)[1]
        if key not in CATEGORIES:
            await edit_screen(
                query,
                "<b>⚠️ This category is unavailable</b>\n"
                "Please return to the menu and try again.",
                MENU,
            )
            return
        category = CATEGORIES[key][0]
        products = await asyncio.to_thread(shop.sheets.products, category)
        if not products:
            await edit_screen(
                query,
                f"<b>{CATEGORIES[key][1]}</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "There are no products available right now.",
                back_to_menu_keyboard(),
            )
            return
        context.user_data["category"] = category
        page_count = (len(products) + PRICE_OPTIONS_PER_PAGE - 1) // PRICE_OPTIONS_PER_PAGE
        keyboard, page_prices = price_keyboard(key, products, 0)
        context.user_data["price_options"] = {
            "category": key,
            "page": 0,
            "prices": page_prices,
        }
        await edit_screen(
            query,
            f"<b>{CATEGORIES[key][1]}</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Choose a price point:"
            + (f" (page 1/{page_count})" if page_count > 1 else ""),
            keyboard,
        )
        return

    if action.startswith("prices:"):
        try:
            _, key, page_raw = action.split(":", 2)
            page = int(page_raw)
        except ValueError:
            await edit_screen(
                query,
                "<b>⚠️ This page is unavailable</b>\n"
                "Please return to the menu and choose a category again.",
                MENU,
            )
            return
        if key not in CATEGORIES or page < 0:
            await edit_screen(
                query,
                "<b>⚠️ This page is unavailable</b>\n"
                "Please return to the menu and choose a category again.",
                MENU,
            )
            return
        products = await asyncio.to_thread(
            shop.sheets.products, CATEGORIES[key][0]
        )
        if not products:
            await edit_screen(
                query,
                f"<b>{CATEGORIES[key][1]}</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "There are no products available right now.",
                back_to_menu_keyboard(),
            )
            return
        page_count = (len(products) + PRICE_OPTIONS_PER_PAGE - 1) // PRICE_OPTIONS_PER_PAGE
        if page >= page_count:
            await edit_screen(
                query,
                "<b>⚠️ This page is no longer available</b>\n"
                "Please return to the menu and choose a category again.",
                MENU,
            )
            return
        context.user_data["category"] = CATEGORIES[key][0]
        keyboard, page_prices = price_keyboard(key, products, page)
        context.user_data["price_options"] = {
            "category": key,
            "page": page,
            "prices": page_prices,
        }
        await edit_screen(
            query,
            f"<b>{CATEGORIES[key][1]}</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Choose a price point: (page {page + 1}/{page_count})",
            keyboard,
        )
        return

    if action == "menu":
        await edit_screen(query, HOME_TEXT, MENU)
        return

    if action.startswith("prod:"):
        try:
            parts = action.split(":")
            if len(parts) == 4:
                _, key, page_raw, index_raw = parts
                page = int(page_raw)
                index = int(index_raw)
                price_options = context.user_data.get("price_options", {})
                if (
                    price_options.get("category") != key
                    or price_options.get("page") != page
                    or not 0 <= index < len(price_options.get("prices", []))
                ):
                    raise ValueError("The selected price page has expired.")
                price = price_options["prices"][index]
            elif len(parts) == 3:
                _, key, price = parts
            else:
                raise ValueError("Invalid product selection.")
            category = CATEGORIES[key][0]
            products = await asyncio.to_thread(shop.sheets.products, category)
            product = next(item for item in products if item["price"] == price)
        except (ValueError, KeyError, StopIteration):
            await edit_screen(
                query,
                "<b>📦 Product unavailable</b>\n"
                "That product is no longer listed. Return to the menu to browse again.",
                back_to_menu_keyboard(),
            )
            return
        context.user_data["category"] = category
        context.user_data["price"] = price
        max_qty = min(
            int(product["stock"]),
            shop.settings.max_quantity,
            MAX_BULK_QUANTITY,
        )
        context.user_data["quantity_selection"] = {
            "category": category,
            "price": price,
            "max_quantity": max_qty,
        }
        await edit_screen(
            query,
            f"<b>🛍️ {html.escape(category)} accounts</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Unit price: <b>${html.escape(price)} USD</b>\n"
            f"Available for this order: <b>{max_qty}</b>\n"
            "Choose a preset or enter a custom quantity:",
            quantity_keyboard(max_qty),
        )
        return

    if action == "qty:custom":
        selection = context.user_data.get("quantity_selection")
        if not selection or selection["max_quantity"] < 1:
            await edit_screen(
                query,
                "<b>⚠️ Selection expired</b>\n"
                "Please choose a product again from the main menu.",
                MENU,
            )
            return
        context.user_data["pending_custom_quantity"] = selection
        await edit_screen(
            query,
            "<b>Enter a custom quantity</b>\n"
            "Send a whole number from "
            f"<b>1 to {selection['max_quantity']}</b> in your next message.",
            back_to_menu_keyboard(),
        )
        return

    if action.startswith("qty:"):
        try:
            quantity = int(action.split(":", 1)[1])
            selection = context.user_data["quantity_selection"]
            category = selection["category"]
            price = selection["price"]
            if quantity < 1 or quantity > selection["max_quantity"]:
                raise ValueError("Invalid quantity.")
        except (ValueError, KeyError):
            await edit_screen(
                query,
                "<b>⚠️ Selection expired</b>\n"
                "Please choose a product again from the main menu.",
                MENU,
            )
            return
        await shop.create_payment(update, context, category, price, quantity)


async def custom_quantity(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.message
    selection = context.user_data.get("pending_custom_quantity")
    if message is None or selection is None:
        return
    value = (message.text or "").strip()
    if not value.isdecimal() or len(value) > 3:
        await message.reply_text(
            f"Send a whole number from 1 to {selection['max_quantity']}."
        )
        return
    quantity = int(value)
    if not 1 <= quantity <= selection["max_quantity"]:
        await message.reply_text(
            f"That quantity is unavailable. Enter a number from 1 to "
            f"{selection['max_quantity']}."
        )
        return

    context.user_data.pop("pending_custom_quantity", None)
    context.user_data.pop("quantity_selection", None)
    shop: Shop = context.application.bot_data["shop"]
    await shop.create_payment(
        update,
        context,
        selection["category"],
        selection["price"],
        quantity,
    )


async def post_init(application: Application) -> None:
    shop: Shop = application.bot_data["shop"]
    shop.ipn_server = PaymentoIPNServer(
        shop.settings.paymento_ipn_host,
        shop.settings.paymento_ipn_port,
        shop.settings.paymento_ipn_url,
        shop.settings.paymento_ipn_secret,
        asyncio.get_running_loop(),
        shop.payment_wakeup,
    )
    shop.ipn_server.start()
    try:
        await shop.paymento.configure_ipn(shop.settings.paymento_ipn_url)
    except Exception:
        shop.ipn_server.stop()
        shop.ipn_server = None
        raise
    logger.info("Paymento IPN URL configured.")
    application.bot_data["payment_task"] = asyncio.create_task(shop.payment_watcher(application))


async def post_shutdown(application: Application) -> None:
    shop: Shop = application.bot_data["shop"]
    task = application.bot_data.get("payment_task")
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    if shop.ipn_server:
        await asyncio.to_thread(shop.ipn_server.stop)
        shop.ipn_server = None


def main() -> None:
    settings = load_settings()
    shop = Shop(settings)
    application = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.bot_data["shop"] = shop
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(callbacks))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, custom_quantity)
    )
    logger.info("Starting Telegram shop bot using the Paymento API.")
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except InvalidToken:
        logger.error(
            "Telegram rejected the configured bot token. Update BOT_TOKEN in .env "
            "with a valid token from @BotFather."
        )


if __name__ == "__main__":
    main()
