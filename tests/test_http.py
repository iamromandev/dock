"""Host-relayed HTTP: allowlist rules, address policy, the request engine, and the wiring through dock."""

import base64
import faulthandler
import hashlib
import json
import os
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cli import http_relay  # noqa: E402
from cli.http_relay import Cancelled, HttpRule, Limits, Refused, Relay, check_url, match, parse_rules  # noqa: E402
from cli.manifest import Manifest, ManifestError  # noqa: E402
from cli.process import CANCELLED, OUTBOX_BULK, OUTBOX_CONTROL, PluginError, PluginProcess, SendAborted  # noqa: E402
from cli.lifecycle import Session  # noqa: E402
from cli.store import Store  # noqa: E402
from cli.ui import TerminalUI  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


def rule(entry) -> HttpRule:
    return parse_rules([entry])[0]


class ParseRules(unittest.TestCase):
    """AC1 / R1: every valid entry form parses; every invalid one fails naming the entry."""

    def test_a_plain_host_is_https_only_on_the_default_port_and_not_private(self):
        self.assertEqual(rule("api.example.com"), HttpRule("api.example.com", None, False, False, "api.example.com"))

    def test_the_host_is_folded_to_lower_case_and_loses_its_trailing_dot(self):
        r = rule("API.Example.COM.")
        self.assertEqual((r.host, r.raw), ("api.example.com", "API.Example.COM."))

    def test_an_idn_is_stored_as_punycode(self):
        self.assertEqual(rule("bücher.example").host, "xn--bcher-kva.example")

    def test_a_wildcard_keeps_its_prefix(self):
        self.assertEqual(rule("*.Example.com").host, "*.example.com")

    def test_the_http_prefix_allows_plain_http(self):
        r = rule("http://example.com")
        self.assertEqual((r.host, r.allow_http, r.port), ("example.com", True, None))

    def test_a_port_is_kept(self):
        self.assertEqual(rule("example.com:8443").port, 8443)
        self.assertEqual(rule("*.example.com:8443").port, 8443)

    def test_localhost_and_ip_literals_are_private(self):
        for entry in ("localhost", "LocalHost", "127.0.0.1", "10.0.0.5:8080", "[::1]", "[::1]:8080", "[fe80::1]"):
            with self.subTest(entry=entry):
                self.assertTrue(rule(entry).private)

    def test_other_hosts_are_not_private(self):
        for entry in ("example.com", "*.example.com", "intranet"):
            with self.subTest(entry=entry):
                self.assertFalse(rule(entry).private)

    def test_ipv6_literals_are_stored_without_brackets_in_canonical_form(self):
        self.assertEqual(rule("[0:0:0:0:0:0:0:1]:9").host, "::1")
        self.assertEqual(rule("[0:0:0:0:0:0:0:1]:9").port, 9)

    def test_a_table_entry_can_set_private(self):
        r = rule({"host": "http://intranet.example:81", "private": True})
        self.assertEqual((r.host, r.port, r.allow_http, r.private, r.raw), ("intranet.example", 81, True, True, "http://intranet.example:81"))

    def test_a_table_entry_without_private_is_like_a_string(self):
        self.assertEqual(rule({"host": "a.com"}), rule("a.com"))
        self.assertFalse(rule({"host": "a.com", "private": False}).private)

    def test_an_empty_list_is_no_rules(self):
        self.assertEqual(parse_rules([]), [])

    def test_the_order_is_kept(self):
        self.assertEqual([r.host for r in parse_rules(["b.com", "a.com"])], ["b.com", "a.com"])

    def test_invalid_entries_fail_naming_the_entry(self):
        bad = [
            "https://a.com/path", "ftp://a.com", "*", "*.com", "", "   ", "a.com/path", "a.com?x=1", "user@a.com",
            "https://a.com", "a b.com", "a.com:0", "a.com:99999", "a.com:", "a.com:x", "::1", "[::1", "*a.com",
            "a.*.com", "*.", "*.*.com", "1.2.3", "a..com", "http://", "*:80", "*.127.0.0.1", "[::1]x", "[::1]:",
            "fe80::1%eth0", "[fe80::1%eth0]", "a.com#frag", "a\\b.com", "a_b$.com", "-a.com",
            "x" * 64 + ".com", "999.1.1.1",
        ]
        for entry in bad:
            with self.subTest(entry=entry):
                with self.assertRaises(ValueError) as cm:
                    parse_rules([entry])
                self.assertTrue(str(cm.exception).startswith(f"{entry}: "), str(cm.exception))

    def test_a_bad_table_entry_is_named_and_its_reason_given(self):
        for entry, text in [
            ({"host": "a.com", "privat": True}, "unknown key 'privat'"),
            ({"host": "a.com", "private": "yes"}, "'private' must be true or false"),
            ({"host": "a.com", "private": 1}, "'private' must be true or false"),
            ({}, "needs a 'host'"),
            ({"host": 3}, "'host' must be a string"),
            ({"host": "*.com"}, "wildcard"),
        ]:
            with self.subTest(entry=entry):
                with self.assertRaisesRegex(ValueError, text):
                    parse_rules([entry])
        with self.assertRaisesRegex(ValueError, r"^a\.com: unknown key 'privat'"):
            parse_rules([{"host": "a.com", "privat": True}])

    def test_entries_that_are_not_text_or_tables_are_refused(self):
        for entry in (5, None, True, ["a.com"]):
            with self.subTest(entry=entry):
                with self.assertRaisesRegex(ValueError, "must be a host name"):
                    parse_rules([entry])

    def test_what_is_not_a_list_is_refused(self):
        for value in ("a.com", {"host": "a.com"}, 3):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "must be a list"):
                    parse_rules(value)


class Match(unittest.TestCase):
    def test_an_exact_host_matches_on_the_default_port_of_the_scheme(self):
        rules = parse_rules(["http://a.com", "b.com"])
        self.assertEqual(match(rules, "http", "a.com", 80), rules[0])
        self.assertEqual(match(rules, "https", "a.com", 443), rules[0])
        self.assertEqual(match(rules, "https", "b.com", 443), rules[1])

    def test_a_port_that_is_not_the_default_needs_an_explicit_entry(self):
        self.assertIsNone(match(parse_rules(["a.com"]), "https", "a.com", 8443))
        self.assertIsNotNone(match(parse_rules(["a.com:8443"]), "https", "a.com", 8443))

    def test_an_explicit_port_excludes_the_default_port(self):
        self.assertIsNone(match(parse_rules(["a.com:8443"]), "https", "a.com", 443))

    def test_plain_http_needs_the_http_prefix(self):
        self.assertIsNone(match(parse_rules(["a.com"]), "http", "a.com", 80))
        self.assertIsNone(match(parse_rules(["a.com:80"]), "http", "a.com", 80))

    def test_a_wildcard_matches_subdomains_at_any_depth_but_not_the_apex(self):
        rules = parse_rules(["*.example.com"])
        self.assertIsNotNone(match(rules, "https", "a.example.com", 443))
        self.assertIsNotNone(match(rules, "https", "a.b.example.com", 443))
        self.assertIsNone(match(rules, "https", "example.com", 443))
        self.assertIsNone(match(rules, "https", "badexample.com", 443))
        self.assertIsNone(match(rules, "https", "example.com.evil.com", 443))

    def test_a_wildcard_never_matches_an_ip_literal(self):
        self.assertIsNone(match(parse_rules(["*.example.com"]), "https", "1.2.3.4", 443))

    def test_ip_literals_match_by_value(self):
        rules = parse_rules(["http://[::1]:8080"])
        self.assertIsNotNone(match(rules, "http", "::1", 8080))
        self.assertIsNotNone(match(rules, "http", "0:0:0:0:0:0:0:1", 8080))
        self.assertIsNone(match(rules, "http", "::ffff:127.0.0.1", 8080))

    def test_case_trailing_dot_and_idn_are_normalised_before_comparing(self):
        rules = parse_rules(["bücher.example"])
        self.assertIsNotNone(match(rules, "https", "BÜCHER.example.", 443))
        self.assertIsNotNone(match(rules, "https", "xn--bcher-kva.example", 443))

    def test_the_most_specific_rule_wins_whatever_the_order(self):
        wild, deep, exact = "*.example.com", "*.lan.example.com", {"host": "x.lan.example.com", "private": True}
        for entries in ([wild, deep, exact], [exact, deep, wild], [deep, wild, exact]):
            rules = parse_rules(entries)
            with self.subTest(entries=entries):
                self.assertEqual(match(rules, "https", "x.lan.example.com", 443).raw, "x.lan.example.com")
                self.assertEqual(match(rules, "https", "y.lan.example.com", 443).raw, "*.lan.example.com")
                self.assertEqual(match(rules, "https", "z.example.com", 443).raw, "*.example.com")

    def test_a_wildcard_listed_first_does_not_hide_a_private_exact_entry(self):
        rules = parse_rules(["*.example.com", {"host": "lan.example.com", "private": True}])
        self.assertTrue(match(rules, "https", "lan.example.com", 443).private)
        self.assertFalse(match(rules, "https", "other.example.com", 443).private)

    def test_an_explicit_port_beats_the_default_port_entry_for_the_same_host_and_port(self):
        rules = parse_rules(["a.com", {"host": "a.com:443", "private": True}])
        self.assertEqual(match(rules, "https", "a.com", 443).raw, "a.com:443")

    def test_among_equally_specific_rules_the_first_wins(self):
        rules = parse_rules(["a.com", {"host": "a.com", "private": True}])
        self.assertFalse(match(rules, "https", "a.com", 443).private)

    def test_a_more_specific_rule_that_does_not_fit_does_not_hide_a_wildcard(self):
        rules = parse_rules(["*.example.com", "a.example.com:8443"])
        self.assertEqual(match(rules, "https", "a.example.com", 443).raw, "*.example.com")

    def test_no_rules_match_nothing(self):
        self.assertIsNone(match([], "https", "a.com", 443))

    def test_a_host_that_cannot_be_a_name_matches_nothing(self):
        self.assertIsNone(match(parse_rules(["a.com"]), "https", "a..com", 443))


class CheckUrl(unittest.TestCase):
    """R3: what is refused before anything is resolved or sent."""

    def refused(self, rules, url, text):
        with self.assertRaises(Refused) as cm:
            check_url(parse_rules(rules), url)
        self.assertIn(text, str(cm.exception))

    def test_a_declared_https_url_is_split_into_its_parts(self):
        rules = parse_rules(["api.example.com"])
        got = check_url(rules, "https://API.example.com/v1/now?city=Oslo#frag")
        self.assertEqual(got, (rules[0], "https", "api.example.com", 443, "/v1/now?city=Oslo"))

    def test_no_path_means_a_slash(self):
        self.assertEqual(check_url(parse_rules(["a.com"]), "https://a.com")[4], "/")
        self.assertEqual(check_url(parse_rules(["a.com"]), "https://a.com?x=1")[4], "/?x=1")

    def test_plain_http_to_an_http_entry_with_an_explicit_port(self):
        rules = parse_rules(["http://localhost:8080"])
        self.assertEqual(check_url(rules, "http://localhost:8080/x"), (rules[0], "http", "localhost", 8080, "/x"))

    def test_ipv6_literals_in_brackets(self):
        rules = parse_rules(["http://[::1]:8080"])
        self.assertEqual(check_url(rules, "http://[::1]:8080/")[2:4], ("::1", 8080))

    def test_the_spec_example_message_for_an_undeclared_host(self):
        self.refused(["api.weather.example"], "https://evil.example/",
                     "host 'evil.example' is not declared in [capabilities].http")

    def test_a_port_that_is_not_declared_is_refused_naming_the_port(self):
        self.refused(["a.com"], "https://a.com:8443/", "port 8443")

    def test_plain_http_to_an_https_only_entry_is_refused(self):
        self.refused(["a.com"], "http://a.com/", "plain http")

    def test_userinfo_is_refused(self):
        for url in ("https://user:pw@a.com/", "https://user@a.com/", "https://a.com@evil.com/", "https://evil.com\\@a.com/"):
            with self.subTest(url=url):
                self.refused(["a.com", "evil.com"], url, "userinfo")

    def test_other_schemes_are_refused(self):
        for url in ("ftp://a.com/", "file:///etc/passwd", "gopher://a.com", "//a.com/x", "a.com/x", "javascript:1"):
            with self.subTest(url=url):
                self.refused(["a.com"], url, "scheme")

    def test_a_url_without_a_host_is_refused(self):
        self.refused(["a.com"], "https:///x", "no host")

    def test_control_characters_and_spaces_are_refused(self):
        for url in ("https://a.com/x y", "https://a.com/x\r\nHost: evil", "https://a.com/\x00", "https://a.com/\x7f"):
            with self.subTest(url=repr(url)):
                self.refused(["a.com"], url, "control characters")

    def test_a_url_that_is_not_text_is_refused(self):
        for url in (None, 5, b"https://a.com/"):
            with self.subTest(url=url):
                self.refused(["a.com"], url, "url must be a string")

    def test_a_bad_port_is_refused(self):
        for url in ("https://a.com:0/", "https://a.com:99999/", "https://a.com:x/"):
            with self.subTest(url=url):
                self.refused(["a.com"], url, "port")

    def test_a_wildcard_does_not_cover_the_apex(self):
        self.refused(["*.example.com"], "https://example.com/", "not declared")

    def test_a_non_ascii_path_is_percent_encoded(self):
        self.assertEqual(check_url(parse_rules(["a.com"]), "https://a.com/ä?q=ö")[4], "/%C3%A4?q=%C3%B6")

    def test_an_idn_host_is_matched_in_punycode_form(self):
        self.assertEqual(check_url(parse_rules(["bücher.example"]), "https://bücher.example/")[2], "xn--bcher-kva.example")

    def test_a_host_with_percent_or_backslash_never_matches(self):
        for url in ("https://a.com%2eevil.com/", "https://a.com\\.evil.com/", "https://[fe80::1%25eth0]/"):
            with self.subTest(url=url):
                with self.assertRaises(Refused):
                    check_url(parse_rules(["a.com", "*.evil.com", "[fe80::1]"]), url)


PLUGIN = """\
id = "dev.dock.sample"
version = "1.0.0"
api_version = "1"

[run]
any = {{ cmd = "python3" }}

[[commands]]
name = "go"

[capabilities]
{caps}
"""


def make_plugin(caps: str) -> Path:
    root = Path(tempfile.mkdtemp(prefix="dock-http-plugin-"))
    unittest.addModuleCleanup(shutil.rmtree, root, True)
    (root / "plugin.toml").write_text(PLUGIN.format(caps=caps))
    return root


class ManifestHttp(unittest.TestCase):
    """AC1 through install: the manifest carries the rules, and a bad entry stops the install naming it."""

    def test_the_declared_entries_become_rules_in_order(self):
        m = Manifest.load(make_plugin('http = ["api.example.com", "http://localhost:8080", { host = "x.test", private = true }]'))
        self.assertEqual([(r.host, r.port, r.allow_http, r.private) for r in m.http],
                         [("api.example.com", None, False, False), ("localhost", 8080, True, True), ("x.test", None, False, True)])

    def test_no_entry_means_no_rules(self):
        self.assertEqual(Manifest.load(make_plugin("")).http, [])

    def test_every_invalid_entry_fails_the_install_naming_it(self):
        for entry, name in [
            ('"https://a.com/path"', "https://a.com/path"), ('"ftp://a.com"', "ftp://a.com"), ('"*"', "*"),
            ('"*.com"', "*.com"), ('""', ""), ('{ host = "a.com", privat = true }', "privat"),
        ]:
            with self.subTest(entry=entry):
                with self.assertRaises(ManifestError) as cm:
                    Manifest.load(make_plugin(f"http = [{entry}]"))
                self.assertTrue(str(cm.exception).startswith("dev.dock.sample: [capabilities].http: "), str(cm.exception))
                self.assertIn(name, str(cm.exception))

    def test_a_value_that_is_not_a_list_fails(self):
        with self.assertRaisesRegex(ManifestError, r"\[capabilities\]\.http: must be a list"):
            Manifest.load(make_plugin('http = "a.com"'))

    def test_a_failed_install_adds_nothing(self):
        store = Store(Path(tempfile.mkdtemp(prefix="dock-http-home-")))
        unittest.addModuleCleanup(shutil.rmtree, store.home, True)
        with self.assertRaises(ManifestError):
            store.install(make_plugin('http = ["*"]'))
        self.assertEqual(store.index(), {})

    def test_http_is_independent_of_network_for_native_plugins(self):
        m = Manifest.load(make_plugin('permissions = ["network"]\nhttp = ["a.com"]'))
        self.assertEqual((m.permissions, [r.host for r in m.http]), (["network"], ["a.com"]))


class HttpConfig(unittest.TestCase):
    """AC6 / R7: `[http]` in config.toml, strict like [isolation] and [wasm]."""

    def store(self, config=None) -> Store:
        home = Path(tempfile.mkdtemp(prefix="dock-http-home-"))
        unittest.addModuleCleanup(shutil.rmtree, home, True)
        if config is not None:
            (home / "config.toml").write_text(config)
        return Store(home)

    def test_the_defaults_are_the_specified_limits(self):
        limits = http_relay.config(self.store())
        self.assertEqual(
            (limits.max_response_bytes, limits.max_body_bytes, limits.max_header_bytes,
             limits.idle_timeout, limits.max_redirects, limits.chunk),
            (256 * 2**20, 16 * 2**20, 64 * 2**10, 30.0, 5, 64 * 2**10))
        self.assertEqual(limits, http_relay.Limits())

    def test_other_tables_leave_the_defaults(self):
        self.assertEqual(http_relay.config(self.store("[shell]\nidle_timeout = 5\n[http]\n")), http_relay.Limits())

    def test_the_response_cap_is_configurable(self):
        self.assertEqual(http_relay.config(self.store("[http]\nmax_response_bytes = 1048576\n")).max_response_bytes, 2**20)

    def test_an_unknown_key_is_refused_naming_it_and_the_known_ones(self):
        store = self.store("[http]\nmax_respons_bytes = 5\n")
        with self.assertRaises(ManifestError) as cm:
            http_relay.config(store)
        self.assertEqual(str(cm.exception),
                         f"{store.home / 'config.toml'}: [http] has unknown key 'max_respons_bytes' (known: max_response_bytes, total_timeout)")

    def test_several_unknown_keys_are_all_named(self):
        with self.assertRaisesRegex(ManifestError, "unknown keys 'a', 'b'"):
            http_relay.config(self.store("[http]\na = 1\nb = 2\n"))

    def test_wrong_values_are_refused(self):
        for body in ("[http]\nmax_response_bytes = 0\n", "[http]\nmax_response_bytes = -5\n",
                     '[http]\nmax_response_bytes = "9"\n', "[http]\nmax_response_bytes = true\n",
                     "[http]\nmax_response_bytes = 1.5\n", "http = 5\n"):
            with self.subTest(body=body):
                with self.assertRaisesRegex(ManifestError, r"\[http\]"):
                    http_relay.config(self.store(body))

    def test_lookalike_tables_and_a_top_level_key_are_refused_when_there_is_no_http_table(self):
        for body in ("[Http]\nmax_response_bytes = 5\n", "[htp]\nmax_response_bytes = 5\n", "[https]\n",
                     "max_response_bytes = 5\n"):
            with self.subTest(body=body):
                with self.assertRaises(ManifestError) as cm:
                    http_relay.config(self.store(body))
                self.assertIn("[http]", str(cm.exception))

    def test_a_config_that_does_not_parse_is_refused(self):
        with self.assertRaisesRegex(ManifestError, "config.toml"):
            http_relay.config(self.store("[http\nmax_response_bytes = 1\n"))

    def test_the_test_override_shortens_the_idle_timeout(self):
        with mock.patch.dict(os.environ, {"DOCK_TEST_HTTP_IDLE_TIMEOUT": "0.25"}):
            self.assertEqual(http_relay.config(self.store()).idle_timeout, 0.25)

    def test_the_override_is_ignored_unless_it_is_a_positive_number(self):
        for value in ("", "abc", "0", "-1", "0.0", "nan", "inf", "-inf", "1e999"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"DOCK_TEST_HTTP_IDLE_TIMEOUT": value}):
                self.assertEqual(http_relay.config(self.store()).idle_timeout, 30.0)

    def test_the_total_timeout_defaults_to_ten_minutes_and_is_configurable(self):
        self.assertEqual(http_relay.config(self.store()).total_timeout, 600.0)
        self.assertEqual(http_relay.config(self.store("[http]\ntotal_timeout = 90\n")).total_timeout, 90)
        self.assertEqual(http_relay.config(self.store("[http]\ntotal_timeout = 2.5\n")).total_timeout, 2.5)

    def test_a_wrong_total_timeout_is_refused_naming_the_key(self):
        for body in ("[http]\ntotal_timeout = 0\n", "[http]\ntotal_timeout = -1\n", '[http]\ntotal_timeout = "9"\n',
                     "[http]\ntotal_timeout = true\n", "[http]\ntotal_timeout = nan\n", "[http]\ntotal_timeout = inf\n"):
            with self.subTest(body=body):
                with self.assertRaisesRegex(ManifestError, r"\[http\] total_timeout must be a positive number"):
                    http_relay.config(self.store(body))

    def test_the_other_tables_still_load_next_to_http(self):
        from cli import isolation, wasm
        store = self.store('[http]\nmax_response_bytes = 5\n[wasm]\nruntime = "wasmtime"\n')
        self.assertEqual(wasm.config(store), "wasmtime")
        self.assertIsNone(isolation.config(store))


# a local server -----------------------------------------------------------------
class _Quiet(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):  # a client that went away is part of some tests
        pass


class Server:
    """A local HTTP/1.1 server on 127.0.0.1 that records requests and answers with `fn(handler)`."""

    def __init__(self, fn, tls: tuple[str, str] | None = None):
        self.requests: list[tuple[str, str, dict, bytes]] = []  # (method, path, lower-case headers, body)
        self.sni: list = []                                      # server names clients asked for (tls only)
        self.gone = threading.Event()                           # set when a response write found the client gone
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def dispatch(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                server.requests.append((self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body))
                try:
                    fn(self)
                except (BrokenPipeError, ConnectionError):
                    server.gone.set()

            do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = dispatch

            def log_message(self, *args):
                pass

        self.httpd = _Quiet(("127.0.0.1", 0), Handler)
        if tls:  # (certificate file, key file)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(*tls)
            context.sni_callback = lambda sock, name, ctx: self.sni.append(name)
            self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, args=(0.01,), daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def send(h, status=200, body=b"", headers=None, length=True):
    h.send_response(status)
    for k, v in (headers or {}).items():
        h.send_header(k, v)
    if length:
        h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    if body and h.command != "HEAD":
        h.wfile.write(body)


def chunked(h, pieces, status=200):
    h.send_response(status)
    h.send_header("Transfer-Encoding", "chunked")
    h.end_headers()
    for piece in pieces:
        h.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece))
        h.wfile.flush()
    h.wfile.write(b"0\r\n\r\n")


BODY = bytes(range(256)) * 800  # 200 KiB


class RelayCase(unittest.TestCase):
    """Run a Relay in-process against local servers; events and log lines are collected."""

    WATCHDOG = 20.0  # seconds: a relay that runs longer is cancelled, so a bug cannot hang the suite

    def setUp(self):
        self.events: list[dict] = []
        self.logs: list[str] = []
        faulthandler.dump_traceback_later(self.WATCHDOG * 3, exit=False)  # say where, if even the watchdog fails
        self.addCleanup(faulthandler.cancel_dump_traceback_later)

    def serve(self, fn, tls=None) -> Server:
        server = Server(fn, tls)
        self.addCleanup(server.close)
        return server

    def relay(self, entries, limits=None) -> Relay:
        relay = Relay(parse_rules(entries), limits or Limits(), self.events.append, self.logs.append)
        watchdog = threading.Timer(self.WATCHDOG, relay.cancel)
        watchdog.daemon = True
        watchdog.start()
        self.addCleanup(watchdog.cancel)
        return relay

    def run_relay(self, entries, params, limits=None):
        return self.relay(entries, limits).run(params)

    def get(self, entries, url, limits=None, **extra):
        return self.run_relay(entries, {"method": "GET", "url": url, **extra}, limits)

    def head(self):
        return self.events[0]

    def body(self) -> bytes:
        return b"".join(base64.b64decode(e["chunk"]) for e in self.events if "chunk" in e)

    def refused(self, entries, params, text, limits=None):
        with self.assertRaises(Refused) as cm:
            self.run_relay(entries, params, limits)
        self.assertIn(text, str(cm.exception))


class Streaming(RelayCase):
    """AC2 / R2, R6: the head event, chunks of at most 64 KiB, then the result."""

    def test_a_get_delivers_head_chunks_and_the_result(self):
        server = self.serve(lambda h: send(h, 200, BODY, {"Content-Type": "application/octet-stream", "X-Two": "2"}))
        result = self.get([f"http://localhost:{server.port}"], f"http://localhost:{server.port}/blob")
        self.assertEqual(result, {"status": 200, "bytes": 204800})
        head = self.head()
        self.assertEqual(head["status"], 200)
        self.assertIn(["content-type", "application/octet-stream"], head["headers"])
        self.assertIn(["x-two", "2"], head["headers"])
        chunks = self.events[1:]
        self.assertTrue(all(set(e) == {"chunk"} for e in chunks))
        self.assertTrue(all(len(base64.b64decode(e["chunk"])) <= 64 * 1024 for e in chunks))
        self.assertGreaterEqual(len(chunks), 4)
        self.assertEqual(self.body(), BODY)
        self.assertEqual(set(head), {"status", "headers"})

    def test_a_post_body_reaches_the_server_intact_with_its_headers(self):
        server = self.serve(lambda h: send(h, 201, b"made"))
        payload = bytes(range(256)) * 10
        result = self.run_relay([f"http://127.0.0.1:{server.port}"], {
            "method": "POST", "url": f"http://127.0.0.1:{server.port}/items",
            "headers": {"Content-Type": "application/x-test", "X-Token": "abc"},
            "body": base64.b64encode(payload).decode()})
        self.assertEqual(result, {"status": 201, "bytes": 4})
        method, path, headers, body = server.requests[0]
        self.assertEqual((method, path, body), ("POST", "/items", payload))
        self.assertEqual((headers["content-type"], headers["x-token"], headers["content-length"]), ("application/x-test", "abc", str(len(payload))))

    def test_put_patch_and_delete_are_sent_with_their_method(self):
        server = self.serve(lambda h: send(h, 200, h.command.encode()))
        for method in ("PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                self.events.clear()
                self.run_relay([f"http://127.0.0.1:{server.port}"], {"method": method, "url": f"http://127.0.0.1:{server.port}/"})
                self.assertEqual(self.body(), method.encode())

    def test_a_post_without_a_body_sends_an_empty_one(self):
        server = self.serve(lambda h: send(h))
        self.run_relay([f"http://127.0.0.1:{server.port}"], {"method": "POST", "url": f"http://127.0.0.1:{server.port}/"})
        self.assertEqual(server.requests[0][2]["content-length"], "0")

    def test_head_sends_the_head_event_and_no_chunks(self):
        server = self.serve(lambda h: send(h, 200, b"x" * 10))  # Content-Length 10, but a HEAD response has no body
        result = self.run_relay([f"http://127.0.0.1:{server.port}"], {"method": "HEAD", "url": f"http://127.0.0.1:{server.port}/"})
        self.assertEqual(result, {"status": 200, "bytes": 0})
        self.assertEqual(len(self.events), 1)
        self.assertIn(["content-length", "10"], self.head()["headers"])

    def test_204_and_304_send_the_head_event_and_no_chunks(self):
        server = self.serve(lambda h: send(h, int(h.path[1:]), length=False))
        for status in (204, 304):
            with self.subTest(status=status):
                self.events.clear()
                result = self.get([f"http://127.0.0.1:{server.port}"], f"http://127.0.0.1:{server.port}/{status}")
                self.assertEqual(result, {"status": status, "bytes": 0})
                self.assertEqual(len(self.events), 1)

    def test_non_2xx_statuses_are_results_not_errors(self):
        server = self.serve(lambda h: send(h, int(h.path[1:]), b"nope"))
        for status in (400, 404, 500, 503):
            with self.subTest(status=status):
                self.events.clear()
                self.assertEqual(self.get([f"http://127.0.0.1:{server.port}"], f"http://127.0.0.1:{server.port}/{status}"),
                                 {"status": status, "bytes": 4})
                self.assertEqual(self.body(), b"nope")

    def test_a_chunked_response_is_passed_on_decoded(self):
        server = self.serve(lambda h: chunked(h, [b"hello ", b"chunked ", b"world"]))
        result = self.get([f"http://127.0.0.1:{server.port}"], f"http://127.0.0.1:{server.port}/")
        self.assertEqual((self.body(), result["bytes"]), (b"hello chunked world", 19))

    def test_a_response_without_a_length_ends_when_the_server_closes(self):
        def fn(h):
            send(h, 200, length=False)
            h.wfile.write(b"until close")
            h.close_connection = True
        server = self.serve(fn)
        self.get([f"http://127.0.0.1:{server.port}"], f"http://127.0.0.1:{server.port}/")
        self.assertEqual(self.body(), b"until close")

    def test_the_body_is_streamed_not_held_back_until_the_end(self):
        proceed = threading.Event()

        def fn(h):
            h.send_response(200)
            h.send_header("Transfer-Encoding", "chunked")
            h.end_headers()
            h.wfile.write(b"5\r\nfirst\r\n")
            h.wfile.flush()
            proceed.wait(5)  # released only once dock has delivered the first piece
            h.wfile.write(b"6\r\nsecond\r\n0\r\n\r\n")

        server = self.serve(fn)
        seen_first = []

        def on_event(event):
            self.events.append(event)
            if "chunk" in event and not seen_first:
                seen_first.append(event)
                proceed.set()

        relay = Relay(parse_rules([f"http://127.0.0.1:{server.port}"]), Limits(), on_event, self.logs.append)
        relay.run({"method": "GET", "url": f"http://127.0.0.1:{server.port}/"})
        self.assertTrue(proceed.is_set())
        self.assertEqual(self.body(), b"firstsecond")

    def test_a_truncated_body_is_an_error_not_a_short_success(self):
        def fn(h):
            h.send_response(200)
            h.send_header("Content-Length", "100")
            h.end_headers()
            h.wfile.write(b"only a little")
            h.close_connection = True

        server = self.serve(fn)
        self.refused([f"http://127.0.0.1:{server.port}"], {"method": "GET", "url": f"http://127.0.0.1:{server.port}/"}, "ended early")

    def test_dock_sends_only_what_the_plugin_sets_plus_the_protocol_headers(self):
        server = self.serve(lambda h: send(h))
        self.get([f"http://127.0.0.1:{server.port}"], f"http://127.0.0.1:{server.port}/", headers={"X-Mine": "1"})
        headers = server.requests[0][2]
        self.assertEqual(set(headers), {"host", "accept-encoding", "connection", "x-mine"})
        self.assertEqual((headers["host"], headers["accept-encoding"], headers["connection"]),
                         (f"127.0.0.1:{server.port}", "identity", "close"))

    def test_the_plugin_may_choose_its_accept_encoding(self):
        server = self.serve(lambda h: send(h))
        self.get([f"http://127.0.0.1:{server.port}"], f"http://127.0.0.1:{server.port}/", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(server.requests[0][2]["accept-encoding"], "gzip")

    def test_headers_that_dock_owns_are_dropped(self):
        server = self.serve(lambda h: send(h))
        self.run_relay([f"http://127.0.0.1:{server.port}"], {
            "method": "POST", "url": f"http://127.0.0.1:{server.port}/", "body": base64.b64encode(b"abc").decode(),
            "headers": {"Host": "evil.example", "Content-Length": "999", "Transfer-Encoding": "chunked",
                        "Connection": "keep-alive", "Upgrade": "websocket", "Expect": "100-continue", "X-Ok": "1"}})
        headers = server.requests[0][2]
        self.assertEqual((headers["host"], headers["content-length"], headers["connection"], headers["x-ok"]),
                         (f"127.0.0.1:{server.port}", "3", "close", "1"))
        self.assertTrue({"transfer-encoding", "upgrade", "expect"}.isdisjoint(headers))

    def test_a_non_ascii_path_and_header_value_go_out_encoded(self):
        server = self.serve(lambda h: send(h))
        self.get([f"http://127.0.0.1:{server.port}"], f"http://127.0.0.1:{server.port}/ä", headers={"X-Name": "Zoë"})
        self.assertEqual(server.requests[0][1], "/%C3%A4")
        self.assertEqual(server.requests[0][2]["x-name"], "Zoë".encode().decode("latin-1"))

    def test_a_declared_localhost_name_is_reached_even_when_it_resolves_to_ipv6_first(self):
        server = self.serve(lambda h: send(h, 200, b"ok"))  # listens on IPv4 only
        v6_first = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", server.port, 0, 0)),
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", server.port))]
        with mock.patch.object(http_relay, "resolver", lambda *a, **k: v6_first):
            result = self.get([f"http://localhost:{server.port}"], f"http://localhost:{server.port}/")
        self.assertEqual(result["status"], 200)
        self.assertEqual(server.requests[0][2]["host"], f"localhost:{server.port}")

    def test_the_real_localhost_lookup_works_for_a_localhost_entry(self):
        server = self.serve(lambda h: send(h, 200, b"ok"))
        self.assertEqual(self.get([f"http://localhost:{server.port}"], f"http://localhost:{server.port}/")["status"], 200)


class Refusals(RelayCase):
    """AC3 / R3, NFR1: refused with the reason, and the server never sees a request."""

    def setUp(self):
        super().setUp()
        self.server = self.serve(lambda h: send(h, 200, b"secret"))
        self.p = self.server.port

    def assert_untouched(self):
        self.assertEqual(self.server.requests, [])
        self.assertEqual(self.events, [])

    def test_an_undeclared_host(self):
        self.refused([f"http://127.0.0.1:{self.p}"], {"method": "GET", "url": f"http://localhost:{self.p}/"}, "host 'localhost' is not declared")
        self.assert_untouched()

    def test_a_port_that_is_not_declared(self):
        self.refused([f"http://127.0.0.1:{self.p}"], {"method": "GET", "url": f"http://127.0.0.1:{self.p + 1}/"}, "port")
        self.assert_untouched()

    def test_plain_http_for_an_https_only_entry(self):
        self.refused([f"127.0.0.1:{self.p}"], {"method": "GET", "url": f"http://127.0.0.1:{self.p}/"}, "plain http")
        self.assert_untouched()

    def test_userinfo(self):
        self.refused([f"http://127.0.0.1:{self.p}"], {"method": "GET", "url": f"http://user:pw@127.0.0.1:{self.p}/"}, "userinfo")
        self.assert_untouched()

    def test_a_public_looking_name_that_resolves_to_a_private_address_is_refused_before_connecting(self):
        connects = []
        with mock.patch.object(http_relay, "resolver", lambda *a, **k: [(socket.AF_INET, 1, 6, "", ("10.0.0.5", 80))]), \
                mock.patch.object(http_relay, "connector", lambda *a, **k: connects.append(a)):
            self.refused(["http://api.example.com"], {"method": "GET", "url": "http://api.example.com/"}, "private address")
        self.assertEqual(connects, [])

    def test_the_refusal_does_not_reveal_the_address(self):
        with mock.patch.object(http_relay, "resolver", lambda *a, **k: [(socket.AF_INET, 1, 6, "", ("10.1.2.3", 80))]):
            with self.assertRaises(Refused) as cm:
                self.get(["api.example.com"], "https://api.example.com/")
        self.assertNotIn("10.1.2.3", str(cm.exception))

    def test_a_declared_name_resolving_to_loopback_is_refused_too(self):
        with mock.patch.object(http_relay, "resolver", lambda *a, **k: [(socket.AF_INET, 1, 6, "", ("127.0.0.1", self.p))]):
            self.refused([f"http://api.example.com:{self.p}"], {"method": "GET", "url": f"http://api.example.com:{self.p}/"}, "private address")
        self.assert_untouched()

    def test_private_true_allows_a_private_address(self):
        with mock.patch.object(http_relay, "resolver", lambda *a, **k: [(socket.AF_INET, 1, 6, "", ("127.0.0.1", self.p))]):
            result = self.get([{"host": f"http://lan.example:{self.p}", "private": True}], f"http://lan.example:{self.p}/")
        self.assertEqual(result["status"], 200)

    def test_a_wildcard_entry_with_private_true_allows_private_subdomains(self):
        with mock.patch.object(http_relay, "resolver", lambda *a, **k: [(socket.AF_INET, 1, 6, "", ("127.0.0.1", self.p))]):
            result = self.get([{"host": f"http://*.lan.example:{self.p}", "private": True}], f"http://a.lan.example:{self.p}/")
        self.assertEqual(result["status"], 200)

    def test_a_name_that_does_not_resolve_is_refused_saying_so(self):
        def fail(*a, **k):
            raise socket.gaierror(8, "nodename nor servname provided")

        with mock.patch.object(http_relay, "resolver", fail):
            self.refused(["a.example.com"], {"method": "GET", "url": "https://a.example.com/"}, "could not resolve 'a.example.com'")

    def test_bad_requests_are_refused_before_anything_happens(self):
        url = f"http://127.0.0.1:{self.p}/"
        entries = [f"http://127.0.0.1:{self.p}"]
        for params, text in [
            ({"method": "TRACE", "url": url}, "method"),
            ({"method": "get", "url": url}, "method"),
            ({"url": url}, "method"),
            ({"method": "GET"}, "url must be a string"),
            ({"method": "GET", "url": url, "headers": ["a"]}, "headers must be an object"),
            ({"method": "GET", "url": url, "headers": {"a": 1}}, "header values must be strings"),
            ({"method": "GET", "url": url, "headers": {"a b": "1"}}, "header name"),
            ({"method": "GET", "url": url, "headers": {"a": "x\r\nHost: evil"}}, "header value"),
            ({"method": "GET", "url": url, "headers": {"a": "x\x00"}}, "header value"),
            ({"method": "POST", "url": url, "body": 5}, "body must be base64 text"),
            ({"method": "POST", "url": url, "body": "not base64!"}, "body must be base64 text"),
        ]:
            with self.subTest(params=params):
                self.refused(entries, params, text)
        self.assert_untouched()

    def test_a_plugin_without_any_rules_can_reach_nothing(self):
        self.refused([], {"method": "GET", "url": f"http://127.0.0.1:{self.p}/"}, "not declared")
        self.assert_untouched()


class AddressPolicy(unittest.TestCase):
    """R3, R5, NFR1: which resolved addresses may be connected to, and that exactly one lookup is made."""

    def resolve(self, addresses, entry="x.example.com", host="x.example.com"):
        info = [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 443)) for a in addresses]
        with mock.patch.object(http_relay, "resolver", lambda *a, **k: info):
            return http_relay.resolve_checked(host, 443, parse_rules([entry])[0])

    def test_non_public_addresses_are_refused(self):
        for address in ("10.0.0.5", "172.16.0.1", "192.168.1.1", "127.0.0.1", "127.1.2.3", "169.254.169.254", "0.0.0.0",
                        "224.0.0.1", "255.255.255.255", "100.64.0.1", "192.0.0.1", "240.0.0.1",
                        "::1", "::", "fe80::1", "fd00::1", "fc00::1", "ff02::1", "::ffff:10.0.0.5", "::ffff:127.0.0.1",
                        "::ffff:169.254.169.254", "64:ff9b::a00:5", "2002:a00:5::1",
                        # the same verdict on every Python: these are listed, not left to is_global (which changed in 3.12.4/3.13)
                        "192.88.99.1", "198.18.0.1", "198.51.100.1", "203.0.113.1", "192.0.2.1", "192.0.0.9", "0.1.2.3",
                        "fec0::1", "2001:db8::1", "2001::1", "2001:0:4136:e378:8000:63bf:3fff:fdd2", "100::1", "64:ff9b:1::1",
                        "3fff::1", "5f00::1", "::2", "::ffff:0:0", "::1.2.3.4", "2002:7f00:1::1", "2002:c0a8:1::1",
                        "64:ff9b::7f00:1", "64:ff9b::c0a8:1", "::ffff:192.88.99.1", "4000::1", "fe00::1", "febf::1"):
            with self.subTest(address=address):
                with self.assertRaisesRegex(Refused, "private address"):
                    self.resolve([address])

    def test_public_addresses_are_accepted(self):
        for address in ("8.8.8.8", "93.184.216.34", "2606:4700:4700::1111", "::ffff:8.8.8.8", "1.1.1.1", "192.31.196.1",
                        "2a00:1450:4001::1", "64:ff9b::808:808", "2002:808:808::1", "2001:4860:4860::8888"):
            with self.subTest(address=address):
                self.assertEqual(self.resolve([address]), address)

    def test_the_first_acceptable_address_is_chosen_among_several(self):
        self.assertEqual(self.resolve(["10.0.0.5", "8.8.8.8", "1.1.1.1"]), "8.8.8.8")

    def test_checked_addresses_lists_every_acceptable_one_in_order_without_duplicates(self):
        info = [(2, 1, 6, "", (a, 443)) for a in ("10.0.0.5", "8.8.8.8", "8.8.8.8", "1.1.1.1")]
        with mock.patch.object(http_relay, "resolver", lambda *a, **k: info):
            self.assertEqual(http_relay.checked_addresses("x.example.com", 443, parse_rules(["x.example.com"])[0]),
                             ["8.8.8.8", "1.1.1.1"])

    def test_a_private_true_entry_accepts_private_addresses(self):
        self.assertEqual(self.resolve(["10.0.0.5"], entry={"host": "x.example.com", "private": True}), "10.0.0.5")

    def test_localhost_and_ip_literal_entries_accept_private_addresses(self):
        self.assertEqual(self.resolve(["127.0.0.1"], entry="localhost", host="localhost"), "127.0.0.1")
        self.assertEqual(self.resolve(["::1"], entry="localhost", host="localhost"), "::1")

    def test_an_ip_literal_is_not_looked_up(self):
        def boom(*a, **k):
            raise AssertionError("no lookup for a literal")

        with mock.patch.object(http_relay, "resolver", boom):
            self.assertEqual(http_relay.resolve_checked("127.0.0.1", 80, parse_rules(["http://127.0.0.1"])[0]), "127.0.0.1")
            self.assertEqual(http_relay.resolve_checked("::1", 80, parse_rules(["http://[::1]"])[0]), "::1")

    def test_a_scope_id_in_a_resolved_address_is_dropped(self):
        info = [(socket.AF_INET6, 1, 6, "", ("fe80::1%eth0", 443, 0, 2))]
        with mock.patch.object(http_relay, "resolver", lambda *a, **k: info):
            with self.assertRaisesRegex(Refused, "private address"):
                http_relay.resolve_checked("x.example.com", 443, parse_rules(["x.example.com"])[0])

    def test_nothing_resolved_is_refused(self):
        with mock.patch.object(http_relay, "resolver", lambda *a, **k: []):
            with self.assertRaisesRegex(Refused, "could not resolve"):
                http_relay.resolve_checked("x.example.com", 443, parse_rules(["x.example.com"])[0])


class Pinning(RelayCase):
    """AC5 / R5: the connection goes to the address that was checked; the name is looked up once per hop."""

    def test_a_second_lookup_that_would_say_private_is_never_made(self):
        server = self.serve(lambda h: send(h, 200, b"ok"))
        calls, connected = [], []

        def resolver(host, port, *a, **k):
            calls.append(host)
            ip = "8.8.8.8" if len(calls) == 1 else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

        def connector(address, timeout=None):  # stands in for the network: records where dock went, then reaches the server
            connected.append(address)
            return socket.create_connection(("127.0.0.1", server.port), timeout=timeout)

        with mock.patch.object(http_relay, "resolver", resolver), mock.patch.object(http_relay, "connector", connector):
            result = self.get([f"http://api.example.com:{server.port}"], f"http://api.example.com:{server.port}/x")
        self.assertEqual(result["status"], 200)
        self.assertEqual(calls, ["api.example.com"])
        self.assertEqual(connected, [("8.8.8.8", server.port)])
        self.assertEqual(server.requests[0][2]["host"], f"api.example.com:{server.port}")

    def test_each_redirect_hop_is_resolved_once_and_connected_where_it_was_checked(self):
        server = self.serve(lambda h: send(h, 302, headers={"Location": f"http://b.example.com:{server.port}/b"}) if h.path == "/" else send(h, 200, b"b"))
        calls, connected = [], []

        def resolver(host, port, *a, **k):
            calls.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8" if host.startswith("a") else "1.1.1.1", port))]

        def connector(address, timeout=None):
            connected.append(address[0])
            return socket.create_connection(("127.0.0.1", server.port), timeout=timeout)

        with mock.patch.object(http_relay, "resolver", resolver), mock.patch.object(http_relay, "connector", connector):
            self.get([f"http://a.example.com:{server.port}", f"http://b.example.com:{server.port}"], f"http://a.example.com:{server.port}/")
        self.assertEqual(calls, ["a.example.com", "b.example.com"])
        self.assertEqual(connected, ["8.8.8.8", "1.1.1.1"])

    def test_when_the_first_checked_address_does_not_connect_the_next_checked_one_is_tried(self):
        server = self.serve(lambda h: send(h, 200, b"ok"))
        tried = []

        def connector(address, timeout=None):
            tried.append(address[0])
            if address[0] == "8.8.8.8":
                raise ConnectionRefusedError(61, "refused")
            return socket.create_connection(("127.0.0.1", server.port), timeout=timeout)

        info = [(2, 1, 6, "", (a, server.port)) for a in ("8.8.8.8", "10.0.0.5", "1.1.1.1")]
        with mock.patch.object(http_relay, "resolver", lambda *a, **k: info), mock.patch.object(http_relay, "connector", connector):
            self.get([f"http://api.example.com:{server.port}"], f"http://api.example.com:{server.port}/")
        self.assertEqual(tried, ["8.8.8.8", "1.1.1.1"])  # the private one was never a candidate

    def test_a_connection_that_fails_is_refused_with_the_reason(self):
        def connector(address, timeout=None):
            raise ConnectionRefusedError(61, "Connection refused")

        with mock.patch.object(http_relay, "resolver", lambda *a, **k: [(2, 1, 6, "", ("8.8.8.8", 80))]), \
                mock.patch.object(http_relay, "connector", connector):
            self.refused(["http://api.example.com"], {"method": "GET", "url": "http://api.example.com/"}, "connection failed")


class Redirects(RelayCase):
    """AC4 / R4: every hop is checked, at most 5 are followed, credentials stay with their host."""

    def two(self, a, b):
        """Servers A and B, declared as 127.0.0.1:<a> and localhost:<b> (two different hosts, both local)."""
        sa, sb = self.serve(a), self.serve(b)
        entries = [f"http://127.0.0.1:{sa.port}", f"http://localhost:{sb.port}"]
        return sa, sb, entries

    def test_a_redirect_to_a_declared_host_is_followed_and_only_its_response_is_delivered(self):
        sa, sb, entries = self.two(lambda h: send(h, 302, b"moved", {"Location": f"http://localhost:{sb.port}/there"}), lambda h: send(h, 200, b"arrived"))
        result = self.get(entries, f"http://127.0.0.1:{sa.port}/")
        self.assertEqual(result, {"status": 200, "bytes": 7})
        self.assertEqual((self.head()["status"], self.body()), (200, b"arrived"))
        self.assertEqual(sb.requests[0][1], "/there")
        self.assertEqual(sb.requests[0][2]["host"], f"localhost:{sb.port}")

    def test_a_redirect_to_an_undeclared_host_is_refused_and_the_plugin_gets_nothing(self):
        sa = self.serve(lambda h: send(h, 302, b"leaking body", {"Location": f"http://localhost:{sb.port}/"}))
        sb = self.serve(lambda h: send(h, 200, b"private"))
        self.refused([f"http://127.0.0.1:{sa.port}"], {"method": "GET", "url": f"http://127.0.0.1:{sa.port}/"}, "host 'localhost' is not declared")
        self.assertEqual(self.events, [])
        self.assertEqual(sb.requests, [])

    def test_a_redirect_to_a_private_address_behind_a_declared_name_is_refused(self):
        sa = self.serve(lambda h: send(h, 302, headers={"Location": "http://rebind.example.com/"}))
        with mock.patch.object(http_relay, "resolver", lambda *a, **k: [(2, 1, 6, "", ("10.0.0.5", 80))]):
            self.refused([f"http://127.0.0.1:{sa.port}", "http://rebind.example.com"], {"method": "GET", "url": f"http://127.0.0.1:{sa.port}/"}, "private address")
        self.assertEqual(self.events, [])

    def test_a_redirect_to_other_schemes_or_userinfo_or_plain_http_is_refused(self):
        for location, extra, text in [("ftp://127.0.0.1/", [], "scheme"), ("http://u:p@127.0.0.1:{p}/", [], "userinfo"),
                                      ("http://localhost:8081/", ["localhost:8081"], "plain http")]:
            with self.subTest(location=location):
                self.events.clear()
                sa = self.serve(lambda h, loc=location: send(h, 302, headers={"Location": loc.replace("{p}", str(sa.port))}))
                self.refused([f"http://127.0.0.1:{sa.port}", *extra], {"method": "GET", "url": f"http://127.0.0.1:{sa.port}/"}, text)
                self.assertEqual(self.events, [])

    def test_relative_locations_are_resolved_against_the_current_url(self):
        server = self.serve(lambda h: send(h, 301, headers={"Location": "next?x=1"}) if h.path == "/a/start" else send(h, 200, h.path.encode()))
        self.get([f"http://127.0.0.1:{server.port}"], f"http://127.0.0.1:{server.port}/a/start")
        self.assertEqual(self.body(), b"/a/next?x=1")

    def test_five_redirects_are_followed_and_the_sixth_is_refused(self):
        def chain(h):
            n = int(h.path[1:])
            send(h, 302, headers={"Location": f"/{n + 1}"}) if n < 5 else send(h, 200, b"end")

        server = self.serve(chain)
        entries = [f"http://127.0.0.1:{server.port}"]
        self.assertEqual(self.get(entries, f"http://127.0.0.1:{server.port}/0")["status"], 200)  # 5 redirects
        self.assertEqual(len(server.requests), 6)
        self.events.clear()
        server.requests.clear()
        self.refused(entries, {"method": "GET", "url": f"http://127.0.0.1:{server.port}/-1"}, "more than 5 redirects")
        self.assertEqual(self.events, [])

    def test_a_redirect_loop_ends_at_the_limit(self):
        server = self.serve(lambda h: send(h, 302, headers={"Location": "/"}))
        self.refused([f"http://127.0.0.1:{server.port}"], {"method": "GET", "url": f"http://127.0.0.1:{server.port}/"}, "redirects")
        self.assertEqual(len(server.requests), 6)

    def test_the_redirect_limit_comes_from_the_limits(self):
        server = self.serve(lambda h: send(h, 302, headers={"Location": "/"}))
        self.refused([f"http://127.0.0.1:{server.port}"], {"method": "GET", "url": f"http://127.0.0.1:{server.port}/"},
                     "more than 1 redirects", Limits(max_redirects=1))

    def test_credential_headers_are_dropped_when_the_host_changes_and_the_others_kept(self):
        sa, sb, entries = self.two(lambda h: send(h, 302, headers={"Location": f"http://localhost:{sb.port}/"}), lambda h: send(h))
        self.get(entries, f"http://127.0.0.1:{sa.port}/", headers={
            "Authorization": "Bearer t", "Cookie": "s=1", "Proxy-Authorization": "Basic x", "X-Keep": "yes"})
        first, second = sa.requests[0][2], sb.requests[0][2]
        self.assertEqual((first["authorization"], first["cookie"], first["proxy-authorization"]), ("Bearer t", "s=1", "Basic x"))
        self.assertTrue({"authorization", "cookie", "proxy-authorization"}.isdisjoint(second), second)
        self.assertEqual(second["x-keep"], "yes")

    def test_credential_headers_are_kept_on_a_same_host_redirect(self):
        server = self.serve(lambda h: send(h, 302, headers={"Location": "/two"}) if h.path == "/" else send(h))
        self.get([f"http://127.0.0.1:{server.port}"], f"http://127.0.0.1:{server.port}/", headers={"Authorization": "Bearer t"})
        self.assertEqual(server.requests[1][2]["authorization"], "Bearer t")

    def test_credentials_are_dropped_for_another_port_of_the_same_host_too(self):
        sa = self.serve(lambda h: send(h, 302, headers={"Location": f"http://127.0.0.1:{sb.port}/"}))
        sb = self.serve(lambda h: send(h))
        self.get([f"http://127.0.0.1:{sa.port}", f"http://127.0.0.1:{sb.port}"], f"http://127.0.0.1:{sa.port}/", headers={"Authorization": "Bearer t"})
        self.assertNotIn("authorization", sb.requests[0][2])

    def test_credentials_do_not_come_back_after_a_round_trip_through_another_host(self):
        hops = []

        def a(h):
            hops.append("a")
            send(h, 302, headers={"Location": f"http://localhost:{sb.port}/"}) if len(hops) == 1 else send(h)

        sa = self.serve(a)
        sb = self.serve(lambda h: send(h, 302, headers={"Location": f"http://127.0.0.1:{sa.port}/again"}))
        self.get([f"http://127.0.0.1:{sa.port}", f"http://localhost:{sb.port}"], f"http://127.0.0.1:{sa.port}/", headers={"Authorization": "Bearer t"})
        self.assertNotIn("authorization", sa.requests[1][2])

    def test_a_post_becomes_a_get_without_a_body_on_301_302_303(self):
        for status in (301, 302, 303):
            with self.subTest(status=status):
                server = self.serve(lambda h: send(h, status, headers={"Location": "/next"}) if h.path == "/" else send(h, 200, h.command.encode()))
                self.events.clear()
                self.run_relay([f"http://127.0.0.1:{server.port}"], {
                    "method": "POST", "url": f"http://127.0.0.1:{server.port}/", "headers": {"Content-Type": "text/plain"},
                    "body": base64.b64encode(b"payload").decode()})
                self.assertEqual(self.body(), b"GET")
                method, path, headers, body = server.requests[1]
                self.assertEqual((method, path, body), ("GET", "/next", b""))
                self.assertNotIn("content-type", headers)

    def test_307_and_308_keep_the_method_and_the_body(self):
        for status in (307, 308):
            with self.subTest(status=status):
                server = self.serve(lambda h: send(h, status, headers={"Location": "/next"}) if h.path == "/" else send(h))
                self.run_relay([f"http://127.0.0.1:{server.port}"], {
                    "method": "PUT", "url": f"http://127.0.0.1:{server.port}/", "body": base64.b64encode(b"payload").decode()})
                self.assertEqual(server.requests[1][:2] + server.requests[1][3:], ("PUT", "/next", b"payload"))

    def test_a_redirect_status_without_a_location_is_an_ordinary_response(self):
        server = self.serve(lambda h: send(h, 302, b"no where"))
        result = self.get([f"http://127.0.0.1:{server.port}"], f"http://127.0.0.1:{server.port}/")
        self.assertEqual((result["status"], self.body()), (302, b"no where"))

    def test_300_and_304_are_not_followed(self):
        server = self.serve(lambda h: send(h, 300, headers={"Location": "/x"}))
        self.assertEqual(self.get([f"http://127.0.0.1:{server.port}"], f"http://127.0.0.1:{server.port}/")["status"], 300)
        self.assertEqual(len(server.requests), 1)


class Limit(RelayCase):
    """AC6 / R7: size, header and idle limits."""

    def test_a_response_over_the_cap_with_a_known_length_ends_before_any_event(self):
        server = self.serve(lambda h: send(h, 200, b"x" * (2 * 2**20)))
        self.refused([f"http://127.0.0.1:{server.port}"], {"method": "GET", "url": f"http://127.0.0.1:{server.port}/"},
                     "response larger than 1048576", Limits(max_response_bytes=2**20))
        self.assertEqual(self.events, [])

    def test_a_response_over_the_cap_without_a_length_is_cut_at_the_cap(self):
        server = self.serve(lambda h: chunked(h, [b"x" * 65536] * 32))  # 2 MiB
        self.refused([f"http://127.0.0.1:{server.port}"], {"method": "GET", "url": f"http://127.0.0.1:{server.port}/"},
                     "response larger than", Limits(max_response_bytes=2**20))
        self.assertEqual(len(self.body()), 2**20)

    def test_a_response_exactly_at_the_cap_is_fine(self):
        for responder in (lambda h: send(h, 200, b"x" * 2**20), lambda h: chunked(h, [b"x" * 65536] * 16)):
            server = self.serve(responder)
            self.events.clear()
            result = self.get([f"http://127.0.0.1:{server.port}"], f"http://127.0.0.1:{server.port}/", Limits(max_response_bytes=2**20))
            self.assertEqual(result["bytes"], 2**20)

    def test_a_head_response_is_not_held_to_the_cap_by_its_content_length(self):
        server = self.serve(lambda h: send(h, 200, b"x" * 100))
        server.requests.clear()
        result = self.run_relay([f"http://127.0.0.1:{server.port}"], {"method": "HEAD", "url": f"http://127.0.0.1:{server.port}/"}, Limits(max_response_bytes=10))
        self.assertEqual(result["bytes"], 0)

    def test_the_default_cap_is_256_mib(self):
        self.assertEqual(Limits().max_response_bytes, 256 * 2**20)

    def test_a_17_mib_request_body_is_refused_and_nothing_is_sent(self):
        server = self.serve(lambda h: send(h))
        body = base64.b64encode(b"\0" * (17 * 2**20)).decode()
        self.refused([f"http://127.0.0.1:{server.port}"], {"method": "POST", "url": f"http://127.0.0.1:{server.port}/", "body": body},
                     "request body larger than 16777216")
        self.assertEqual(server.requests, [])

    def test_a_request_body_at_the_limit_is_sent_and_one_byte_more_is_not(self):
        server = self.serve(lambda h: send(h))
        entries, url = [f"http://127.0.0.1:{server.port}"], f"http://127.0.0.1:{server.port}/"
        limits = Limits(max_body_bytes=1000)
        self.run_relay(entries, {"method": "POST", "url": url, "body": base64.b64encode(b"a" * 1000).decode()}, limits)
        self.assertEqual(len(server.requests[0][3]), 1000)
        self.refused(entries, {"method": "POST", "url": url, "body": base64.b64encode(b"a" * 1001).decode()}, "request body larger than 1000", limits)
        self.assertEqual(len(server.requests), 1)

    def test_request_headers_over_64_kib_are_refused_and_just_under_are_sent(self):
        server = self.serve(lambda h: send(h))
        entries, url = [f"http://127.0.0.1:{server.port}"], f"http://127.0.0.1:{server.port}/"
        self.refused(entries, {"method": "GET", "url": url, "headers": {f"X-{i}": "v" * 1000 for i in range(66)}}, "request headers larger than 65536")
        self.assertEqual(server.requests, [])
        self.get(entries, url, headers={f"X-{i}": "v" * 1000 for i in range(60)})
        self.assertEqual(len(server.requests), 1)

    def test_a_server_that_sends_nothing_times_out(self):
        release = threading.Event()
        self.addCleanup(release.set)
        server = self.serve(lambda h: release.wait(5))
        started = time.monotonic()
        self.refused([f"http://127.0.0.1:{server.port}"], {"method": "GET", "url": f"http://127.0.0.1:{server.port}/"}, "timed out", Limits(idle_timeout=0.3))
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(self.events, [])

    def test_a_server_that_stalls_mid_body_times_out_after_the_bytes_it_did_send(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def fn(h):
            h.send_response(200)
            h.send_header("Content-Length", "1000")
            h.end_headers()
            h.wfile.write(b"x" * 10)
            h.wfile.flush()
            release.wait(5)

        server = self.serve(fn)
        self.refused([f"http://127.0.0.1:{server.port}"], {"method": "GET", "url": f"http://127.0.0.1:{server.port}/"}, "timed out", Limits(idle_timeout=0.3))
        self.assertEqual(self.body(), b"x" * 10)

    def test_a_server_that_keeps_sending_does_not_time_out(self):
        def fn(h):
            h.send_response(200)
            h.send_header("Transfer-Encoding", "chunked")
            h.end_headers()
            for _ in range(8):
                h.wfile.write(b"1\r\nx\r\n")
                h.wfile.flush()
                time.sleep(0.1)
            h.wfile.write(b"0\r\n\r\n")

        server = self.serve(fn)
        result = self.get([f"http://127.0.0.1:{server.port}"], f"http://127.0.0.1:{server.port}/", Limits(idle_timeout=0.4))
        self.assertEqual(result["bytes"], 8)

    def test_the_default_idle_timeout_is_30_seconds(self):
        self.assertEqual(Limits().idle_timeout, 30.0)

    def test_oversized_response_headers_are_refused(self):
        server = self.serve(lambda h: send(h, 200, b"x", {f"X-{i}": "v" * 1000 for i in range(70)}))
        self.refused([f"http://127.0.0.1:{server.port}"], {"method": "GET", "url": f"http://127.0.0.1:{server.port}/"}, "response headers larger than")
        self.assertEqual(self.events, [])

    def test_a_server_that_speaks_nonsense_is_refused_not_crashed(self):
        def junk_server():
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            self.addCleanup(listener.close)

            def serve():
                conn, _ = listener.accept()
                conn.recv(4096)
                conn.sendall(b"this is not http\r\n\r\n")
                conn.close()

            threading.Thread(target=serve, daemon=True).start()
            return listener.getsockname()[1]

        port = junk_server()
        self.refused([f"http://127.0.0.1:{port}"], {"method": "GET", "url": f"http://127.0.0.1:{port}/"}, "bad response")


class Cancel(RelayCase):
    """AC7 / R8: cancel closes the connection and ends run() with Cancelled."""

    def start(self, relay, url):
        outcome = []

        def work():
            try:
                outcome.append(relay.run({"method": "GET", "url": url}))
            except BaseException as e:  # noqa: BLE001 - reported to the test
                outcome.append(e)

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        return thread, outcome

    def test_cancel_during_a_slow_stream_ends_with_cancelled_and_the_server_sees_the_connection_close(self):
        def fn(h):
            h.send_response(200)
            h.send_header("Transfer-Encoding", "chunked")
            h.end_headers()
            for _ in range(600):
                h.wfile.write(b"4\r\ndata\r\n")
                h.wfile.flush()
                time.sleep(0.05)

        server = self.serve(fn)
        first = threading.Event()
        relay = Relay(parse_rules([f"http://127.0.0.1:{server.port}"]), Limits(), lambda e: (self.events.append(e), first.set()), self.logs.append)
        thread, outcome = self.start(relay, f"http://127.0.0.1:{server.port}/")
        self.assertTrue(first.wait(5))
        started = time.monotonic()
        relay.cancel()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 2)
        self.assertIsInstance(outcome[0], Cancelled)
        self.assertTrue(server.gone.wait(5), "the server never saw the connection close")

    def test_cancel_while_waiting_for_the_response_head(self):
        release = threading.Event()
        self.addCleanup(release.set)
        server = self.serve(lambda h: release.wait(10))
        relay = self.relay([f"http://127.0.0.1:{server.port}"])
        thread, outcome = self.start(relay, f"http://127.0.0.1:{server.port}/")
        deadline = time.monotonic() + 5
        while not server.requests and time.monotonic() < deadline:
            time.sleep(0.01)
        relay.cancel()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcome[0], Cancelled)
        self.assertEqual(self.events, [])

    def test_cancel_before_run_sends_nothing(self):
        server = self.serve(lambda h: send(h))
        relay = self.relay([f"http://127.0.0.1:{server.port}"])
        relay.cancel()
        with self.assertRaises(Cancelled):
            relay.run({"method": "GET", "url": f"http://127.0.0.1:{server.port}/"})
        self.assertEqual(server.requests, [])

    def test_cancel_after_the_request_finished_changes_nothing(self):
        server = self.serve(lambda h: send(h, 200, b"ok"))
        relay = self.relay([f"http://127.0.0.1:{server.port}"])
        self.assertEqual(relay.run({"method": "GET", "url": f"http://127.0.0.1:{server.port}/"})["status"], 200)
        relay.cancel()

    def test_cancel_during_connect_ends_the_request(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        server = self.serve(lambda h: send(h))

        def slow_connector(address, timeout=None):
            entered.set()
            release.wait(5)
            return socket.create_connection(("127.0.0.1", server.port), timeout=timeout)

        relay = self.relay([f"http://127.0.0.1:{server.port}"])
        with mock.patch.object(http_relay, "connector", slow_connector):
            thread, outcome = self.start(relay, f"http://127.0.0.1:{server.port}/")
            self.assertTrue(entered.wait(5))
            relay.cancel()
            release.set()
            thread.join(5)
        self.assertIsInstance(outcome[0], Cancelled)
        self.assertEqual(server.requests, [])


class Logging(RelayCase):
    """R10 / NFR2: one log line per request, without query, headers, body or userinfo."""

    def test_a_successful_request_logs_method_url_without_query_and_status(self):
        server = self.serve(lambda h: send(h, 200, b"x"))
        self.get([f"http://127.0.0.1:{server.port}"], f"http://127.0.0.1:{server.port}/a/b?token=SECRET#frag", headers={"Authorization": "SECRET"})
        self.assertEqual(self.logs, [f"http GET http://127.0.0.1:{server.port}/a/b -> 200\n"])

    def test_a_refusal_logs_the_reason(self):
        with self.assertRaises(Refused):
            self.get(["api.example.com"], "https://evil.example/x?key=SECRET")
        self.assertEqual(self.logs, ["http GET https://evil.example/x -> refused: host 'evil.example' is not declared in [capabilities].http\n"])

    def test_userinfo_never_reaches_the_log(self):
        with self.assertRaises(Refused):
            self.get(["a.com"], "https://user:SECRET@a.com/x")
        self.assertNotIn("SECRET", self.logs[0])
        self.assertTrue(self.logs[0].startswith("http GET https://a.com/x -> refused: "))

    def test_a_url_cannot_forge_log_lines(self):
        with self.assertRaises(Refused):
            self.get(["a.com"], "https://a.com/x\nhttp GET https://fake/ -> 200")
        self.assertEqual(len(self.logs), 1)
        self.assertEqual(self.logs[0].count("\n"), 1)

    def test_a_default_port_is_not_written_and_an_explicit_one_is(self):
        with self.assertRaises(Refused):
            self.get(["a.com"], "https://b.com:8443/p")
        self.assertTrue(self.logs[0].startswith("http GET https://b.com:8443/p -> refused"))

    def test_a_timeout_and_a_cancel_are_logged(self):
        release = threading.Event()
        self.addCleanup(release.set)
        server = self.serve(lambda h: release.wait(5))
        with self.assertRaises(Refused):
            self.get([f"http://127.0.0.1:{server.port}"], f"http://127.0.0.1:{server.port}/", Limits(idle_timeout=0.2))
        self.assertEqual(self.logs, [f"http GET http://127.0.0.1:{server.port}/ -> refused: timed out\n"])
        relay = self.relay([f"http://127.0.0.1:{server.port}"])
        relay.cancel()
        with self.assertRaises(Cancelled):
            relay.run({"method": "GET", "url": f"http://127.0.0.1:{server.port}/"})
        self.assertEqual(self.logs[1], f"http GET http://127.0.0.1:{server.port}/ -> cancelled\n")

    def test_a_request_that_is_not_even_a_url_is_logged_without_echoing_it(self):
        with self.assertRaises(Refused):
            self.run_relay(["a.com"], {"method": "GET", "url": 5})
        self.assertEqual(self.logs, ["http GET <invalid url> -> refused: url must be a string\n"])

    def test_a_redirect_chain_is_one_line_for_the_plugins_request(self):
        server = self.serve(lambda h: send(h, 302, headers={"Location": "/two?x=1"}) if h.path == "/" else send(h, 200, b"x"))
        self.get([f"http://127.0.0.1:{server.port}"], f"http://127.0.0.1:{server.port}/")
        self.assertEqual(self.logs, [f"http GET http://127.0.0.1:{server.port}/ -> 200\n"])


class Tls(RelayCase):
    """No local https server: the TLS context and the server name given to it are checked directly."""

    def test_the_context_verifies_certificates_and_host_names(self):
        context = Relay([], Limits(), None, None).tls_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_the_connection_goes_to_the_checked_address_with_the_name_as_sni(self):
        context = mock.Mock()
        context.wrap_socket.side_effect = ssl.SSLCertVerificationError(1, "certificate verify failed: bad")
        relay = self.relay(["api.example.com"])
        relay.tls_context = lambda: context
        connected = []

        def connector(address, timeout=None):
            connected.append(address)
            return mock.Mock(spec=socket.socket)

        with mock.patch.object(http_relay, "resolver", lambda *a, **k: [(2, 1, 6, "", ("8.8.8.8", 443))]), \
                mock.patch.object(http_relay, "connector", connector):
            with self.assertRaisesRegex(Refused, "TLS certificate"):
                relay.run({"method": "GET", "url": "https://api.example.com/x"})
        self.assertEqual(connected, [("8.8.8.8", 443)])
        self.assertEqual(context.wrap_socket.call_args.kwargs["server_hostname"], "api.example.com")
        self.assertEqual(self.logs[0][:-1].split(" -> ")[1][:25], "refused: TLS certificate ")

    def test_other_tls_failures_are_refused_with_the_reason(self):
        context = mock.Mock()
        context.wrap_socket.side_effect = ssl.SSLError(1, "[SSL] handshake failure")
        relay = self.relay(["api.example.com"])
        relay.tls_context = lambda: context
        with mock.patch.object(http_relay, "resolver", lambda *a, **k: [(2, 1, 6, "", ("8.8.8.8", 443))]), \
                mock.patch.object(http_relay, "connector", lambda *a, **k: mock.Mock(spec=socket.socket)):
            with self.assertRaisesRegex(Refused, "TLS error"):
                relay.run({"method": "GET", "url": "https://api.example.com/x"})


class Watchdog(RelayCase):
    def test_a_relay_that_runs_too_long_is_cancelled_by_the_watchdog(self):
        release = threading.Event()
        self.addCleanup(release.set)
        server = self.serve(lambda h: release.wait(10))
        self.WATCHDOG = 0.3
        started = time.monotonic()
        with self.assertRaises(Cancelled):
            self.get([f"http://127.0.0.1:{server.port}"], f"http://127.0.0.1:{server.port}/")
        self.assertLess(time.monotonic() - started, 5)


class Silent:
    """A TCP server that accepts connections and never says a word (a TLS server that never answers the hello)."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.accepted = threading.Event()
        self.heard = threading.Event()  # a client sent something (the TLS hello): it is now waiting for an answer
        self.held: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.held.append(conn)
            self.accepted.set()
            threading.Thread(target=self._listen, args=(conn,), daemon=True).start()

    def _listen(self, conn):
        try:
            if conn.recv(1):
                self.heard.set()
            while conn.recv(4096):
                pass
        except OSError:
            pass

    def close(self):
        self.sock.close()
        for conn in self.held:
            conn.close()


class TlsHandshake(RelayCase):
    """A stalled TLS handshake can be cancelled, and times out like any other silence."""

    def silent(self):
        server = Silent()
        self.addCleanup(server.close)
        return server

    def start(self, relay, url):
        outcome = []

        def work():
            try:
                outcome.append(relay.run({"method": "GET", "url": url}))
            except BaseException as e:  # noqa: BLE001 - reported to the test
                outcome.append(e)

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        return thread, outcome

    def test_cancel_during_the_handshake_returns_quickly(self):
        server = self.silent()
        relay = self.relay([f"127.0.0.1:{server.port}"])
        thread, outcome = self.start(relay, f"https://127.0.0.1:{server.port}/")
        self.assertTrue(server.heard.wait(5))  # the client sent its hello and is now waiting for the server's
        time.sleep(0.1)
        started = time.monotonic()
        relay.cancel()
        thread.join(5)
        self.assertFalse(thread.is_alive(), "cancel could not interrupt the TLS handshake")
        self.assertLess(time.monotonic() - started, 2)
        self.assertIsInstance(outcome[0], Cancelled)

    def test_a_silent_server_times_out_during_the_handshake(self):
        server = self.silent()
        with self.assertRaises(Refused) as cm:
            self.get([f"127.0.0.1:{server.port}"], f"https://127.0.0.1:{server.port}/", Limits(idle_timeout=0.3))
        self.assertEqual(str(cm.exception), "timed out")

    def test_the_total_deadline_ends_a_stalled_handshake_too(self):
        server = self.silent()
        started = time.monotonic()
        with self.assertRaises(Refused) as cm:
            self.get([f"127.0.0.1:{server.port}"], f"https://127.0.0.1:{server.port}/", Limits(total_timeout=0.4))
        self.assertEqual(str(cm.exception), "timed out")
        self.assertLess(time.monotonic() - started, 5)

    def test_the_raw_socket_is_closed_when_the_handshake_fails(self):
        raw = mock.Mock(spec=socket.socket)
        wrapped = mock.Mock()
        wrapped.do_handshake.side_effect = ssl.SSLError(1, "[SSL] handshake failure")
        context = mock.Mock()
        context.wrap_socket.return_value = wrapped
        relay = self.relay(["api.example.com"])
        relay.tls_context = lambda: context
        with mock.patch.object(http_relay, "resolver", lambda *a, **k: [(2, 1, 6, "", ("8.8.8.8", 443))]), \
                mock.patch.object(http_relay, "connector", lambda *a, **k: raw):
            with self.assertRaisesRegex(Refused, "TLS error"):
                relay.run({"method": "GET", "url": "https://api.example.com/x"})
        self.assertEqual(context.wrap_socket.call_args.kwargs["do_handshake_on_connect"], False)
        wrapped.close.assert_called()
        raw.close.assert_called()


class Deadline(RelayCase):
    """R7 / spec gap: [http] total_timeout covers connect, headers and body together."""

    def test_a_drip_feed_that_is_never_idle_still_ends_at_the_total_timeout(self):
        def fn(h):
            h.send_response(200)
            h.send_header("Transfer-Encoding", "chunked")
            h.end_headers()
            for _ in range(600):
                h.wfile.write(b"1\r\nx\r\n")
                h.wfile.flush()
                time.sleep(0.05)

        server = self.serve(fn)
        started = time.monotonic()
        with self.assertRaises(Refused) as cm:
            self.get([f"http://127.0.0.1:{server.port}"], f"http://127.0.0.1:{server.port}/", Limits(total_timeout=0.6))
        self.assertEqual(str(cm.exception), "timed out")
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(self.logs[0].split(" -> ")[1], "refused: timed out\n")
        self.assertTrue(self.body().startswith(b"x"))  # what had arrived was delivered

    def test_response_headers_that_drip_in_end_at_the_total_timeout(self):
        def fn(h):
            h.wfile.write(b"HTTP/1.1 200 OK\r\n")
            for _ in range(600):
                h.wfile.write(b"X")
                time.sleep(0.05)

        server = self.serve(fn)
        started = time.monotonic()
        with self.assertRaises(Refused) as cm:
            self.get([f"http://127.0.0.1:{server.port}"], f"http://127.0.0.1:{server.port}/", Limits(total_timeout=0.6))
        self.assertEqual(str(cm.exception), "timed out")
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(self.events, [])

    def test_the_deadline_is_for_the_whole_request_redirects_included(self):
        def fn(h):
            time.sleep(0.25)
            send(h, 302, headers={"Location": "/"})

        server = self.serve(fn)
        with self.assertRaises(Refused) as cm:
            self.get([f"http://127.0.0.1:{server.port}"], f"http://127.0.0.1:{server.port}/", Limits(total_timeout=0.6))
        self.assertEqual(str(cm.exception), "timed out")
        self.assertLess(len(server.requests), 6)  # it stopped before the redirect limit

    def test_a_request_that_finishes_in_time_is_unaffected_and_leaves_no_timer_behind(self):
        server = self.serve(lambda h: send(h, 200, b"ok"))
        before = threading.active_count()
        self.assertEqual(self.get([f"http://127.0.0.1:{server.port}"], f"http://127.0.0.1:{server.port}/", Limits(total_timeout=30))["status"], 200)
        time.sleep(0.1)
        self.assertLessEqual(threading.active_count(), before + 1)  # the server's own handler thread may linger

    def test_connect_attempts_are_bounded_by_what_is_left_of_the_deadline(self):
        server = self.serve(lambda h: send(h))
        timeouts = []

        def connector(address, timeout=None):
            timeouts.append(timeout)
            return socket.create_connection(address, timeout=timeout)

        with mock.patch.object(http_relay, "connector", connector):
            self.get([f"http://127.0.0.1:{server.port}"], f"http://127.0.0.1:{server.port}/", Limits(idle_timeout=30, total_timeout=2))
        self.assertEqual(len(timeouts), 1)
        self.assertLessEqual(timeouts[0], 2)
        self.assertGreater(timeouts[0], 1)

    def test_a_connect_that_fails_slowly_does_not_try_more_addresses_after_the_deadline(self):
        attempts = []

        def connector(address, timeout=None):
            attempts.append(timeout)
            time.sleep(0.3)
            raise TimeoutError("timed out")

        info = [(2, 1, 6, "", (ip, 80)) for ip in ("8.8.8.8", "8.8.4.4", "1.1.1.1", "1.0.0.1")]
        with mock.patch.object(http_relay, "resolver", lambda *a, **k: info), mock.patch.object(http_relay, "connector", connector):
            with self.assertRaises(Refused) as cm:
                self.get(["http://x.example.com"], "http://x.example.com/", Limits(total_timeout=0.5))
        self.assertEqual(str(cm.exception), "timed out")
        self.assertLess(len(attempts), 4)


class Coalescing(RelayCase):
    """A server that sends a few bytes at a time must not cost one event per read."""

    def drip(self, pieces, pause):
        def fn(h):
            h.send_response(200)
            h.send_header("Transfer-Encoding", "chunked")
            h.end_headers()
            for piece in pieces:
                h.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece))
                h.wfile.flush()
                time.sleep(pause)
            h.wfile.write(b"0\r\n\r\n")

        return self.serve(fn)

    def test_small_reads_are_sent_as_fewer_events_with_the_same_bytes(self):
        pieces = [bytes([65 + i % 26]) for i in range(300)]
        server = self.drip(pieces, 0.002)
        self.get([f"http://127.0.0.1:{server.port}"], f"http://127.0.0.1:{server.port}/")
        self.assertEqual(self.body(), b"".join(pieces))
        chunks = [e for e in self.events if "chunk" in e]
        self.assertLess(len(chunks), 60, f"{len(chunks)} events for 300 one-byte reads")

    def test_a_slow_trickle_is_still_delivered_without_waiting_for_the_next_read(self):
        stamps = []

        def fn(h):
            h.send_response(200)
            h.send_header("Transfer-Encoding", "chunked")
            h.end_headers()
            for piece in (b"a", b"b"):
                h.wfile.write(b"1\r\n%s\r\n" % piece)
                h.wfile.flush()
                stamps.append((piece, time.monotonic()))
                time.sleep(0.01)
            time.sleep(1.5)  # idle with the connection open: what came must not be held back
            h.wfile.write(b"0\r\n\r\n")

        server = self.serve(fn)
        seen = []
        relay = Relay(parse_rules([f"http://127.0.0.1:{server.port}"]),
                      Limits(), lambda e: seen.append((e, time.monotonic())), self.logs.append)
        relay.run({"method": "GET", "url": f"http://127.0.0.1:{server.port}/"})
        got = b"".join(base64.b64decode(e["chunk"]) for e, _ in seen if "chunk" in e)
        self.assertEqual(got, b"ab")
        sent_b = dict(stamps)[b"b"]
        arrived_b = next(t for e, t in seen if "chunk" in e and base64.b64decode(e["chunk"]).endswith(b"b"))
        self.assertLess(arrived_b - sent_b, 0.6, "the last bytes waited for the server to say more")

    def test_a_chunk_is_never_larger_than_the_chunk_limit_and_big_bodies_stay_whole(self):
        server = self.serve(lambda h: send(h, 200, BODY))
        self.get([f"http://127.0.0.1:{server.port}"], f"http://127.0.0.1:{server.port}/", Limits(chunk=10_000))
        self.assertEqual(self.body(), BODY)
        self.assertTrue(all(len(base64.b64decode(e["chunk"])) <= 10_000 for e in self.events if "chunk" in e))

    def test_the_bytes_before_a_failure_are_delivered_first(self):
        server = self.drip([b"abc"] * 3, 0.3)
        with self.assertRaises(Refused):
            self.get([f"http://127.0.0.1:{server.port}"], f"http://127.0.0.1:{server.port}/", Limits(idle_timeout=0.1))
        self.assertTrue(self.body().startswith(b"abc"))


OPENSSL = shutil.which("openssl")


class Pki:
    """A throwaway CA and the leaf certificates it signs, made with the `openssl` command."""

    def __init__(self, directory: Path):
        self.dir = directory
        (directory / "ca.cnf").write_text(
            "[req]\ndistinguished_name = dn\nprompt = no\n[dn]\nCN = dock-test-ca\n"
            "[ext]\nbasicConstraints = critical,CA:TRUE\nkeyUsage = critical,keyCertSign,cRLSign\n"
            "subjectKeyIdentifier = hash\n")
        self.cas: dict[str, tuple[Path, Path]] = {}

    def run(self, *args):
        subprocess.run([OPENSSL, *args], cwd=self.dir, check=True, capture_output=True)

    def ca(self, name: str) -> Path:
        cert, key = self.dir / f"{name}.crt", self.dir / f"{name}.key"
        self.run("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(key))
        self.run("req", "-new", "-x509", "-key", str(key), "-out", str(cert), "-days", "2", "-config", "ca.cnf",
                 "-extensions", "ext")
        self.cas[name] = (cert, key)
        return cert

    def leaf(self, ca: str, dns: str) -> tuple[str, str]:
        base = f"{ca}-{dns}"
        key, csr, cert, conf = (self.dir / f"{base}.{x}" for x in ("key", "csr", "crt", "cnf"))
        conf.write_text(
            f"[req]\ndistinguished_name = dn\nprompt = no\n[dn]\nCN = {dns}\n"
            f"[ext]\nbasicConstraints = CA:FALSE\nkeyUsage = critical,digitalSignature\nextendedKeyUsage = serverAuth\n"
            f"subjectAltName = DNS:{dns}\nauthorityKeyIdentifier = keyid\nsubjectKeyIdentifier = hash\n")
        ca_cert, ca_key = self.cas[ca]
        self.run("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(key))
        self.run("req", "-new", "-key", str(key), "-out", str(csr), "-config", str(conf))
        self.run("x509", "-req", "-in", str(csr), "-CA", str(ca_cert), "-CAkey", str(ca_key), "-CAcreateserial",
                 "-out", str(cert), "-days", "2", "-extfile", str(conf), "-extensions", "ext")
        return str(cert), str(key)


@unittest.skipUnless(OPENSSL, "openssl is not installed")
class RealTls(RelayCase):
    """Real TLS against a local https server: dock verifies the certificate against the name, connects to the pinned IP."""

    NAME = "api.test.example"

    def setUp(self):
        super().setUp()
        directory = Path(tempfile.mkdtemp(prefix="dock-pki-"))
        self.addCleanup(shutil.rmtree, directory, True)
        self.pki = Pki(directory)
        self.trusted = self.pki.ca("trusted")
        self.other = self.pki.ca("other")

    def fetch(self, cert_key, trust=None):
        """GET https://NAME:<port>/hello from a server with `cert_key`, the name resolving to 127.0.0.1 only."""
        server = self.serve(lambda h: send(h, 200, b"secure hello"), tls=cert_key)

        def context():
            return ssl.create_default_context(cafile=str(trust or self.trusted))

        entries = [{"host": f"{self.NAME}:{server.port}", "private": True}]  # private: the name resolves to loopback
        info = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", server.port))]
        with mock.patch.object(http_relay, "tls_context", context), mock.patch.object(http_relay, "resolver", lambda *a, **k: info):
            try:
                return server, self.get(entries, f"https://{self.NAME}:{server.port}/hello"), None
            except Refused as e:
                return server, None, e

    def test_a_certificate_for_the_name_from_a_trusted_ca_is_accepted_with_the_name_as_sni(self):
        server, result, error = self.fetch(self.pki.leaf("trusted", self.NAME))
        self.assertIsNone(error)
        self.assertEqual(result, {"status": 200, "bytes": 12})
        self.assertEqual(self.body(), b"secure hello")
        self.assertEqual(server.sni, [self.NAME])
        self.assertEqual(server.requests[0][2]["host"], f"{self.NAME}:{server.port}")

    def test_a_certificate_from_an_untrusted_ca_is_refused_and_nothing_is_sent(self):
        server, result, error = self.fetch(self.pki.leaf("other", self.NAME))
        self.assertIsNone(result)
        self.assertRegex(str(error), r"^TLS certificate error: ")
        self.assertEqual(server.requests, [])
        self.assertEqual(self.events, [])

    def test_a_certificate_for_another_name_is_refused_and_nothing_is_sent(self):
        server, result, error = self.fetch(self.pki.leaf("trusted", "other.test.example"))
        self.assertIsNone(result)
        self.assertRegex(str(error), r"^TLS certificate error: ")
        self.assertEqual(server.requests, [])

    def test_the_log_line_says_the_certificate_was_the_reason(self):
        self.fetch(self.pki.leaf("trusted", "other.test.example"))
        self.assertIn("refused: TLS certificate error", self.logs[0])


class Footprint(RelayCase):
    """AC12 (memory part) / NFR4: streaming does not hold the body."""

    def test_streaming_50_mib_keeps_the_peak_allocation_small(self):
        block = b"\xa5" * 65536

        def fn(h):
            h.send_response(200)
            h.send_header("Content-Length", str(50 * 2**20))
            h.end_headers()
            for _ in range(800):
                h.wfile.write(block)

        server = self.serve(fn)
        digest, count = hashlib.sha256(), [0]

        def on_event(event):
            if "chunk" in event:
                data = base64.b64decode(event["chunk"])
                digest.update(data)
                count[0] += len(data)

        relay = Relay(parse_rules([f"http://127.0.0.1:{server.port}"]), Limits(), on_event, self.logs.append)
        tracemalloc.start()
        try:
            tracemalloc.reset_peak()
            relay.run({"method": "GET", "url": f"http://127.0.0.1:{server.port}/"})
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(count[0], 50 * 2**20)
        self.assertEqual(digest.hexdigest(), hashlib.sha256(block * 800).hexdigest())
        self.assertLess(peak, 8 * 2**20, f"peak {peak} bytes")


class Python311(unittest.TestCase):
    """pyproject says python >= 3.11: nothing in the sources may need a newer syntax (PEP 701 f-strings)."""

    @staticmethod
    def sources():
        listed = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "*.py"], cwd=ROOT, capture_output=True,
                                text=True, check=False).stdout.split()
        return [ROOT / name for name in listed] or [p for d in ("cli", "tests", "plugin") for p in (ROOT / d).rglob("*.py")]

    @staticmethod
    def reused_quotes(path: Path) -> list[int]:
        """Lines where a string inside an f-string expression uses the quote that delimits the f-string."""
        import tokenize
        if not hasattr(tokenize, "FSTRING_START"):
            return []  # before 3.12 the parser itself refuses such code
        found, outer = [], []
        with open(path, "rb") as f:
            for tok in tokenize.tokenize(f.readline):
                if tok.type == tokenize.FSTRING_START:
                    if outer and tok.string[-1] == outer[-1]:
                        found.append(tok.start[0])
                    outer.append(tok.string[-1])
                elif tok.type == tokenize.FSTRING_END:
                    outer.pop()
                elif tok.type == tokenize.STRING and outer and tok.string[-1] == outer[-1]:
                    found.append(tok.start[0])
        return found

    def test_no_f_string_expression_reuses_the_quote_of_its_f_string(self):
        for path in self.sources():
            with self.subTest(file=str(path.relative_to(ROOT))):
                self.assertEqual(self.reused_quotes(path), [])

    def test_the_check_sees_a_reused_quote(self):
        path = Path(tempfile.mkdtemp(prefix="dock-py311-"))
        self.addCleanup(shutil.rmtree, path, True)
        (path / "bad.py").write_text("d = {'a': 1}\nprint(f\"{d[\"a\"]}\")\n")
        if sys.version_info >= (3, 12):
            self.assertEqual(self.reused_quotes(path / "bad.py"), [2])


def sleeper(manifest_dir: Path) -> PluginProcess:
    """A started PluginProcess whose child never reads its stdin."""
    from cli import isolation
    manifest = Manifest.load(make_plugin('any = { cmd = "python3" }'))
    launch = isolation.Launch([sys.executable, "-c", "import time; time.sleep(300)"], dict(os.environ))
    proc = PluginProcess(manifest, manifest_dir / "plugin.log", {}, launch)
    proc.start()
    return proc


class Outbox(unittest.TestCase):
    """The hub only queues for a plugin; a plugin that reads nothing can block no thread but its own writer."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dock-outbox-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.proc = sleeper(self.tmp)
        self.addCleanup(self.proc.kill)
        self.big = {"chunk": "A" * 100_000}

    def test_a_bulk_sender_waits_for_a_plugin_that_reads_nothing_until_told_to_give_up(self):
        stop = threading.Event()
        outcome = []

        def work():
            try:
                for _ in range(200):
                    self.proc.notify("http_event", self.big, bulk=True, abort=stop.is_set)
            except BaseException as e:  # noqa: BLE001 - reported below
                outcome.append(e)

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        thread.join(1.0)
        self.assertTrue(thread.is_alive(), "the sender should be waiting for the plugin to read")
        stop.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcome[0], SendAborted)

    def flood(self, stop, count=100):
        """Bulk messages from another thread, until `stop` or the plugin is gone."""
        def work():
            try:
                for _ in range(count):
                    self.proc.notify("x", self.big, bulk=True, abort=stop.is_set)
            except PluginError:
                pass

        threading.Thread(target=work, daemon=True).start()

    def test_other_messages_are_queued_at_once_whatever_the_plugin_does(self):
        stop = threading.Event()
        self.addCleanup(stop.set)
        self.flood(stop)
        time.sleep(0.5)  # the pipe and the bulk allowance are full by now
        started = time.monotonic()
        for _ in range(50):
            self.proc.notify("cancel")
        self.assertLess(time.monotonic() - started, 1)

    def test_a_plugin_that_never_reads_gets_only_so_many_queued_messages(self):
        with self.assertRaisesRegex(PluginError, "not reading its input"):
            for _ in range(OUTBOX_CONTROL + 100):
                self.proc.notify("cancel", self.big)

    def test_a_plugin_that_never_reads_gets_only_so_many_queued_bytes(self):
        with self.assertRaisesRegex(PluginError, "not reading its input"):
            for _ in range(OUTBOX_CONTROL - 1):
                self.proc.notify("cancel", {"pad": "A" * 1_000_000})

    def test_messages_to_a_plugin_that_does_read_arrive_in_order(self):
        from cli import isolation
        manifest = Manifest.load(make_plugin('any = { cmd = "python3" }'))
        out = self.tmp / "received"
        script = f"import sys\nwith open({str(out)!r}, 'w') as f:\n    for line in sys.stdin:\n        f.write(line)\n"
        proc = PluginProcess(manifest, self.tmp / "p2.log", {}, isolation.Launch([sys.executable, "-c", script], dict(os.environ)))
        proc.start()
        self.addCleanup(proc.kill)
        for i in range(300):
            proc.notify("n", {"i": i}, bulk=i % 2 == 0, abort=lambda: False)
        proc.stop(timeout=5)
        got = [m["params"]["i"] for m in map(json.loads, out.read_text().splitlines()) if m["method"] == "n"]
        self.assertEqual(got, list(range(300)))

    def test_a_plugin_that_exited_makes_a_waiting_sender_fail_instead_of_wait(self):
        outcome = []

        def work():
            try:
                for _ in range(200):
                    self.proc.notify("http_event", self.big, bulk=True, abort=lambda: False)
            except BaseException as e:  # noqa: BLE001 - reported below
                outcome.append(e)

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        time.sleep(0.5)
        self.proc.proc.kill()
        thread.join(5)
        self.assertFalse(thread.is_alive(), "the sender is still waiting for a plugin that is gone")
        self.assertIsInstance(outcome[0], PluginError)
        self.assertNotIsInstance(outcome[0], SendAborted)

    def test_stop_kills_a_plugin_that_does_not_read_instead_of_waiting_for_it(self):
        stop = threading.Event()
        self.addCleanup(stop.set)
        self.flood(stop, OUTBOX_BULK + 1)
        time.sleep(0.5)
        started = time.monotonic()
        self.proc.stop(timeout=0.5)
        self.assertLess(time.monotonic() - started, 0.5 + 1.0 + 2)
        self.assertIsNotNone(self.proc.proc.poll())


class ErrorCodes(unittest.TestCase):
    """PluginError.code applies to every handler's error, so what a plugin sends as a code must be a number."""

    def proc(self):
        from cli import isolation
        manifest = Manifest.load(make_plugin('any = { cmd = "python3" }'))
        return PluginProcess(manifest, os.devnull, {}, isolation.Launch([sys.executable, "-c", "pass"], {}))

    def wait(self, error):
        proc = self.proc()
        proc.hub.replies[(proc, 1)] = {"id": 1, "error": error}
        with self.assertRaises(PluginError) as cm:
            proc.wait_for(1, "m", None)
        return cm.exception

    def test_a_code_that_is_not_an_integer_becomes_the_generic_error_code(self):
        for code in ("boom", 1.5, True, None, [1], {"a": 1}):
            with self.subTest(code=code):
                e = self.wait({"code": code, "message": "m"})
                self.assertEqual((e.code, str(e)), (-32000, "m"))

    def test_a_missing_code_and_an_error_that_is_not_an_object_become_the_generic_error_code(self):
        self.assertEqual(self.wait({"message": "m"}).code, -32000)
        self.assertEqual(self.wait("boom").code, -32000)
        self.assertEqual(self.wait({"code": -32000}).args, ("plugin error",))

    def test_integer_codes_are_kept(self):
        for code in (-32800, -32001, 7):
            with self.subTest(code=code):
                self.assertEqual(self.wait({"code": code, "message": "m"}).code, code)

    def test_a_handler_error_goes_to_the_caller_with_its_own_code_and_other_failures_with_the_generic_one(self):
        proc = self.proc()
        sent = []
        proc._send = sent.append

        def cancelled(params):
            raise PluginError("stopped", CANCELLED)

        def broken(params):
            raise RuntimeError("boom")

        proc.handlers.update(cancelled=cancelled, broken=broken)
        proc._handle_incoming({"method": "cancelled", "id": 1})
        proc._handle_incoming({"method": "broken", "id": 2})
        self.assertEqual(sent, [{"id": 1, "error": {"code": -32800, "message": "stopped"}},
                                {"id": 2, "error": {"code": -32000, "message": "boom"}}])

    def test_a_cancelled_provider_call_reaches_the_caller_as_cancelled(self):
        home = Path(tempfile.mkdtemp(prefix="dock-codes-home-"))
        self.addCleanup(shutil.rmtree, home, True)
        store = Store(home)
        for name in ("capcaller", "capprovider"):
            src = Path(tempfile.mkdtemp(prefix="dock-codes-src-")) / name
            shutil.copytree(FIXTURES / name, src, ignore=shutil.ignore_patterns("__pycache__"))
            self.addCleanup(shutil.rmtree, src.parent, True)
            store.install(src)
        session = Session(store)
        self.addCleanup(session.close)
        manifest = store.get("dev.dock.capcaller")
        caller = session.start(manifest, TerminalUI(manifest.id))
        sent = []
        caller._send = lambda msg, bulk=False, abort=None: sent.append(msg)  # what the host would write to the caller
        request = {"method": "call", "id": "c1", "params": {"capability": "probecap", "method": "run", "params": {}}}
        thread = threading.Thread(target=caller._handle_incoming, args=(request,), daemon=True)
        thread.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not any(p.manifest.id == "dev.dock.capprovider" for p in session.procs):
            time.sleep(0.05)
        time.sleep(0.5)  # the provider is now in its call
        session.cancel_all()
        thread.join(15)
        self.assertFalse(thread.is_alive())
        self.assertEqual([m for m in sent if "id" in m], [{"id": "c1", "error": {"code": CANCELLED, "message": "cancelled"}}])


class DockHttp(unittest.TestCase):
    """The real host with a native plugin: `python -m cli`, a throwaway DOCK_HOME, and a scrubbed environment."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dock-http-e2e-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        (self.tmp / "user").mkdir()
        self.secret = f"host-secret-{uuid.uuid4().hex}"
        self.servers: list[Server] = []

    def serve(self, fn) -> Server:
        server = Server(fn)
        self.servers.append(server)
        self.addCleanup(server.close)
        return server

    def env(self, **extra) -> dict:
        """Only what dock and python3 need, plus a host secret that must never reach a server or a log."""
        env = {"PATH": os.environ["PATH"], "HOME": str(self.tmp / "user"), "DOCK_HOME": str(self.home),
               "PYTHONPATH": str(ROOT), "DOCK_HTTP_SECRET": self.secret}
        env.update({k: os.environ[k] for k in ("LANG", "SYSTEMROOT", "TMPDIR") if k in os.environ})
        return {**env, **extra}

    def dock(self, *args, timeout=60, env=None):
        return subprocess.run([sys.executable, "-m", "cli", *args], capture_output=True, text=True, check=False,
                              env=env or self.env(), cwd=self.tmp / "user", timeout=timeout)

    def install(self, *entries, config=None, tables=None):
        src = self.tmp / "plugin"
        shutil.copytree(FIXTURES / "httpnative", src, ignore=shutil.ignore_patterns("__pycache__"))
        if entries or tables:
            listed = [json.dumps(e) for e in entries] + list(tables or [])
            with open(src / "plugin.toml", "a") as f:
                f.write(f"\n[capabilities]\nhttp = [{', '.join(listed)}]\n")
        if config:
            self.home.mkdir(exist_ok=True)
            (self.home / "config.toml").write_text(config)
        r = self.dock("plugin", "install", str(src))
        self.assertEqual(r.returncode, 0, r.stderr)

    def log(self) -> str:
        return (self.home / "logs" / "dev.dock.httpnative.log").read_text()

    def out(self, r) -> list[str]:
        return r.stdout.splitlines()


class Relayed(DockHttp):
    """AC2, AC3, AC9 end to end: the plugin's `http` request through the hub."""

    def test_a_get_returns_the_status_and_the_body(self):
        server = self.serve(lambda h: send(h, 200, b"hello from the server"))
        self.install(f"http://127.0.0.1:{server.port}")
        r = self.dock("fetch", f"http://127.0.0.1:{server.port}/greeting")
        self.assertEqual((r.returncode, self.out(r)), (0, ["status:200", "body:hello from the server"]), r.stderr)
        self.assertEqual(server.requests[0][:2], ("GET", "/greeting"))

    def test_a_200_kib_body_arrives_whole(self):
        server = self.serve(lambda h: send(h, 200, BODY))
        self.install(f"http://127.0.0.1:{server.port}")
        r = self.dock("fetch", f"http://127.0.0.1:{server.port}/", "--digest")
        self.assertEqual(self.out(r), ["status:200", "bytes:204800", f"sha256:{hashlib.sha256(BODY).hexdigest()}"], r.stderr)

    def test_a_post_with_a_body_and_a_header_reaches_the_server_intact(self):
        server = self.serve(lambda h: send(h, 201, b"made"))
        self.install(f"http://127.0.0.1:{server.port}")
        r = self.dock("fetch", f"http://127.0.0.1:{server.port}/items", "--method", "POST", "--body", "name=ünï&x=1", "--header", "X-Token: abc")
        self.assertEqual(self.out(r), ["status:201", "body:made"], r.stderr)
        method, path, headers, body = server.requests[0]
        self.assertEqual((method, path, body, headers["x-token"]), ("POST", "/items", "name=ünï&x=1".encode(), "abc"))

    def test_an_undeclared_host_is_refused_with_the_code_and_nothing_is_sent(self):
        server = self.serve(lambda h: send(h, 200, b"secret"))
        self.install(f"http://127.0.0.1:{server.port}")
        r = self.dock("fetch", f"http://localhost:{server.port}/")
        self.assertEqual(self.out(r), [f"error:-32001:host 'localhost' is not declared in [capabilities].http"], r.stderr)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(server.requests, [])

    def test_a_plugin_without_the_capability_is_refused_for_every_request(self):
        server = self.serve(lambda h: send(h, 200, b"secret"))
        self.install()
        r = self.dock("fetch", f"http://127.0.0.1:{server.port}/")
        self.assertEqual(len(self.out(r)), 1)
        self.assertTrue(self.out(r)[0].startswith("error:-32001:"), r.stdout)
        self.assertIn("[capabilities].http", r.stdout)
        self.assertEqual(server.requests, [])
        self.assertIn(f"http GET http://127.0.0.1:{server.port}/ -> refused: ", self.log())

    def test_a_redirect_to_an_undeclared_host_gives_the_plugin_nothing(self):
        other = self.serve(lambda h: send(h, 200, b"secret"))
        server = self.serve(lambda h: send(h, 302, b"redirect body", {"Location": f"http://localhost:{other.port}/"}))
        self.install(f"http://127.0.0.1:{server.port}")
        r = self.dock("fetch", f"http://127.0.0.1:{server.port}/")
        self.assertEqual(self.out(r), ["error:-32001:host 'localhost' is not declared in [capabilities].http"], r.stderr)
        self.assertEqual(other.requests, [])

    def test_the_response_cap_comes_from_config_toml(self):
        server = self.serve(lambda h: send(h, 200, b"x" * (2 * 2**20)))
        self.install(f"http://127.0.0.1:{server.port}", config="[http]\nmax_response_bytes = 1048576\n")
        r = self.dock("fetch", f"http://127.0.0.1:{server.port}/", "--digest")
        self.assertEqual(self.out(r), ["error:-32001:response larger than 1048576 bytes"], r.stderr)

    def test_a_broken_http_table_fails_the_request_naming_the_key(self):
        server = self.serve(lambda h: send(h, 200, b"x"))
        self.install(f"http://127.0.0.1:{server.port}", config="[http]\nmax_respons_bytes = 5\n")
        r = self.dock("fetch", f"http://127.0.0.1:{server.port}/")
        self.assertIn("max_respons_bytes", r.stdout)
        self.assertEqual(server.requests, [])

    def test_a_silent_server_ends_the_request_with_timed_out(self):
        release = threading.Event()
        self.addCleanup(release.set)
        server = self.serve(lambda h: release.wait(10))
        self.install(f"http://127.0.0.1:{server.port}")
        r = self.dock("fetch", f"http://127.0.0.1:{server.port}/", env=self.env(DOCK_TEST_HTTP_IDLE_TIMEOUT="0.5"))
        self.assertEqual(self.out(r), ["error:-32001:timed out"], r.stderr)

    def test_a_second_request_while_one_is_in_flight_is_refused_and_the_first_still_completes(self):
        server = self.serve(lambda h: (time.sleep(0.6), send(h, 200, b"ok")))
        self.install(f"http://127.0.0.1:{server.port}")
        url = f"http://127.0.0.1:{server.port}/"
        r = self.dock("fetch2", url, url)
        self.assertEqual(self.out(r), ["a:status:200 bytes:2", "b:error:-32001:one http request at a time"], r.stderr)
        self.assertEqual(len(server.requests), 1)

    def test_requests_one_after_the_other_are_fine(self):
        server = self.serve(lambda h: send(h, 200, b"ok"))
        self.install(f"http://127.0.0.1:{server.port}")
        url = f"http://127.0.0.1:{server.port}/"
        r = self.dock("fetch2", url, url, "--sequential")
        self.assertEqual(self.out(r), ["a:status:200 bytes:2", "b:status:200 bytes:2"], r.stderr)

    def test_ctrl_c_during_a_slow_stream_ends_the_request_with_cancelled_and_closes_the_connection(self):
        first = threading.Event()

        def fn(h):
            h.send_response(200)
            h.send_header("Transfer-Encoding", "chunked")
            h.end_headers()
            for _ in range(400):
                h.wfile.write(b"4\r\ndata\r\n")
                h.wfile.flush()
                first.set()
                time.sleep(0.05)

        server = self.serve(fn)
        self.install(f"http://127.0.0.1:{server.port}")
        p = subprocess.Popen([sys.executable, "-m", "cli", "fetch", f"http://127.0.0.1:{server.port}/"], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env(), cwd=self.tmp / "user")
        self.addCleanup(p.kill)
        self.assertTrue(first.wait(15))
        time.sleep(0.3)
        p.send_signal(signal.SIGINT)
        out, err = p.communicate(timeout=20)
        self.assertEqual(p.returncode, 130, err)
        self.assertEqual(out.splitlines(), ["error:-32800:cancelled"], err)
        self.assertTrue(server.gone.wait(10), "the server never saw the connection close")
        self.assertIn("-> cancelled", self.log())


class InfoAndLog(DockHttp):
    """AC9 / R10, NFR2."""

    def test_plugin_info_shows_the_declared_entries_marking_private_ones(self):
        self.install("api.example.com", "http://localhost:8080", "*.tiles.example", tables=['{ host = "lan.example", private = true }'])
        r = self.dock("plugin", "info", "dev.dock.httpnative")
        self.assertEqual(r.returncode, 0, r.stderr)
        line = next(l for l in self.out(r) if l.startswith("http:"))
        self.assertEqual(line, "http:        api.example.com, http://localhost:8080 (private), *.tiles.example, lan.example (private)")

    def test_plugin_info_shows_a_dash_without_entries(self):
        self.install()
        line = next(l for l in self.out(self.dock("plugin", "info", "dev.dock.httpnative")) if l.startswith("http:"))
        self.assertEqual(line, "http:        -")

    def test_every_request_is_one_log_line_without_the_query_and_no_host_secret_reaches_the_server_or_the_log(self):
        server = self.serve(lambda h: send(h, 200, b"ok"))
        self.install(f"http://127.0.0.1:{server.port}")
        base = f"http://127.0.0.1:{server.port}"
        self.dock("fetch", f"{base}/one?token=QUERYSECRET", "--header", "X-Plugin: mine")
        self.dock("fetch", f"http://localhost:{server.port}/denied?token=QUERYSECRET")
        lines = [l for l in self.log().splitlines() if l.startswith("http ")]
        self.assertEqual(lines, [f"http GET {base}/one -> 200",
                                 f"http GET http://localhost:{server.port}/denied -> refused: host 'localhost' is not declared in [capabilities].http"])
        self.assertNotIn("QUERYSECRET", self.log())
        for _, _, headers, body in server.requests:
            self.assertNotIn(self.secret, json.dumps(headers) + body.decode())
            self.assertEqual(set(headers), {"host", "accept-encoding", "connection", "x-plugin"})
        self.assertNotIn(self.secret, self.log())

    def test_a_host_secret_in_the_plugins_environment_is_not_added_to_requests(self):
        # The native plugin inherits dock's environment, DOCK_HTTP_SECRET included; dock itself adds nothing of it.
        server = self.serve(lambda h: send(h, 200, b"ok"))
        self.install(f"http://127.0.0.1:{server.port}")
        self.dock("fetch", f"http://127.0.0.1:{server.port}/")
        self.assertNotIn(self.secret, json.dumps(server.requests[0][2]))


class SessionWiring(DockHttp):
    """R8, edge cases: what the host does with a running relay when it cancels or stops the plugin."""

    def start(self):
        self.install("http://127.0.0.1:1")
        store = Store(self.home)
        session = Session(store)
        self.addCleanup(session.close)
        manifest = store.get("dev.dock.httpnative")
        return session, session.start(manifest, TerminalUI(manifest.id))

    def test_cancel_all_cancels_the_relay_of_a_plugin(self):
        session, proc = self.start()
        relay = mock.Mock()
        proc.http_active = relay
        session.cancel_all()
        relay.cancel.assert_called_once_with()

    def test_stopping_a_plugin_cancels_its_relay(self):
        session, proc = self.start()
        relay = mock.Mock()
        proc.http_active = relay
        session.stop_proc(proc)
        relay.cancel.assert_called_once_with()

    def test_the_relay_is_marked_active_only_when_its_thread_starts_and_unmarked_if_that_fails(self):
        session, proc = self.start()
        seen = []

        class Failing:
            def __init__(self, *args, **kwargs):
                seen.append(("built", proc.http_active))

            def start(self):
                seen.append(("starting", proc.http_active is not None))
                raise RuntimeError("can't start new thread")

        with mock.patch("cli.lifecycle.threading.Thread", Failing):
            with self.assertRaisesRegex(RuntimeError, "can't start"):
                session._http(proc, {"method": "GET", "url": "http://127.0.0.1:1/"}, 1)
        self.assertEqual(seen, [("built", None), ("starting", True)])
        self.assertIsNone(proc.http_active)

    def test_a_plugin_without_a_request_in_flight_is_stopped_as_before(self):
        session, proc = self.start()
        self.assertIsNone(proc.http_active)
        session.stop_proc(proc)
        self.assertTrue(proc.forgotten)

    def test_a_plugin_that_dies_mid_request_makes_dock_close_the_connection(self):
        first = threading.Event()

        def fn(h):
            h.send_response(200)
            h.send_header("Transfer-Encoding", "chunked")
            h.end_headers()
            for _ in range(400):
                h.wfile.write(b"4\r\ndata\r\n")
                h.wfile.flush()
                first.set()
                time.sleep(0.05)

        server = self.serve(fn)
        self.install(f"http://127.0.0.1:{server.port}")
        store = Store(self.home)
        session = Session(store)
        self.addCleanup(session.close)
        manifest = store.get("dev.dock.httpnative")
        proc = session.start(manifest, TerminalUI(manifest.id))
        proc.send_request("invoke", {"command": "fetch", "args": [], "cwd": str(self.tmp), "inputs": {
            "url": f"http://127.0.0.1:{server.port}/", "method": "GET", "digest": True}})
        deadline = time.monotonic() + 15
        while not first.is_set() and time.monotonic() < deadline:
            session.hub.pump(0.1)  # the host serves the plugin's `http` request
        self.assertTrue(first.is_set())
        proc.proc.kill()
        self.assertTrue(server.gone.wait(10), "the connection stayed open after the plugin died")
        deadline = time.monotonic() + 5
        while proc.http_active is not None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIsNone(proc.http_active)


class DeafPlugin(DockHttp):
    """A plugin that sends `http` and then never reads stdin must not be able to freeze the host (Ctrl-C included)."""

    def setUp(self):
        super().setUp()
        block = b"\x5a" * 65536
        self.sending = threading.Event()

        def fn(h):
            h.send_response(200)
            h.send_header("Content-Length", str(400 * len(block)))  # 25 MiB: far more than a pipe holds
            h.end_headers()
            self.sending.set()
            for _ in range(400):
                h.wfile.write(block)

        self.server = self.serve(fn)
        self.install(f"http://127.0.0.1:{self.server.port}")
        store = Store(self.home)
        self.session = Session(store)
        manifest = store.get("dev.dock.httpnative")
        self.proc = self.session.start(manifest, TerminalUI(manifest.id))
        self.addCleanup(self.proc.kill)  # last resort: never leave the deaf child behind
        self.proc.send_request("invoke", {"command": "fetch_deaf", "args": [], "cwd": str(self.tmp), "inputs": {
            "url": f"http://127.0.0.1:{self.server.port}/"}})
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not (self.sending.is_set() and self.proc.http_active is not None):
            self.session.hub.pump(0.05)
        self.assertTrue(self.sending.is_set())
        end = time.monotonic() + 1.0  # let the relay fill the pipe and the plugin's backlog
        while time.monotonic() < end:
            self.session.hub.pump(0.05)

    def within(self, seconds, fn, what):
        """Run fn on a thread; fail (instead of hanging the suite) when it takes longer than `seconds`."""
        outcome = []

        def work():
            try:
                fn()
            except BaseException as e:  # noqa: BLE001 - reported below
                outcome.append(e)

        thread = threading.Thread(target=work, daemon=True)
        started = time.monotonic()
        thread.start()
        thread.join(seconds)
        self.assertFalse(thread.is_alive(), f"{what} did not finish within {seconds}s: the host is frozen")
        self.assertEqual(outcome, [])
        return time.monotonic() - started

    def test_cancel_all_returns_at_once_although_the_plugin_reads_nothing(self):
        self.within(2, self.session.cancel_all, "cancel_all")

    def test_ctrl_c_ends_with_the_deaf_plugin_killed_within_the_grace_period(self):
        with mock.patch("cli.lifecycle.CANCEL_GRACE", 0.5):
            took = self.within(6, lambda: self.session._interrupted(TerminalUI("dev.dock.httpnative")), "the Ctrl-C handling")
        self.assertLess(took, 0.5 + 2)
        self.assertTrue(self.proc.forgotten)
        self.assertIsNotNone(self.proc.proc.poll())

    def test_stopping_the_plugin_ends_within_its_grace_period(self):
        self.session._abort_http(self.proc)
        took = self.within(8, lambda: self.proc.stop(timeout=0.5), "stop")
        self.assertLess(took, 0.5 * 2 + 2)
        self.assertIsNotNone(self.proc.proc.poll())

    def test_a_reply_to_the_deaf_plugin_does_not_block_the_hub(self):
        self.within(2, lambda: [self.proc.notify("ping") for _ in range(20)], "a few notifications")

    def test_the_relay_stops_when_the_plugin_is_cancelled_even_though_its_output_was_stuck(self):
        self.session.cancel_all()
        deadline = time.monotonic() + 5
        while self.proc.http_active is not None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIsNone(self.proc.http_active)
        self.assertTrue(self.server.gone.wait(5), "the connection stayed open")


class Throughput(DockHttp):
    """AC12 / NFR4: 50 MiB through a native plugin in under 10 s (the memory part is `Footprint`)."""

    def test_50_mib_streams_through_dock_in_under_10_seconds_with_the_right_bytes(self):
        block = bytes(range(256)) * 256  # 64 KiB

        def fn(h):
            h.send_response(200)
            h.send_header("Content-Length", str(800 * len(block)))
            h.end_headers()
            for _ in range(800):
                h.wfile.write(block)

        server = self.serve(fn)
        self.install(f"http://127.0.0.1:{server.port}")
        started = time.monotonic()
        r = self.dock("fetch", f"http://127.0.0.1:{server.port}/big", "--digest", timeout=60)
        elapsed = time.monotonic() - started
        self.assertEqual(self.out(r), ["status:200", f"bytes:{50 * 2**20}", f"sha256:{hashlib.sha256(block * 800).hexdigest()}"], r.stderr)
        self.assertLess(elapsed, 10, f"took {elapsed:.1f}s")


if __name__ == "__main__":
    unittest.main()
