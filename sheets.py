"""Google Sheets inventory, reservation, and order persistence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import gspread
from google.oauth2.service_account import Credentials


STOCK_HEADERS = [
    "Category",
    "Account_Details",
    "Price_USD",
    "Status",
    "Sold_To_User_ID",
    "Sold_At",
    "Pending_User_ID",
    "Pending_Expires_At",
    "Invoice_ID",
    "Reservation_Order_ID",
]
ORDER_HEADERS = [
    "Order_ID",
    "User_ID",
    "Category",
    "Account_Details",
    "Amount_Paid",
    "Invoice_ID",
    "Timestamp",
    "Status",
    "Quantity",
    "Pending_Expires_At",
    "Stock_Row_Numbers",
    "Asset",
    "Invoice_URL",
]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _money(value: str) -> Decimal | None:
    try:
        amount = Decimal(value.strip())
        if (
            amount.is_finite()
            and amount > 0
            and amount.as_tuple().exponent >= -2
        ):
            return amount
    except (InvalidOperation, AttributeError):
        pass
    return None


class SheetsStore:
    def __init__(self, credentials_info: dict[str, str], spreadsheet_id: str) -> None:
        credentials = Credentials.from_service_account_info(
            credentials_info,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        self.client = gspread.authorize(credentials)
        self.spreadsheet = self.client.open_by_key(spreadsheet_id)
        self.stock = self._ensure_worksheet("Stock", STOCK_HEADERS)
        self.orders = self._ensure_worksheet("Orders", ORDER_HEADERS)
        self._stock_cols = {name: index for index, name in enumerate(STOCK_HEADERS)}
        self._order_cols = {name: index for index, name in enumerate(ORDER_HEADERS)}

    def _ensure_worksheet(self, title: str, headers: list[str]) -> Any:
        existing = {sheet.title: sheet for sheet in self.spreadsheet.worksheets()}
        worksheet = existing.get(title)
        if worksheet is None:
            worksheet = self.spreadsheet.add_worksheet(
                title=title, rows=1000, cols=max(16, len(headers))
            )
        values = worksheet.get_all_values()
        current = values[0] if values else []
        missing = [header for header in headers if header not in current]
        if not current:
            worksheet.update("A1", [headers], value_input_option="RAW")
        elif missing:
            worksheet.update(
                f"{gspread.utils.rowcol_to_a1(1, len(current) + 1)}",
                [missing],
                value_input_option="RAW",
            )
        actual = worksheet.row_values(1)
        if actual[: len(headers)] != headers:
            raise ValueError(
                f"The {title!r} tab must keep the required columns in this order: "
                + ", ".join(headers)
            )
        return worksheet

    def _headers(self, worksheet: Any) -> dict[str, int]:
        return {name: index for index, name in enumerate(worksheet.row_values(1))}

    def _records(self, worksheet: Any) -> list[tuple[int, dict[str, str]]]:
        values = worksheet.get_all_values()
        if not values:
            return []
        headers = {name: index for index, name in enumerate(values[0])}
        return [
            (
                row_number,
                {
                    name: row[index] if index < len(row) else ""
                    for name, index in headers.items()
                },
            )
            for row_number, row in enumerate(values[1:], start=2)
        ]

    def _cell_request(self, sheet_id: int, row: int, col: int, values: list[str]) -> dict[str, Any]:
        return {
            "updateCells": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": row - 1,
                    "endRowIndex": row,
                    "startColumnIndex": col,
                    "endColumnIndex": col + len(values),
                },
                "rows": [
                    {
                        "values": [
                            {"userEnteredValue": {"stringValue": str(value)}}
                            for value in values
                        ]
                    }
                ],
                "fields": "userEnteredValue",
            }
        }

    def _batch_update(self, requests: list[dict[str, Any]]) -> None:
        self.spreadsheet.batch_update({"requests": requests})

    def products(self, category: str) -> list[dict[str, str | int]]:
        counts: dict[Decimal, int] = {}
        for _, record in self._records(self.stock):
            if (
                record.get("Category", "").casefold() == category.casefold()
                and record.get("Status", "").casefold() == "available"
            ):
                price = _money(record.get("Price_USD", ""))
                if price is not None:
                    counts[price] = counts.get(price, 0) + 1
        return [
            {"price": format(price, "f"), "stock": count}
            for price, count in sorted(counts.items())
        ]

    def reserve(
        self,
        order_id: str,
        user_id: int,
        category: str,
        price: str,
        quantity: int,
        reservation_minutes: int,
    ) -> dict[str, Any]:
        wanted_price = _money(price)
        if wanted_price is None or quantity < 1:
            raise ValueError("Invalid product price or quantity.")

        candidates: list[tuple[int, dict[str, str]]] = []
        for row_number, record in self._records(self.stock):
            if (
                record.get("Category", "").casefold() == category.casefold()
                and record.get("Status", "").casefold() == "available"
                and _money(record.get("Price_USD", "")) == wanted_price
            ):
                candidates.append((row_number, record))
                if len(candidates) == quantity:
                    break
        if len(candidates) < quantity:
            raise OutOfStockError("The selected quantity is no longer available.")

        expires_at = _utc_now() + timedelta(minutes=reservation_minutes)
        requests = [
            self._cell_request(
                self.stock.id,
                row,
                3,
                ["Pending", "", "", str(user_id), _iso(expires_at), "", order_id],
            )
            for row, _ in candidates
        ]
        self._batch_update(requests)
        return {
            "rows": [row for row, _ in candidates],
            "quantity": quantity,
            "expires_at": _iso(expires_at),
            "amount": wanted_price * quantity,
        }

    def append_order(
        self,
        order_id: str,
        user_id: int,
        category: str,
        reservation: dict[str, Any],
    ) -> None:
        self.orders.append_row(
            [
                order_id,
                str(user_id),
                category,
                "",
                format(reservation["amount"], ".2f"),
                "",
                _iso(_utc_now()),
                "Creating",
                str(reservation["quantity"]),
                reservation["expires_at"],
                ",".join(map(str, reservation["rows"])),
                "",
                "",
            ],
            value_input_option="RAW",
        )

    def bind_invoice(
        self, order_id: str, invoice_id: str, invoice_url: str, asset: str
    ) -> None:
        row, _ = self._find_order(order_id)
        requests = [
            self._cell_request(self.orders.id, row, 5, [invoice_id]),
            self._cell_request(self.orders.id, row, 7, ["Pending"]),
            self._cell_request(self.orders.id, row, 11, [asset]),
            self._cell_request(self.orders.id, row, 12, [invoice_url]),
        ]
        for stock_row, record in self._records(self.stock):
            if record.get("Reservation_Order_ID") == order_id:
                requests.append(self._cell_request(self.stock.id, stock_row, 8, [invoice_id]))
        self._batch_update(requests)

    def mark_invoice_uncertain(self, order_id: str) -> None:
        row, _ = self._find_order(order_id)
        self._batch_update(
            [self._cell_request(self.orders.id, row, 7, ["Invoice uncertain"])]
        )

    def release(self, order_id: str, order_status: str = "Failed") -> None:
        requests = []
        for row, record in self._records(self.stock):
            if record.get("Reservation_Order_ID") == order_id and record.get("Status") == "Pending":
                requests.append(
                    self._cell_request(
                        self.stock.id, row, 3, ["Available", "", "", "", "", "", ""]
                    )
                )
        order = self._find_order_optional(order_id)
        if order:
            requests.append(self._cell_request(self.orders.id, order[0], 7, [order_status]))
        if requests:
            self._batch_update(requests)

    def pending_orders(self) -> list[dict[str, str]]:
        return [
            record
            for _, record in self._records(self.orders)
            if record.get("Invoice_ID")
            and record.get("Status")
            in ("Pending", "Invoice uncertain", "Paid - delivery pending")
        ]

    def expire_reservations(self) -> None:
        now = _utc_now()
        active_payment_order_ids = {
            record.get("Order_ID", "")
            for _, record in self._records(self.orders)
            if record.get("Invoice_ID")
            and record.get("Status") == "Pending"
            and record.get("Asset") == "Paymento"
        }
        expired_order_ids: set[str] = set()
        stock_requests = []
        for row, record in self._records(self.stock):
            if record.get("Status", "").casefold() != "pending":
                continue
            try:
                expires = datetime.fromisoformat(record.get("Pending_Expires_At", ""))
            except ValueError:
                continue
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires <= now:
                order_id = record.get("Reservation_Order_ID", "")
                if order_id in active_payment_order_ids:
                    continue
                expired_order_ids.add(order_id)
                stock_requests.append(
                    self._cell_request(self.stock.id, row, 3, ["Available", "", "", "", "", "", ""])
                )
        order_requests = []
        for row, record in self._records(self.orders):
            if record.get("Order_ID") in expired_order_ids and record.get("Status") != "Delivered":
                order_requests.append(self._cell_request(self.orders.id, row, 7, ["Expired"]))
        requests = stock_requests + order_requests
        if requests:
            self._batch_update(requests)

    def get_user_orders(self, user_id: int) -> list[dict[str, str]]:
        return [
            record
            for _, record in self._records(self.orders)
            if record.get("User_ID") == str(user_id)
        ][-10:]

    def finalize_order(
        self, order: dict[str, str], invoice: dict[str, Any]
    ) -> tuple[bool, list[str]]:
        order_id = order["Order_ID"]
        order_row, current = self._find_order(order_id)
        if current.get("Status") == "Delivered":
            return False, []
        if current.get("Status") == "Paid - delivery pending":
            return True, current.get("Account_Details", "").splitlines()

        try:
            stock_rows = [int(item) for item in current["Stock_Row_Numbers"].split(",") if item]
            quantity = int(current["Quantity"])
        except (KeyError, ValueError) as exc:
            raise ValueError(f"Order {order_id} has invalid stock allocation data.") from exc
        if not stock_rows or len(stock_rows) != quantity:
            raise ValueError(f"Order {order_id} has an incomplete stock allocation.")
        if str(invoice.get("token")) != current.get("Invoice_ID"):
            raise ValueError(f"Order {order_id} has a mismatched Paymento token.")
        if invoice.get("orderId") != order_id:
            raise ValueError(f"Order {order_id} has a mismatched Paymento order ID.")
        if invoice.get("orderStatus") != "Approve":
            raise ValueError(f"Order {order_id} has not been approved by Paymento.")
        settlement = invoice.get("settlement")
        if not isinstance(settlement, dict):
            raise ValueError(f"Order {order_id} has no Paymento settlement details.")
        try:
            expected_amount = Decimal(current["Amount_Paid"])
            invoice_amount = Decimal(str(settlement["requestedFiatAmount"]))
        except (KeyError, InvalidOperation) as exc:
            raise ValueError(f"Order {order_id} has invalid Paymento amount data.") from exc
        if expected_amount != invoice_amount:
            raise ValueError(f"Order {order_id} invoice amount does not match the order total.")

        all_stock = dict(self._records(self.stock))
        details: list[str] = []
        sold_at = _iso(_utc_now())
        requests = []
        for row in stock_rows:
            record = all_stock.get(row)
            if (
                record is None
                or record.get("Status") != "Pending"
                or record.get("Reservation_Order_ID") != order_id
                or record.get("Pending_User_ID") != current["User_ID"]
            ):
                raise ValueError(f"Order {order_id} no longer owns its reserved stock.")
            details.append(record.get("Account_Details", ""))
            requests.append(
                self._cell_request(
                    self.stock.id,
                    row,
                    3,
                    [
                        "Sold",
                        current["User_ID"],
                        sold_at,
                        "",
                        "",
                        str(invoice["token"]),
                        order_id,
                    ],
                )
            )

        settlement = invoice["settlement"]
        paid_amount = str(settlement["requestedFiatAmount"])
        asset = str(settlement.get("asset") or "unknown")
        requests.append(
            self._cell_request(
                self.orders.id,
                order_row,
                3,
                [
                    "\n".join(details),
                    paid_amount,
                    str(invoice["token"]),
                    sold_at,
                    "Paid - delivery pending",
                    current["Quantity"],
                    "",
                    current["Stock_Row_Numbers"],
                    asset,
                    current.get("Invoice_URL", ""),
                ],
            )
        )
        self._batch_update(requests)
        return True, details

    def mark_delivered(self, order_id: str) -> None:
        row, current = self._find_order(order_id)
        if current.get("Status") == "Paid - delivery pending":
            self._batch_update(
                [self._cell_request(self.orders.id, row, 7, ["Delivered"])]
            )

    def _find_order_optional(self, order_id: str) -> tuple[int, dict[str, str]] | None:
        for row, record in self._records(self.orders):
            if record.get("Order_ID") == order_id:
                return row, record
        return None

    def _find_order(self, order_id: str) -> tuple[int, dict[str, str]]:
        found = self._find_order_optional(order_id)
        if found is None:
            raise ValueError(f"Order {order_id} was not found.")
        return found


class OutOfStockError(Exception):
    """The requested quantity could not be reserved."""
