"""Unit tests for receive-only PEX (BEP 11): the handshake and parse_pex."""

import socket
import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugin" / "dm"))

from dmcore.torrent import bencode, metadata  # noqa: E402


def compact(*addrs):
    return b"".join(socket.inet_aton(h) + struct.pack(">H", p) for h, p in addrs)


def pex(**fields):
    return bencode.encode(fields)


class Handshake(unittest.TestCase):
    def test_lists_ut_pex_unless_private(self):
        on = bencode.decode(metadata.ext_handshake())[0][b"m"]
        self.assertEqual(on, {b"ut_metadata": metadata.OUR_UT_METADATA, b"ut_pex": metadata.OUR_UT_PEX})
        off = bencode.decode(metadata.ext_handshake(pex=False))[0][b"m"]
        self.assertEqual(off, {b"ut_metadata": metadata.OUR_UT_METADATA})

    def test_parse_reads_their_ut_pex_id(self):
        body = bencode.encode({"m": {"ut_metadata": 7, "ut_pex": 1}})
        self.assertEqual(metadata.parse_ext_handshake(body).ut_pex, 1)
        for bad in (0, 300, b"a"):
            with self.subTest(bad=bad):
                body = bencode.encode({"m": {"ut_pex": bad}})
                self.assertIsNone(metadata.parse_ext_handshake(body).ut_pex)


class ParsePex(unittest.TestCase):
    def test_added_entries_are_returned(self):
        addrs = [("8.8.8.8", 6881), ("1.2.3.4", 51413)]
        self.assertEqual(metadata.parse_pex(pex(added=compact(*addrs))), addrs)

    def test_only_added_is_read(self):
        body = pex(added=compact(("8.8.8.8", 1)), dropped=compact(("1.1.1.1", 2)),
                   added6=b"x" * 18)
        self.assertEqual(metadata.parse_pex(body), [("8.8.8.8", 1)])

    def test_no_added_is_an_empty_list(self):
        self.assertEqual(metadata.parse_pex(pex(dropped=b"")), [])

    def test_at_most_50_entries(self):
        many = [(f"8.8.{i // 250}.{i % 250 + 1}", 6881) for i in range(1000)]
        self.assertEqual(len(metadata.parse_pex(pex(added=compact(*many)))), 50)

    def test_unusable_and_trailing_bytes_are_skipped(self):
        body = pex(added=compact(("127.0.0.1", 80), ("10.0.0.1", 80), ("8.8.8.8", 0), ("8.8.4.4", 53))
                   + b"\x01\x02")
        self.assertEqual(metadata.parse_pex(body), [("8.8.4.4", 53)])

    def test_loopback_and_private_only_when_allowed(self):
        body = pex(added=compact(("127.0.0.1", 80), ("10.0.0.1", 80)))
        self.assertEqual(metadata.parse_pex(body, allow_loopback=True, allow_private=True),
                         [("127.0.0.1", 80), ("10.0.0.1", 80)])

    def test_malformed_raises_value_error(self):
        deep = b"l" * 100 + b"e" * 100
        for body in (b"", b"xx", b"i1e", b"le", pex(added=3), pex(added=[b"a"]), deep):
            with self.subTest(body=body[:12]):
                with self.assertRaises(ValueError):
                    metadata.parse_pex(body)


if __name__ == "__main__":
    unittest.main()
