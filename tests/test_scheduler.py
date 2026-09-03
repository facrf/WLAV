import os
from datetime import datetime, timedelta

from ingestor.scheduler import apply_retention


def test_retention_only_removes_old_automatic_backups(tmp_path):
    old_auto = tmp_path / "wlav-auto-20200101-000000.wlavenc"
    recent_auto = tmp_path / "wlav-auto-20990101-000000.wlavenc"
    manual = tmp_path / "wlav-manual.wlavenc"
    for path in (old_auto, recent_auto, manual):
        path.write_bytes(b"backup")
    old_time = (datetime.now().astimezone() - timedelta(days=60)).timestamp()
    os.utime(old_auto, (old_time, old_time))

    assert apply_retention(tmp_path, retention_days=30) == 1
    assert not old_auto.exists()
    assert recent_auto.exists()
    assert manual.exists()
