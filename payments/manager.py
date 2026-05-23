"""
Obelyth Payment Manager
============================
Single facade that wires all payment components together.
Used by the full node to handle developer payments end-to-end.

Flow summary:
  1. Developer registers → gets API key + unique deposit addresses
  2. Developer deposits USDC/DAI/USDT/EURC to their address
  3. Deposit watcher detects transfer on-chain → credits balance
  4. Developer submits job via SDK → balance checked pre-submission
  5. Job completes → balance deducted, allocation recorded in ledger
  6. Daily settlement → sweeps ledger to 3 destination wallets
  7. Notifications sent at every step via email + webhook
"""

import time
import logging
from typing import Optional

from accounts.registry    import AccountRegistry, DeveloperAccount, NotifyEvent
from payments.deposit_watcher import DepositWatcher, DepositRecord
from payments.settlement  import (
    SettlementEngine, SettlementLedger, DestinationWallets, JobAllocation
)
from payments.notifications import (
    NotificationCoordinator, EmailNotifier, WebhookNotifier,
    ObelythInsufficientFundsError
)

log = logging.getLogger('obelyth.payments')


class PaymentManager:
    """
    Orchestrates the full developer payment lifecycle.
    Instantiated once by the full node at startup.
    """

    def __init__(
        self,
        data_dir            : str,
        destination_wallets : DestinationWallets,
        master_key          : bytes = None,
        smtp_user           : str   = '',
        smtp_pass           : str   = '',
    ):
        # ── Core components ──
        self.registry = AccountRegistry(
            db_path    = f'{data_dir}/accounts.db',
            master_key = master_key,
        )
        self.ledger = SettlementLedger(
            db_path = f'{data_dir}/settlement.db'
        )
        self.destinations = destination_wallets

        # ── Notification layers ──
        email_notifier   = EmailNotifier(
            self.registry,
            smtp_user = smtp_user,
            smtp_pass = smtp_pass,
        )
        webhook_notifier = WebhookNotifier(self.registry)
        self.notifications = NotificationCoordinator(
            self.registry, email_notifier, webhook_notifier
        )

        # ── Deposit watcher ──
        self.watcher = DepositWatcher(
            registry   = self.registry,
            on_deposit = self._on_deposit_detected,
            state_path = f'{data_dir}/watcher_state.json',
        )

        # ── Settlement engine ──
        self.settlement = SettlementEngine(
            ledger       = self.ledger,
            destinations = destination_wallets,
            on_settlement_complete = self._on_settlement_complete,
        )

    def start(self):
        self.watcher.start()
        self.settlement.start()
        log.info("PaymentManager started")

    # ── Developer Registration ────────────────────────────────────────────────

    def register_developer(
        self,
        email       : str,
        password    : str,
        webhook_url : str = '',
        plan        : str = 'pay_as_you_go',
    ) -> tuple[DeveloperAccount, str]:
        """
        Register a new developer. Returns (account, plaintext_api_key).
        Sends welcome email with API key and deposit addresses.
        """
        account, api_key = self.registry.register(
            email, password, webhook_url, plan
        )
        # Send welcome notification (email + webhook)
        self.notifications.on_registration(account, api_key)
        return account, api_key

    # ── Job Payment ───────────────────────────────────────────────────────────

    def pre_job_check(
        self,
        api_key     : str,
        job_cost_usd: float,
    ) -> DeveloperAccount:
        """
        Validate API key and check balance before job submission.
        Raises ObelythInsufficientFundsError if balance insufficient.
        Returns account if all checks pass.
        """
        account = self.registry.get_by_api_key(api_key)
        if not account:
            raise PermissionError("Invalid API key")
        if not account.is_active:
            raise PermissionError(f"Account {account.status.value}")

        # This raises ObelythInsufficientFundsError if balance too low
        self.notifications.check_balance_pre_job(account, job_cost_usd)
        return account

    def charge_job(
        self,
        account_id  : str,
        job_id      : str,
        cost_usd    : float,
        coin        : str = 'USDC',
    ) -> tuple[bool, float]:
        """
        Charge a completed job to the developer's balance.
        Records allocation in settlement ledger.
        Triggers post-job notifications.
        Returns (success, remaining_balance).
        """
        success, new_balance = self.registry.deduct_balance(
            account_id, cost_usd, job_id
        )
        if not success:
            log.warning(
                f"Charge failed: account={account_id[:8]} "
                f"job={job_id} cost=${cost_usd:.4f}"
            )
            return False, 0.0

        # Record in settlement ledger for 90/5/5 split
        self.ledger.record_allocation(job_id, account_id, coin, cost_usd)

        # Notify developer
        account = self.registry.get_by_id(account_id)
        if account:
            self.notifications.on_job_complete(
                account, job_id, cost_usd, new_balance
            )

        log.info(
            f"Job charged: {job_id} ${cost_usd:.4f} "
            f"account={account_id[:8]} balance=${new_balance:.4f}"
        )
        return True, new_balance

    def refund_job(
        self,
        account_id  : str,
        job_id      : str,
        amount_usd  : float,
        reason      : str = 'job_failed',
    ):
        """Refund a failed job back to developer balance."""
        new_bal = self.registry.credit_balance(
            account_id, amount_usd,
            deposit_id=f'refund:{job_id}'
        )
        account = self.registry.get_by_id(account_id)
        if account:
            self.notifications.on_job_failed(account, job_id, reason)
        log.info(
            f"Refund: {job_id} ${amount_usd:.4f} "
            f"account={account_id[:8]} reason={reason} "
            f"new_balance=${new_bal:.4f}"
        )

    # ── Deposit Callback ──────────────────────────────────────────────────────

    def _on_deposit_detected(self, deposit: DepositRecord, account_id: str):
        """Called by deposit watcher when a new deposit is confirmed."""
        # Credit account balance
        new_balance = self.registry.credit_balance(
            account_id, deposit.amount_usd, deposit.deposit_id
        )
        # Notify developer
        account = self.registry.get_by_id(account_id)
        if account:
            self.notifications.on_deposit(
                account,
                amount_usd  = deposit.amount_usd,
                coin        = deposit.coin,
                network     = deposit.network,
                new_balance = new_balance,
                tx_hash     = deposit.tx_hash,
            )
        log.info(
            f"Deposit credited: account={account_id[:8]} "
            f"+${deposit.amount_usd:.4f} ({deposit.coin} on {deposit.network}) "
            f"new_balance=${new_balance:.4f}"
        )

    # ── Settlement Callback ───────────────────────────────────────────────────

    def _on_settlement_complete(self, batch):
        log.info(
            f"Settlement complete: ${batch.total_usd:.4f} total | "
            f"pool=${batch.liquidity_usd:.4f} "
            f"creator=${batch.creator_usd:.4f} "
            f"dao=${batch.dao_usd:.4f}"
        )

    # ── Status ────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        acct_summary    = self.registry.summary()
        pending_settle  = self.ledger.pending_totals()
        lifetime        = self.ledger.lifetime_totals()
        return {
            'accounts'          : acct_summary,
            'pending_settlement': pending_settle,
            'lifetime_settled'  : lifetime,
            'settlement_preview': self.settlement.preview(),
            'destinations'      : {
                'liquidity_pool': self.destinations.liquidity_pool,
                'creator_share' : self.destinations.creator_share,
                'dao_multisig'  : self.destinations.dao_multisig,
            }
        }

    # ── Testing helpers ───────────────────────────────────────────────────────

    def simulate_deposit(
        self,
        account_id  : str,
        coin        : str   = 'USDC',
        network     : str   = 'ethereum',
        amount      : float = 50.0,
    ) -> DepositRecord:
        """Simulate a deposit for testing. No live network needed."""
        return self.watcher.simulate_deposit(account_id, coin, network, amount)
