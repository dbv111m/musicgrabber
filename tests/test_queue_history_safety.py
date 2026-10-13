"""Regression checks for safeguards around persistent Queue history."""

import pytest

from conftest import QueueHistorySafeSession


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:38274/api/jobs/cleanup",
        "http://localhost:38274/api/jobs/cleanup?confirm=true",
        "http://localhost:38274/musicgrabber/api/stats?confirm=true",
    ],
)
def test_test_client_blocks_history_deletion_without_network(url):
    session = QueueHistorySafeSession(allow_history_deletion=False)

    with pytest.raises(RuntimeError, match="Queue history"):
        session.delete(url)


def test_history_guard_does_not_misclassify_ordinary_requests():
    assert not QueueHistorySafeSession.deletes_history(
        "GET",
        "http://localhost:38274/api/jobs/cleanup",
    )
    assert not QueueHistorySafeSession.deletes_history(
        "DELETE",
        "http://localhost:38274/api/watched-playlists/test-id",
    )
