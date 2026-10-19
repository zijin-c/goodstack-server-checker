import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from goodstack_batch_verify import ProxyRotator, run_parallel


class ParallelWorkerTests(unittest.TestCase):
    def test_workers_run_concurrently_and_return_every_result(self):
        barrier = threading.Barrier(3)
        active_lock = threading.Lock()
        active = 0
        max_active = 0

        @contextmanager
        def worker_context():
            def process(target):
                nonlocal active, max_active
                with active_lock:
                    active += 1
                    max_active = max(max_active, active)
                barrier.wait(timeout=3)
                with active_lock:
                    active -= 1
                return {"inputKey": target["inputKey"], "status": "ok"}

            yield process

        results = []
        targets = [{"inputKey": str(index)} for index in range(6)]
        run_parallel(targets, 3, worker_context, results.append)

        self.assertEqual(len(results), 6)
        self.assertEqual({row["inputKey"] for row in results}, {str(index) for index in range(6)})
        self.assertEqual(max_active, 3)


class ProxyRotatorTests(unittest.TestCase):
    def test_dashboard_proxy_rotation_and_disable(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "proxy_settings.json"
            config.write_text(json.dumps({
                "enabled": True,
                "proxies": ["http://alice:secret@proxy-one.example:8001", "proxy-two.example:8002"],
            }), encoding="utf-8")
            clean_env = {"DATA_DIR": directory}
            with patch.dict(os.environ, clean_env, clear=False):
                for key in ("GOODSTACK_PROXY_URL", "GOODSTACK_PROXY_LIST_URL", "GOODSTACK_PROXY_TOKEN"):
                    os.environ.pop(key, None)
                rotator = ProxyRotator()
                first = rotator.next_proxy()
                second = rotator.next_proxy()
                self.assertEqual(first["server"], "http://proxy-one.example:8001")
                self.assertEqual(first["username"], "alice")
                self.assertEqual(first["password"], "secret")
                self.assertEqual(second, {"server": "http://proxy-two.example:8002"})

                time.sleep(0.002)
                config.write_text(json.dumps({"enabled": False, "proxies": ["http://proxy-one.example:8001"]}), encoding="utf-8")
                self.assertIsNone(rotator.next_proxy())

    def test_legacy_host_port_user_password_format(self):
        normalised = ProxyRotator.normalise_proxy("proxy.example:8080:alice:p@ss")
        self.assertEqual(normalised, "http://alice:p%40ss@proxy.example:8080")


if __name__ == "__main__":
    unittest.main()
