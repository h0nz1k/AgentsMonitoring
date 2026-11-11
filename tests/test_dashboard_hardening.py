"""Audit 2026-09-08: the dashboard must not depend on a third-party script and must refuse to
listen off-loopback without auth."""
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from agentsmon import dashboard  # noqa: E402


class Hardening(unittest.TestCase):
    def test_page_loads_static_css_from_package(self):
        self.assertIn('href="/static/dashboard.css"', dashboard.PAGE)
        self.assertNotIn('src="/static/tailwind.js"', dashboard.PAGE)
        self.assertNotIn("cdn.tailwindcss.com", dashboard.PAGE)
        css, ctype = dashboard._STATIC_ASSETS["/static/dashboard.css"]
        self.assertIn(b"tailwindcss v3.4.17", css)
        self.assertEqual(ctype, "text/css; charset=utf-8")

    def test_off_loopback_without_auth_refuses_to_start(self):
        with self.assertRaises(SystemExit):
            dashboard.require_auth_or_die("100.64.0.10", {"dashboard": {}})
        with self.assertRaises(SystemExit):
            dashboard.require_auth_or_die("0.0.0.0", {"dashboard": {"auth": {"user": "x"}}})

    def test_loopback_or_configured_auth_is_fine(self):
        dashboard.require_auth_or_die("127.0.0.1", {"dashboard": {}})
        dashboard.require_auth_or_die("100.64.0.10", {"dashboard": {"auth": {"user": "admin", "pwhash": "ab" * 32}}})

    def test_explicit_lan_exception_requires_private_ipv4_and_boolean(self):
        cfg = {"dashboard": {"allow_unauthenticated_lan": True}}
        for host in ("192.168.1.153", "10.1.2.3", "172.16.0.1"):
            dashboard.require_auth_or_die(host, cfg)
        for host in ("0.0.0.0", "::", "8.8.8.8", "100.64.0.10", "169.254.1.2", "lan.local", "172.32.0.1"):
            with self.subTest(host=host), self.assertRaises(SystemExit):
                dashboard.require_auth_or_die(host, cfg)
        for flag in (False, "true", 1, None):
            with self.subTest(flag=flag), self.assertRaises(SystemExit):
                dashboard.require_auth_or_die("192.168.1.153", {"dashboard": {"allow_unauthenticated_lan": flag}})


if __name__ == "__main__":
    unittest.main()
