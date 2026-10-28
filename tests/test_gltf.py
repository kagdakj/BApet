import json
import math
import struct

import numpy as np
import pytest

from bapet import gltf


def _pad(b: bytes, fill: bytes = b"\0") -> bytes:
    return b + fill * ((4 - len(b) % 4) % 4)


def build_glb(interp: str = "LINEAR") -> bytes:
    """Two-joint skinned triangle with a morph target and an animation."""
    blobs: list[bytes] = []
    views, accessors = [], []

    def add(arr: np.ndarray, ctype: int, typ: str, minmax: bool = False) -> int:
        data = _pad(arr.tobytes())
        offset = sum(len(b) for b in blobs)
        blobs.append(data)
        views.append({"buffer": 0, "byteOffset": offset, "byteLength": arr.nbytes})
        acc = {"bufferView": len(views) - 1, "componentType": ctype, "count": len(arr), "type": typ}
        if minmax:
            acc["min"] = arr.min(axis=0).tolist()
            acc["max"] = arr.max(axis=0).tolist()
        accessors.append(acc)
        return len(accessors) - 1

    pos = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], np.float32)
    nrm = np.array([[0, 0, 1]] * 3, np.float32)
    uv = np.array([[0, 0], [1, 0], [0, 1]], np.float32)
    joints = np.array([[0, 0, 0, 0], [1, 0, 0, 0], [1, 0, 0, 0]], np.uint16)
    weights = np.array([[1, 0, 0, 0], [1, 0, 0, 0], [1, 0, 0, 0]], np.float32)
    morph = np.array([[0, 0, 0], [0, 0, 1], [0, 0, 0]], np.float32)
    idx = np.array([0, 1, 2], np.uint16)
    ibm = np.stack([np.eye(4, dtype=np.float32)] * 2).transpose(0, 2, 1).reshape(2, 16)

    a_pos = add(pos, 5126, "VEC3", True)
    a_nrm = add(nrm, 5126, "VEC3")
    a_uv = add(uv, 5126, "VEC2")
    a_j = add(joints, 5123, "VEC4")
    a_w = add(weights, 5126, "VEC4")
    a_m = add(morph, 5126, "VEC3")
    a_i = add(idx, 5123, "SCALAR")
    a_ibm = add(ibm, 5126, "MAT4")

    times = np.array([0.0, 1.0], np.float32)
    a_t = add(times, 5126, "SCALAR")
    if interp == "CUBICSPLINE":
        tr = np.array([[0, 0, 0], [0, 0, 0], [0, 0, 0], [0, 0, 0], [2, 0, 0], [0, 0, 0]], np.float32)
    else:
        tr = np.array([[0, 0, 0], [2, 0, 0]], np.float32)
    a_tr = add(tr, 5126, "VEC3")
    half = math.sqrt(0.5)
    rot = np.array([[0, 0, 0, 1], [0, half, 0, half]], np.float32)  # 0 -> 90 deg about Y
    a_rot = add(rot, 5126, "VEC4")
    w = np.array([[0.0], [1.0]], np.float32)
    a_w_anim = add(w, 5126, "SCALAR")

    doc = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0, 2]}],
        "nodes": [
            {"name": "root", "children": [1]},
            {"name": "bone", "translation": [0, 1, 0]},
            {"name": "mesh", "mesh": 0, "skin": 0},
        ],
        "meshes": [{
            "name": "tri",
            "primitives": [{
                "attributes": {"POSITION": a_pos, "NORMAL": a_nrm, "TEXCOORD_0": a_uv,
                               "JOINTS_0": a_j, "WEIGHTS_0": a_w},
                "indices": a_i,
                "material": 0,
                "targets": [{"POSITION": a_m}],
            }],
            "weights": [0.0],
        }],
        "skins": [{"joints": [0, 1], "inverseBindMatrices": a_ibm}],
        "materials": [{"name": "m", "pbrMetallicRoughness": {"baseColorFactor": [1, 0, 0, 1]}}],
        "animations": [
            {
                "name": "Cafe_Idle",
                "samplers": [
                    {"input": a_t, "output": a_tr, "interpolation": interp},
                    {"input": a_t, "output": a_rot, "interpolation": "LINEAR"},
                    {"input": a_t, "output": a_w_anim, "interpolation": "STEP"},
                ],
                "channels": [
                    {"sampler": 0, "target": {"node": 1, "path": "translation"}},
                    {"sampler": 1, "target": {"node": 0, "path": "rotation"}},
                    {"sampler": 2, "target": {"node": 2, "path": "weights"}},
                ],
            }
        ],
        "buffers": [{"byteLength": sum(len(b) for b in blobs)}],
        "bufferViews": views,
        "accessors": accessors,
    }
    js = _pad(json.dumps(doc).encode(), b" ")
    binary = b"".join(blobs)
    total = 12 + 8 + len(js) + 8 + len(binary)
    return (
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<II", len(js), 0x4E4F534A) + js
        + struct.pack("<II", len(binary), 0x004E4942) + binary
    )


def test_parse_structure():
    m = gltf.load_bytes(build_glb())
    assert [n.name for n in m.nodes] == ["root", "bone", "mesh"]
    assert m.nodes[1].parent == 0
    assert m.order.index(0) < m.order.index(1)
    prim = m.meshes[0].primitives[0]
    assert prim.positions.shape == (3, 3)
    assert prim.joints.dtype == np.float32
    assert len(prim.morph_positions) == 1
    assert m.skins[0].inverse_bind.shape == (2, 4, 4)
    assert m.find_animation("cafe_idle") == 0
    assert m.animations[0].duration == pytest.approx(1.0)


def test_linear_and_slerp_sampling():
    m = gltf.load_bytes(build_glb())
    baked = m.bake(0)
    world, weights = baked.sample(0.5, loop=False)
    # Animated bone translation (replaces the node default) is (1, 0, 0) halfway;
    # the root is rotated 45 deg about Y.
    bone = world[1]
    a = math.radians(45)
    expected = np.array([math.cos(a), 0.0, -math.sin(a)])
    np.testing.assert_allclose(bone[:3, 3], expected, atol=1e-2)
    # STEP morph weight is still 0 before t=1
    assert weights[2][0] == pytest.approx(0.0)
    _, weights_end = baked.sample(1.0, loop=False)
    assert weights_end[2][0] == pytest.approx(1.0)


def test_cubic_spline_hermite():
    m = gltf.load_bytes(build_glb("CUBICSPLINE"))
    ch = m.animations[0].channels[0]
    assert ch.values.shape == (2, 3, 3)
    v = gltf.sample_channel(ch, np.array([0.0, 0.5, 1.0], np.float32))
    # Zero tangents -> smoothstep between 0 and 2
    np.testing.assert_allclose(v[:, 0], [0.0, 1.0, 2.0], atol=1e-5)


def test_skinning_bounds():
    m = gltf.load_bytes(build_glb())
    rest = m.rest_pose()
    pts = m.pose_points(rest.world[0], {n: w[0] for n, w in rest.weights.items()})
    # Bone at y=1 carries vertices 1 and 2 (identity inverse bind).
    assert pts[:, 1].max() == pytest.approx(2.0)
    r, lo, hi = m.animation_bounds(m.bake(0))
    assert hi > lo and r > 0


def test_slerp_endpoints_and_shortest_path():
    q0 = np.array([[0, 0, 0, 1]], np.float32)
    q1 = np.array([[0, 0, 0, -1]], np.float32)  # same rotation, opposite sign
    out = gltf.slerp(q0, q1, np.array([0.5], np.float32))
    np.testing.assert_allclose(np.abs(out), [[0, 0, 0, 1]], atol=1e-6)


def test_trs_matches_composition():
    t = np.array([1, 2, 3], np.float32)
    half = math.sqrt(0.5)
    r = np.array([half, 0, 0, half], np.float32)  # 90 deg about X
    s = np.array([2, 2, 2], np.float32)
    m = gltf.trs_to_matrices(t, r, s)
    p = m @ np.array([0, 1, 0, 1], np.float32)
    np.testing.assert_allclose(p[:3], [1, 2, 5], atol=1e-5)


@pytest.mark.parametrize("data", [b"", b"glTF", b"notgltf" * 10, b"glTF" + struct.pack("<II", 1, 100)])
def test_rejects_garbage(data):
    with pytest.raises(gltf.GLTFError):
        gltf.load_bytes(data)


def test_truncated_glb():
    data = build_glb()
    with pytest.raises(gltf.GLTFError):
        gltf.load_bytes(data[: len(data) // 2])
