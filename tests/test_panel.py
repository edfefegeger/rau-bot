import json

import pytest

import panel


def test_worker_count_is_saved_to_config(monkeypatch, tmp_path):
    config = tmp_path / "config.json"
    config.write_text('{"workers": 1, "dry_run": true}', encoding="utf-8")
    monkeypatch.setattr(panel, "CONFIG", str(config))

    panel.set_worker_count(15)

    assert panel.get_worker_count() == 15
    saved = json.loads(config.read_text(encoding="utf-8"))
    assert saved["workers"] == 15
    assert saved["dry_run"] is True


@pytest.mark.parametrize("workers", [0, 16, 1.5, True, "3", None])
def test_worker_count_rejects_values_outside_supported_range(workers):
    with pytest.raises(ValueError, match="от 1 до 15"):
        panel.set_worker_count(workers)
