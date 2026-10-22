"""Helpers for pulling disposable recovery-test images."""
import time


def pull_fixture_image(tag, docker, sleep=time.sleep, attempts=3):
    """Retry public registry pulls, which can fail transiently in CI."""
    for attempt in range(attempts):
        try:
            return docker(['pull', tag], timeout=900)
        except Exception as exc:
            if attempt + 1 == attempts:
                raise RuntimeError(f'Unable to pull recovery fixture image after {attempts} attempts: {tag}') from exc
            sleep(2 ** (attempt + 1))
