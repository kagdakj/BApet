import json

from bapet import catalog
from bapet.catalog import Catalog
from bapet.config import resource_path

PAGE = """
<div id="toc"><h2 id="mw-toc-heading">Contents</h2><ul><li>Airi</li></ul></div>
<div class="mw-heading mw-heading2"><h2 id="Airi"><a href="/wiki/Airi" title="Airi">Airi</a></h2></div>
<div class="model-viewer" data-models="&#91;&#123;&quot;name&quot;:&quot;Default&quot;,&quot;src&quot;:&quot;https&#58;//cdn.jsdelivr.net/gh/x/y@main/Airi.glb&quot;&#125;,&#123;&quot;name&quot;:&quot;Band&quot;,&quot;src&quot;:&quot;https&#58;//cdn.jsdelivr.net/gh/x/y@main/Airi%20%28Band%29.glb&quot;&#125;&#93;" data-height="480"></div>
<div class="mw-heading mw-heading2"><h2 id="Hatsune_Miku"><a href="/wiki/Hatsune_Miku">Hatsune Miku</a></h2></div>
<div class="model-viewer" data-models="&#91;&#123;&quot;name&quot;:&quot;Default&quot;,&quot;src&quot;:&quot;https&#58;//cdn/Hatsune%20Miku.glb&quot;&#125;&#93;"></div>
<h2 id="See_also">See also</h2><p>nothing</p>
"""


def test_parse_wiki_html():
    out = catalog.parse_wiki_html(PAGE)
    assert out == [
        ("Airi", [("Default", "Airi.glb"), ("Band", "Airi (Band).glb")]),
        ("Hatsune Miku", [("Default", "Hatsune Miku.glb")]),
    ]


def test_bundled_catalog_is_complete():
    cat = Catalog.load(resource_path("catalog.json"))
    assert len(cat) >= 140
    names = [c.name for c in cat.characters]
    assert "Airi" in names and "Contents" not in names
    for skin in cat.skins:
        assert skin.file.endswith(".glb")
        assert skin.size > 0 and len(skin.sha) == 40


def test_roundtrip(tmp_path):
    cat = Catalog.load(resource_path("catalog.json"))
    path = tmp_path / "c.json"
    cat.save(path)
    again = Catalog.load(path)
    assert again.to_json() == cat.to_json()
    skin = again.skins[0]
    assert again.skin(skin.file) == skin
    assert all("%20" in u or " " not in skin.file for u in skin.urls())
    assert json.loads(path.read_text())["version"] == catalog.CATALOG_VERSION
