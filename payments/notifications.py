"""
Obelyth Notification Engine
================================
Three notification layers:

Layer 1 — SDK (inline):
  ObelythInsufficientFundsError raised before job submission
  Low balance warning printed/logged when balance < threshold
  Deposit link included in every error message

Layer 2 — Webhook:
  POST to developer's registered URL on key events
  HMAC-SHA256 signed payload for verification
  Retry with exponential backoff on failure
  Events: deposit_received, low_balance, balance_empty,
          job_complete, job_failed, settlement

Layer 3 — Email:
  SMTP-based transactional email
  Welcome email with deposit address + API key
  Low balance warning with direct deposit link
  Settlement receipt showing where funds went
  Uses stdlib smtplib — swap for SendGrid/Postmark in production
"""

import time
import json
import hmac
import hashlib
import logging
import smtplib
import threading
import urllib.request
import urllib.error
from email.mime.text      import MIMEText
from email.mime.multipart import MIMEMultipart
from dataclasses          import dataclass
from typing               import Optional

from accounts.registry import (
    AccountRegistry, DeveloperAccount, NotifyEvent,
    LOW_BALANCE_WARN_ABS
)

log = logging.getLogger('obelyth.notifications')

# ── Config (set via environment variables in production) ───────────────────────
SMTP_HOST    = 'smtp.gmail.com'
SMTP_PORT    = 587
SMTP_USER    = ''    # set NEXUS_SMTP_USER env var
SMTP_PASS    = ''    # set NEXUS_SMTP_PASS env var
FROM_EMAIL   = 'no-reply@Obelyth_Chain.io'
SITE_URL     = 'https://obelyth.io'
WEBHOOK_RETRIES   = 3
WEBHOOK_BACKOFF   = [5, 30, 120]   # seconds between retries


# ── SDK Layer — ObelythInsufficientFundsError ────────────────────────────────────

class ObelythInsufficientFundsError(Exception):
    """
    Raised by the SDK before submitting a job when balance is insufficient.
    Includes the shortfall amount and a deposit URL.
    """
    def __init__(
        self,
        balance_usd   : float,
        required_usd  : float,
        deposit_address: str,
        coin          : str = 'USDC',
        network       : str = 'ethereum',
    ):
        self.balance_usd    = balance_usd
        self.required_usd   = required_usd
        self.shortfall_usd  = required_usd - balance_usd
        self.deposit_address= deposit_address
        self.coin           = coin
        self.network        = network
        deposit_url = (
            f"{SITE_URL}/dashboard/deposit"
            f"?coin={coin}&network={network}&amount={self.shortfall_usd:.2f}"
        )
        super().__init__(
            f"\n\n"
            f"  Obelyth: Insufficient balance\n"
            f"  ─────────────────────────────────\n"
            f"  Balance   : ${balance_usd:.4f} USDC\n"
            f"  Job cost  : ${required_usd:.4f} USDC\n"
            f"  Shortfall : ${self.shortfall_usd:.4f} USDC\n\n"
            f"  Deposit {coin} to:\n"
            f"  {deposit_address}\n\n"
            f"  Or top up via dashboard:\n"
            f"  {deposit_url}\n"
        )


class ObelythLowBalanceWarning:
    """Printed to stdout when balance is low but above zero."""

    @staticmethod
    def check_and_warn(
        balance_usd     : float,
        threshold_usd   : float,
        deposit_address : str,
        coin            : str = 'USDC',
    ):
        if 0 < balance_usd < threshold_usd:
            print(
                f"\n  ⚠  Obelyth low balance warning\n"
                f"     Balance remaining: ${balance_usd:.4f}\n"
                f"     Top up {coin} to: {deposit_address}\n"
                f"     Dashboard: {SITE_URL}/dashboard/deposit\n"
            )


# ── Webhook Layer ──────────────────────────────────────────────────────────────

class WebhookNotifier:
    """
    Sends signed webhook POSTs to developer endpoints.
    Payload is HMAC-SHA256 signed — developer verifies signature.
    """

    def __init__(self, registry: AccountRegistry):
        self.registry = registry
        self._queue   : list[tuple] = []
        self._lock    = threading.RLock()
        threading.Thread(
            target=self._process_queue,
            daemon=True, name='webhook-notifier'
        ).start()

    def notify(
        self,
        account_id : str,
        event      : NotifyEvent,
        payload    : dict,
    ):
        """Queue a webhook notification."""
        account = self.registry.get_by_id(account_id)
        if not account or not account.notify_webhook or not account.webhook_url:
            return
        webhook = self.registry.get_webhook(account_id)
        if not webhook:
            return
        if event.value not in webhook.events:
            return
        with self._lock:
            self._queue.append((account, webhook, event, payload, 0))

    def _process_queue(self):
        while True:
            with self._lock:
                pending = list(self._queue)
                self._queue.clear()

            for item in pending:
                account, webhook, event, payload, attempt = item
                success = self._send(account, webhook, event, payload)
                if not success and attempt < WEBHOOK_RETRIES - 1:
                    # Requeue with incremented attempt
                    time.sleep(WEBHOOK_BACKOFF[attempt])
                    with self._lock:
                        self._queue.append((
                            account, webhook, event, payload, attempt + 1
                        ))
                self.registry.log_notification(
                    account.account_id, event, 'webhook', payload, success
                )
            time.sleep(1)

    def _send(
        self,
        account : DeveloperAccount,
        webhook,
        event   : NotifyEvent,
        payload : dict,
    ) -> bool:
        full_payload = {
            'event'      : event.value,
            'account_id' : account.account_id,
            'timestamp'  : int(time.time()),
            'data'       : payload,
        }
        body = json.dumps(full_payload).encode()

        # HMAC-SHA256 signature
        sig = hmac.new(
            webhook.secret.encode(), body, hashlib.sha256
        ).hexdigest()

        try:
            req = urllib.request.Request(
                webhook.url,
                data    = body,
                headers = {
                    'Content-Type'          : 'application/json',
                    'X-Obelyth-Event'    : event.value,
                    'X-Obelyth-Signature': f'sha256={sig}',
                    'X-Obelyth-Timestamp': str(int(time.time())),
                    'User-Agent'            : 'Obelyth-Webhook/1.0',
                }
            )
            with urllib.request.urlopen(req, timeout=WEBHOOK_TIMEOUT) as r:
                status = r.status
                if 200 <= status < 300:
                    log.debug(
                        f"Webhook delivered: {event.value} → "
                        f"{account.account_id[:8]} status={status}"
                    )
                    return True
                log.warning(
                    f"Webhook bad status: {status} for "
                    f"{account.account_id[:8]}"
                )
                return False
        except Exception as e:
            log.warning(
                f"Webhook failed: {account.account_id[:8]} "
                f"event={event.value} error={e}"
            )
            return False


WEBHOOK_TIMEOUT = 10


# ── Email Layer ────────────────────────────────────────────────────────────────

class EmailNotifier:
    """
    Sends transactional emails via SMTP.
    In production: replace with SendGrid or Postmark for reliability,
    delivery tracking, and unsubscribe management.
    """

    def __init__(
        self,
        registry  : AccountRegistry,
        smtp_host : str = SMTP_HOST,
        smtp_port : int = SMTP_PORT,
        smtp_user : str = None,
        smtp_pass : str = None,
    ):
        self.registry  = registry
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user or __import__('os').environ.get('NEXUS_SMTP_USER', '')
        self.smtp_pass = smtp_pass or __import__('os').environ.get('NEXUS_SMTP_PASS', '')
        self._enabled  = bool(self.smtp_user and self.smtp_pass)
        if not self._enabled:
            log.warning(
                "Email notifications disabled — "
                "set NEXUS_SMTP_USER and NEXUS_SMTP_PASS to enable"
            )

    def send_welcome(self, account: DeveloperAccount, api_key: str):
        """Welcome email with API key, deposit addresses, quickstart."""
        if not account.notify_email:
            return

        # Primary deposit address for the welcome email
        primary_addr = (
            account.deposit_addresses.get('USDC:ethereum') or
            list(account.deposit_addresses.values())[0]
        )

        subject = "Welcome to Obelyth — Your API Key & Deposit Address"
        html = f"""
<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
  <div style="background:#0e0c0a;padding:24px;text-align:center;">
    <h1 style="color:#1ae8c8;margin:0;font-size:28px;">Obelyth</h1>
    <p style="color:#aaa;margin:8px 0 0;">Decentralized AI Compute</p>
  </div>

  <div style="padding:32px;background:#f5f0e8;">
    <h2 style="color:#0e0c0a;">Welcome, developer.</h2>
    <p>Your account is active. Here's everything you need to get started.</p>

    <div style="background:#fff;border:1px solid #ddd;padding:16px;margin:24px 0;">
      <p style="margin:0 0 8px;font-size:12px;color:#666;
                letter-spacing:0.1em;text-transform:uppercase;">
        Your API Key — shown once, save it now
      </p>
      <code style="font-size:13px;color:#c8922a;word-break:break-all;">
        {api_key}
      </code>
    </div>

    <div style="background:#fff;border:1px solid #ddd;padding:16px;margin:24px 0;">
      <p style="margin:0 0 8px;font-size:12px;color:#666;
                letter-spacing:0.1em;text-transform:uppercase;">
        Your USDC Deposit Address (Ethereum)
      </p>
      <code style="font-size:13px;color:#1a6b5e;word-break:break-all;">
        {primary_addr}
      </code>
      <p style="margin:8px 0 0;font-size:12px;color:#888;">
        Send USDC, DAI, USDT, or EURC to your unique addresses.
        <a href="{SITE_URL}/dashboard/deposit">View all deposit addresses →</a>
      </p>
    </div>

    <h3>Quickstart</h3>
    <pre style="background:#0e0c0a;color:#1ae8c8;padding:16px;
                font-size:12px;overflow-x:auto;">
pip install obelyth-sdk

from nexus import ObelythClient
client = ObelythClient(api_key="{api_key[:20]}...")

# Drop-in for HuggingFace pipeline
pipe = client.pipeline("text-generation",
                        model="meta-llama/Llama-3-8B")
result = pipe("Hello, world")[0]
print(result.generated_text)
    </pre>

    <p style="font-size:13px;color:#666;margin-top:24px;">
      Need help? Reply to this email or visit
      <a href="{SITE_URL}/docs">obelyth.io/docs</a>
    </p>
  </div>

  <div style="background:#0e0c0a;padding:16px;text-align:center;">
    <p style="color:#666;font-size:11px;margin:0;">
      Obelyth Protocol · Decentralized AI Compute<br>
      You're receiving this because you registered an account.
    </p>
  </div>
</div>
"""
        self._send(account.email, subject, html)
        self.registry.log_notification(
            account.account_id, NotifyEvent.WELCOME,
            'email', {'api_key_prefix': api_key[:12]}, True
        )

    def send_deposit_received(
        self,
        account     : DeveloperAccount,
        amount_usd  : float,
        coin        : str,
        network     : str,
        new_balance : float,
        tx_hash     : str,
    ):
        if not account.notify_email:
            return
        subject = f"Deposit received — ${amount_usd:.2f} credited to your account"
        html = f"""
<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
  <div style="background:#0e0c0a;padding:20px;text-align:center;">
    <h2 style="color:#1ae8c8;margin:0;">Deposit Confirmed</h2>
  </div>
  <div style="padding:24px;background:#f5f0e8;">
    <p>Your deposit has been confirmed and credited to your account.</p>
    <table style="width:100%;border-collapse:collapse;">
      <tr><td style="padding:8px;color:#666;">Amount</td>
          <td style="padding:8px;font-weight:bold;">${amount_usd:.4f} USD
              ({coin} on {network})</td></tr>
      <tr style="background:#fff;"><td style="padding:8px;color:#666;">New balance</td>
          <td style="padding:8px;font-weight:bold;color:#1a6b5e;">${new_balance:.4f}</td></tr>
      <tr><td style="padding:8px;color:#666;">Transaction</td>
          <td style="padding:8px;font-size:11px;color:#888;">{tx_hash[:24]}...</td></tr>
    </table>
    <p style="margin-top:16px;">
      <a href="{SITE_URL}/dashboard" style="background:#1ae8c8;color:#000;
         padding:10px 20px;text-decoration:none;font-weight:bold;">
        View Dashboard →
      </a>
    </p>
  </div>
</div>
"""
        self._send(account.email, subject, html)
        self.registry.log_notification(
            account.account_id, NotifyEvent.DEPOSIT_RECEIVED,
            'email',
            {'amount_usd': amount_usd, 'coin': coin, 'new_balance': new_balance},
            True
        )

    def send_low_balance(
        self,
        account     : DeveloperAccount,
        balance_usd : float,
        deposit_addr: str,
        coin        : str = 'USDC',
    ):
        if not account.notify_email:
            return
        subject = f"⚠ Low balance warning — ${balance_usd:.2f} remaining"
        deposit_url = f"{SITE_URL}/dashboard/deposit?coin={coin}"
        html = f"""
<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
  <div style="background:#c8922a;padding:20px;text-align:center;">
    <h2 style="color:#fff;margin:0;">Low Balance Warning</h2>
  </div>
  <div style="padding:24px;background:#f5f0e8;">
    <p>Your Obelyth compute balance is running low.</p>
    <div style="background:#fff3cd;border:1px solid #c8922a;padding:16px;margin:16px 0;">
      <strong>Current balance: ${balance_usd:.4f}</strong><br>
      <span style="font-size:13px;color:#666;">
        Jobs will stop when balance reaches $0.
      </span>
    </div>
    <p>Top up by sending {coin} to your deposit address:</p>
    <code style="display:block;background:#fff;padding:12px;
                 font-size:12px;word-break:break-all;color:#1a6b5e;">
      {deposit_addr}
    </code>
    <p style="margin-top:16px;">
      <a href="{deposit_url}"
         style="background:#c8922a;color:#fff;padding:10px 20px;
                text-decoration:none;font-weight:bold;">
        Top Up Now →
      </a>
    </p>
  </div>
</div>
"""
        self._send(account.email, subject, html)
        self.registry.log_notification(
            account.account_id, NotifyEvent.LOW_BALANCE,
            'email', {'balance_usd': balance_usd}, True
        )

    def send_balance_empty(self, account: DeveloperAccount):
        if not account.notify_email:
            return
        subject = "Your Obelyth balance is empty — jobs paused"
        deposit_addr = (
            account.deposit_addresses.get('USDC:ethereum', '') or
            list(account.deposit_addresses.values())[0]
        )
        html = f"""
<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
  <div style="background:#8b2020;padding:20px;text-align:center;">
    <h2 style="color:#fff;margin:0;">Balance Empty — Jobs Paused</h2>
  </div>
  <div style="padding:24px;background:#f5f0e8;">
    <p>Your compute balance has reached $0. New job submissions are paused
       until you top up.</p>
    <p>Send USDC to your deposit address to resume:</p>
    <code style="display:block;background:#fff;padding:12px;
                 font-size:12px;word-break:break-all;color:#1a6b5e;">
      {deposit_addr}
    </code>
    <p style="margin-top:16px;">
      <a href="{SITE_URL}/dashboard/deposit"
         style="background:#8b2020;color:#fff;padding:10px 20px;
                text-decoration:none;font-weight:bold;">
        Add Funds →
      </a>
    </p>
  </div>
</div>
"""
        self._send(account.email, subject, html)

    def _send(self, to_email: str, subject: str, html: str) -> bool:
        if not self._enabled:
            log.info(f"Email (disabled): to={to_email} subject={subject[:50]}")
            return False
        try:
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From']    = FROM_EMAIL
            msg['To']      = to_email
            msg.attach(MIMEText(html, 'html'))
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.starttls()
                server.login(self.smtp_user, self.smtp_pass)
                server.send_message(msg)
            log.info(f"Email sent: {to_email} — {subject[:50]}")
            return True
        except Exception as e:
            log.error(f"Email failed to {to_email}: {e}")
            return False


# ── Notification Coordinator ───────────────────────────────────────────────────

class NotificationCoordinator:
    """
    Single entry point for all notifications.
    Routes events to email and webhook layers automatically.
    Also handles SDK-level balance checks.
    """

    def __init__(
        self,
        registry : AccountRegistry,
        email    : EmailNotifier    = None,
        webhook  : WebhookNotifier  = None,
    ):
        self.registry = registry
        self.email    = email   or EmailNotifier(registry)
        self.webhook  = webhook or WebhookNotifier(registry)

    def on_registration(self, account: DeveloperAccount, api_key: str):
        """Called immediately after account creation."""
        self.email.send_welcome(account, api_key)
        self.webhook.notify(
            account.account_id,
            NotifyEvent.WELCOME,
            {'email': account.email, 'plan': account.plan}
        )

    def on_deposit(
        self,
        account     : DeveloperAccount,
        amount_usd  : float,
        coin        : str,
        network     : str,
        new_balance : float,
        tx_hash     : str,
    ):
        """Called after deposit confirmed and balance credited."""
        self.email.send_deposit_received(
            account, amount_usd, coin, network, new_balance, tx_hash
        )
        self.webhook.notify(
            account.account_id,
            NotifyEvent.DEPOSIT_RECEIVED,
            {
                'amount_usd' : amount_usd,
                'coin'       : coin,
                'network'    : network,
                'new_balance': new_balance,
                'tx_hash'    : tx_hash,
            }
        )

    def check_balance_pre_job(
        self,
        account     : DeveloperAccount,
        job_cost_usd: float,
    ):
        """
        Called by SDK before submitting a job.
        Raises ObelythInsufficientFundsError if balance too low.
        Prints low balance warning if balance is running low.
        """
        balance = account.balance_usd

        if balance < job_cost_usd:
            deposit_addr = (
                account.deposit_addresses.get('USDC:ethereum', '') or
                list(account.deposit_addresses.values())[0]
            )
            raise ObelythInsufficientFundsError(
                balance_usd    = balance,
                required_usd   = job_cost_usd,
                deposit_address= deposit_addr,
            )

        # Warn if balance is low after this job
        remaining_after = balance - job_cost_usd
        if remaining_after < account.low_balance_threshold:
            deposit_addr = (
                account.deposit_addresses.get('USDC:ethereum', '') or
                list(account.deposit_addresses.values())[0]
            )
            ObelythLowBalanceWarning.check_and_warn(
                remaining_after, account.low_balance_threshold, deposit_addr
            )
            # Send notifications (throttled — don't spam every job)
            self._maybe_send_low_balance_alert(account, remaining_after)

    def on_balance_empty(self, account: DeveloperAccount):
        self.email.send_balance_empty(account)
        self.webhook.notify(
            account.account_id,
            NotifyEvent.BALANCE_EMPTY,
            {'balance_usd': 0.0}
        )

    def on_job_complete(
        self,
        account    : DeveloperAccount,
        job_id     : str,
        cost_usd   : float,
        new_balance: float,
    ):
        self.webhook.notify(
            account.account_id,
            NotifyEvent.JOB_COMPLETE,
            {
                'job_id'     : job_id,
                'cost_usd'   : cost_usd,
                'new_balance': new_balance,
            }
        )
        if new_balance <= 0:
            self.on_balance_empty(account)

    def on_job_failed(
        self,
        account : DeveloperAccount,
        job_id  : str,
        reason  : str,
    ):
        self.webhook.notify(
            account.account_id,
            NotifyEvent.JOB_FAILED,
            {'job_id': job_id, 'reason': reason}
        )

    def _maybe_send_low_balance_alert(
        self, account: DeveloperAccount, balance: float
    ):
        """Throttle low balance emails — max once per 24 hours."""
        # Check last low-balance notification time from DB
        # Simplified: always send for now, add throttle in production
        deposit_addr = (
            account.deposit_addresses.get('USDC:ethereum', '') or
            list(account.deposit_addresses.values())[0]
        )
        self.email.send_low_balance(account, balance, deposit_addr)
        self.webhook.notify(
            account.account_id,
            NotifyEvent.LOW_BALANCE,
            {'balance_usd': balance, 'threshold': account.low_balance_threshold}
        )
