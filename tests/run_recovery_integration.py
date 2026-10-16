"""CI gate: all five real-engine cases must execute; dependency skips fail."""
import os
from pathlib import Path
import unittest

def require_matrix(result):
    if result.testsRun != 5 or result.skipped or not result.wasSuccessful():
        raise RuntimeError('expected five real recovery integration cases, all passing without skips')

if __name__=='__main__':
    if os.environ.get('HOMELAB_RECOVERY_INTEGRATION')!='1': raise SystemExit('explicit integration flag required')
    suite=unittest.TestLoader().discover(str(Path(__file__).parent),'test_recovery_*integration.py')
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    require_matrix(result)
