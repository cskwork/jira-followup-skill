"""unittest discover의 안전망 걸이. 이름이 가장 앞서 다른 시험 모듈보다 먼저 import된다(_harness.py 설명)."""
import sys
import unittest

# 이 모듈보다 먼저 import된 시험 모듈(test_000_harness와 같은 이름 순 앞자리). 있으면 보호 없이 import된 것이다.
EARLIER = sorted(name for name in sys.modules if name.startswith("test_") and name != __name__)
# pytest는 conftest.py가 먼저 _harness를 import한다. 그때는 순서가 상관없다.
ALREADY = "_harness" in sys.modules

import _harness  # noqa: E402,F401  (import 자체가 보호를 켠다)


class HarnessLoadsFirst(unittest.TestCase):
    def test_no_test_module_was_imported_before_the_harness(self):
        self.assertTrue(ALREADY or not EARLIER, f"안전망보다 먼저 import된 시험 모듈: {EARLIER}")


if __name__ == "__main__":
    unittest.main()
