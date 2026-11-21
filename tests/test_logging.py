import logging

import pytest

from live_clipper.logging_setup import redact, register_secret, setup_logging


@pytest.mark.parametrize("text, leaked", [
    ("Authorization: Bearer abc.def-123_xyz", "abc.def-123_xyz"),
    ("headers={'Authorization': 'Basic dXNlcjpwYXNz'}", "dXNlcjpwYXNz"),
    ("GET https://api.example.com/cb?code=AUTHCODE42&state=s1", "AUTHCODE42"),
    ("url=https://x.test/?a=1&access_token=TOK123&b=2", "TOK123"),
    ("refresh_token=RT-999 grant", "RT-999"),
    ('{"client_secret": "CS-abc", "name": "ok"}', "CS-abc"),
    ("{'api_key': 'AK-777'}", "AK-777"),
    ('{"access_token":"AT-1","refresh_token":"RT-2"}', "AT-1"),
])
def test_redacts_known_patterns(text, leaked):
    out = redact(text)
    assert leaked not in out
    assert "***" in out


def test_keeps_harmless_text():
    assert redact("clip 12 cut in 3.2s, returncode=0") == "clip 12 cut in 3.2s, returncode=0"
    assert redact('{"name": "ok", "state": "s1"}') == '{"name": "ok", "state": "s1"}'


def test_runtime_registered_secret():
    register_secret("hunter2-very-secret")
    assert redact("password is hunter2-very-secret!") == "password is ***!"


def test_log_file_and_args_and_exceptions_are_redacted(home):
    log_dir = home / "logs"
    log_dir.mkdir(parents=True)
    setup_logging(log_dir, console=False)
    register_secret("S3CRET-VALUE-XYZ")
    log = logging.getLogger("live_clipper.test")
    log.info("token %s and header %s", "S3CRET-VALUE-XYZ", {"Authorization": "Bearer zzz111"})
    try:
        raise ValueError("bad access_token=LEAKME")
    except ValueError:
        log.exception("boom")
    for h in logging.getLogger().handlers:
        h.flush()
    content = (log_dir / "live-clipper.log").read_text(encoding="utf-8")
    assert "token *** and header" in content
    for leaked in ("S3CRET-VALUE-XYZ", "zzz111", "LEAKME"):
        assert leaked not in content
    assert "ValueError" in content


@pytest.mark.parametrize("text, leaked", [
    ("GET https://graph.example.com/oauth/access_token?fb_exchange_token=FBSHORT99&x=1", "FBSHORT99"),
    ("x?id_token=IDT123&y=2", "IDT123"),
    ('{"fb_exchange_token": "FBSHORT98"}', "FBSHORT98"),
])
def test_redacts_exchange_and_generic_token_keys(text, leaked):
    assert leaked not in redact(text)
