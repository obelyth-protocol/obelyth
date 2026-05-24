"""
Basic blockchain tests — runs with unittest (no pytest required).
Run: python -m pytest tests/  OR  python -m unittest discover tests/
"""
import sys, unittest
sys.path.insert(0, '.')

from core.crypto import generate_keypair
from core.blockchain import (
    Blockchain, ConsensusType,
    FOUNDER_TOTAL, COMMUNITY_TOTAL, DAO_DISCRETIONARY_TOTAL,
    TOTAL_SUPPLY, PRE_MINE_TOTAL, MINED_SUPPLY_PCT
)


class TestGenesisAllocation(unittest.TestCase):

    def setUp(self):
        _, _, self.addr = generate_keypair()
        self.chain = Blockchain(founder_address=self.addr)

    def test_3_3_2_92_split(self):
        alloc = self.chain.state_summary()['genesis_allocation']
        self.assertEqual(alloc['founder_oby'],            630_000.0)
        self.assertEqual(alloc['community_pool_oby'],     630_000.0)
        self.assertEqual(alloc['dao_discretionary_oby'],  420_000.0)
        self.assertEqual(alloc['mined_pct'],               '92%')
        self.assertEqual(alloc['vc_allocation'],           'None — ever.')

    def test_hard_cap_21m(self):
        total = (FOUNDER_TOTAL + COMMUNITY_TOTAL +
                 DAO_DISCRETIONARY_TOTAL + (TOTAL_SUPPLY - PRE_MINE_TOTAL))
        self.assertAlmostEqual(total, 21_000_000.0, places=0)

    def test_founder_utxo(self):
        self.assertEqual(self.chain.utxos.balance(self.addr), FOUNDER_TOTAL)

    def test_community_pool_utxo(self):
        self.assertEqual(self.chain.utxos.balance('OBY_COMMUNITY_POOL'),
                         COMMUNITY_TOTAL)

    def test_block_mining(self):
        block = self.chain.mine_block(self.addr, consensus=ConsensusType.POW)
        self.assertIsNotNone(block)
        self.assertEqual(block.height, 1)

    def test_dao_mining_tax(self):
        block    = self.chain.mine_block(self.addr, consensus=ConsensusType.POW)
        coinbase = block.transactions[0]
        dao_out  = [o for o in coinbase.outputs if o.address == self.chain.dao_address]
        self.assertEqual(len(dao_out), 1)
        total = sum(o.amount for o in coinbase.outputs)
        self.assertAlmostEqual(dao_out[0].amount / total, 0.05, places=2)

    def test_supply_constants(self):
        self.assertEqual(TOTAL_SUPPLY, 21_000_000.0)
        self.assertEqual(PRE_MINE_TOTAL, 1_680_000.0)
        self.assertAlmostEqual(MINED_SUPPLY_PCT, 0.92, places=9)


if __name__ == '__main__':
    unittest.main()
