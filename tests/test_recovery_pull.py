import unittest
import recovery_pull


class RecoveryImagePullRetry(unittest.TestCase):
    def test_pull_retries_transient_registry_errors(self):
        calls = []
        delays = []

        def docker(args, timeout):
            calls.append((args, timeout))
            if len(calls) < 3:
                raise RuntimeError('temporary registry error')
            return b'image pulled'

        result = recovery_pull.pull_fixture_image('example/image:tag', docker, delays.append)

        self.assertEqual(result, b'image pulled')
        self.assertEqual(len(calls), 3)
        self.assertEqual(delays, [2, 4])

    def test_pull_reports_failure_after_final_attempt(self):
        calls = []

        def docker(args, timeout):
            calls.append(args)
            raise RuntimeError('registry unavailable')

        with self.assertRaisesRegex(RuntimeError, 'after 3 attempts'):
            recovery_pull.pull_fixture_image('example/image:tag', docker, lambda _: None)

        self.assertEqual(len(calls), 3)
