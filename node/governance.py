"""
Obelyth Progressive Governance Engine
==========================================
Three-phase decentralization model:

PHASE 1 — Year 1: Founder Executive Authority
  - Founder makes all decisions unilaterally
  - Community votes are advisory only
  - DAO vault accumulates, not deployed without founder approval
  - Fast, decisive — appropriate when network is fragile

PHASE 2 — Year 2: Qualified Community Governance
  - Votes binding IF quorum is met (15% circulating for routine, 25% for treasury)
  - If quorum NOT met → founder retains casting decision
  - Prevents minority governance capture
  - NIP process: proposal → vote → founder executes if passed

PHASE 3 — Year 3-4: Steering Committee + Community
  - 5-7 elected committee members hold real resource allocation power
  - Community votes drive strategic direction
  - Founder retains emergency veto (constitutional threats only) + tiebreaker
  - Committee can be replaced by community vote
  - Routine decisions: committee majority
  - Major decisions: community vote + committee ratification
  - Emergency veto: ONLY for constitutional parameter threats

CONSTITUTIONAL PARAMETERS (cannot be changed by any governance vote):
  - 21,000,000 OBY hard cap
  - 90% liquidity lock
  - Creator Share (5% of fees)
  - DAO mining tax (5% of miner earnings)
  These require 90% supermajority — practically immutable.

EMERGENCY VETO SCOPE (founder can block):
  ✓ Proposal to reduce Creator Share
  ✓ Proposal to unlock liquidity reserve
  ✓ Proposal to change OBY supply cap
  ✓ Proposal to eliminate DAO mining tax
  ✗ Routine treasury allocation founder disagrees with
  ✗ Grant recipient founder doesn't prefer
  ✗ Fee parameter change founder dislikes
  ✗ Any decision that isn't a constitutional threat
"""

import time
import json
import uuid
import sqlite3
import logging
import threading
from pathlib     import Path
from dataclasses import dataclass, field, asdict
from typing      import Optional
from enum        import Enum

log = logging.getLogger('obelyth.governance')

# ── Phase Configuration ────────────────────────────────────────────────────────
PHASE_1_END_DAYS     = 365        # ~Year 1
PHASE_2_END_DAYS     = 730        # ~Year 2
PHASE_3_START_DAYS   = 730        # Year 3+ steering committee

# Phase 2 quorum requirements (% of circulating OBY supply)
QUORUM_ROUTINE       = 0.15       # 15% for routine decisions
QUORUM_TREASURY      = 0.25       # 25% for treasury allocations
QUORUM_CONSTITUTIONAL= 0.90       # 90% supermajority for constitutional changes

# Phase 2 approval thresholds (% of votes cast)
APPROVAL_ROUTINE     = 0.50       # simple majority
APPROVAL_TREASURY    = 0.60       # 60% for treasury
APPROVAL_STEERING    = 0.67       # 2/3 for steering committee changes

# Steering committee
COMMITTEE_SIZE       = 7          # seats
COMMITTEE_TERM_DAYS  = 365        # annual elections

# Voting periods
VOTE_PERIOD_DAYS     = 7          # standard vote window
VOTE_PERIOD_EMERGENCY= 2          # emergency vote window

# Constitutional parameters — veto applicable to proposals threatening these
CONSTITUTIONAL_PARAMS = [
    'oby_supply_cap',
    'liquidity_lock_pct',
    'creator_share_pct',
    'dao_mining_tax_pct',
]


# ── Enums ──────────────────────────────────────────────────────────────────────

class GovernancePhase(int, Enum):
    FOUNDER_AUTHORITY  = 1
    QUALIFIED_COMMUNITY= 2
    STEERING_COMMITTEE = 3


class ProposalType(str, Enum):
    ROUTINE         = 'routine'          # fee params, block size targets
    TREASURY        = 'treasury'         # DAO fund allocation
    GRANT           = 'grant'            # ecosystem grant
    STEERING_CHANGE = 'steering_change'  # add/remove committee member
    CONSTITUTIONAL  = 'constitutional'   # change protected parameters (requires 90%)
    EXCHANGE_LISTING= 'exchange_listing' # OBY vault for exchange liquidity
    BURN_ENABLE     = 'burn_enable'      # enable/adjust DAO burn


class ProposalStatus(str, Enum):
    DRAFT     = 'draft'
    ACTIVE    = 'active'       # voting open
    PASSED    = 'passed'       # quorum met, approved
    FAILED    = 'failed'       # quorum met, rejected
    NO_QUORUM = 'no_quorum'    # quorum not met → founder decides
    VETOED    = 'vetoed'       # founder emergency veto applied
    EXECUTED  = 'executed'     # on-chain action taken
    CANCELLED = 'cancelled'


class VoteChoice(str, Enum):
    FOR     = 'for'
    AGAINST = 'against'
    ABSTAIN = 'abstain'


# ── Data Classes ───────────────────────────────────────────────────────────────

@dataclass
class Proposal:
    proposal_id     : str
    nip_number      : int           # NIP-001, NIP-002, etc.
    title           : str
    description     : str
    proposal_type   : ProposalType
    proposed_by     : str           # OBY address
    phase_created   : GovernancePhase
    # Voting
    votes_for       : float = 0.0   # OBY weight
    votes_against   : float = 0.0
    votes_abstain   : float = 0.0
    voter_count     : int   = 0
    # Timeline
    created_at      : int   = field(default_factory=lambda: int(time.time()))
    voting_ends_at  : int   = 0
    executed_at     : int   = 0
    # Status
    status          : ProposalStatus = ProposalStatus.DRAFT
    quorum_required : float = QUORUM_ROUTINE
    approval_required: float = APPROVAL_ROUTINE
    # Execution
    execution_data  : dict  = field(default_factory=dict)
    founder_decision: str   = ''    # if no_quorum: 'approved' | 'rejected'
    veto_reason     : str   = ''
    # Affects constitutional params?
    is_constitutional: bool = False

    @property
    def total_votes(self) -> float:
        return self.votes_for + self.votes_against + self.votes_abstain

    @property
    def approval_pct(self) -> float:
        cast = self.votes_for + self.votes_against
        return self.votes_for / cast if cast > 0 else 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d['proposal_type']  = self.proposal_type.value
        d['phase_created']  = self.phase_created.value
        d['status']         = self.status.value
        d['approval_pct']   = round(self.approval_pct * 100, 2)
        return d


@dataclass
class Vote:
    vote_id     : str
    proposal_id : str
    voter_addr  : str
    choice      : VoteChoice
    oby_weight  : float     # OBY balance at snapshot
    cast_at     : int = field(default_factory=lambda: int(time.time()))

    def to_dict(self) -> dict:
        d = asdict(self)
        d['choice'] = self.choice.value
        return d


@dataclass
class SteeringMember:
    address     : str
    name        : str
    elected_at  : int
    term_ends   : int
    votes_received: float = 0.0
    proposals_voted: int  = 0
    active      : bool    = True

    def to_dict(self) -> dict:
        return asdict(self)


# ── Governance Engine ──────────────────────────────────────────────────────────

class GovernanceEngine:
    """
    Progressive decentralization governance engine.
    Phase advances automatically based on network age.
    All decisions logged immutably for audit.
    """

    def __init__(
        self,
        db_path          : str,
        genesis_timestamp: int,
        founder_address  : str,
        circulating_supply_fn: callable = None,  # returns current OBY supply
    ):
        self.db_path      = db_path
        self.genesis_ts   = genesis_timestamp
        self.founder_addr = founder_address
        self._get_supply  = circulating_supply_fn or (lambda: 1_000_000.0)
        self._lock        = threading.RLock()
        self._nip_counter = 0
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._load_nip_counter()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS proposals (
                    proposal_id      TEXT PRIMARY KEY,
                    nip_number       INTEGER NOT NULL,
                    title            TEXT NOT NULL,
                    description      TEXT NOT NULL,
                    proposal_type    TEXT NOT NULL,
                    proposed_by      TEXT NOT NULL,
                    phase_created    INTEGER NOT NULL,
                    votes_for        REAL NOT NULL DEFAULT 0,
                    votes_against    REAL NOT NULL DEFAULT 0,
                    votes_abstain    REAL NOT NULL DEFAULT 0,
                    voter_count      INTEGER NOT NULL DEFAULT 0,
                    created_at       INTEGER NOT NULL,
                    voting_ends_at   INTEGER NOT NULL DEFAULT 0,
                    executed_at      INTEGER NOT NULL DEFAULT 0,
                    status           TEXT NOT NULL DEFAULT 'draft',
                    quorum_required  REAL NOT NULL,
                    approval_required REAL NOT NULL,
                    execution_data   TEXT NOT NULL DEFAULT '{}',
                    founder_decision TEXT NOT NULL DEFAULT '',
                    veto_reason      TEXT NOT NULL DEFAULT '',
                    is_constitutional INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS votes (
                    vote_id      TEXT PRIMARY KEY,
                    proposal_id  TEXT NOT NULL,
                    voter_addr   TEXT NOT NULL,
                    choice       TEXT NOT NULL,
                    oby_weight   REAL NOT NULL,
                    cast_at      INTEGER NOT NULL,
                    UNIQUE(proposal_id, voter_addr),
                    FOREIGN KEY(proposal_id) REFERENCES proposals(proposal_id)
                );

                CREATE TABLE IF NOT EXISTS steering_committee (
                    address       TEXT PRIMARY KEY,
                    name          TEXT NOT NULL,
                    elected_at    INTEGER NOT NULL,
                    term_ends     INTEGER NOT NULL,
                    votes_received REAL NOT NULL DEFAULT 0,
                    proposals_voted INTEGER NOT NULL DEFAULT 0,
                    active        INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS governance_log (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    event      TEXT NOT NULL,
                    actor      TEXT NOT NULL,
                    target     TEXT NOT NULL,
                    details    TEXT NOT NULL,
                    timestamp  INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_proposals_status
                    ON proposals(status, voting_ends_at);
                CREATE INDEX IF NOT EXISTS idx_votes_proposal
                    ON votes(proposal_id);
            ''')

    def _load_nip_counter(self):
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                'SELECT MAX(nip_number) FROM proposals'
            ).fetchone()
        self._nip_counter = (row[0] or 0)

    # ── Phase Management ──────────────────────────────────────────────────────

    @property
    def current_phase(self) -> GovernancePhase:
        days_since_genesis = (time.time() - self.genesis_ts) / 86_400
        if days_since_genesis < PHASE_1_END_DAYS:
            return GovernancePhase.FOUNDER_AUTHORITY
        elif days_since_genesis < PHASE_2_END_DAYS:
            return GovernancePhase.QUALIFIED_COMMUNITY
        else:
            return GovernancePhase.STEERING_COMMITTEE

    @property
    def days_in_network(self) -> int:
        return int((time.time() - self.genesis_ts) / 86_400)

    def phase_description(self) -> dict:
        phase = self.current_phase
        days  = self.days_in_network
        descriptions = {
            GovernancePhase.FOUNDER_AUTHORITY: {
                'name'       : 'Phase 1 — Founder Executive Authority',
                'description': 'Founder makes all decisions. Community votes advisory only.',
                'transitions_in_days': max(0, PHASE_1_END_DAYS - days),
            },
            GovernancePhase.QUALIFIED_COMMUNITY: {
                'name'       : 'Phase 2 — Qualified Community Governance',
                'description': f'Votes binding at {QUORUM_ROUTINE*100:.0f}% quorum. '
                               f'Founder decides if quorum not met.',
                'transitions_in_days': max(0, PHASE_2_END_DAYS - days),
            },
            GovernancePhase.STEERING_COMMITTEE: {
                'name'       : 'Phase 3 — Steering Committee + Community',
                'description': 'Elected committee allocates resources. '
                               'Founder holds emergency veto (constitutional threats only).',
                'transitions_in_days': None,
            },
        }
        return {'phase': phase.value, **descriptions[phase]}

    # ── Proposals ────────────────────────────────────────────────────────────

    def create_proposal(
        self,
        title            : str,
        description      : str,
        proposal_type    : ProposalType,
        proposed_by      : str,
        execution_data   : dict = None,
        emergency        : bool = False,
    ) -> Proposal:
        """Create a new governance proposal (NIP)."""
        phase = self.current_phase

        # In Phase 1 only founder can create binding proposals
        if (phase == GovernancePhase.FOUNDER_AUTHORITY and
                proposed_by != self.founder_addr):
            log.info(
                f"Phase 1: proposal by {proposed_by[:12]} recorded as advisory"
            )

        # Determine quorum and approval requirements
        is_constitutional = any(
            param in json.dumps(execution_data or {})
            for param in CONSTITUTIONAL_PARAMS
        ) or proposal_type == ProposalType.CONSTITUTIONAL

        if is_constitutional:
            quorum    = QUORUM_CONSTITUTIONAL
            approval  = QUORUM_CONSTITUTIONAL
        elif proposal_type in (ProposalType.TREASURY,
                                ProposalType.EXCHANGE_LISTING):
            quorum    = QUORUM_TREASURY
            approval  = APPROVAL_TREASURY
        elif proposal_type == ProposalType.STEERING_CHANGE:
            quorum    = QUORUM_ROUTINE
            approval  = APPROVAL_STEERING
        else:
            quorum    = QUORUM_ROUTINE
            approval  = APPROVAL_ROUTINE

        vote_days  = VOTE_PERIOD_EMERGENCY if emergency else VOTE_PERIOD_DAYS

        with self._lock:
            self._nip_counter += 1
            nip_num = self._nip_counter

        proposal = Proposal(
            proposal_id      = str(uuid.uuid4()),
            nip_number       = nip_num,
            title            = title,
            description      = description,
            proposal_type    = proposal_type,
            proposed_by      = proposed_by,
            phase_created    = phase,
            voting_ends_at   = int(time.time()) + vote_days * 86_400,
            quorum_required  = quorum,
            approval_required= approval,
            execution_data   = execution_data or {},
            is_constitutional= is_constitutional,
            status           = ProposalStatus.ACTIVE
                               if phase != GovernancePhase.FOUNDER_AUTHORITY
                               else ProposalStatus.DRAFT,
        )

        self._save_proposal(proposal)
        self._log('proposal_created', proposed_by, proposal.proposal_id,
                  f'NIP-{nip_num:03d}: {title}')

        log.info(
            f"NIP-{nip_num:03d} created: {title} | "
            f"type={proposal_type.value} | "
            f"phase={phase.value} | "
            f"quorum={quorum*100:.0f}% | "
            f"constitutional={is_constitutional}"
        )
        return proposal

    def cast_vote(
        self,
        proposal_id : str,
        voter_addr  : str,
        choice      : VoteChoice,
        oby_balance : float,
    ) -> tuple[bool, str]:
        """
        Cast a vote on a proposal.
        Returns (success, message).
        """
        proposal = self._get_proposal(proposal_id)
        if not proposal:
            return False, "Proposal not found"
        if proposal.status != ProposalStatus.ACTIVE:
            return False, f"Proposal not active (status: {proposal.status.value})"
        if int(time.time()) > proposal.voting_ends_at:
            return False, "Voting period has ended"
        if self.current_phase == GovernancePhase.FOUNDER_AUTHORITY:
            return False, "Phase 1: community votes are advisory — recorded but not binding"
        if oby_balance <= 0:
            return False, "Must hold OBY to vote"

        # Check not already voted
        with sqlite3.connect(self.db_path) as conn:
            existing = conn.execute(
                'SELECT vote_id FROM votes WHERE proposal_id=? AND voter_addr=?',
                (proposal_id, voter_addr)
            ).fetchone()
            if existing:
                return False, "Already voted on this proposal"

        vote = Vote(
            vote_id     = str(uuid.uuid4()),
            proposal_id = proposal_id,
            voter_addr  = voter_addr,
            choice      = choice,
            oby_weight  = oby_balance,
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                INSERT INTO votes (vote_id, proposal_id, voter_addr,
                    choice, oby_weight, cast_at)
                VALUES (?,?,?,?,?,?)
            ''', (vote.vote_id, vote.proposal_id, vote.voter_addr,
                  vote.choice.value, vote.oby_weight, vote.cast_at))
            # Update proposal vote totals
            col = {'for': 'votes_for', 'against': 'votes_against',
                   'abstain': 'votes_abstain'}[choice.value]
            conn.execute(
                f'UPDATE proposals SET {col}={col}+?, voter_count=voter_count+1 '
                f'WHERE proposal_id=?',
                (oby_balance, proposal_id)
            )

        self._log('vote_cast', voter_addr, proposal_id,
                  f'{choice.value} {oby_balance:.2f} OBY')
        return True, f"Vote recorded: {choice.value} ({oby_balance:.2f} OBY)"

    def finalize_proposal(
        self,
        proposal_id        : str,
        founder_override   : str = None,  # 'approved'|'rejected' if no quorum
    ) -> tuple[ProposalStatus, str]:
        """
        Called when voting period ends.
        Determines outcome based on phase, quorum, and approval threshold.
        Returns (status, explanation).
        """
        proposal = self._get_proposal(proposal_id)
        if not proposal:
            return ProposalStatus.CANCELLED, "Proposal not found"

        phase           = self.current_phase
        circulating     = self._get_supply()
        quorum_oby      = circulating * proposal.quorum_required
        quorum_met      = proposal.total_votes >= quorum_oby

        # ── Phase 1: founder decides ──
        if phase == GovernancePhase.FOUNDER_AUTHORITY:
            decision = founder_override or 'pending'
            if decision == 'approved':
                proposal.status         = ProposalStatus.PASSED
                proposal.founder_decision = 'approved'
                msg = "Phase 1: founder approved"
            elif decision == 'rejected':
                proposal.status         = ProposalStatus.FAILED
                proposal.founder_decision = 'rejected'
                msg = "Phase 1: founder rejected"
            else:
                proposal.status = ProposalStatus.DRAFT
                msg = "Phase 1: awaiting founder decision"
            self._save_proposal(proposal)
            self._log('proposal_finalized', self.founder_addr, proposal_id, msg)
            return proposal.status, msg

        # ── Phase 2+: check quorum ──
        if not quorum_met:
            proposal.status = ProposalStatus.NO_QUORUM
            if founder_override in ('approved', 'rejected'):
                proposal.founder_decision = founder_override
                proposal.status = (ProposalStatus.PASSED
                                   if founder_override == 'approved'
                                   else ProposalStatus.FAILED)
                msg = (
                    f"Quorum not met ({proposal.total_votes:.0f}/"
                    f"{quorum_oby:.0f} OBY) — "
                    f"founder decision: {founder_override}"
                )
            else:
                msg = (
                    f"Quorum not met ({proposal.total_votes:.0f}/"
                    f"{quorum_oby:.0f} OBY required). "
                    f"Awaiting founder casting decision."
                )
            self._save_proposal(proposal)
            self._log('proposal_no_quorum', self.founder_addr, proposal_id, msg)
            log.info(f"NIP-{proposal.nip_number:03d} no quorum: {msg}")
            return proposal.status, msg

        # Quorum met — check approval threshold
        approved = proposal.approval_pct >= proposal.approval_required
        proposal.status = ProposalStatus.PASSED if approved else ProposalStatus.FAILED
        msg = (
            f"Quorum met ({proposal.total_votes:.0f} OBY). "
            f"Approval: {proposal.approval_pct*100:.1f}% "
            f"({'✓' if approved else '✗'} threshold {proposal.approval_required*100:.0f}%)"
        )
        self._save_proposal(proposal)
        self._log(
            'proposal_finalized', 'community', proposal_id,
            f"{'PASSED' if approved else 'FAILED'}: {msg}"
        )
        log.info(f"NIP-{proposal.nip_number:03d} {'PASSED' if approved else 'FAILED'}: {msg}")
        return proposal.status, msg

    # ── Emergency Veto ────────────────────────────────────────────────────────

    def apply_veto(self, proposal_id: str, reason: str) -> tuple[bool, str]:
        """
        Founder applies emergency veto.
        ONLY valid for constitutional threats — validated here.
        Returns (success, message).
        """
        proposal = self._get_proposal(proposal_id)
        if not proposal:
            return False, "Proposal not found"

        # Validate veto is within scope
        if not proposal.is_constitutional:
            msg = (
                f"Veto REJECTED: NIP-{proposal.nip_number:03d} does not "
                f"threaten constitutional parameters. "
                f"Emergency veto is only valid for proposals affecting: "
                f"{', '.join(CONSTITUTIONAL_PARAMS)}."
            )
            log.warning(msg)
            self._log('veto_rejected', self.founder_addr, proposal_id, msg)
            return False, msg

        proposal.status     = ProposalStatus.VETOED
        proposal.veto_reason= reason
        self._save_proposal(proposal)
        msg = (
            f"Constitutional veto applied to NIP-{proposal.nip_number:03d}. "
            f"Reason: {reason}"
        )
        self._log('veto_applied', self.founder_addr, proposal_id, msg)
        log.warning(f"VETO: {msg}")
        return True, msg

    # ── Steering Committee ────────────────────────────────────────────────────

    def add_committee_member(
        self,
        address        : str,
        name           : str,
        votes_received : float = 0.0,
    ) -> SteeringMember:
        member = SteeringMember(
            address        = address,
            name           = name,
            elected_at     = int(time.time()),
            term_ends      = int(time.time()) + COMMITTEE_TERM_DAYS * 86_400,
            votes_received = votes_received,
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                INSERT OR REPLACE INTO steering_committee
                (address, name, elected_at, term_ends, votes_received, active)
                VALUES (?,?,?,?,?,1)
            ''', (member.address, member.name, member.elected_at,
                  member.term_ends, member.votes_received))
        self._log('committee_member_added', self.founder_addr, address, name)
        log.info(f"Steering committee: {name} ({address[:12]}...) added")
        return member

    def get_committee(self) -> list[SteeringMember]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                'SELECT * FROM steering_committee WHERE active=1'
            ).fetchall()
        return [SteeringMember(**dict(r)) for r in rows]

    # ── Queries ───────────────────────────────────────────────────────────────

    def active_proposals(self) -> list[Proposal]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM proposals WHERE status='active' "
                "ORDER BY created_at DESC"
            ).fetchall()
        return [self._row_to_proposal(r) for r in rows]

    def proposal_history(self, limit: int = 20) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                'SELECT * FROM proposals ORDER BY created_at DESC LIMIT ?',
                (limit,)
            ).fetchall()
        return [self._row_to_proposal(r).to_dict() for r in rows]

    def status(self) -> dict:
        phase_info = self.phase_description()
        with sqlite3.connect(self.db_path) as conn:
            counts = conn.execute('''
                SELECT status, COUNT(*) FROM proposals GROUP BY status
            ''').fetchall()
        proposal_counts = {r[0]: r[1] for r in counts}
        committee = self.get_committee()
        return {
            'governance_phase'  : phase_info,
            'days_since_genesis': self.days_in_network,
            'proposals'         : proposal_counts,
            'committee_size'    : len(committee),
            'committee_members' : [m.to_dict() for m in committee],
            'quorum_routine'    : f'{QUORUM_ROUTINE*100:.0f}% of circulating OBY',
            'quorum_treasury'   : f'{QUORUM_TREASURY*100:.0f}% of circulating OBY',
            'constitutional_threshold': f'{QUORUM_CONSTITUTIONAL*100:.0f}% supermajority',
            'veto_scope'        : CONSTITUTIONAL_PARAMS,
        }

    # ── Internal ─────────────────────────────────────────────────────────────

    def _save_proposal(self, p: Proposal):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                INSERT OR REPLACE INTO proposals
                (proposal_id, nip_number, title, description, proposal_type,
                 proposed_by, phase_created, votes_for, votes_against,
                 votes_abstain, voter_count, created_at, voting_ends_at,
                 executed_at, status, quorum_required, approval_required,
                 execution_data, founder_decision, veto_reason, is_constitutional)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''', (
                p.proposal_id, p.nip_number, p.title, p.description,
                p.proposal_type.value, p.proposed_by, p.phase_created.value,
                p.votes_for, p.votes_against, p.votes_abstain, p.voter_count,
                p.created_at, p.voting_ends_at, p.executed_at, p.status.value,
                p.quorum_required, p.approval_required,
                json.dumps(p.execution_data), p.founder_decision,
                p.veto_reason, int(p.is_constitutional),
            ))

    def _get_proposal(self, proposal_id: str) -> Optional[Proposal]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                'SELECT * FROM proposals WHERE proposal_id=?', (proposal_id,)
            ).fetchone()
        return self._row_to_proposal(row) if row else None

    def _row_to_proposal(self, row) -> Proposal:
        return Proposal(
            proposal_id      = row['proposal_id'],
            nip_number       = row['nip_number'],
            title            = row['title'],
            description      = row['description'],
            proposal_type    = ProposalType(row['proposal_type']),
            proposed_by      = row['proposed_by'],
            phase_created    = GovernancePhase(row['phase_created']),
            votes_for        = row['votes_for'],
            votes_against    = row['votes_against'],
            votes_abstain    = row['votes_abstain'],
            voter_count      = row['voter_count'],
            created_at       = row['created_at'],
            voting_ends_at   = row['voting_ends_at'],
            executed_at      = row['executed_at'],
            status           = ProposalStatus(row['status']),
            quorum_required  = row['quorum_required'],
            approval_required= row['approval_required'],
            execution_data   = json.loads(row['execution_data'] or '{}'),
            founder_decision = row['founder_decision'],
            veto_reason      = row['veto_reason'],
            is_constitutional= bool(row['is_constitutional']),
        )

    def _log(self, event: str, actor: str, target: str, details: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                'INSERT INTO governance_log (event,actor,target,details,timestamp)'
                ' VALUES (?,?,?,?,?)',
                (event, actor, target, details, int(time.time()))
            )
