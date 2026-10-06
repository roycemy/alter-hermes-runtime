import os
from datetime import datetime
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo
from unittest.mock import patch
import pytest

# Keep imports and tests off the container's real /data and /app directories.
_import_data = tempfile.TemporaryDirectory(prefix="hermes-tests-")
os.environ.setdefault("HERMES_DATA_DIR", _import_data.name)
os.environ.setdefault("HERMES_APP_DIR", str(Path(__file__).resolve().parent))
import hermes


@pytest.fixture(autouse=True)
def isolated_reader(tmp_path, monkeypatch):
    monkeypatch.setattr(hermes, "DATA", tmp_path)
    monkeypatch.setattr(hermes, "DB_PATH", tmp_path / "hermes.db")
    local_store = hermes.Store()
    monkeypatch.setattr(hermes, "store", local_store)
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("HERMES_DELIVERY_ENABLED", "false")
    yield
    local_store.db.close()


def freeze_day(monkeypatch, day):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls.fromisoformat(day + "T01:00:00")
            return value.replace(tzinfo=tz) if tz else value
    monkeypatch.setattr(hermes, "datetime", FrozenDateTime)


def fixed_board(monkeypatch):
    monkeypatch.setattr(hermes, "load_config", lambda: {"stall_days": 3})
    monkeypatch.setattr(hermes, "read_memory_files", lambda cfg: {
        "projects.md": "## Proof\nLast moved: 2026-09-25\nNext: verify source"
    })
    monkeypatch.setattr(hermes, "read_inbox", lambda cfg: ([], []))

def test_nested_schedule():
    dt = hermes.next_run_dt({'schedule': {'run_time':'03:17','timezone':'America/New_York'}})
    assert (dt.hour,dt.minute)==(3,17)
    assert dt.tzinfo==ZoneInfo('America/New_York')

def test_stall_boundary():
    today=datetime.now(ZoneInfo('America/New_York')).date()
    from datetime import timedelta
    p=hermes.parse_projects('## Test\nLast moved: '+str(today-timedelta(days=3))+'\nNext: verify source',3)[0]
    assert p['stalled'] and p['next']=='verify source'

def test_unknown_price_rejected():
    with patch.dict(os.environ,{},clear=True):
        with pytest.raises(ValueError): hermes.token_price('unknown')

def test_dry_run_never_delivers(capsys):
    with patch.dict(os.environ, {'OPENAI_API_KEY':''}), patch.object(hermes.mail,'send',side_effect=AssertionError('sent')):
        hermes.run_once(dry_run=True)
    assert 'Proof' in capsys.readouterr().out

def test_unchanged_skips_model_and_send():
    (hermes.DATA/'last-delivered-fingerprint').unlink(missing_ok=True)
    with patch.dict(os.environ, {'OPENAI_API_KEY':'','HERMES_DELIVERY_ENABLED':'false'}), patch.object(hermes.mail,'send',side_effect=AssertionError('sent')):
        hermes.run_once()
        with patch.object(hermes,'model_pass',side_effect=AssertionError('called')):
            hermes.run_once()
    assert (hermes.DATA/'latest-report.txt').exists()

def test_integrations_rejected():
    with patch.dict(os.environ, {'TELEGRAM_TOKEN':'example'}):
        with pytest.raises(SystemExit): hermes.validate_architecture()

def test_budget_preflight_no_provider_call():
    with patch.dict(os.environ, {'OPENAI_API_KEY':'fake'}), patch.object(hermes.store,'month_spend',return_value=1.0), patch('openai.OpenAI') as client:
        with pytest.raises(ValueError,match='budget'):
            hermes.model_pass({}, {'projects.md':'example'}, [], [])
        client.return_value.chat.completions.create.assert_not_called()


@pytest.mark.parametrize("with_model", [False, True])
def test_next_day_refreshes_ages(monkeypatch, with_model):
    fixed_board(monkeypatch)
    if with_model:
        monkeypatch.setenv("OPENAI_API_KEY", "fake")
    with patch.object(hermes, "model_pass", side_effect=lambda cfg, memory, summaries, projects:
                      (hermes.deterministic_report(projects, []), 0, 0, 0.0)) as model, \
         patch.object(hermes.mail, "send") as send:
        freeze_day(monkeypatch, "2026-09-30")
        hermes.run_once()
        assert "Proof: 5 days" in (hermes.DATA / "latest-report.txt").read_text()
        first = (hermes.DATA / "last-delivered-fingerprint").read_text()
        freeze_day(monkeypatch, "2026-10-01")
        hermes.run_once()
        assert "Proof: 6 days" in (hermes.DATA / "latest-report.txt").read_text()
        assert (hermes.DATA / "last-delivered-fingerprint").read_text() != first
        assert model.call_count == (2 if with_model else 0)
        send.assert_not_called()


def test_same_content_skips_real_model_path_and_delivery(monkeypatch):
    fixed_board(monkeypatch)
    freeze_day(monkeypatch, "2026-10-01")
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    monkeypatch.setenv("HERMES_DELIVERY_ENABLED", "true")
    with patch.object(hermes, "model_pass", return_value=("model report", 10, 20, 0.0)) as model, \
         patch.object(hermes.mail, "send") as send:
        hermes.run_once()
        hermes.run_once()
        model.assert_called_once()
        send.assert_called_once()
        assert (hermes.DATA / "latest-report.txt").read_text().startswith("model report")
        event = hermes.store.db.execute("SELECT detail FROM events WHERE kind='unchanged'").fetchone()
        assert "Recomputed report unchanged" in event[0]


def test_budget_cap_still_refreshes_next_day(monkeypatch):
    fixed_board(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    with patch.object(hermes.store, "month_spend", return_value=hermes.MONTHLY_BUDGET_USD), \
         patch.object(hermes, "model_pass") as model, patch.object(hermes.mail, "send") as send:
        for day, age in [("2026-09-30", 5), ("2026-10-01", 6)]:
            freeze_day(monkeypatch, day)
            hermes.run_once()
            report = (hermes.DATA / "latest-report.txt").read_text()
            assert f"Proof: {age} days" in report
            assert "budget cap hit" in report
        model.assert_not_called()
        send.assert_not_called()
    assert hermes.store.db.execute("SELECT COUNT(*) FROM runs WHERE status='budget_cap'").fetchone()[0] == 2


def test_budget_preflight_rejects_request_that_would_cross_cap():
    with patch.dict(os.environ, {"OPENAI_API_KEY": "fake"}), \
         patch.object(hermes.store, "month_spend", return_value=hermes.MONTHLY_BUDGET_USD - 0.000001), \
         patch("openai.OpenAI") as client:
        with pytest.raises(ValueError, match="budget"):
            hermes.model_pass({}, {"projects.md": "example"}, [], [])
        client.return_value.chat.completions.create.assert_not_called()
    assert hermes.store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


@pytest.mark.parametrize("configured, expected", [("9", 1.0), ("-1", 0.0), ("0.25", 0.25)])
def test_monthly_budget_setting_is_clamped(configured, expected, tmp_path):
    import subprocess
    import sys
    env = dict(os.environ, HERMES_MONTHLY_BUDGET_USD=configured, HERMES_DATA_DIR=str(tmp_path))
    result = subprocess.run([sys.executable, "-c",
                             "import hermes; print(hermes.MONTHLY_BUDGET_USD)"],
                            env=env, cwd=Path(__file__).resolve().parent,
                            check=True, capture_output=True, text=True)
    assert float(result.stdout.strip()) == expected
