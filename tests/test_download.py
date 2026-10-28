import hashlib

import pytest

from bapet import catalog
from bapet.catalog import Skin
from bapet.models import DownloadError, Downloader, glb_complete
from tests.test_gltf import build_glb


def git_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


@pytest.fixture
def source(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    data = build_glb()
    (src / "Test Model.glb").write_bytes(data)
    monkeypatch.setattr(catalog, "MIRRORS", (src.as_uri() + "/{file}",))
    return data


def test_download_verifies_and_installs(tmp_path, source):
    dl = Downloader(tmp_path / "models")
    skin = Skin("Test", "Default", "Test Model.glb", len(source), git_sha(source))
    path = dl._download(skin)
    assert path.read_bytes() == source
    assert glb_complete(path) and dl.is_cached(skin)
    assert not list((tmp_path / "models").glob("*.part"))


def test_checksum_mismatch_still_accepts_valid_glb(tmp_path, source):
    dl = Downloader(tmp_path / "models")
    skin = Skin("Test", "Default", "Test Model.glb", len(source), "0" * 40)
    assert dl._download(skin).exists()


def test_missing_file_fails(tmp_path, source, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    dl = Downloader(tmp_path / "models")
    skin = Skin("Test", "Default", "Nope.glb", 10, "")
    with pytest.raises(DownloadError):
        dl._download(skin)
    assert not dl.is_cached(skin)


def test_truncated_file_not_cached(tmp_path):
    p = tmp_path / "x.glb"
    p.write_bytes(build_glb()[:-10])
    assert not glb_complete(p)
