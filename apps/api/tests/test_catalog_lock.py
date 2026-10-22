"""The catalog lock holds across PROCESSES: two landings on different uvicorn workers
read-change-write the same contrib YAML and both rows survive."""
import multiprocessing
import os
import sys
import time
from pathlib import Path

import yaml


def _land_row(path_str: str, key: str, hold_s: float) -> None:
    os.environ["TIFFS_CONTRIB_PATH"] = path_str
    from app.contrib import catalog_io
    p = Path(path_str)
    with catalog_io.LOCK:
        cat = (yaml.safe_load(p.read_text()) or {}) if p.exists() else {}
        time.sleep(hold_s)                       # the window the race used to fall into
        cat[key] = {"title": key}
        catalog_io.write_text(p, yaml.safe_dump(cat, sort_keys=False))


def test_two_processes_landing_at_once_keep_both_rows(tmp_path):
    path = tmp_path / "tiffs.contrib.yml"
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_land_row, args=(str(path), f"layer_{i}", 0.6)) for i in range(2)]
    for pr in procs:
        pr.start()
    for pr in procs:
        pr.join(timeout=60)
        assert pr.exitcode == 0, pr.exitcode
    cat = yaml.safe_load(path.read_text())
    assert set(cat) == {"layer_0", "layer_1"}, cat
    assert (tmp_path / ".contrib.lock").exists()


def test_the_lock_is_reentrant_in_one_thread(tmp_path, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "tiffs_contrib_path", tmp_path / "tiffs.contrib.yml")
    from app.contrib import catalog_io
    with catalog_io.LOCK:
        with catalog_io.LOCK:                    # write_text takes it again inside a holder
            catalog_io.write_text(tmp_path / "tiffs.contrib.yml", "a: 1\n")
    assert (tmp_path / "tiffs.contrib.yml").read_text() == "a: 1\n"
