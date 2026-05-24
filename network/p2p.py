"""
Obelyth P2P Network Layer
==============================
- TCP socket server (one thread per peer)
- Message protocol: length-prefixed JSON frames
- Messages: HELLO, GETBLOCKS, BLOCK, TX, PING, PONG, PEERS
- Peer discovery via seed nodes + peer exchange
- Block & transaction propagation (gossip)
- Chain sync on connect
"""

import json
import socket
import threading
import logging
import time
import struct
from typing import Callable, Optional

log = logging.getLogger('obelyth.network')

# ── Protocol Constants ────────────────────────────────────────────────────────
PROTOCOL_VERSION = 1
MAGIC            = b'OBY1'          # 4-byte magic prefix for all frames
MAX_MESSAGE_SIZE = 10 * 1024 * 1024 # 10 MB
PING_INTERVAL    = 30               # seconds
PEER_TIMEOUT     = 90               # disconnect after this many seconds of silence
MAX_PEERS        = 50


# ── Message Types ─────────────────────────────────────────────────────────────

class MsgType:
    HELLO     = 'HELLO'
    PING      = 'PING'
    PONG      = 'PONG'
    GETBLOCKS = 'GETBLOCKS'
    BLOCKS    = 'BLOCKS'
    BLOCK     = 'BLOCK'
    TX        = 'TX'
    PEERS     = 'PEERS'
    REJECT    = 'REJECT'
    INV       = 'INV'        # inventory: announce known hashes


def make_msg(msg_type: str, payload: dict = None) -> bytes:
    """Encode a message as: MAGIC(4) + length(4) + JSON body."""
    body = json.dumps({'type': msg_type, 'payload': payload or {}},
                      separators=(',', ':')).encode()
    return MAGIC + struct.pack('>I', len(body)) + body


def parse_msg(data: bytes) -> Optional[dict]:
    try:
        return json.loads(data.decode())
    except Exception:
        return None


# ── Peer Connection ────────────────────────────────────────────────────────────

class Peer:
    def __init__(self, sock: socket.socket, addr: tuple, outbound: bool = False):
        self.sock      = sock
        self.addr      = addr          # (host, port)
        self.outbound  = outbound
        self.version   = None
        self.height    = 0
        self.last_seen = time.time()
        self.known_inv : set[str] = set()   # hashes this peer told us about
        self._send_lock = threading.Lock()

    def send(self, msg_type: str, payload: dict = None):
        try:
            frame = make_msg(msg_type, payload)
            with self._send_lock:
                self.sock.sendall(frame)
        except Exception as e:
            log.debug(f"Send error to {self.addr}: {e}")

    def recv_frame(self) -> Optional[dict]:
        """Read one length-prefixed frame from the socket."""
        try:
            # Read magic
            magic = self._recv_exact(4)
            if magic != MAGIC:
                return None
            # Read length
            length_bytes = self._recv_exact(4)
            length = struct.unpack('>I', length_bytes)[0]
            if length > MAX_MESSAGE_SIZE:
                log.warning(f"Oversized message from {self.addr}: {length}")
                return None
            body = self._recv_exact(length)
            self.last_seen = time.time()
            return parse_msg(body)
        except Exception:
            return None

    def _recv_exact(self, n: int) -> bytes:
        data = b''
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError("Peer disconnected")
            data += chunk
        return data

    def is_alive(self) -> bool:
        return time.time() - self.last_seen < PEER_TIMEOUT

    def __repr__(self):
        return f"Peer({self.addr[0]}:{self.addr[1]} h={self.height})"


# ── Network Node ───────────────────────────────────────────────────────────────

class NetworkNode:
    """
    Manages peer connections, message routing, block/tx propagation.
    Callbacks connect it to the Blockchain engine.
    """

    def __init__(
        self,
        host         : str  = '0.0.0.0',
        port         : int  = 8333,
        seed_nodes   : list = None,
        node_version : str  = '0.1.0',
    ):
        self.host         = host
        self.port         = port
        self.seed_nodes   = seed_nodes or []
        self.node_version = node_version

        self._peers       : list[Peer] = []
        self._peers_lock  = threading.RLock()
        self._known_inv   : set[str] = set()    # all hashes we've seen

        self._server_sock : Optional[socket.socket] = None
        self._running     = False

        # Callbacks — set by the full node
        self.on_block_received : Optional[Callable] = None
        self.on_tx_received    : Optional[Callable] = None
        self.get_block_dict    : Optional[Callable] = None  # hash -> dict
        self.get_chain_height  : Optional[Callable] = None
        self.get_block_hashes  : Optional[Callable] = None  # since height -> [hashes]
        self.get_peer_addrs    : Optional[Callable] = None

    # ── Server ────────────────────────────────────────────────────────────────

    def start(self):
        self._running = True
        threading.Thread(target=self._server_loop, daemon=True, name='p2p-server').start()
        threading.Thread(target=self._peer_maintenance, daemon=True, name='p2p-maint').start()
        log.info(f"P2P listening on {self.host}:{self.port}")

        # Connect to seed nodes
        for host, port in self.seed_nodes:
            self.connect(host, port)

    def stop(self):
        self._running = False
        if self._server_sock:
            try:
                self._server_sock.close()
            except Exception:
                pass

    def _server_loop(self):
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._server_sock.bind((self.host, self.port))
            self._server_sock.listen(MAX_PEERS)
            while self._running:
                try:
                    self._server_sock.settimeout(1.0)
                    sock, addr = self._server_sock.accept()
                    peer = Peer(sock, addr, outbound=False)
                    self._add_peer(peer)
                    threading.Thread(
                        target=self._peer_handler, args=(peer,),
                        daemon=True, name=f'peer-{addr[0]}'
                    ).start()
                except socket.timeout:
                    continue
                except Exception as e:
                    if self._running:
                        log.error(f"Accept error: {e}")
        except Exception as e:
            log.error(f"Server error: {e}")

    # ── Outbound Connect ──────────────────────────────────────────────────────

    def connect(self, host: str, port: int):
        try:
            sock = socket.create_connection((host, port), timeout=10)
            peer = Peer(sock, (host, port), outbound=True)
            self._add_peer(peer)
            threading.Thread(
                target=self._peer_handler, args=(peer,),
                daemon=True, name=f'peer-out-{host}'
            ).start()
            log.info(f"Connected to {host}:{port}")
        except Exception as e:
            log.debug(f"Could not connect to {host}:{port}: {e}")

    def _add_peer(self, peer: Peer):
        with self._peers_lock:
            if len(self._peers) >= MAX_PEERS:
                peer.sock.close()
                return
            self._peers.append(peer)
        # Send HELLO
        peer.send(MsgType.HELLO, {
            'version' : self.node_version,
            'protocol': PROTOCOL_VERSION,
            'height'  : self.get_chain_height() if self.get_chain_height else 0,
            'port'    : self.port,
        })

    def _remove_peer(self, peer: Peer):
        with self._peers_lock:
            if peer in self._peers:
                self._peers.remove(peer)
        try:
            peer.sock.close()
        except Exception:
            pass
        log.info(f"Peer disconnected: {peer.addr}")

    # ── Message Handling ──────────────────────────────────────────────────────

    def _peer_handler(self, peer: Peer):
        while self._running:
            msg = peer.recv_frame()
            if msg is None:
                break
            self._handle_message(peer, msg)
        self._remove_peer(peer)

    def _handle_message(self, peer: Peer, msg: dict):
        mtype   = msg.get('type')
        payload = msg.get('payload', {})

        if mtype == MsgType.HELLO:
            peer.version = payload.get('version')
            peer.height  = payload.get('height', 0)
            log.info(f"HELLO from {peer.addr} v={peer.version} h={peer.height}")
            # If they're ahead, request blocks
            my_height = self.get_chain_height() if self.get_chain_height else 0
            if peer.height > my_height:
                peer.send(MsgType.GETBLOCKS, {'from_height': my_height})
            # Share peers
            peer.send(MsgType.PEERS, {'peers': self._peer_list()})

        elif mtype == MsgType.PING:
            peer.send(MsgType.PONG, {'nonce': payload.get('nonce')})

        elif mtype == MsgType.PONG:
            pass  # last_seen updated in recv_frame

        elif mtype == MsgType.GETBLOCKS:
            from_height = payload.get('from_height', 0)
            if self.get_block_hashes:
                hashes = self.get_block_hashes(from_height)
                # Send up to 500 block hashes as inventory
                peer.send(MsgType.INV, {'hashes': hashes[:500]})

        elif mtype == MsgType.INV:
            hashes = payload.get('hashes', [])
            # Request blocks we don't have
            wanted = [h for h in hashes if h not in self._known_inv]
            for h in wanted[:50]:   # cap per message
                peer.send(MsgType.GETBLOCKS, {'hash': h})

        elif mtype == MsgType.BLOCK:
            block_dict = payload.get('block')
            if block_dict and self.on_block_received:
                bh = block_dict.get('header', {}).get('hash', '')
                if bh not in self._known_inv:
                    self._known_inv.add(bh)
                    peer.known_inv.add(bh)
                    accepted = self.on_block_received(block_dict)
                    if accepted:
                        # Relay to other peers
                        self._relay(MsgType.BLOCK, {'block': block_dict}, exclude=peer)

        elif mtype == MsgType.TX:
            tx_dict = payload.get('tx')
            if tx_dict and self.on_tx_received:
                th = tx_dict.get('hash', '')
                if th not in self._known_inv:
                    self._known_inv.add(th)
                    accepted = self.on_tx_received(tx_dict)
                    if accepted:
                        self._relay(MsgType.TX, {'tx': tx_dict}, exclude=peer)

        elif mtype == MsgType.PEERS:
            peers = payload.get('peers', [])
            for p in peers[:20]:
                host, port = p.get('host'), p.get('port')
                if host and port:
                    # Don't connect to ourselves or already-connected
                    if not self._already_connected(host, port):
                        self.connect(host, port)

        elif mtype == MsgType.REJECT:
            reason = payload.get('reason', 'unknown')
            log.warning(f"Peer {peer.addr} rejected: {reason}")

    # ── Broadcast ────────────────────────────────────────────────────────────

    def broadcast_block(self, block_dict: dict):
        bh = block_dict.get('header', {}).get('hash', '')
        self._known_inv.add(bh)
        self._relay(MsgType.BLOCK, {'block': block_dict})

    def broadcast_tx(self, tx_dict: dict):
        th = tx_dict.get('hash', '')
        self._known_inv.add(th)
        self._relay(MsgType.TX, {'tx': tx_dict})

    def _relay(self, msg_type: str, payload: dict, exclude: Peer = None):
        with self._peers_lock:
            targets = [p for p in self._peers if p is not exclude]
        for peer in targets:
            peer.send(msg_type, payload)

    # ── Maintenance ───────────────────────────────────────────────────────────

    def _peer_maintenance(self):
        """Periodically ping peers and cull dead connections."""
        while self._running:
            time.sleep(PING_INTERVAL)
            with self._peers_lock:
                peers = list(self._peers)
            for peer in peers:
                if not peer.is_alive():
                    self._remove_peer(peer)
                else:
                    peer.send(MsgType.PING, {'nonce': int(time.time())})

    def _peer_list(self) -> list[dict]:
        with self._peers_lock:
            return [{'host': p.addr[0], 'port': p.port}
                    for p in self._peers if hasattr(p, 'port')]

    def _already_connected(self, host: str, port: int) -> bool:
        with self._peers_lock:
            return any(p.addr == (host, port) for p in self._peers)

    # ── Status ────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        with self._peers_lock:
            return {
                'peers'    : len(self._peers),
                'inbound'  : sum(1 for p in self._peers if not p.outbound),
                'outbound' : sum(1 for p in self._peers if p.outbound),
                'known_inv': len(self._known_inv),
                'listening': f"{self.host}:{self.port}",
            }
