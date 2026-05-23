"""
Obelyth Bug Bounty Program
==============================
Security bounty funded from the DAO NXS vault — no hard cap.
The DAO allocates to security on an ongoing basis through governance.
Critical vulnerabilities will always be paid regardless of vault balance.

Two programs:

  Ongoing Standing Bounty:
    SOURCE: DAO NXS Vault (5% mining tax, perpetual)
    Permanent program on Immunefi. Open to any researcher, any time, forever.
    The network's immune system — always on, no per-payout governance vote.
    DAO sets severity tiers and award ranges once. Program runs continuously.

  Upgrade Reserve (per-event):
    SOURCE: DAO NXS Vault (5% mining tax, per governance vote)
    War chest for specific high-stakes events requiring extraordinary scrutiny:
      - Rust node port ready for mainnet
      - ZK proof system deployment
      - On-chain governance smart contract launch
      - Major consensus rule change
    Each use requires a DAO governance vote specifying scope, researcher, amount.

  Testnet security (pre-mainnet):
    SOURCE: 3% pre-mainnet community pool (separate from bounty program)
    Security researchers who find vulnerabilities during testnet are rewarded
    from the same pool as early miners and developers — not a separate budget.

All awards vest 6 months from date. Responsible disclosure required.
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

log = logging.getLogger('obelyth.bounty')

# ── Constants ──────────────────────────────────────────────────────────────────
TOTAL_OBY_SUPPLY      = 21_000_000.0

# No hard cap — DAO funds security from vault on an ongoing basis
BOUNTY_HARD_CAP       = False
BOUNTY_FUNDING_SOURCE = 'DAO NXS Vault (5% mining tax on all miner earnings)'

DISCLOSURE_ACK_HRS    = 72
DISCLOSURE_FIX_HRS    = 14 * 24
AWARD_VEST_MONTHS     = 6

SECURITY_EMAIL        = 'security@Obelyth_Chain.io'
IMMUNEFI_URL          = 'https://immunefi.com/bounty/obelyth'

# Tranche sources for documentation
TRANCHE_SOURCES = {
    'ongoing' : 'DAO NXS Vault — standing program, no per-payout vote required',
    'upgrade' : 'DAO NXS Vault — per-event DAO governance vote required',
}


# ── Severity Tiers ─────────────────────────────────────────────────────────────

class Severity(str, Enum):
    CRITICAL      = 'critical'       # consensus break, fund theft, supply inflation
    HIGH          = 'high'           # network disruption, economic attack
    MEDIUM        = 'medium'         # degraded performance, minor economic issue
    LOW           = 'low'            # code quality, best practice
    INFORMATIONAL = 'informational'  # docs, minor suggestions

# Award ranges in OBY per severity
AWARD_RANGES = {
    Severity.CRITICAL     : (25_000, 50_000),
    Severity.HIGH         : (10_000, 25_000),
    Severity.MEDIUM       : (2_000,  5_000),
    Severity.LOW          : (250,    1_000),
    Severity.INFORMATIONAL: (0,      250),
}

SEVERITY_DESCRIPTIONS = {
    Severity.CRITICAL: (
        'Consensus break, fund theft, supply cap bypass, '
        'permanent network halt, private key exposure'
    ),
    Severity.HIGH: (
        'Temporary network disruption, economic attack enabling '
        'significant profit, privacy breach, validator slashing bypass'
    ),
    Severity.MEDIUM: (
        'Degraded performance under load, minor economic imbalance, '
        'non-critical smart contract logic error'
    ),
    Severity.LOW: (
        'Code quality issues, best practice deviations, '
        'non-exploitable edge cases'
    ),
    Severity.INFORMATIONAL: (
        'Documentation errors, minor suggestions, '
        'out-of-scope observations'
    ),
}


class Tranche(str, Enum):
    ONGOING  = 'ongoing'    # standing Immunefi program — no cap, DAO funded
    UPGRADE  = 'upgrade'    # per-event DAO vote — war chest for major upgrades


class ReportStatus(str, Enum):
    SUBMITTED    = 'submitted'    # received, not yet triaged
    ACKNOWLEDGED = 'acknowledged' # within 72hr window
    VALID        = 'valid'        # confirmed real vulnerability
    INVALID      = 'invalid'      # not a vulnerability / out of scope
    FIXED        = 'fixed'        # patch deployed
    AWARDED      = 'awarded'      # NXS award issued
    DUPLICATE    = 'duplicate'    # already reported
    DISCLOSED    = 'disclosed'    # publicly disclosed after fix


# ── Data Classes ───────────────────────────────────────────────────────────────

@dataclass
class BountyReport:
    report_id       : str
    tranche         : Tranche
    researcher_addr : str       # NXS address for award payment
    researcher_name : str       # handle / name
    title           : str
    description     : str
    severity_claimed: Severity
    severity_assigned: Optional[Severity] = None
    affected_component: str    = ''   # e.g. 'core/blockchain.py', 'AMM', 'P2P'
    proof_of_concept: str      = ''   # PoC code or steps to reproduce
    # Status
    status          : ReportStatus = ReportStatus.SUBMITTED
    submitted_at    : int      = field(default_factory=lambda: int(time.time()))
    acknowledged_at : int      = 0
    fixed_at        : int      = 0
    awarded_at      : int      = 0
    # Award
    award_nxs       : float    = 0.0
    vest_months     : int      = AWARD_VEST_MONTHS
    award_rationale : str      = ''
    # Duplicate ref
    duplicate_of    : str      = ''
    # Internal notes
    notes           : str      = ''

    @property
    def is_overdue_ack(self) -> bool:
        if self.status != ReportStatus.SUBMITTED:
            return False
        return (time.time() - self.submitted_at) > DISCLOSURE_ACK_HRS * 3600

    @property
    def researcher_can_disclose(self) -> bool:
        """After fix window, researcher may disclose publicly."""
        if self.status == ReportStatus.FIXED:
            return True
        if (self.severity_assigned == Severity.CRITICAL and
                self.acknowledged_at > 0 and
                (time.time() - self.acknowledged_at) > DISCLOSURE_FIX_HRS * 3600):
            return True
        return False

    def to_dict(self) -> dict:
        d = asdict(self)
        d['severity_claimed']  = self.severity_claimed.value
        d['severity_assigned'] = self.severity_assigned.value if self.severity_assigned else None
        d['status']            = self.status.value
        d['tranche']           = self.tranche.value
        d['is_overdue_ack']    = self.is_overdue_ack
        d['can_disclose']      = self.researcher_can_disclose
        return d


@dataclass
class BountyAward:
    award_id        : str
    report_id       : str
    researcher_addr : str
    oby_amount      : float
    vest_months     : int
    # Vesting schedule
    issued_at       : int = field(default_factory=lambda: int(time.time()))
    vest_start      : int = field(default_factory=lambda: int(time.time()))
    fully_vested_at : int = 0
    total_claimed   : float = 0.0

    def vested_amount(self, at_timestamp: int = None) -> float:
        now = at_timestamp or int(time.time())
        elapsed_months = (now - self.vest_start) / (30.44 * 86400)
        fraction = min(1.0, elapsed_months / self.vest_months)
        return round(self.oby_amount * fraction, 8)

    def claimable_amount(self, at_timestamp: int = None) -> float:
        return round(self.vested_amount(at_timestamp) - self.total_claimed, 8)

    def to_dict(self) -> dict:
        now = int(time.time())
        return {
            **asdict(self),
            'vested_now'   : self.vested_amount(now),
            'claimable_now': self.claimable_amount(now),
            'pct_vested'   : round(self.vested_amount(now) / self.oby_amount * 100, 1),
        }


# ── Bounty Program ─────────────────────────────────────────────────────────────

class BountyProgram:
    """
    Manages the full bug bounty lifecycle:
    submission → triage → validation → fix → award → vesting
    """

    def __init__(
        self,
        db_path    : str,
        on_critical: callable = None,   # called immediately on critical report
    ):
        self.db_path     = db_path
        self.on_critical = on_critical
        self._lock       = threading.RLock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS reports (
                    report_id           TEXT PRIMARY KEY,
                    tranche             TEXT NOT NULL,
                    researcher_addr     TEXT NOT NULL,
                    researcher_name     TEXT NOT NULL,
                    title               TEXT NOT NULL,
                    description         TEXT NOT NULL,
                    severity_claimed    TEXT NOT NULL,
                    severity_assigned   TEXT,
                    affected_component  TEXT NOT NULL DEFAULT '',
                    proof_of_concept    TEXT NOT NULL DEFAULT '',
                    status              TEXT NOT NULL DEFAULT 'submitted',
                    submitted_at        INTEGER NOT NULL,
                    acknowledged_at     INTEGER NOT NULL DEFAULT 0,
                    fixed_at            INTEGER NOT NULL DEFAULT 0,
                    awarded_at          INTEGER NOT NULL DEFAULT 0,
                    award_nxs           REAL NOT NULL DEFAULT 0,
                    vest_months         INTEGER NOT NULL DEFAULT 6,
                    award_rationale     TEXT NOT NULL DEFAULT '',
                    duplicate_of        TEXT NOT NULL DEFAULT '',
                    notes               TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS awards (
                    award_id        TEXT PRIMARY KEY,
                    report_id       TEXT NOT NULL,
                    researcher_addr TEXT NOT NULL,
                    oby_amount      REAL NOT NULL,
                    vest_months     INTEGER NOT NULL,
                    issued_at       INTEGER NOT NULL,
                    vest_start      INTEGER NOT NULL,
                    fully_vested_at INTEGER NOT NULL,
                    total_claimed   REAL NOT NULL DEFAULT 0,
                    FOREIGN KEY(report_id) REFERENCES reports(report_id)
                );

                CREATE TABLE IF NOT EXISTS tranche_balances (
                    tranche     TEXT PRIMARY KEY,
                    total_nxs   REAL NOT NULL,
                    awarded_nxs REAL NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_reports_status
                    ON reports(status, submitted_at);
                CREATE INDEX IF NOT EXISTS idx_awards_researcher
                    ON awards(researcher_addr);
            ''')
            # Seed tranche tracking (no hard caps — DAO funds from vault)
            for tranche in [Tranche.ONGOING.value, Tranche.UPGRADE.value]:
                conn.execute(
                    'INSERT OR IGNORE INTO tranche_balances (tranche, total_nxs)'
                    ' VALUES (?,?)',
                    (tranche, 0.0)   # 0 = no cap; DAO allocates as needed
                )

    # ── Submission ────────────────────────────────────────────────────────────

    def submit_report(
        self,
        researcher_addr    : str,
        researcher_name    : str,
        title              : str,
        description        : str,
        severity_claimed   : Severity,
        affected_component : str,
        proof_of_concept   : str = '',
        tranche            : Tranche = Tranche.ONGOING,
    ) -> BountyReport:
        """Submit a new vulnerability report."""
        report = BountyReport(
            report_id          = str(uuid.uuid4())[:16],
            tranche            = tranche,
            researcher_addr    = researcher_addr,
            researcher_name    = researcher_name,
            title              = title,
            description        = description,
            severity_claimed   = severity_claimed,
            affected_component = affected_component,
            proof_of_concept   = proof_of_concept,
        )
        self._save_report(report)
        log.info(
            f"Bounty report submitted: {report.report_id} | "
            f"{severity_claimed.value} | {title[:50]} | "
            f"by {researcher_name}"
        )
        # Alert immediately on claimed critical
        if severity_claimed == Severity.CRITICAL and self.on_critical:
            self.on_critical(report)
        return report

    # ── Triage ────────────────────────────────────────────────────────────────

    def acknowledge(self, report_id: str, notes: str = '') -> BountyReport:
        """Acknowledge receipt within 72-hour window."""
        report = self._get_report(report_id)
        if not report:
            raise ValueError(f"Report not found: {report_id}")
        report.status         = ReportStatus.ACKNOWLEDGED
        report.acknowledged_at= int(time.time())
        if notes:
            report.notes      = notes
        self._save_report(report)
        log.info(f"Report acknowledged: {report_id}")
        return report

    def validate(
        self,
        report_id        : str,
        severity_assigned: Severity,
        is_valid         : bool,
        duplicate_of     : str = '',
        notes            : str = '',
    ) -> BountyReport:
        """Triage team validates the report and assigns severity."""
        report = self._get_report(report_id)
        if not report:
            raise ValueError(f"Report not found: {report_id}")
        report.severity_assigned = severity_assigned
        report.notes             = notes
        if not is_valid:
            report.status = ReportStatus.INVALID
        elif duplicate_of:
            report.status       = ReportStatus.DUPLICATE
            report.duplicate_of = duplicate_of
        else:
            report.status = ReportStatus.VALID
        self._save_report(report)
        log.info(
            f"Report validated: {report_id} | "
            f"status={report.status.value} | "
            f"severity={severity_assigned.value}"
        )
        return report

    def mark_fixed(self, report_id: str, notes: str = '') -> BountyReport:
        """Mark vulnerability as patched and deployed."""
        report = self._get_report(report_id)
        if not report:
            raise ValueError(f"Report not found: {report_id}")
        report.status   = ReportStatus.FIXED
        report.fixed_at = int(time.time())
        if notes:
            report.notes = notes
        self._save_report(report)
        log.info(f"Report fixed: {report_id}")
        return report

    # ── Awards ────────────────────────────────────────────────────────────────

    def issue_award(
        self,
        report_id      : str,
        oby_amount     : float,
        rationale      : str,
        vest_months    : int = AWARD_VEST_MONTHS,
    ) -> BountyAward:
        """
        Issue NXS award to researcher.
        Validates against severity tier ranges and tranche balance.
        """
        report = self._get_report(report_id)
        if not report:
            raise ValueError(f"Report not found: {report_id}")
        if report.status not in (ReportStatus.VALID, ReportStatus.FIXED):
            raise ValueError(
                f"Can only award valid/fixed reports, got {report.status.value}"
            )
        # Validate amount is within severity range
        if report.severity_assigned:
            min_nxs, max_nxs = AWARD_RANGES[report.severity_assigned]
            if oby_amount < min_nxs or oby_amount > max_nxs:
                raise ValueError(
                    f"Award {oby_amount} NXS outside range for "
                    f"{report.severity_assigned.value}: "
                    f"{min_nxs:,}–{max_nxs:,} NXS"
                )
        # Check tranche balance — DAO-funded tranches have no hard cap
        # The DAO vault funds these; balance check skipped (governance controls spend)
        balance = self.tranche_balance(report.tranche)
        if balance['total_nxs'] > 0 and oby_amount > balance['remaining_nxs']:
            raise ValueError(
                f"Tranche {report.tranche.value} insufficient: "
                f"need {oby_amount:,.0f} NXS, "
                f"have {balance['remaining_nxs']:,.0f} NXS"
            )

        vest_start     = int(time.time())
        fully_vested   = int(vest_start + vest_months * 30.44 * 86400)

        award = BountyAward(
            award_id        = str(uuid.uuid4())[:16],
            report_id       = report_id,
            researcher_addr = report.researcher_addr,
            oby_amount      = oby_amount,
            vest_months     = vest_months,
            vest_start      = vest_start,
            fully_vested_at = fully_vested,
        )

        report.status         = ReportStatus.AWARDED
        report.award_nxs      = oby_amount
        report.award_rationale= rationale
        report.awarded_at     = int(time.time())
        report.vest_months    = vest_months

        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('''
                    INSERT INTO awards
                    (award_id, report_id, researcher_addr, oby_amount,
                     vest_months, issued_at, vest_start, fully_vested_at)
                    VALUES (?,?,?,?,?,?,?,?)
                ''', (
                    award.award_id, award.report_id, award.researcher_addr,
                    award.oby_amount, award.vest_months, award.issued_at,
                    award.vest_start, award.fully_vested_at,
                ))
                conn.execute(
                    'UPDATE tranche_balances SET awarded_nxs = awarded_nxs + ? '
                    'WHERE tranche = ?',
                    (oby_amount, report.tranche.value)
                )
            self._save_report(report)

        log.info(
            f"Award issued: {award.award_id} | "
            f"{oby_amount:,.0f} NXS | "
            f"vest={vest_months}mo | "
            f"researcher={report.researcher_name} | "
            f"{report.researcher_addr[:16]}..."
        )
        return award

    # ── Queries ───────────────────────────────────────────────────────────────

    def tranche_balance(self, tranche: Tranche) -> dict:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                'SELECT total_nxs, awarded_nxs FROM tranche_balances '
                'WHERE tranche = ?', (tranche.value,)
            ).fetchone()
        if not row:
            return {'total_nxs': 0, 'awarded_nxs': 0, 'remaining_nxs': 0}
        total, awarded = row
        return {
            'tranche'      : tranche.value,
            'total_nxs'    : total,
            'awarded_nxs'  : round(awarded, 4),
            'remaining_nxs': round(total - awarded, 4),
            'pct_used'     : round(awarded / total * 100, 1) if total else 0,
        }

    def researcher_awards(self, researcher_addr: str) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                'SELECT * FROM awards WHERE researcher_addr = ? '
                'ORDER BY issued_at DESC', (researcher_addr,)
            ).fetchall()
        awards = []
        for r in rows:
            a = BountyAward(**dict(r))
            awards.append(a.to_dict())
        return awards

    def open_reports(self) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM reports WHERE status NOT IN "
                "('invalid','duplicate','awarded','disclosed') "
                "ORDER BY submitted_at ASC"
            ).fetchall()
        return [self._row_to_report(r).to_dict() for r in rows]

    def overdue_acknowledgements(self) -> list[dict]:
        cutoff = int(time.time()) - DISCLOSURE_ACK_HRS * 3600
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM reports WHERE status = 'submitted' "
                "AND submitted_at < ?", (cutoff,)
            ).fetchall()
        return [self._row_to_report(r).to_dict() for r in rows]

    def status(self) -> dict:
        with sqlite3.connect(self.db_path) as conn:
            counts = conn.execute(
                'SELECT status, COUNT(*) FROM reports GROUP BY status'
            ).fetchall()
            total_awarded = conn.execute(
                'SELECT SUM(oby_amount) FROM awards'
            ).fetchone()[0] or 0
        tranches = {}
        for t in Tranche:
            bal = self.tranche_balance(t)
            bal['funding_source'] = TRANCHE_SOURCES[t.value]
            tranches[t.value] = bal
        return {
            'hard_cap'           : BOUNTY_HARD_CAP,
            'funding_source'     : BOUNTY_FUNDING_SOURCE,
            'total_awarded_nxs'  : round(total_awarded, 4),
            'note'               : 'No fixed cap — DAO allocates from vault as needed',
            'tranches'           : tranches,
            'reports'            : {r[0]: r[1] for r in counts},
            'severity_ranges'    : {
                s.value: {
                    'min_nxs'    : AWARD_RANGES[s][0],
                    'max_nxs'    : AWARD_RANGES[s][1],
                    'description': SEVERITY_DESCRIPTIONS[s],
                }
                for s in Severity
            },
            'platforms'          : {
                'immunefi'         : IMMUNEFI_URL,
                'email'            : SECURITY_EMAIL,
                'vest_months'      : AWARD_VEST_MONTHS,
                'ack_hrs'          : DISCLOSURE_ACK_HRS,
                'fix_hrs_critical' : DISCLOSURE_FIX_HRS,
            },
        }

    # ── Internal ──────────────────────────────────────────────────────────────

    def _save_report(self, r: BountyReport):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                INSERT OR REPLACE INTO reports
                (report_id, tranche, researcher_addr, researcher_name,
                 title, description, severity_claimed, severity_assigned,
                 affected_component, proof_of_concept, status,
                 submitted_at, acknowledged_at, fixed_at, awarded_at,
                 award_nxs, vest_months, award_rationale, duplicate_of, notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''', (
                r.report_id, r.tranche.value, r.researcher_addr,
                r.researcher_name, r.title, r.description,
                r.severity_claimed.value,
                r.severity_assigned.value if r.severity_assigned else None,
                r.affected_component, r.proof_of_concept, r.status.value,
                r.submitted_at, r.acknowledged_at, r.fixed_at, r.awarded_at,
                r.award_nxs, r.vest_months, r.award_rationale,
                r.duplicate_of, r.notes,
            ))

    def _get_report(self, report_id: str) -> Optional[BountyReport]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                'SELECT * FROM reports WHERE report_id=?', (report_id,)
            ).fetchone()
        return self._row_to_report(row) if row else None

    def _row_to_report(self, row) -> BountyReport:
        return BountyReport(
            report_id          = row['report_id'],
            tranche            = Tranche(row['tranche']),
            researcher_addr    = row['researcher_addr'],
            researcher_name    = row['researcher_name'],
            title              = row['title'],
            description        = row['description'],
            severity_claimed   = Severity(row['severity_claimed']),
            severity_assigned  = Severity(row['severity_assigned'])
                                 if row['severity_assigned'] else None,
            affected_component = row['affected_component'],
            proof_of_concept   = row['proof_of_concept'],
            status             = ReportStatus(row['status']),
            submitted_at       = row['submitted_at'],
            acknowledged_at    = row['acknowledged_at'],
            fixed_at           = row['fixed_at'],
            awarded_at         = row['awarded_at'],
            award_nxs          = row['award_nxs'],
            vest_months        = row['vest_months'],
            award_rationale    = row['award_rationale'],
            duplicate_of       = row['duplicate_of'],
            notes              = row['notes'],
        )
