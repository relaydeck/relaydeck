"""tests/docker/conftest.py — bucket-aware gating for Docker tests.

Every test under `tests/docker/` carries the
`docker` marker (auto-applied below) so a host `pytest` leaves them alone,
and any test that needs a specific permutation declares it with
`@pytest.mark.bucket("zero", "all")`. At collection time we read the
`RELAYDECK_TEST_BUCKET` env var (set by `docker run -e`) and skip any test
whose declared buckets don't include it. No env var → only bucket-agnostic
tests run.

This sidesteps pytest's marker grammar, which can't express
`bucket('zero')` in a `-m` expression. The env var also composes cleanly
with `docker run` and stays out of pytest's CLI surface.
"""
from __future__ import annotations

import os

import pytest

# Set by `docker run -e RELAYDECK_TEST_BUCKET=zero`. When None, only
# bucket-agnostic tests run (anything without a bucket marker).
ACTIVE_BUCKET = os.environ.get("RELAYDECK_TEST_BUCKET")


def _bucket_arg_set(marker: pytest.Mark) -> set[str]:
    """Flatten `@pytest.mark.bucket("a", "b")` args to a set of strings."""
    return {str(a) for a in marker.args}


_DOCKER_DIR = os.path.dirname(os.path.abspath(__file__))


def _is_docker_test(item) -> bool:
    """True for items collected from tests/docker/ (or any subdirectory)."""
    return str(item.fspath).startswith(_DOCKER_DIR + os.sep)


def pytest_collection_modifyitems(config, items):  # noqa: D401
    """Auto-apply `docker` to tests in this dir + skip non-matching buckets.

    pytest invokes every `pytest_collection_modifyitems` hook with the FULL
    collected item list, not just items relative to this conftest's
    directory. So we must guard explicitly on fspath — otherwise the
    `docker` marker gets pasted onto every test in the repo and the host
    `-m "not docker"` filter excludes everything.
    """
    for item in items:
        if not _is_docker_test(item):
            continue

        # All tests under `tests/docker/` inherit the `docker` marker.
        item.add_marker(pytest.mark.docker)

        # Bucket gating. Tests without any `bucket()` marker are bucket-agnostic
        # and always run inside Docker; tests with one or more `bucket()` markers
        # run only when ACTIVE_BUCKET matches at least one declared name.
        bucket_markers = list(item.iter_markers("bucket"))
        if not bucket_markers:
            continue
        declared: set[str] = set()
        for m in bucket_markers:
            declared.update(_bucket_arg_set(m))
        if ACTIVE_BUCKET is None:
            item.add_marker(
                pytest.mark.skip(
                    reason=(
                        f"declares bucket(s) {sorted(declared)} but "
                        f"RELAYDECK_TEST_BUCKET is unset"
                    )
                )
            )
        elif ACTIVE_BUCKET not in declared:
            item.add_marker(
                pytest.mark.skip(
                    reason=(
                        f"bucket {ACTIVE_BUCKET!r} not in declared "
                        f"{sorted(declared)}"
                    )
                )
            )


@pytest.fixture(scope="session")
def active_bucket() -> str | None:
    """The current RELAYDECK_TEST_BUCKET (or None when running ungated)."""
    return ACTIVE_BUCKET


@pytest.fixture(scope="session")
def in_docker() -> bool:
    """True when running inside a container. Detection via /.dockerenv +
    cgroup probe — neither is bulletproof on every runtime (containerd /
    podman / CI runners differ), so docker tests should mark themselves with
    `@pytest.mark.docker` rather than dynamically branch on this."""
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup", "rb") as fh:
            return any(b in fh.read() for b in (b"docker", b"containerd", b"kubepods"))
    except OSError:
        return False
