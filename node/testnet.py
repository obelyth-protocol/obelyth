"""
Obelyth Pre-Mainnet Community Tracker
==========================================
Tracks ALL contributors who earn from the 3% pre-mainnet community pool
(630,000 OBY) before mainnet launch.

WHO IS IN THIS POOL:
  Validators         — keep the testnet alive, sign blocks
  GPU Miners         — run real AI compute jobs on GPUs
  AI Developers      — use the SDK, test models, give product feedback
  Code Contributors  — improve the node, SDK, tooling, add features
  Documentation      — tutorials, guides, translated docs
  Data Scientists    — economic models, dashboards, network analytics
  Security Researchers — bug reports during testnet (pre-mainnet bounty)
  Community          — Discord, advocacy, onboarding, translations

PRINCIPLE:
  No single role dominates. A great documentation writer earns as much
  as a good GPU miner. This builds a genuinely diverse community, not
  just a mining pool with a few devs around the edges.

  All roles compete on the same leaderboard. Points convert to OBY at
  a published rate before mainnet launches. Vesting: 6 months linear.

POOL: 630,000 OBY from genesis block address OBY_COMMUNITY_POOL
MAX PER PARTICIPANT: 15,000 OBY (prevents single-actor dominance)
POINTS → OBY: published conversion rate set 30 days before mainnet

GRADUATION CRITERIA (all four must be met before mainnet):
  - Rust node running as primary for 60+ consecutive stable days
  - 60 consecutive stable days with no critical bug
  - 10+ independent validators across 3+ countries
  - 5+ developers with completed SDK jobs
"""

import time
import json
import math
import sqlite3
import threading
import logging
from pathlib     import Path
from dataclasses import dataclass, field, asdict
from typing      import Optional
from enum        import Enum

log = logging.getLogger('obelyth.testnet')

# ── Pool Constants ─────────────────────────────────────────────────────────────
COMMUNITY_POOL_OBY   = 630_000.0    # 3% of 21M supply
MAX_PER_PARTICIPANT  = 15_000.0     # cap per person — prevents dominance
GENESIS_VEST_MONTHS  = 6            # linear vesting from mainnet

# Graduation thresholds
GRADUATION_STABLE_DAYS  = 30
GRADUATION_VALIDATORS   = 10
GRADUATION_COUNTRIES    = 3
GRADUATION_DEV_JOBS     = 5

# ── Role Definitions ───────────────────────────────────────────────────────────

class Role(str, Enum):
    VALIDATOR    = 'validator'
    MINER        = 'miner'
    AI_DEV       = 'ai_developer'
    CODE_CONTRIB = 'code_contributor'
    DOCS         = 'documentation'
    DATA_SCI     = 'data_scientist'
    SECURITY     = 'security_researcher'
    COMMUNITY    = 'community'

ROLE_LABELS = {
    Role.VALIDATOR   : 'Validator',
    Role.MINER       : 'GPU Miner',
    Role.AI_DEV      : 'AI Developer',
    Role.CODE_CONTRIB: 'Code Contributor',
    Role.DOCS        : 'Documentation',
    Role.DATA_SCI    : 'Data Scientist',
    Role.SECURITY    : 'Security Researcher',
    Role.COMMUNITY   : 'Community',
}

ROLE_DESCRIPTIONS = {
    Role.VALIDATOR   : 'Run a validator node. Earn points for uptime, blocks signed, and days active.',
    Role.MINER       : 'Run GPU compute jobs. Earn points for verified jobs, GPU-hours, and uptime.',
    Role.AI_DEV      : 'Use the SDK to run real AI jobs. Earn points for jobs run, models tested, and written feedback.',
    Role.CODE_CONTRIB: 'Merge PRs to the Obelyth repo. Earn points for code quality, complexity, and community reviews.',
    Role.DOCS        : 'Write tutorials, guides, and translated documentation. Earn points for quality and reach.',
    Role.DATA_SCI    : 'Build economic models, analytics dashboards, and network health tools. Earn points per accepted deliverable.',
    Role.SECURITY    : 'Find and responsibly disclose vulnerabilities. Points scale with severity.',
    Role.COMMUNITY   : 'Grow and support the community. Earn points for active days, helpful messages, and onboarding new participants.',
}

# Maximum base points per role (before multipliers)
# Calibrated so no single role has an insurmountable advantage
ROLE_MAX_POINTS = {
    Role.VALIDATOR   : 10_000,
    Role.MINER       : 10_000,
    Role.AI_DEV      :  8_000,
    Role.CODE_CONTRIB: 10_000,
    Role.DOCS        :  7_000,
    Role.DATA_SCI    :  8_000,
    Role.SECURITY    : 10_000,  # critical bug = full points
    Role.COMMUNITY   :  5_000,
}

# ── Activity Stats per Role ────────────────────────────────────────────────────

@dataclass
class ValidatorActivity:
    uptime_pct    : float = 0.0    # 0–100
    blocks_signed : int   = 0
    days_active   : int   = 0
    stake_oby     : float = 0.0
    country       : str   = ''

    def points(self) -> float:
        uptime  = (self.uptime_pct / 100) ** 1.5   # rewards consistency
        blocks  = min(1.0, self.blocks_signed / 15_000)
        days    = min(1.0, self.days_active / 90)
        return round(ROLE_MAX_POINTS[Role.VALIDATOR] *
                     (uptime * 0.50 + blocks * 0.30 + days * 0.20), 2)


@dataclass
class MinerActivity:
    gpu_count      : int   = 1
    verified_jobs  : int   = 0
    gpu_hours      : float = 0.0
    days_active    : int   = 0
    avg_job_time_s : float = 0.0

    def points(self) -> float:
        jobs  = min(1.0, self.verified_jobs / 200)
        hours = min(1.0, self.gpu_hours / (720 * self.gpu_count))
        days  = min(1.0, self.days_active / 90)
        per_gpu = ROLE_MAX_POINTS[Role.MINER] * (
            jobs * 0.50 + hours * 0.30 + days * 0.20
        )
        # Multi-GPU bonus: diminishing returns above 4 GPUs
        gpu_mult = min(3.0, 1.0 + math.log(self.gpu_count, 2) * 0.5)
        return round(per_gpu * gpu_mult, 2)


@dataclass
class AIDeveloperActivity:
    jobs_run       : int  = 0
    verified_jobs  : int  = 0
    models_tested  : int  = 0
    feedback_given : bool = False
    sdk_version    : str  = ''
    use_case       : str  = ''   # what they're building

    def points(self) -> float:
        if self.verified_jobs < 3:
            return round(ROLE_MAX_POINTS[Role.AI_DEV] *
                         (self.verified_jobs / 3) * 0.4, 2)
        jobs     = min(1.0, self.verified_jobs / 50)
        models   = min(1.0, self.models_tested / 10)
        feedback = 0.20 if self.feedback_given else 0.0
        usecase  = 0.10 if self.use_case else 0.0
        return round(ROLE_MAX_POINTS[Role.AI_DEV] *
                     (jobs * 0.45 + models * 0.25 + feedback + usecase), 2)


@dataclass
class CodeContributorActivity:
    prs_merged        : int   = 0
    lines_added       : int   = 0
    lines_removed     : int   = 0   # refactoring counts too
    issues_resolved   : int   = 0
    reviews_given     : int   = 0   # reviewing others' PRs
    complexity_score  : float = 0.0  # 0–10, assigned by maintainer
    areas             : list  = field(default_factory=list)
    # e.g. ['sdk', 'node', 'docs', 'tooling', 'tests']

    def points(self) -> float:
        if self.prs_merged == 0:
            return 0.0
        prs       = min(1.0, self.prs_merged / 20)
        complexity= min(1.0, self.complexity_score / 10)
        reviews   = min(0.15, self.reviews_given * 0.03)
        issues    = min(0.15, self.issues_resolved * 0.05)
        # Bonus for covering multiple areas of the codebase
        diversity = min(0.10, len(set(self.areas)) * 0.025)
        return round(ROLE_MAX_POINTS[Role.CODE_CONTRIB] *
                     (prs * 0.45 + complexity * 0.30 +
                      reviews + issues + diversity), 2)


@dataclass
class DocumentationActivity:
    pages_written     : int   = 0
    tutorials_written : int   = 0
    languages         : int   = 1   # translations multiply contribution
    community_rating  : float = 0.0  # avg rating out of 5 from community votes
    views_estimate    : int   = 0   # rough reach metric

    def points(self) -> float:
        content  = min(1.0, (self.pages_written * 0.5 +
                             self.tutorials_written * 2.0) / 20)
        lang_mult= min(2.0, 1.0 + (self.languages - 1) * 0.25)
        quality  = (self.community_rating / 5.0) if self.community_rating else 0.5
        reach    = min(0.10, self.views_estimate / 10_000)
        return round(ROLE_MAX_POINTS[Role.DOCS] *
                     content * lang_mult * quality + reach * ROLE_MAX_POINTS[Role.DOCS],
                     2)


@dataclass
class DataScientistActivity:
    deliverables_accepted: int   = 0
    avg_quality_score    : float = 0.0   # 0–10, assigned by DAO
    tools_deployed       : int   = 0     # tools actually used by community
    areas                : list  = field(default_factory=list)
    # e.g. ['tokenomics', 'miner_economics', 'network_health', 'dashboards']

    def points(self) -> float:
        if self.deliverables_accepted == 0:
            return 0.0
        deliverables = min(1.0, self.deliverables_accepted / 5)
        quality      = min(1.0, self.avg_quality_score / 10)
        deployed     = min(0.20, self.tools_deployed * 0.10)
        diversity    = min(0.10, len(set(self.areas)) * 0.025)
        return round(ROLE_MAX_POINTS[Role.DATA_SCI] *
                     (deliverables * 0.40 + quality * 0.35 +
                      deployed + diversity), 2)


@dataclass
class SecurityActivity:
    # Severity breakdown of confirmed findings
    critical_bugs : int = 0
    high_bugs     : int = 0
    medium_bugs   : int = 0
    low_bugs      : int = 0
    # Bonus for responsible disclosure quality
    poc_quality   : float = 0.0  # 0–10

    def points(self) -> float:
        # Security is the highest-stakes role — one critical bug = full points
        raw = (
            self.critical_bugs * ROLE_MAX_POINTS[Role.SECURITY] * 1.0 +
            self.high_bugs     * ROLE_MAX_POINTS[Role.SECURITY] * 0.40 +
            self.medium_bugs   * ROLE_MAX_POINTS[Role.SECURITY] * 0.15 +
            self.low_bugs      * ROLE_MAX_POINTS[Role.SECURITY] * 0.05
        )
        quality_mult = 1.0 + (self.poc_quality / 10) * 0.20
        return round(min(ROLE_MAX_POINTS[Role.SECURITY], raw * quality_mult), 2)


@dataclass
class CommunityActivity:
    active_days       : int = 0
    helpful_messages  : int = 0   # upvoted/endorsed by community
    participants_onboarded: int = 0  # brought in new testnet participants
    events_organised  : int = 0
    forum_posts       : int = 0
    translations      : int = 0   # non-docs translations (announcements etc.)

    def points(self) -> float:
        days      = min(0.30, self.active_days * 0.004)
        helpful   = min(0.25, self.helpful_messages * 0.01)
        onboarded = min(0.25, self.participants_onboarded * 0.05)
        events    = min(0.10, self.events_organised * 0.05)
        forum     = min(0.05, self.forum_posts * 0.002)
        trans     = min(0.05, self.translations * 0.02)
        total_frac= days + helpful + onboarded + events + forum + trans
        return round(ROLE_MAX_POINTS[Role.COMMUNITY] * total_frac, 2)


# ── Participant ────────────────────────────────────────────────────────────────

@dataclass
class Participant:
    address          : str
    role             : Role
    name             : str   = ''
    joined_at        : int   = field(default_factory=lambda: int(time.time()))
    mainnet_address  : str   = ''
    country          : str   = ''
    # Role-specific activity (only one populated per participant)
    validator_stats  : Optional[ValidatorActivity]      = None
    miner_stats      : Optional[MinerActivity]          = None
    ai_dev_stats     : Optional[AIDeveloperActivity]    = None
    code_stats       : Optional[CodeContributorActivity]= None
    docs_stats       : Optional[DocumentationActivity]  = None
    data_sci_stats   : Optional[DataScientistActivity]  = None
    security_stats   : Optional[SecurityActivity]       = None
    community_stats  : Optional[CommunityActivity]      = None
    # Multi-role bonus: participants contributing across roles earn extra
    secondary_roles  : list  = field(default_factory=list)
    notes            : str   = ''

    @property
    def raw_points(self) -> float:
        stats_map = {
            Role.VALIDATOR   : self.validator_stats,
            Role.MINER       : self.miner_stats,
            Role.AI_DEV      : self.ai_dev_stats,
            Role.CODE_CONTRIB: self.code_stats,
            Role.DOCS        : self.docs_stats,
            Role.DATA_SCI    : self.data_sci_stats,
            Role.SECURITY    : self.security_stats,
            Role.COMMUNITY   : self.community_stats,
        }
        stats = stats_map.get(self.role)
        return stats.points() if stats else 0.0

    @property
    def multi_role_bonus(self) -> float:
        """5% bonus per additional role with meaningful contribution."""
        return self.raw_points * len(self.secondary_roles) * 0.05

    @property
    def total_points(self) -> float:
        return round(self.raw_points + self.multi_role_bonus, 2)

    @property
    def genesis_oby(self) -> float:
        """Capped at MAX_PER_PARTICIPANT to prevent single-actor dominance."""
        return round(min(MAX_PER_PARTICIPANT, self.total_points * 0.1), 4)

    @property
    def vest_per_month(self) -> float:
        return round(self.genesis_oby / GENESIS_VEST_MONTHS, 8)

    def to_dict(self) -> dict:
        return {
            'address'        : self.address,
            'name'           : self.name or 'Anonymous',
            'role'           : self.role.value,
            'role_label'     : ROLE_LABELS[self.role],
            'country'        : self.country,
            'joined_at'      : self.joined_at,
            'mainnet_address': self.mainnet_address,
            'raw_points'     : self.raw_points,
            'multi_role_bonus': round(self.multi_role_bonus, 2),
            'total_points'   : self.total_points,
            'genesis_oby'    : self.genesis_oby,
            'vest_per_month' : self.vest_per_month,
            'secondary_roles': self.secondary_roles,
        }


# ── Tracker ────────────────────────────────────────────────────────────────────

class CommunityTracker:
    """
    Tracks all pre-mainnet participants across all eight roles.
    Single leaderboard. Published conversion rate before mainnet.
    No founder discretion — all calculations deterministic.
    """

    def __init__(self, db_path: str = './obelyth_data/community.db'):
        self.db_path      = db_path
        self._participants: dict[str, Participant] = {}
        self._lock        = threading.RLock()
        self.started_at   = int(time.time())
        self.stable_since : Optional[int] = None
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS participants (
                    address     TEXT PRIMARY KEY,
                    role        TEXT NOT NULL,
                    name        TEXT NOT NULL DEFAULT '',
                    joined_at   INTEGER NOT NULL,
                    country     TEXT NOT NULL DEFAULT '',
                    mainnet_address TEXT NOT NULL DEFAULT '',
                    activity_json   TEXT NOT NULL DEFAULT '{}',
                    secondary_roles TEXT NOT NULL DEFAULT '[]',
                    notes       TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS activity_log (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    address     TEXT NOT NULL,
                    event       TEXT NOT NULL,
                    detail      TEXT NOT NULL,
                    timestamp   INTEGER NOT NULL
                );
            ''')

    # ── Registration ──────────────────────────────────────────────────────────

    def register(
        self,
        address : str,
        role    : Role,
        name    : str = '',
        country : str = '',
    ) -> Participant:
        stats_defaults = {
            Role.VALIDATOR   : ValidatorActivity,
            Role.MINER       : MinerActivity,
            Role.AI_DEV      : AIDeveloperActivity,
            Role.CODE_CONTRIB: CodeContributorActivity,
            Role.DOCS        : DocumentationActivity,
            Role.DATA_SCI    : DataScientistActivity,
            Role.SECURITY    : SecurityActivity,
            Role.COMMUNITY   : CommunityActivity,
        }
        p = Participant(address=address, role=role, name=name, country=country)
        # Initialise activity stats for the role
        stats_cls = stats_defaults[role]
        if   role == Role.VALIDATOR   : p.validator_stats   = stats_cls()
        elif role == Role.MINER       : p.miner_stats       = stats_cls()
        elif role == Role.AI_DEV      : p.ai_dev_stats      = stats_cls()
        elif role == Role.CODE_CONTRIB: p.code_stats        = stats_cls()
        elif role == Role.DOCS        : p.docs_stats        = stats_cls()
        elif role == Role.DATA_SCI    : p.data_sci_stats    = stats_cls()
        elif role == Role.SECURITY    : p.security_stats    = stats_cls()
        elif role == Role.COMMUNITY   : p.community_stats   = stats_cls()

        with self._lock:
            self._participants[address] = p
        self._save(p)
        self._log(address, 'registered', f'role={role.value} country={country}')
        log.info(f"Testnet: {ROLE_LABELS[role]} registered — {name or address[:16]}")
        return p

    # ── Activity Updates ──────────────────────────────────────────────────────

    def update(self, address: str, **kwargs) -> Optional[Participant]:
        """
        Generic update for any activity stat.
        kwargs map directly to the participant's activity stats fields.

        Examples:
          tracker.update('OBY...', uptime_pct=98.5, blocks_signed=8500)
          tracker.update('OBY...', prs_merged=5, complexity_score=7.2)
          tracker.update('OBY...', verified_jobs=42, gpu_hours=320)
          tracker.update('OBY...', critical_bugs=1, poc_quality=9.0)
          tracker.update('OBY...', pages_written=8, community_rating=4.5)
          tracker.update('OBY...', deliverables_accepted=2, avg_quality_score=8.5)
          tracker.update('OBY...', active_days=45, participants_onboarded=3)
        """
        with self._lock:
            p = self._participants.get(address)
            if not p:
                return None
            # Find the right stats object and update matching fields
            stats_obj = self._get_stats(p)
            if stats_obj:
                for k, v in kwargs.items():
                    if hasattr(stats_obj, k):
                        setattr(stats_obj, k, v)
        self._save(p)
        self._log(address, 'activity_update', json.dumps(kwargs))
        return p

    def add_secondary_role(self, address: str, role: Role):
        """Participant contributing in a second role gets a 5% bonus."""
        with self._lock:
            p = self._participants.get(address)
            if p and role.value not in p.secondary_roles:
                p.secondary_roles.append(role.value)
        self._save(p)

    def set_mainnet_address(self, testnet_addr: str, mainnet_addr: str):
        with self._lock:
            p = self._participants.get(testnet_addr)
            if p:
                p.mainnet_address = mainnet_addr
        self._save(p)

    # ── Leaderboard ───────────────────────────────────────────────────────────

    def leaderboard(self, limit: int = 100, role_filter: Role = None) -> list[dict]:
        with self._lock:
            participants = list(self._participants.values())
        if role_filter:
            participants = [p for p in participants if p.role == role_filter]
        ranked = sorted(participants, key=lambda p: p.total_points, reverse=True)
        return [
            {
                'rank'          : i + 1,
                **p.to_dict(),
            }
            for i, p in enumerate(ranked[:limit])
        ]

    def role_summary(self) -> dict:
        """Points and participant counts broken down by role."""
        with self._lock:
            ps = list(self._participants.values())
        summary = {}
        for role in Role:
            group = [p for p in ps if p.role == role]
            summary[role.value] = {
                'label'         : ROLE_LABELS[role],
                'description'   : ROLE_DESCRIPTIONS[role],
                'participants'  : len(group),
                'total_points'  : round(sum(p.total_points for p in group), 2),
                'total_genesis_oby': round(sum(p.genesis_oby for p in group), 4),
                'max_points'    : ROLE_MAX_POINTS[role],
            }
        return summary

    # ── Graduation ────────────────────────────────────────────────────────────

    def graduation_status(self, stable_days: int = 0) -> dict:
        with self._lock:
            ps = list(self._participants.values())
        validators = [p for p in ps if p.role == Role.VALIDATOR]
        ai_devs    = [p for p in ps if p.role == Role.AI_DEV]
        dev_jobs   = sum(
            (p.ai_dev_stats.verified_jobs if p.ai_dev_stats else 0)
            for p in ai_devs
        )
        countries = {p.country for p in validators if p.country}
        criteria = {
            'stable_days' : {
                'current' : stable_days,
                'required': GRADUATION_STABLE_DAYS,
                'met'     : stable_days >= GRADUATION_STABLE_DAYS,
            },
            'validators'  : {
                'current' : len(validators),
                'required': GRADUATION_VALIDATORS,
                'met'     : len(validators) >= GRADUATION_VALIDATORS,
            },
            'countries'   : {
                'current' : len(countries),
                'required': GRADUATION_COUNTRIES,
                'met'     : len(countries) >= GRADUATION_COUNTRIES,
            },
            'developer_jobs': {
                'current' : dev_jobs,
                'required': GRADUATION_DEV_JOBS,
                'met'     : dev_jobs >= GRADUATION_DEV_JOBS,
            },
        }
        met   = sum(1 for c in criteria.values() if c['met'])
        total = len(criteria)
        return {
            'ready_for_mainnet': met == total,
            'criteria_met'     : met,
            'criteria_total'   : total,
            'summary'          : f'{met}/{total} criteria met',
            'criteria'         : criteria,
        }

    # ── Genesis Export ────────────────────────────────────────────────────────

    def export_genesis_allocations(self, output_path: str = None) -> list[dict]:
        """
        Final genesis allocations for mainnet block zero.
        Published 30 days before mainnet so participants can verify.
        """
        with self._lock:
            ps = list(self._participants.values())

        total_points = sum(p.total_points for p in ps)
        total_pool   = COMMUNITY_POOL_OBY

        # Dynamic conversion: pool / total points, capped per participant
        if total_points <= 0:
            return []

        allocs = []
        for p in sorted(ps, key=lambda x: x.total_points, reverse=True):
            if p.total_points <= 0:
                continue
            # Pro-rata share of pool, capped at MAX_PER_PARTICIPANT
            pro_rata = (p.total_points / total_points) * total_pool
            oby      = round(min(MAX_PER_PARTICIPANT, pro_rata), 4)
            allocs.append({
                'address'      : p.mainnet_address or p.address,
                'name'         : p.name or 'Anonymous',
                'role'         : p.role.value,
                'role_label'   : ROLE_LABELS[p.role],
                'points'       : p.total_points,
                'oby_amount'   : oby,
                'vest_months'  : GENESIS_VEST_MONTHS,
                'vest_per_month': round(oby / GENESIS_VEST_MONTHS, 8),
            })

        if output_path:
            Path(output_path).write_text(json.dumps(allocs, indent=2))
            log.info(
                f"Genesis allocations exported: {len(allocs)} participants | "
                f"{sum(a['oby_amount'] for a in allocs):,.2f} OBY total"
            )
        return allocs

    def pool_status(self) -> dict:
        with self._lock:
            ps = list(self._participants.values())
        total_pts = sum(p.total_points for p in ps)
        total_oby = sum(p.genesis_oby for p in ps)
        return {
            'pool_oby'          : COMMUNITY_POOL_OBY,
            'participants'      : len(ps),
            'total_points'      : round(total_pts, 2),
            'estimated_oby_out' : round(total_oby, 4),
            'pool_remaining'    : round(COMMUNITY_POOL_OBY - total_oby, 4),
            'max_per_participant': MAX_PER_PARTICIPANT,
            'vest_months'       : GENESIS_VEST_MONTHS,
            'graduation'        : self.graduation_status(),
            'by_role'           : self.role_summary(),
        }

    # ── Persistence ───────────────────────────────────────────────────────────

    def _get_stats(self, p: Participant):
        return {
            Role.VALIDATOR   : p.validator_stats,
            Role.MINER       : p.miner_stats,
            Role.AI_DEV      : p.ai_dev_stats,
            Role.CODE_CONTRIB: p.code_stats,
            Role.DOCS        : p.docs_stats,
            Role.DATA_SCI    : p.data_sci_stats,
            Role.SECURITY    : p.security_stats,
            Role.COMMUNITY   : p.community_stats,
        }.get(p.role)

    def _save(self, p: Participant):
        if not p:
            return
        stats = self._get_stats(p)
        activity_json = json.dumps(asdict(stats)) if stats else '{}'
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                INSERT OR REPLACE INTO participants
                (address, role, name, joined_at, country, mainnet_address,
                 activity_json, secondary_roles, notes)
                VALUES (?,?,?,?,?,?,?,?,?)
            ''', (
                p.address, p.role.value, p.name, p.joined_at,
                p.country, p.mainnet_address, activity_json,
                json.dumps(p.secondary_roles), p.notes,
            ))

    def _log(self, address: str, event: str, detail: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                'INSERT INTO activity_log (address, event, detail, timestamp)'
                ' VALUES (?,?,?,?)',
                (address, event, detail, int(time.time()))
            )

    def load_from_db(self):
        """Reload participant state from database (e.g. after node restart)."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('SELECT * FROM participants').fetchall()
        loaded = 0
        for row in rows:
            try:
                role  = Role(row['role'])
                p     = Participant(
                    address         = row['address'],
                    role            = role,
                    name            = row['name'],
                    joined_at       = row['joined_at'],
                    country         = row['country'],
                    mainnet_address = row['mainnet_address'],
                    secondary_roles = json.loads(row['secondary_roles'] or '[]'),
                    notes           = row['notes'],
                )
                activity = json.loads(row['activity_json'] or '{}')
                stats_cls = {
                    Role.VALIDATOR   : ValidatorActivity,
                    Role.MINER       : MinerActivity,
                    Role.AI_DEV      : AIDeveloperActivity,
                    Role.CODE_CONTRIB: CodeContributorActivity,
                    Role.DOCS        : DocumentationActivity,
                    Role.DATA_SCI    : DataScientistActivity,
                    Role.SECURITY    : SecurityActivity,
                    Role.COMMUNITY   : CommunityActivity,
                }[role]
                stats = stats_cls(**{
                    k: v for k, v in activity.items()
                    if k in stats_cls.__dataclass_fields__
                })
                attr = {
                    Role.VALIDATOR   : 'validator_stats',
                    Role.MINER       : 'miner_stats',
                    Role.AI_DEV      : 'ai_dev_stats',
                    Role.CODE_CONTRIB: 'code_stats',
                    Role.DOCS        : 'docs_stats',
                    Role.DATA_SCI    : 'data_sci_stats',
                    Role.SECURITY    : 'security_stats',
                    Role.COMMUNITY   : 'community_stats',
                }[role]
                setattr(p, attr, stats)
                self._participants[p.address] = p
                loaded += 1
            except Exception as e:
                log.warning(f"Could not load participant {row['address']}: {e}")
        log.info(f"Loaded {loaded} participants from database")
        return loaded
