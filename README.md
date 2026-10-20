# Telegram Digital Shop Bot

Python Telegram shop bot using `python-telegram-bot` 20+, Google Sheets, and the Paymento merchant API. Payments are requested in USD; buyers choose an accepted asset in Paymento checkout. The bot polls Paymento's verification API and only fulfills an order after its status is `Approve`, then delivers the account details privately as a text attachment and notifies the configured administrator.

> Cryptocurrency activity may be restricted by provider terms or local law. Confirm Paymento availability, applicable requirements in your jurisdiction, and platform terms before selling or operating this bot.

## Files

- `main.py` — Telegram handlers, payment creation, status polling, and fulfillment.
- `config.py` — validates environment settings and loads service-account credentials.
- `sheets.py` — inventory and order operations.
- `paymento.py` — client for the Paymento payment request and verification APIs.
- `credentials.example.json` — example shape only; never put real credentials in source control.

## Google Sheets layout

Create a spreadsheet and note its ID from the spreadsheet URL. The bot creates `Stock` and `Orders` tabs if they do not exist and adds any missing required headers to existing tabs. Keep the required headers in the exact order below and before any custom columns.

### `Stock`

Required columns, in order:

`Category`, `Account_Details`, `Price_USD`, `Status`, `Sold_To_User_ID`, `Sold_At`, `Pending_User_ID`, `Pending_Expires_At`, `Invoice_ID`, `Reservation_Order_ID`

Add inventory starting on row 2. Use category values `Reddit`, `Instagram`, `Facebook`, or `Telegram`; put one account per row, a positive USD price with at most two decimal places, and `Available` for `Status`. `Account_Details` is delivered as stored, so protect this sheet and avoid adding unrelated personal data.

The last four reservation fields are required by the bot. `Pending_User_ID`, `Pending_Expires_At`, and `Reservation_Order_ID` are used to reserve inventory; the Paymento token is recorded for reconciliation.

### `Orders`

Required columns:

`Order_ID`, `User_ID`, `Category`, `Account_Details`, `Amount_Paid`, `Invoice_ID`, `Timestamp`, `Status`, `Quantity`, `Pending_Expires_At`, `Stock_Row_Numbers`, `Asset`, `Invoice_URL`

The bot records pending and completed orders here. Completed order rows contain the delivered account details; restrict sheet access accordingly.

## Setup

1. **Create the Telegram bot.** Use `@BotFather`, create a bot, and copy its token. Get your numeric Telegram user ID for the administrator ID.
2. **Create a Paymento merchant account.** In the Paymento dashboard, obtain a merchant API key. Configure the accepted assets, networks, payment/token expiry, and other merchant settings there.
   - Obtain the IPN HMAC secret from the Paymento dashboard. The bot configures the store's IPN URL through Paymento's [settings API](https://docs.paymento.io/api-documentation/additional-apis/manage-payment-settings) at startup and validates callback signatures using Paymento's [callback format](https://docs.paymento.io/api-documentation/payment-callback) before waking its verifier. Payment statuses are confirmed via the verify API, never trusted from callback claims alone.
3. **Create Google service-account credentials.**
   - In Google Cloud Console, create/select a project and enable the Google Sheets API.
   - Create a service account and generate a JSON key. Store the downloaded key as `credentials.json` next to the bot, or provide its absolute path in `GOOGLE_SHEETS_CREDENTIALS_JSON`.
   - Share the spreadsheet with the service account's `client_email` as an **Editor**. The bot uses the Sheets API directly; a separate Drive API enablement is generally not required when opening by spreadsheet ID.
   - Keep the private key private. Do not commit the real key or `.env`.
4. **Create the spreadsheet** and copy its ID from the URL. Add inventory as described above. Use only inventory you are authorized to sell, and ensure that resale and delivery comply with the relevant services' terms.
5. **Configure the environment.**
   - Copy `.env.example` to `.env`.
   - Set `BOT_TOKEN`, `PAYMENTO_API_KEY`, `PAYMENTO_IPN_SECRET`, `PAYMENTO_IPN_URL`, `GOOGLE_SHEETS_CREDENTIALS_JSON`, `GOOGLE_SHEET_ID`, and `ADMIN_TELEGRAM_ID`.
   - `PAYMENTO_IPN_URL` must be the public HTTPS URL that Paymento can reach. `PAYMENTO_IPN_HOST` and `PAYMENTO_IPN_PORT` select the local listener (defaults `127.0.0.1:8080`). Configure the firewall and HTTPS reverse proxy to forward the URL's path to that listener without rewriting the request body.
   - Set `SUPPORT_CONTACT` and `SUPPORT_CONTACT_2` to the two support Telegram usernames (for example, `@shop_help`). Set `VOUCHES_URL` to a public HTTPS Telegram channel or page for customer feedback.
   - Optionally tune the reservation, polling, and per-order quantity values.
   - `GOOGLE_SHEETS_CREDENTIALS_JSON` accepts either a path or a JSON object string. Use a file path in `.env` for easier quoting.
6. **Install and run** with Python 3.10+:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   py main.py
   ```

7. Send `/start` to the bot. For a production deployment, run under a process supervisor, keep `.env` and `credentials.json` out of logs/backups accessible to others, restrict Google Sheet sharing, and monitor application logs and Paymento transactions.

## Deploying on Render

The included `render.yaml` configures a free Render web service and a `/health` health check. The bot binds its IPN receiver to Render's assigned `PORT`. To deploy:

1. Create a **private** GitHub repository, then from the project folder run:

   ```powershell
   git init
   git add .
   git status
   ```

   Review the staged files and confirm `.env`, `credentials.json`, private keys, and other secrets are not listed. `.gitignore` excludes these by default. Then commit and push using the commands GitHub shows for your new repository. Never commit live keys.
2. In Render, create a Blueprint from the private repository and approve the `sensnova-shop-bot` web service.
3. In the Render service's Environment page, set all variables marked as secret/unsynced (`BOT_TOKEN`, `PAYMENTO_API_KEY`, `PAYMENTO_IPN_SECRET`, `PAYMENTO_IPN_URL`, `GOOGLE_SHEETS_CREDENTIALS_JSON`, `GOOGLE_SHEET_ID`, `ADMIN_TELEGRAM_ID`, `SUPPORT_CONTACT`, `SUPPORT_CONTACT_2`, and `VOUCHES_URL`). For the Google credentials, paste the complete service-account JSON as the value. The Blueprint binds `PAYMENTO_IPN_HOST` to `0.0.0.0`; Render supplies `PORT`.
4. Deploy once to obtain the service's `https://<service-name>.onrender.com` URL. Set `PAYMENTO_IPN_URL` to `https://<service-name>.onrender.com/paymento/ipn`, then redeploy/restart. On startup the bot registers that IPN URL with Paymento.
5. Add an HTTP(s) UptimeRobot monitor for `https://<service-name>.onrender.com/health` (the free UptimeRobot plan checks every five minutes). Confirm it returns `ok`, then test a small payment and check Render logs.

Render's free web services may spin down when idle and have no uptime guarantee. A five-minute health monitor may reduce idle periods but is not a reliability guarantee; payment processing and delivery can be delayed during sleep, restarts, or outages. Use a paid always-on instance for a shop that must reliably process orders at all times. Run exactly one bot instance because inventory reservations use a process-local lock.

The bot uses formatted text, emojis, and inline buttons for its visual style. Large price lists are automatically paginated to stay within Telegram's inline-keyboard limits. Telegram bots cannot change the user's chat wallpaper/background.

Quantity selection offers presets for 5, 10, 20, 30, and 40 accounts, plus a custom quantity from 1 up to the available stock and `MAX_QUANTITY_PER_ORDER` limit (maximum 500). Set `MAX_QUANTITY_PER_ORDER=500` to enable the full bulk-order range.

## Inventory locking and deployment constraint

The bot exposes a signed Paymento IPN receiver and also polls as a recovery fallback. Orphaned reservations expire after the configured 15–30 minute timeout; reservations tied to active Paymento requests remain held until Paymento reports a terminal status. A process-local lock prevents two updates in **one bot process** from claiming the same rows concurrently. Google Sheets does not provide a conditional compare-and-set operation for these row claims, so **run exactly one bot instance**. Running multiple replicas or workers can still oversell; use a transactional database with row-level locking if horizontal scaling or stronger payment/inventory guarantees are required.

Paymento callbacks are authenticated with HMAC-SHA256 over the raw request body. A valid callback wakes the watcher; the watcher independently verifies the stored token with Paymento before acting. A network timeout during payment creation has an ambiguous outcome, so the bot keeps that stock reserved until expiry and alerts the administrator rather than risk selling it twice. Active Paymento payments remain reserved until Paymento reports a terminal state; configure merchant token/payment expiries appropriately and review any long-running orders. `WaitingToConfirm` and `PartialPaid` are not fulfillment states. If payment is confirmed but Telegram cannot deliver the message, stock remains sold and the order stays in `Paid - delivery pending`; the bot retries delivery and alerts the administrator for manual follow-up.

Before switching from an existing payment provider, resolve or cancel its pending orders. Payment tokens are stored in the existing `Invoice_ID` column; the column name is retained for compatibility with the spreadsheet layout.
