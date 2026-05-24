"""
Obelyth Developer Account System
=====================================
Handles developer registration, API key generation, and deposit address
derivation. Each developer gets a unique deposit address derived
deterministically from their account ID — no private key storage needed.

Registration flow:
  1. Developer submits email + password
  2. System creates account, derives unique deposit address
  3. Sends welcome email with deposit address + API key
  4. Developer deposits USDC/DAI/USDT/EURC to their address
  5. Deposit watcher credits their account balance
  6. Developer uses API key to submit jobs — balance drawn down per job
"""

import os
import re
import time
import uuid
import hmac
import json
import hashlib
import sqlite3
import secrets
import logging
import threading
from pathlib      import Path
from dataclasses  import dataclass, field, asdict
from typing       import Optional
from enum         import Enum

log = logging.getLogger('obelyth.accounts')

# ── Constants ──────────────────────────────────────────────────────────────────
API_KEY_PREFIX        = 'oby_'
API_KEY_LENGTH        = 32          # bytes of entropy
MIN_DEPOSIT_USD       = 1.00        # minimum deposit
LOW_BALANCE_WARN_PCT  = 0.20        # warn at 20% of initial deposit
LOW_BALANCE_WARN_ABS  = 5.00        # warn if balance < $5 regardless of %
WEBHOOK_TIMEOUT_SEC   = 10

# Supported stablecoins and their EVM networks
SUPPORTED_COINS = {
    'USDC': ['ethereum', 'polygon', 'base', 'arbitrum'],
    'DAI' : ['ethereum', 'polygon', 'base', 'arbitrum'],
    'USDT': ['ethereum', 'polygon', 'base', 'arbitrum'],
    'EURC': ['ethereum', 'base'],
}


# ── Enums ──────────────────────────────────────────────────────────────────────

class AccountStatus(str, Enum):
    PENDING  = 'pending'     # email not yet verified
    ACTIVE   = 'active'
    SUSPENDED= 'suspended'
    CLOSED   = 'closed'

class NotifyEvent(str, Enum):
    WELCOME          = 'welcome'
    DEPOSIT_RECEIVED = 'deposit_received'
    LOW_BALANCE      = 'low_balance'
    BALANCE_EMPTY    = 'balance_empty'
    JOB_COMPLETE     = 'job_complete'
    JOB_FAILED       = 'job_failed'
    SETTLEMENT       = 'settlement'


# ── Data Classes ───────────────────────────────────────────────────────────────

@dataclass
class DeveloperAccount:
    account_id       : str
    email            : str
    api_key_hash     : str      # bcrypt/sha3 hash — never store plaintext
    status           : AccountStatus
    created_at       : int
    # Deposit addresses — one per coin per network
    deposit_addresses: dict     # {'USDC:ethereum': '0x...', ...}
    # Balances in USD equivalent
    balance_usd      : float = 0.0
    total_deposited  : float = 0.0
    total_spent      : float = 0.0
    jobs_submitted   : int   = 0
    jobs_completed   : int   = 0
    # Notification preferences
    notify_email     : bool  = True
    notify_webhook   : bool  = False
    webhook_url      : str   = ''
    low_balance_threshold : float = LOW_BALANCE_WARN_ABS
    # Metadata
    last_active      : int   = 0
    plan             : str   = 'pay_as_you_go'
    notes            : str   = ''

    def to_dict(self) -> dict:
        d = asdict(self)
        d['status'] = self.status.value
        return d

    @property
    def is_active(self) -> bool:
        return self.status == AccountStatus.ACTIVE

    @property
    def balance_sufficient(self) -> bool:
        return self.balance_usd >= MIN_DEPOSIT_USD

    @property
    def needs_low_balance_warning(self) -> bool:
        return (self.balance_usd > 0 and
                self.balance_usd < self.low_balance_threshold)


@dataclass
class DepositRecord:
    deposit_id       : str
    account_id       : str
    coin             : str
    network          : str
    amount_coin      : float
    amount_usd       : float
    tx_hash          : str
    block_number     : int
    deposit_address  : str
    status           : str   = 'pending'   # pending|confirmed|credited
    credited_at      : int   = 0
    detected_at      : int   = field(default_factory=lambda: int(time.time()))

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class WebhookRegistration:
    account_id  : str
    url         : str
    secret      : str     # HMAC secret for signature verification
    events      : list    # which NotifyEvents to send
    created_at  : int = field(default_factory=lambda: int(time.time()))
    last_success: int = 0
    failures    : int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# ── Address Derivation ─────────────────────────────────────────────────────────

class DepositAddressDeriver:
    """
    Derives unique, deterministic EVM deposit addresses from a master key
    and account ID. No private keys stored per account.

    In production: use BIP32 HD wallet derivation (account_id → child index).
    This implementation uses HMAC-SHA3 as a simplified version.

    The master key is the single secret that must be protected —
    store it in a hardware HSM or AWS KMS in production.
    """

    def __init__(self, master_key: bytes = None):
        if master_key is None:
            # In production: load from environment variable or HSM
            master_key = os.environ.get('OBELYTH_MASTER_KEY', '').encode()
            if not master_key:
                master_key = secrets.token_bytes(32)
                log.warning(
                    "No OBELYTH_MASTER_KEY set — using ephemeral key. "
                    "Set OBELYTH_MASTER_KEY environment variable in production."
                )
        self._master = master_key

    def derive_address(self, account_id: str, coin: str, network: str) -> str:
        """
        Derive a unique deposit address for account+coin+network combination.
        The same inputs always produce the same address.
        """
        # Derive child key: HMAC-SHA3(master, account_id:coin:network)
        data    = f"{account_id}:{coin}:{network}".encode()
        child   = hmac.new(self._master, data, hashlib.sha3_256).digest()

        # Convert to EVM-style address (20 bytes, 0x prefix, EIP-55 checksum)
        addr_bytes = child[:20]
        addr_hex   = addr_bytes.hex()
        return self._eip55_checksum('0x' + addr_hex)

    def _eip55_checksum(self, address: str) -> str:
        """Apply EIP-55 mixed-case checksum to Ethereum address."""
        addr    = address.lower().replace('0x', '')
        keccak  = hashlib.sha3_256(addr.encode()).hexdigest()
        result  = '0x'
        for i, char in enumerate(addr):
            if char.isdigit():
                result += char
            elif int(keccak[i], 16) >= 8:
                result += char.upper()
            else:
                result += char
        return result

    def derive_all_addresses(self, account_id: str) -> dict:
        """Derive deposit addresses for all supported coin/network combinations."""
        addresses = {}
        for coin, networks in SUPPORTED_COINS.items():
            for network in networks:
                key = f"{coin}:{network}"
                addresses[key] = self.derive_address(account_id, coin, network)
        return addresses


# ── API Key Management ─────────────────────────────────────────────────────────

def generate_api_key() -> tuple[str, str]:
    """
    Generate a new API key.
    Returns (plaintext_key, hashed_key).
    Plaintext shown once to developer — never stored.
    """
    raw     = secrets.token_bytes(API_KEY_LENGTH)
    key     = API_KEY_PREFIX + raw.hex()
    hashed  = hashlib.sha3_256(key.encode()).hexdigest()
    return key, hashed

def verify_api_key(plaintext: str, stored_hash: str) -> bool:
    candidate = hashlib.sha3_256(plaintext.encode()).hexdigest()
    return hmac.compare_digest(candidate, stored_hash)


# ── Account Registry ───────────────────────────────────────────────────────────

class AccountRegistry:
    """
    Persistent developer account store backed by SQLite.
    Thread-safe. Handles registration, balance management,
    deposit address lookup, and notification preferences.
    """

    def __init__(
        self,
        db_path       : str = './obelyth_data/accounts.db',
        master_key    : bytes = None,
    ):
        self.db_path  = db_path
        self.deriver  = DepositAddressDeriver(master_key)
        self._lock    = threading.RLock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS accounts (
                    account_id        TEXT PRIMARY KEY,
                    email             TEXT UNIQUE NOT NULL,
                    api_key_hash      TEXT NOT NULL,
                    status            TEXT NOT NULL DEFAULT 'pending',
                    created_at        INTEGER NOT NULL,
                    deposit_addresses TEXT NOT NULL DEFAULT '{}',
                    balance_usd       REAL NOT NULL DEFAULT 0.0,
                    total_deposited   REAL NOT NULL DEFAULT 0.0,
                    total_spent       REAL NOT NULL DEFAULT 0.0,
                    jobs_submitted    INTEGER NOT NULL DEFAULT 0,
                    jobs_completed    INTEGER NOT NULL DEFAULT 0,
                    notify_email      INTEGER NOT NULL DEFAULT 1,
                    notify_webhook    INTEGER NOT NULL DEFAULT 0,
                    webhook_url       TEXT NOT NULL DEFAULT '',
                    low_balance_threshold REAL NOT NULL DEFAULT 5.0,
                    last_active       INTEGER NOT NULL DEFAULT 0,
                    plan              TEXT NOT NULL DEFAULT 'pay_as_you_go',
                    notes             TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS deposits (
                    deposit_id      TEXT PRIMARY KEY,
                    account_id      TEXT NOT NULL,
                    coin            TEXT NOT NULL,
                    network         TEXT NOT NULL,
                    amount_coin     REAL NOT NULL,
                    amount_usd      REAL NOT NULL,
                    tx_hash         TEXT NOT NULL,
                    block_number    INTEGER NOT NULL,
                    deposit_address TEXT NOT NULL,
                    status          TEXT NOT NULL DEFAULT 'pending',
                    credited_at     INTEGER NOT NULL DEFAULT 0,
                    detected_at     INTEGER NOT NULL,
                    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
                );

                CREATE TABLE IF NOT EXISTS webhooks (
                    account_id  TEXT PRIMARY KEY,
                    url         TEXT NOT NULL,
                    secret      TEXT NOT NULL,
                    events      TEXT NOT NULL DEFAULT '[]',
                    created_at  INTEGER NOT NULL,
                    last_success INTEGER NOT NULL DEFAULT 0,
                    failures    INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
                );

                CREATE TABLE IF NOT EXISTS notifications (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id  TEXT NOT NULL,
                    event       TEXT NOT NULL,
                    channel     TEXT NOT NULL,
                    payload     TEXT NOT NULL,
                    sent_at     INTEGER NOT NULL,
                    success     INTEGER NOT NULL DEFAULT 1
                );

                CREATE INDEX IF NOT EXISTS idx_deposits_account
                    ON deposits(account_id);
                CREATE INDEX IF NOT EXISTS idx_deposits_address
                    ON deposits(deposit_address);
                CREATE INDEX IF NOT EXISTS idx_deposits_txhash
                    ON deposits(tx_hash);
            ''')
        log.info(f"Account DB initialised: {self.db_path}")

    # ── Registration ──────────────────────────────────────────────────────────

    def register(
        self,
        email       : str,
        password    : str,
        webhook_url : str = '',
        plan        : str = 'pay_as_you_go',
    ) -> tuple[DeveloperAccount, str]:
        """
        Register a new developer account.
        Returns (account, plaintext_api_key).
        API key is shown once — developer must save it.
        """
        email = email.lower().strip()
        if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
            raise ValueError(f"Invalid email: {email}")

        account_id   = str(uuid.uuid4())
        api_key, api_hash = generate_api_key()

        # Hash password (simplified — use bcrypt in production)
        pw_hash = hashlib.sha3_256(
            (password + account_id).encode()
        ).hexdigest()

        # Derive all deposit addresses
        addresses = self.deriver.derive_all_addresses(account_id)

        account = DeveloperAccount(
            account_id        = account_id,
            email             = email,
            api_key_hash      = api_hash,
            status            = AccountStatus.ACTIVE,  # skip email verify for now
            created_at        = int(time.time()),
            deposit_addresses = addresses,
            webhook_url       = webhook_url,
            notify_webhook    = bool(webhook_url),
            plan              = plan,
        )

        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('''
                    INSERT INTO accounts
                    (account_id, email, api_key_hash, status, created_at,
                     deposit_addresses, notify_webhook, webhook_url, plan)
                    VALUES (?,?,?,?,?,?,?,?,?)
                ''', (
                    account.account_id, account.email, account.api_key_hash,
                    account.status.value, account.created_at,
                    json.dumps(addresses),
                    int(account.notify_webhook), webhook_url, plan,
                ))
                if webhook_url:
                    secret = secrets.token_hex(32)
                    conn.execute('''
                        INSERT INTO webhooks (account_id, url, secret,
                            events, created_at)
                        VALUES (?,?,?,?,?)
                    ''', (account_id, webhook_url, secret,
                          json.dumps([e.value for e in NotifyEvent]),
                          int(time.time())))

        log.info(
            f"Account registered: {email} "
            f"id={account_id[:8]}... "
            f"addresses={len(addresses)}"
        )
        return account, api_key

    # ── Lookup ────────────────────────────────────────────────────────────────

    def get_by_api_key(self, api_key: str) -> Optional[DeveloperAccount]:
        key_hash = hashlib.sha3_256(api_key.encode()).hexdigest()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                'SELECT * FROM accounts WHERE api_key_hash = ?', (key_hash,)
            ).fetchone()
        return self._row_to_account(row) if row else None

    def get_by_id(self, account_id: str) -> Optional[DeveloperAccount]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                'SELECT * FROM accounts WHERE account_id = ?', (account_id,)
            ).fetchone()
        return self._row_to_account(row) if row else None

    def get_by_deposit_address(
        self, address: str
    ) -> Optional[DeveloperAccount]:
        """Find which account owns a deposit address."""
        address_lower = address.lower()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('SELECT * FROM accounts').fetchall()
        for row in rows:
            addrs = json.loads(row['deposit_addresses'])
            if any(a.lower() == address_lower for a in addrs.values()):
                return self._row_to_account(row)
        return None

    def get_all_deposit_addresses(self) -> dict[str, str]:
        """Returns {address: account_id} for all active accounts."""
        result = {}
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT account_id, deposit_addresses FROM accounts "
                "WHERE status = 'active'"
            ).fetchall()
        for row in rows:
            addrs = json.loads(row['deposit_addresses'])
            for addr in addrs.values():
                result[addr.lower()] = row['account_id']
        return result

    # ── Balance Management ────────────────────────────────────────────────────

    def credit_balance(
        self,
        account_id  : str,
        amount_usd  : float,
        deposit_id  : str,
    ) -> float:
        """Credit account balance after deposit confirmed. Returns new balance."""
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('''
                    UPDATE accounts
                    SET balance_usd     = balance_usd + ?,
                        total_deposited = total_deposited + ?
                    WHERE account_id = ?
                ''', (amount_usd, amount_usd, account_id))
                conn.execute('''
                    UPDATE deposits
                    SET status = 'credited', credited_at = ?
                    WHERE deposit_id = ?
                ''', (int(time.time()), deposit_id))
                row = conn.execute(
                    'SELECT balance_usd FROM accounts WHERE account_id = ?',
                    (account_id,)
                ).fetchone()
        new_bal = row[0] if row else 0.0
        log.info(
            f"Balance credited: {account_id[:8]} "
            f"+${amount_usd:.4f} → ${new_bal:.4f}"
        )
        return new_bal

    def deduct_balance(
        self,
        account_id  : str,
        amount_usd  : float,
        job_id      : str,
    ) -> tuple[bool, float]:
        """
        Deduct job cost from balance.
        Returns (success, remaining_balance).
        """
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    'SELECT balance_usd FROM accounts WHERE account_id = ?',
                    (account_id,)
                ).fetchone()
                if not row or row[0] < amount_usd:
                    return False, row[0] if row else 0.0
                conn.execute('''
                    UPDATE accounts
                    SET balance_usd    = balance_usd - ?,
                        total_spent    = total_spent + ?,
                        jobs_submitted = jobs_submitted + 1,
                        last_active    = ?
                    WHERE account_id = ?
                ''', (amount_usd, amount_usd, int(time.time()), account_id))
                new_bal = conn.execute(
                    'SELECT balance_usd FROM accounts WHERE account_id = ?',
                    (account_id,)
                ).fetchone()[0]
        return True, new_bal

    def record_deposit(self, deposit: DepositRecord):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                INSERT OR IGNORE INTO deposits
                (deposit_id, account_id, coin, network, amount_coin,
                 amount_usd, tx_hash, block_number, deposit_address,
                 status, detected_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ''', (
                deposit.deposit_id, deposit.account_id, deposit.coin,
                deposit.network, deposit.amount_coin, deposit.amount_usd,
                deposit.tx_hash, deposit.block_number, deposit.deposit_address,
                deposit.status, deposit.detected_at,
            ))

    def get_webhook(self, account_id: str) -> Optional[WebhookRegistration]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                'SELECT * FROM webhooks WHERE account_id = ?', (account_id,)
            ).fetchone()
        if not row:
            return None
        return WebhookRegistration(
            account_id   = row['account_id'],
            url          = row['url'],
            secret       = row['secret'],
            events       = json.loads(row['events']),
            created_at   = row['created_at'],
            last_success = row['last_success'],
            failures     = row['failures'],
        )

    def log_notification(
        self,
        account_id : str,
        event      : NotifyEvent,
        channel    : str,
        payload    : dict,
        success    : bool,
    ):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                INSERT INTO notifications
                (account_id, event, channel, payload, sent_at, success)
                VALUES (?,?,?,?,?,?)
            ''', (
                account_id, event.value, channel,
                json.dumps(payload), int(time.time()), int(success)
            ))

    def _row_to_account(self, row) -> DeveloperAccount:
        return DeveloperAccount(
            account_id        = row['account_id'],
            email             = row['email'],
            api_key_hash      = row['api_key_hash'],
            status            = AccountStatus(row['status']),
            created_at        = row['created_at'],
            deposit_addresses = json.loads(row['deposit_addresses']),
            balance_usd       = row['balance_usd'],
            total_deposited   = row['total_deposited'],
            total_spent       = row['total_spent'],
            jobs_submitted    = row['jobs_submitted'],
            jobs_completed    = row['jobs_completed'],
            notify_email      = bool(row['notify_email']),
            notify_webhook    = bool(row['notify_webhook']),
            webhook_url       = row['webhook_url'],
            low_balance_threshold = row['low_balance_threshold'],
            last_active       = row['last_active'],
            plan              = row['plan'],
        )

    def summary(self) -> dict:
        with sqlite3.connect(self.db_path) as conn:
            accts = conn.execute(
                "SELECT COUNT(*), SUM(balance_usd), SUM(total_deposited), "
                "SUM(total_spent) FROM accounts WHERE status='active'"
            ).fetchone()
            deps = conn.execute(
                "SELECT COUNT(*), SUM(amount_usd) FROM deposits "
                "WHERE status='credited'"
            ).fetchone()
        return {
            'active_accounts'    : accts[0] or 0,
            'total_balance_usd'  : round(accts[1] or 0, 4),
            'total_deposited_usd': round(accts[2] or 0, 4),
            'total_spent_usd'    : round(accts[3] or 0, 4),
            'confirmed_deposits' : deps[0] or 0,
            'confirmed_volume'   : round(deps[1] or 0, 4),
        }
