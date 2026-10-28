import math

import pytest

from bapet.renderer import Frame


def test_frame_fit_contains_bounds():
    f = Frame.fit(radius=0.3, y_min=0.0, y_max=1.1, ppu=200, pitch=math.radians(12), margin=4)
    assert f.width == math.ceil(0.6 * 200) + 8
    assert f.anchor_x == f.width / 2
    # feet (origin) must be inside the window, head above it
    assert 0 < f.anchor_y < f.height
    vp = f.view_proj()
    head = vp @ [0, 1.1, 0, 1]
    feet = vp @ [0, 0, 0, 1]
    assert -1 <= head[1] <= 1 and -1 <= feet[1] <= 1 and head[1] > feet[1]


def test_renderer_smoke():
    """Render the synthetic model if an OpenGL 3.3 context is available."""
    from bapet.renderer import Renderer, RendererError
    from bapet import gltf
    from tests.test_gltf import build_glb

    try:
        r = Renderer()
    except RendererError:
        pytest.skip("no OpenGL 3.3 context")
    m = gltf.load_bytes(build_glb())
    gpu = r.upload(m, [])
    world, weights = m.bake(0).sample(0.0)
    f = Frame.fit(*m.animation_bounds(m.bake(0)), ppu=50, pitch=0.0, margin=2)
    w, h, rgba = r.render(gpu, world, weights, f, yaw=0.0)
    assert rgba.shape == (h, w, 4)
    assert rgba[..., 3].max() == 255  # something was drawn
    gpu.release()
    r.release()
