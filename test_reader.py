import os
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch
import pytest
import hermes

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
