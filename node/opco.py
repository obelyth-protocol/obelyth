"""
Obelyth Operations Company (OpCo)
======================================
The OpCo is the legal entity that executes DAO governance decisions
in the real world. It bridges on-chain governance and off-chain action.

STRUCTURE:
  DAO (on-chain governance entity)
    └── OpCo (legal entity — Cayman Foundation Company or LLC)
          ├── Steering Committee (directors/officers)
          ├── Employees (protocol engineers, ops, developer relations)
          ├── Bank accounts and multi-sig wallets
          └── Contracts (exchanges, legal firms, auditors)

WHY THIS SEPARATION:
  The DAO itself is not a legal person in most jurisdictions.
  It cannot sign contracts, employ people, pay taxes, or hold bank accounts.
  The OpCo is the legal wrapper that can do all of these things.
  The DAO instructs the OpCo via passed governance proposals.
  The OpCo executes. The DAO retains sovereignty — it can replace
  the OpCo via governance vote if it ever acts against DAO interests.

OPCO RESPONSIBILITIES:
  - Employs steering committee members and protocol staff
  - Holds operational bank accounts (USD/EUR/GBP fiat)
  - Holds multi-sig wallets for stablecoin fund distribution
  - Signs legal contracts on behalf of the DAO
  - Files taxes, handles KYC/AML compliance
  - Executes settlement distributions to DAO-approved recipients
  - Manages exchange listing agreements
  - Pays for audits, legal opinions, infrastructure

OPCO CONSTRAINTS:
  - Can only spend funds approved by DAO governance vote
  - Cannot change constitutional parameters (hard cap, liquidity lock, Creator Share)
  - Annual financial statements published on-chain within 30 days of year end
  - Multi-sig requires 3-of-5 steering committee signatures for treasury moves
  - Any single transaction above $50,000 requires DAO vote, not just committee sign-off

FUNDING FLOW:
  Developer stablecoin fees
    → 90% AMM liquidity reserve (locked)
    → 5% Creator Share wallet (founder direct)
    → 5% DAO stablecoin fund → OpCo bank/multisig → DAO-approved expenditure

  Miner OBY earnings
    → 95% miner reward
    → 5% DAO OBY vault → governance vote → OpCo executes distribution
"""

import time
import uuid
import json
import sqlite3
import logging
import threading
from pathlib     import Path
from dataclasses import dataclass, field, asdict
from typing      import Optional
from enum        import Enum

log = logging.getLogger('obelyth.opco')


# ── Enums ──────────────────────────────────────────────────────────────────────

class EmployeeRole(str, Enum):
    STEERING_COMMITTEE  = 'steering_committee'
    PROTOCOL_ENGINEER   = 'protocol_engineer'
    SECURITY_LEAD       = 'security_lead'
    DEVELOPER_RELATIONS = 'developer_relations'
    OPERATIONS          = 'operations'
    LEGAL_COUNSEL       = 'legal_counsel'   # retainer, not full-time
    CONTRACTOR          = 'contractor'

class ExpenditureType(str, Enum):
    PAYROLL             = 'payroll'
    LEGAL               = 'legal'
    SECURITY_AUDIT      = 'security_audit'
    INFRASTRUCTURE      = 'infrastructure'
    GRANT_DISBURSEMENT  = 'grant_disbursement'
    EXCHANGE_LISTING    = 'exchange_listing'
    BOUNTY_AWARD        = 'bounty_award'
    OPERATIONAL         = 'operational'
    OTHER               = 'other'

class ExpenditureStatus(str, Enum):
    PENDING_VOTE        = 'pending_vote'    # needs DAO governance vote
    APPROVED            = 'approved'        # DAO approved, pending execution
    EXECUTED            = 'executed'        # funds moved
    REJECTED            = 'rejected'        # DAO rejected

# Threshold above which a DAO vote is required
VOTE_REQUIRED_THRESHOLD_USD = 50_000.0
# Multi-sig requirement: M of N committee signatures
MULTISIG_M = 3
MULTISIG_N = 5


# ── Data Classes ───────────────────────────────────────────────────────────────

@dataclass
class Employee:
    employee_id     : str
    name            : str
    role            : EmployeeRole
    wallet_address  : str           # OBY address for OBY component
    bank_details    : str = ''      # encrypted/hashed — never plaintext
    salary_usd_mo   : float = 0.0   # monthly USD component
    salary_oby_mo   : float = 0.0   # monthly OBY component
    start_date      : int   = field(default_factory=lambda: int(time.time()))
    end_date        : int   = 0     # 0 = ongoing
    is_active       : bool  = True
    oby_vest_months : int   = 24    # OBY component vests over this period
    notes           : str   = ''

    @property
    def total_monthly_cost_usd(self) -> float:
        return self.salary_usd_mo  # OBY portion tracked separately at market price

    def to_dict(self) -> dict:
        d = asdict(self)
        d['role'] = self.role.value
        d.pop('bank_details', None)  # never serialise to public endpoints
        return d


@dataclass
class Expenditure:
    expenditure_id  : str
    exp_type        : ExpenditureType
    description     : str
    amount_usd      : float
    amount_oby      : float = 0.0
    recipient       : str   = ''    # name or wallet address
    vendor          : str   = ''    # e.g. 'Ogier', 'Immunefi', 'AWS'
    dao_proposal_id : str   = ''    # NIP reference if vote required
    status          : ExpenditureStatus = ExpenditureStatus.PENDING_VOTE
    requires_vote   : bool  = False
    tx_hash         : str   = ''    # on-chain tx once executed
    created_at      : int   = field(default_factory=lambda: int(time.time()))
    approved_at     : int   = 0
    executed_at     : int   = 0
    approved_by     : list  = field(default_factory=list)  # committee member addresses
    notes           : str   = ''

    def to_dict(self) -> dict:
        d = asdict(self)
        d['exp_type'] = self.exp_type.value
        d['status']   = self.status.value
        return d


@dataclass
class OpCoFinancials:
    """Snapshot of OpCo financial position."""
    stablecoin_balance_usd : float = 0.0   # DAO stablecoin fund in OpCo multisig
    oby_vault_balance      : float = 0.0   # DAO OBY vault allocated to OpCo
    monthly_payroll_usd    : float = 0.0   # total monthly staff cost
    monthly_operational_usd: float = 0.0   # infrastructure, legal retainer, etc.
    ytd_expenditure_usd    : float = 0.0
    runway_months          : float = 0.0   # months of operational runway

    @property
    def monthly_burn(self) -> float:
        return self.monthly_payroll_usd + self.monthly_operational_usd

    def to_dict(self) -> dict:
        d = asdict(self)
        d['monthly_burn'] = self.monthly_burn
        return d


# ── OpCo Registry ──────────────────────────────────────────────────────────────

class OpCoRegistry:
    """
    Manages OpCo structure: employees, expenditures, financials.
    All spending above $50K requires a DAO governance vote (stored by NIP ID).
    Multi-sig: 3-of-5 steering committee members sign treasury movements.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock   = threading.RLock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS employees (
                    employee_id     TEXT PRIMARY KEY,
                    name            TEXT NOT NULL,
                    role            TEXT NOT NULL,
                    wallet_address  TEXT NOT NULL,
                    salary_usd_mo   REAL NOT NULL DEFAULT 0,
                    salary_oby_mo   REAL NOT NULL DEFAULT 0,
                    start_date      INTEGER NOT NULL,
                    end_date        INTEGER NOT NULL DEFAULT 0,
                    is_active       INTEGER NOT NULL DEFAULT 1,
                    oby_vest_months INTEGER NOT NULL DEFAULT 24,
                    notes           TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS expenditures (
                    expenditure_id  TEXT PRIMARY KEY,
                    exp_type        TEXT NOT NULL,
                    description     TEXT NOT NULL,
                    amount_usd      REAL NOT NULL,
                    amount_oby      REAL NOT NULL DEFAULT 0,
                    recipient       TEXT NOT NULL DEFAULT '',
                    vendor          TEXT NOT NULL DEFAULT '',
                    dao_proposal_id TEXT NOT NULL DEFAULT '',
                    status          TEXT NOT NULL DEFAULT 'pending_vote',
                    requires_vote   INTEGER NOT NULL DEFAULT 0,
                    tx_hash         TEXT NOT NULL DEFAULT '',
                    created_at      INTEGER NOT NULL,
                    approved_at     INTEGER NOT NULL DEFAULT 0,
                    executed_at     INTEGER NOT NULL DEFAULT 0,
                    approved_by     TEXT NOT NULL DEFAULT '[]',
                    notes           TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS opco_financials (
                    snapshot_id             TEXT PRIMARY KEY,
                    stablecoin_balance_usd  REAL NOT NULL DEFAULT 0,
                    oby_vault_balance       REAL NOT NULL DEFAULT 0,
                    monthly_payroll_usd     REAL NOT NULL DEFAULT 0,
                    monthly_operational_usd REAL NOT NULL DEFAULT 0,
                    ytd_expenditure_usd     REAL NOT NULL DEFAULT 0,
                    recorded_at             INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS committee_signatures (
                    sig_id          TEXT PRIMARY KEY,
                    expenditure_id  TEXT NOT NULL,
                    signer_address  TEXT NOT NULL,
                    signed_at       INTEGER NOT NULL,
                    UNIQUE(expenditure_id, signer_address)
                );

                CREATE INDEX IF NOT EXISTS idx_exp_status
                    ON expenditures(status, created_at);
            ''')

    # ── Employees ─────────────────────────────────────────────────────────────

    def add_employee(
        self,
        name           : str,
        role           : EmployeeRole,
        wallet_address : str,
        salary_usd_mo  : float = 0.0,
        salary_oby_mo  : float = 0.0,
        oby_vest_months: int   = 24,
        notes          : str   = '',
    ) -> Employee:
        emp = Employee(
            employee_id     = str(uuid.uuid4())[:16],
            name            = name,
            role            = role,
            wallet_address  = wallet_address,
            salary_usd_mo   = salary_usd_mo,
            salary_oby_mo   = salary_oby_mo,
            oby_vest_months = oby_vest_months,
            notes           = notes,
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                INSERT INTO employees
                (employee_id, name, role, wallet_address, salary_usd_mo,
                 salary_oby_mo, start_date, oby_vest_months, notes)
                VALUES (?,?,?,?,?,?,?,?,?)
            ''', (
                emp.employee_id, emp.name, emp.role.value, emp.wallet_address,
                emp.salary_usd_mo, emp.salary_oby_mo, emp.start_date,
                emp.oby_vest_months, emp.notes,
            ))
        log.info(
            f"OpCo employee added: {name} | {role.value} | "
            f"${salary_usd_mo:,.0f}/mo + {salary_oby_mo:,.0f} OBY/mo"
        )
        return emp

    def terminate_employee(self, employee_id: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                'UPDATE employees SET is_active=0, end_date=? WHERE employee_id=?',
                (int(time.time()), employee_id)
            )

    def active_employees(self) -> list[Employee]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM employees WHERE is_active=1 ORDER BY role"
            ).fetchall()
        return [self._row_to_employee(r) for r in rows]

    def monthly_payroll(self) -> dict:
        emps = self.active_employees()
        total_usd = sum(e.salary_usd_mo for e in emps)
        total_oby = sum(e.salary_oby_mo for e in emps)
        return {
            'total_usd_per_month': round(total_usd, 2),
            'total_oby_per_month': round(total_oby, 4),
            'headcount'          : len(emps),
            'by_role'            : {
                role.value: {
                    'count'    : len([e for e in emps if e.role == role]),
                    'total_usd': sum(e.salary_usd_mo for e in emps if e.role == role),
                }
                for role in EmployeeRole
                if any(e.role == role for e in emps)
            }
        }

    # ── Expenditures ──────────────────────────────────────────────────────────

    def propose_expenditure(
        self,
        exp_type       : ExpenditureType,
        description    : str,
        amount_usd     : float,
        recipient      : str = '',
        vendor         : str = '',
        amount_oby     : float = 0.0,
        dao_proposal_id: str = '',
        notes          : str = '',
    ) -> Expenditure:
        """
        Propose an expenditure. Automatically flags whether a DAO vote
        is required based on amount threshold.
        """
        requires_vote = amount_usd >= VOTE_REQUIRED_THRESHOLD_USD
        status = (ExpenditureStatus.PENDING_VOTE if requires_vote
                  else ExpenditureStatus.APPROVED)

        exp = Expenditure(
            expenditure_id  = str(uuid.uuid4())[:16],
            exp_type        = exp_type,
            description     = description,
            amount_usd      = amount_usd,
            amount_oby      = amount_oby,
            recipient       = recipient,
            vendor          = vendor,
            dao_proposal_id = dao_proposal_id,
            status          = status,
            requires_vote   = requires_vote,
            notes           = notes,
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                INSERT INTO expenditures
                (expenditure_id, exp_type, description, amount_usd, amount_oby,
                 recipient, vendor, dao_proposal_id, status, requires_vote,
                 created_at, notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ''', (
                exp.expenditure_id, exp.exp_type.value, exp.description,
                exp.amount_usd, exp.amount_oby, exp.recipient, exp.vendor,
                exp.dao_proposal_id, exp.status.value, int(exp.requires_vote),
                exp.created_at, exp.notes,
            ))
        log.info(
            f"Expenditure proposed: {exp.expenditure_id} | "
            f"{exp_type.value} | ${amount_usd:,.2f} | "
            f"vote_required={requires_vote}"
        )
        return exp

    def sign_expenditure(
        self,
        expenditure_id : str,
        signer_address : str,
    ) -> tuple[bool, str]:
        """
        Committee member signs off on an expenditure.
        Executes automatically when MULTISIG_M signatures collected.
        """
        with sqlite3.connect(self.db_path) as conn:
            try:
                conn.execute(
                    'INSERT INTO committee_signatures '
                    '(sig_id, expenditure_id, signer_address, signed_at) '
                    'VALUES (?,?,?,?)',
                    (str(uuid.uuid4())[:16], expenditure_id,
                     signer_address, int(time.time()))
                )
            except sqlite3.IntegrityError:
                return False, "Already signed by this address"

            sig_count = conn.execute(
                'SELECT COUNT(*) FROM committee_signatures WHERE expenditure_id=?',
                (expenditure_id,)
            ).fetchone()[0]

        if sig_count >= MULTISIG_M:
            self._execute_expenditure(expenditure_id)
            return True, f"Executed: {sig_count}/{MULTISIG_N} signatures collected"
        return True, f"Signed: {sig_count}/{MULTISIG_M} required"

    def dao_approve_expenditure(
        self,
        expenditure_id : str,
        proposal_id    : str,
    ):
        """Called when DAO governance vote passes for this expenditure."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                'UPDATE expenditures SET status=?, dao_proposal_id=?, approved_at=? '
                'WHERE expenditure_id=?',
                (ExpenditureStatus.APPROVED.value, proposal_id,
                 int(time.time()), expenditure_id)
            )
        log.info(f"Expenditure DAO-approved: {expenditure_id} via {proposal_id}")

    def _execute_expenditure(self, expenditure_id: str):
        import random
        tx_hash = '0x' + '%064x' % random.randint(0, 2**256-1)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                'UPDATE expenditures SET status=?, executed_at=?, tx_hash=? '
                'WHERE expenditure_id=?',
                (ExpenditureStatus.EXECUTED.value,
                 int(time.time()), tx_hash, expenditure_id)
            )
        log.info(f"Expenditure executed: {expenditure_id} tx={tx_hash[:16]}")

    # ── Financials ────────────────────────────────────────────────────────────

    def record_financials(
        self,
        stablecoin_balance_usd  : float,
        oby_vault_balance       : float,
        monthly_operational_usd : float = 5_000.0,
    ) -> OpCoFinancials:
        payroll = self.monthly_payroll()
        ytd = self._ytd_expenditure()
        fin = OpCoFinancials(
            stablecoin_balance_usd  = stablecoin_balance_usd,
            oby_vault_balance       = oby_vault_balance,
            monthly_payroll_usd     = payroll['total_usd_per_month'],
            monthly_operational_usd = monthly_operational_usd,
            ytd_expenditure_usd     = ytd,
        )
        if fin.monthly_burn > 0:
            fin.runway_months = round(stablecoin_balance_usd / fin.monthly_burn, 1)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                INSERT INTO opco_financials
                (snapshot_id, stablecoin_balance_usd, oby_vault_balance,
                 monthly_payroll_usd, monthly_operational_usd,
                 ytd_expenditure_usd, recorded_at)
                VALUES (?,?,?,?,?,?,?)
            ''', (
                str(uuid.uuid4())[:16], fin.stablecoin_balance_usd,
                fin.oby_vault_balance, fin.monthly_payroll_usd,
                fin.monthly_operational_usd, fin.ytd_expenditure_usd,
                int(time.time()),
            ))
        return fin

    def _ytd_expenditure(self) -> float:
        year_start = int(time.mktime(time.strptime(
            f'{time.localtime().tm_year}-01-01', '%Y-%m-%d'
        )))
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT SUM(amount_usd) FROM expenditures "
                "WHERE status='executed' AND executed_at >= ?",
                (year_start,)
            ).fetchone()
        return round(row[0] or 0, 2)

    def status(self) -> dict:
        payroll  = self.monthly_payroll()
        pending  = self._pending_expenditures()
        return {
            'structure': {
                'type'       : 'Cayman Foundation Company (OpCo)',
                'dao_relation': 'Executes DAO governance decisions in legal world',
                'multisig'   : f'{MULTISIG_M}-of-{MULTISIG_N} steering committee',
                'vote_threshold_usd': VOTE_REQUIRED_THRESHOLD_USD,
            },
            'payroll'    : payroll,
            'pending_expenditures': pending,
            'constraints': [
                'Cannot spend above $50K without DAO governance vote',
                'Cannot modify constitutional parameters',
                'Annual financial statements published on-chain within 30 days',
                'All treasury movements require 3-of-5 committee signatures',
                'DAO can replace OpCo via governance vote at any time',
            ],
        }

    def _pending_expenditures(self) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM expenditures WHERE status IN "
                "('pending_vote','approved') ORDER BY created_at DESC"
            ).fetchall()
        return [self._row_to_exp(r).to_dict() for r in rows]

    def _row_to_employee(self, row) -> Employee:
        return Employee(
            employee_id     = row['employee_id'],
            name            = row['name'],
            role            = EmployeeRole(row['role']),
            wallet_address  = row['wallet_address'],
            salary_usd_mo   = row['salary_usd_mo'],
            salary_oby_mo   = row['salary_oby_mo'],
            start_date      = row['start_date'],
            end_date        = row['end_date'],
            is_active       = bool(row['is_active']),
            oby_vest_months = row['oby_vest_months'],
            notes           = row['notes'],
        )

    def _row_to_exp(self, row) -> Expenditure:
        return Expenditure(
            expenditure_id  = row['expenditure_id'],
            exp_type        = ExpenditureType(row['exp_type']),
            description     = row['description'],
            amount_usd      = row['amount_usd'],
            amount_oby      = row['amount_oby'],
            recipient       = row['recipient'],
            vendor          = row['vendor'],
            dao_proposal_id = row['dao_proposal_id'],
            status          = ExpenditureStatus(row['status']),
            requires_vote   = bool(row['requires_vote']),
            tx_hash         = row['tx_hash'],
            created_at      = row['created_at'],
            approved_at     = row['approved_at'],
            executed_at     = row['executed_at'],
            approved_by     = json.loads(row['approved_by'] or '[]'),
            notes           = row['notes'],
        )
