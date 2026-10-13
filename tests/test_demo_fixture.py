import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


def test_protected_harness_fails_bundled_bug_and_passes_curated_repair(tmp_path):
    """Trusted, author-supplied fixtures only. This is not a Docker/local-model demo."""
    if not shutil.which('node'):
        pytest.skip('Node required for bundled fixture verification')
    root=Path(__file__).resolve().parent.parent
    original=(root/'demo/cart-app/cart.js').read_text()
    (tmp_path/'cart.js').write_text(original)
    def run():
        result=subprocess.run(['node',str(root/'verification/cart-check.cjs')],env={**os.environ,'GHOSTDEV_TEST_WORKDIR':str(tmp_path)},capture_output=True,text=True,timeout=15)
        return result,json.loads(result.stdout.splitlines()[-1])
    before,report=run()
    assert before.returncode==1 and report['tests']==3 and report['passed']==1
    # The known-good fixture is part of testing only; production repairs come from the local model.
    (tmp_path/'cart.js').write_text(original.replace('return price;','return price * quantity;'))
    after,report=run()
    assert after.returncode==0 and report['tests']==report['passed']==3
