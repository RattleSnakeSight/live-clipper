import pytest
from ops_common import client, make_ctx, no_keyring  # noqa: F401

from live_clipper import setup_service
from live_clipper.setup_service import (
    SetupService,
    build_questions,
    detect_hardware,
    hardware_defaults,
    is_visible,
)

GB = 1024**3


def fake_run(cmd, timeout=8):
    if cmd[0] == "nvidia-smi":
        return "NVIDIA GeForce Example, 12288 MiB\n"
    if cmd[-1] == "-version":
        return "ffmpeg version 7.1 Copyright (c) the FFmpeg developers\n"
    return "streamlink 7.0.0\n"


class FakeAutostart:
    def __init__(self):
        self.installed = False
        self.calls = []

    def install(self):
        self.installed = True
        self.calls.append("install")

    def uninstall(self):
        self.installed = False
        self.calls.append("uninstall")

    def is_installed(self):
        return self.installed


def hw(gpu="none", cores=8, ram=16, free=500):
    g = {"kind": gpu, "name": "X", "vram_mb": 12288 if gpu == "nvidia" else None}
    return {"cpu": {"model": "m", "cores": cores}, "ram_bytes": ram * GB, "gpu": g,
            "disk": {"path": "/", "free_bytes": free * GB}}


def test_hardware_detection_with_fake_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_service.shutil, "which", lambda n: f"/bin/{n}")
    h = detect_hardware(tmp_path / "not" / "yet", fake_run)
    assert h["gpu"] == {"kind": "nvidia", "name": "NVIDIA GeForce Example", "vram_mb": 12288}
    assert h["ffmpeg"]["found"] and h["ffmpeg"]["version"] == "7.1"
    assert h["disk"]["free_bytes"] > 0 and h["cpu"]["cores"] >= 1 and h["ram_bytes"] > 0


def test_hardware_without_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_service.shutil, "which", lambda n: None)
    h = detect_hardware(tmp_path, fake_run)
    assert h["gpu"]["kind"] in ("none", "apple") and not h["ffmpeg"]["found"] and not h["streamlink"]["found"]


def test_defaults_follow_hardware():
    d = hardware_defaults(hw("nvidia"))
    assert (d["processing.asr_device"], d["processing.asr_compute_type"], d["processing.asr_model"]) == (
        "cuda", "float16", "large-v3")
    d = hardware_defaults(hw(cores=2, ram=4, free=10))
    assert d["processing.asr_device"] == "cpu" and d["processing.asr_model"] == "tiny"
    assert d["processing.max_concurrent_recordings"] == 1 and d["storage.cap_gb"] == 5
    d = hardware_defaults(hw(cores=16, ram=32, free=1000))
    assert d["processing.asr_model"] == "small" and d["storage.cap_gb"] == 50


def test_questions_shape_and_show_if():
    qs = build_questions(hw(), "/media")
    ids = [q["id"] for q in qs]
    assert len(ids) == len(set(ids)) and "storage.cap_gb" in ids and "notify.webhook_url" in ids
    for q in qs:
        assert q["title"] and q["help"] and "\n" not in q["help"] and q["type"] in {
            "select", "multiselect", "number", "boolean", "path", "url"}
    by = {q["id"]: q for q in qs}
    assert not is_visible(by["posting.mode"], {"posting.destinations": ["none"]})
    assert is_visible(by["posting.mode"], {"posting.destinations": ["youtube"]})
    assert not is_visible(by["notify.webhook_url"], {"notify.method": "browser"})
    assert is_visible(by["notify.webhook_url"], {"notify.method": "both"})
    assert is_visible(by["setup.permission_confirmed"], {"setup.usage": "permission"})


def test_answers_validation_and_persistence(tmp_path):
    ctx = make_ctx()
    c = client(ctx, SetupService(run=fake_run, autostart_mod=FakeAutostart()))
    with c:
        assert c.get("/api/setup/hardware").json()["cpu"]["cores"] >= 1
        assert len(c.get("/api/setup/questions").json()["questions"]) > 10
        bad = c.put("/api/setup/answers", json={"answers": {"storage.cap_gb": 0, "nope": 1,
                                                            "notify.webhook_url": "ftp://x"}})
        assert bad.status_code == 422
        errors = bad.json()["detail"]["errors"]
        assert set(errors) == {"storage.cap_gb", "nope", "notify.webhook_url"}
        assert ctx.settings.get("storage.cap_gb") is None  # atomic
        folder = tmp_path / "clips"
        ok = c.put("/api/setup/answers", json={"answers": {
            "storage.cap_gb": 12, "storage.dir": str(folder), "notify.method": "webhook",
            "notify.webhook_url": "https://ntfy.example.com/secret_topic_abc"}})
        assert ok.status_code == 200
        assert ctx.settings.get("storage.cap_gb") == 12 and folder.is_dir()
        assert ctx.secrets.get("notifications") == {"url": "https://ntfy.example.com/secret_topic_abc"}
        body = ok.json()
        assert "secret_topic_abc" not in str(body) and ctx.settings.get("notify.webhook_url") is None
        assert "notify.webhook_url" not in body["remaining"]


def test_complete_fills_defaults_sets_flag_and_autostart():
    ctx, auto = make_ctx(), FakeAutostart()
    c = client(ctx, SetupService(run=fake_run, autostart_mod=auto))
    with c:
        assert c.get("/api/setup/state").json()["completed"] is False
        assert c.post("/api/setup/complete").status_code == 200
        st = c.get("/api/setup/state").json()
        assert st["completed"] and ctx.app_state.get("setup.completed") is True
        # answers land on the keys their consumers read: edit's speech recognition and the publish worker
        assert ctx.settings.get("asr.model") and ctx.settings.get("asr.device") == "cpu"
        assert ctx.settings.get("asr.compute_type") == "int8"
        assert ctx.settings.get("processing.asr_model") is None
        assert ctx.settings.get("storage.retention_days") == 14
        assert ctx.settings.get("publish.mode") is None  # posting.mode is hidden: the worker default applies
        assert auto.calls == []


def test_posting_mode_answer_is_the_publish_mode():
    ctx = make_ctx()
    c = client(ctx, SetupService(run=fake_run, autostart_mod=FakeAutostart()))
    with c:
        qs = {q["id"]: q for q in c.get("/api/setup/questions").json()["questions"]}
        assert qs["posting.mode"]["default"] == "auto" and "processing.render_threads" not in qs
        r = c.put("/api/setup/answers", json={"answers": {"posting.destinations": ["youtube"],
                                                          "posting.mode": "manual"}})
        assert r.status_code == 200
        assert ctx.settings.get("publish.mode") == "manual" and ctx.settings.get("posting.mode") is None
        ctx.settings.set("publish.mode", "auto")  # changed on Destinations: Settings and the wizard follow
        assert c.get("/api/setup/state").json()["answers"]["posting.mode"] == "auto"
        c.post("/api/setup/complete")
        assert ctx.settings.get("publish.mode") == "auto"


def test_complete_requires_visible_required_answers():
    ctx = make_ctx()
    c = client(ctx, SetupService(run=fake_run, autostart_mod=FakeAutostart()))
    with c:
        c.put("/api/setup/answers", json={"answers": {"notify.method": "webhook"}})
        r = c.post("/api/setup/complete")
        assert r.status_code == 422 and "notify.webhook_url" in r.json()["detail"]["errors"]
        assert not ctx.app_state.get("setup.completed")


def test_start_at_login_applied_on_complete():
    ctx, auto = make_ctx(), FakeAutostart()
    c = client(ctx, SetupService(run=fake_run, autostart_mod=auto))
    with c:
        c.put("/api/setup/answers", json={"answers": {"service.start_at_login": True}})
        c.post("/api/setup/complete")
        assert auto.calls == ["install"]


def test_guide_pages_served_and_traversal_refused():
    ctx = make_ctx()
    c = client(ctx, SetupService(run=fake_run, autostart_mod=FakeAutostart()))
    with c:
        pages = c.get("/api/guide").json()["pages"]
        assert [p["slug"] for p in pages][:2] == ["install", "first-run"]
        for p in pages:
            r = c.get(f"/api/guide/{p['slug']}")
            assert r.status_code == 200 and r.text.startswith("# ")
        assert c.get("/api/guide/nope").status_code == 404
        assert c.get("/api/guide/..%2F__init__").status_code == 404


@pytest.mark.parametrize("v,ok", [("https://discord.com/api/webhooks/1/x", True), ("nothing", False)])
def test_url_validation(v, ok):
    q = {q["id"]: q for q in build_questions(hw(), "/m")}["notify.webhook_url"]
    assert (setup_service.validate_value(q, v)[1] is None) is ok


def test_storage_folder_refuses_roots_and_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(setup_service.Path, "home", classmethod(lambda cls: home))
    for bad in (tmp_path.anchor, str(home)):
        _, err = setup_service._check_dir(bad)
        assert err
    assert not (home / ".live-clipper-data").exists()


def test_storage_folder_nonempty_gets_app_subfolder(tmp_path):
    chosen = tmp_path / "Documents"
    chosen.mkdir()
    (chosen / "thesis.docx").write_text("mine")
    value, err = setup_service._check_dir(str(chosen))
    assert err is None and value == str(chosen / "LiveClipper") and (chosen / "LiveClipper").is_dir()
    # choosing the app folder again (it carries the marker) keeps it as is, even once it has content
    (chosen / "LiveClipper" / "stuff.txt").write_text("x")
    assert setup_service._check_dir(value) == (value, None)
    # an empty folder is used directly
    empty = tmp_path / "empty"
    empty.mkdir()
    assert setup_service._check_dir(str(empty)) == (str(empty), None)
    assert (chosen / "thesis.docx").read_text() == "mine"
