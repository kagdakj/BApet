import json

from bapet import config
from bapet.config import PetState, Settings


def test_settings_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path)
    s = Settings(size=1.5, movement="walk", pets=[PetState("Airi.glb", 10, 20, 2.0, True)])
    s.save()
    loaded = Settings.load()
    assert loaded.size == 1.5 and loaded.movement == "walk"
    assert loaded.pets[0].model == "Airi.glb" and loaded.pets[0].still


def test_settings_sanitize_and_unknown_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path)
    (tmp_path / "settings.json").write_text(json.dumps({
        "size": 99, "movement": "fly", "fps": 1000, "bogus": 1,
        "pets": [{"model": "A.glb", "size": -3, "extra": 1}, {"nomodel": True}, 5],
    }))
    s = Settings.load()
    assert s.size == 4.0 and s.movement == "float" and s.fps == 120
    assert len(s.pets) == 1 and s.pets[0].size == 0.3


def test_corrupt_settings_are_backed_up(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path)
    (tmp_path / "settings.json").write_text("{not json")
    s = Settings.load()
    assert s.pets == []
    assert (tmp_path / "settings.corrupt.json").exists()
