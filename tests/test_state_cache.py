"""/api/state cache: fresh hits, background refresh when aging, sync build when cold, invalidation."""
import threading
import time
import unittest
from unittest.mock import patch

from agentsmon import dashboard


class StateCache(unittest.TestCase):
    def setUp(self):
        dashboard.invalidate_state_cache()
        dashboard._state_building = False
        self.addCleanup(dashboard.invalidate_state_cache)

    def test_cold_builds_synchronously_then_serves_cache(self):
        with patch.object(dashboard, "_state", side_effect=[b"1", b"2"]) as st:
            self.assertEqual(dashboard._cached_state(), b"1")
            self.assertEqual(dashboard._cached_state(), b"1")
            self.assertEqual(st.call_count, 1)

    def test_aging_cache_served_while_rebuilding_in_background(self):
        started = threading.Event()
        release = threading.Event()

        def slow():
            started.set()
            release.wait(2)
            return b"new"
        dashboard._state_cache = (time.monotonic() - dashboard.STATE_FRESH_SECONDS - 1, b"old")
        with patch.object(dashboard, "_state", side_effect=slow):
            t0 = time.monotonic()
            self.assertEqual(dashboard._cached_state(), b"old")
            self.assertLess(time.monotonic() - t0, 0.5, "aging cache must not block the request")
            self.assertTrue(started.wait(2))
            self.assertEqual(dashboard._cached_state(), b"old")   # only one rebuild at a time
            release.set()
            deadline = time.monotonic() + 2
            while dashboard._state_building and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertEqual(dashboard._state_cache[1], b"new")

    def test_too_old_cache_is_not_served(self):
        dashboard._state_cache = (time.monotonic() - dashboard.STATE_MAX_AGE_SECONDS - 1, b"ancient")
        with patch.object(dashboard, "_state", return_value=b"now"):
            self.assertEqual(dashboard._cached_state(), b"now")

    def test_invalidate_forces_rebuild(self):
        dashboard._state_cache = (time.monotonic(), b"pre-action")
        dashboard.invalidate_state_cache()
        with patch.object(dashboard, "_state", return_value=b"post-action"):
            self.assertEqual(dashboard._cached_state(), b"post-action")

    def test_failed_build_releases_flag(self):
        with patch.object(dashboard, "_state", side_effect=RuntimeError("x")):
            with self.assertRaises(RuntimeError):
                dashboard._cached_state()
        self.assertFalse(dashboard._state_building)


if __name__ == "__main__":
    unittest.main()
