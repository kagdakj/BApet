"""Minimal glTF 2.0 / GLB loader focused on skinned, animated character models.

Supports everything the Blue Archive model exports use: skins, morph targets,
STEP / LINEAR / CUBICSPLINE animation samplers (including morph weights),
interleaved / normalized / sparse accessors and embedded images.

Animations are "baked" lazily into per-frame node world matrices so the
per-frame cost at runtime is a single vectorized interpolation.
"""

from __future__ import annotations

import base64
import json
import math
import struct
from dataclasses import dataclass, field

import numpy as np

GLB_MAGIC = b"glTF"
CHUNK_JSON = 0x4E4F534A
CHUNK_BIN = 0x004E4942

_COMPONENT_DTYPES = {
    5120: np.int8,
    5121: np.uint8,
    5122: np.int16,
    5123: np.uint16,
    5125: np.uint32,
    5126: np.float32,
}
_TYPE_SIZES = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT2": 4, "MAT3": 9, "MAT4": 16}

MODE_TRIANGLES = 4
BAKE_FPS = 60.0


class GLTFError(Exception):
    """Raised when a file is not a valid / supported glTF asset."""


@dataclass
class Image:
    data: bytes
    mime: str


@dataclass
class Texture:
    image: int
    wrap_s: int = 10497
    wrap_t: int = 10497
    mag_filter: int | None = None
    min_filter: int | None = None


@dataclass
class Material:
    name: str
    base_color: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)
    texture: int | None = None
    texcoord: int = 0
    alpha_mode: str = "OPAQUE"
    alpha_cutoff: float = 0.5
    double_sided: bool = False


@dataclass
class Primitive:
    positions: np.ndarray  # (V,3) float32
    normals: np.ndarray  # (V,3) float32
    uvs: np.ndarray  # (V,2) float32
    joints: np.ndarray | None  # (V,4) float32 (joint indices as floats)
    weights: np.ndarray | None  # (V,4) float32
    indices: np.ndarray  # (I,) uint32
    material: int | None
    morph_positions: list[np.ndarray] = field(default_factory=list)


@dataclass
class Mesh:
    name: str
    primitives: list[Primitive]
    default_weights: np.ndarray  # (T,)


@dataclass
class Skin:
    joints: np.ndarray  # (J,) int
    inverse_bind: np.ndarray  # (J,4,4) float32


@dataclass
class Node:
    name: str
    parent: int = -1
    children: list[int] = field(default_factory=list)
    translation: np.ndarray = field(default_factory=lambda: np.zeros(3, np.float32))
    rotation: np.ndarray = field(default_factory=lambda: np.array([0, 0, 0, 1], np.float32))
    scale: np.ndarray = field(default_factory=lambda: np.ones(3, np.float32))
    matrix: np.ndarray | None = None  # static local matrix if given explicitly
    mesh: int | None = None
    skin: int | None = None


@dataclass
class Channel:
    node: int
    path: str  # translation | rotation | scale | weights
    times: np.ndarray  # (K,)
    values: np.ndarray  # (K,C) or (K,3,C) for CUBICSPLINE
    interpolation: str


@dataclass
class Animation:
    name: str
    duration: float
    channels: list[Channel]


@dataclass
class BakedAnimation:
    """Node world matrices sampled at a fixed rate."""

    name: str
    duration: float
    fps: float
    world: np.ndarray  # (F,N,4,4) float32
    weights: dict[int, np.ndarray]  # node index -> (F,T)

    @property
    def frame_count(self) -> int:
        return self.world.shape[0]

    def _frame(self, t: float, loop: bool) -> tuple[int, int, float]:
        if self.duration <= 0 or self.frame_count == 1:
            return 0, 0, 0.0
        if loop:
            t = t % self.duration
        else:
            t = min(max(t, 0.0), self.duration)
        f = t * self.fps
        i0 = min(int(f), self.frame_count - 1)
        i1 = min(i0 + 1, self.frame_count - 1)
        return i0, i1, f - i0

    def sample(self, t: float, loop: bool = True) -> tuple[np.ndarray, dict[int, np.ndarray]]:
        i0, i1, a = self._frame(t, loop)
        if i0 == i1 or a <= 1e-6:
            world = self.world[i0]
            weights = {n: w[i0] for n, w in self.weights.items()}
        else:
            world = self.world[i0] + (self.world[i1] - self.world[i0]) * np.float32(a)
            weights = {n: w[i0] + (w[i1] - w[i0]) * a for n, w in self.weights.items()}
        return world, weights


# --------------------------------------------------------------------------- math


def trs_to_matrices(t: np.ndarray, r: np.ndarray, s: np.ndarray) -> np.ndarray:
    """Vectorized TRS -> 4x4 (column-vector convention). Shapes (...,3),(...,4 xyzw),(...,3)."""
    shape = t.shape[:-1]
    x, y, z, w = r[..., 0], r[..., 1], r[..., 2], r[..., 3]
    m = np.zeros(shape + (4, 4), np.float32)
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    m[..., 0, 0] = (1 - 2 * (yy + zz)) * s[..., 0]
    m[..., 1, 0] = 2 * (xy + wz) * s[..., 0]
    m[..., 2, 0] = 2 * (xz - wy) * s[..., 0]
    m[..., 0, 1] = 2 * (xy - wz) * s[..., 1]
    m[..., 1, 1] = (1 - 2 * (xx + zz)) * s[..., 1]
    m[..., 2, 1] = 2 * (yz + wx) * s[..., 1]
    m[..., 0, 2] = 2 * (xz + wy) * s[..., 2]
    m[..., 1, 2] = 2 * (yz - wx) * s[..., 2]
    m[..., 2, 2] = (1 - 2 * (xx + yy)) * s[..., 2]
    m[..., :3, 3] = t
    m[..., 3, 3] = 1.0
    return m


def _normalize_quats(q: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(q, axis=-1, keepdims=True)
    return q / np.where(n > 1e-12, n, 1.0)


def slerp(q0: np.ndarray, q1: np.ndarray, s: np.ndarray) -> np.ndarray:
    """Vectorized quaternion slerp. q0,q1: (...,4), s: (...,)"""
    dot = np.sum(q0 * q1, axis=-1)
    q1 = np.where((dot < 0)[..., None], -q1, q1)
    dot = np.abs(dot)
    s = s[..., None]
    close = (dot > 0.9995)[..., None]
    theta = np.arccos(np.clip(dot, -1.0, 1.0))[..., None]
    sin_t = np.sin(theta)
    safe = np.where(sin_t > 1e-8, sin_t, 1.0)
    a = np.where(close, 1.0 - s, np.sin((1.0 - s) * theta) / safe)
    b = np.where(close, s, np.sin(s * theta) / safe)
    return _normalize_quats(a * q0 + b * q1)


def sample_channel(ch: Channel, t: np.ndarray) -> np.ndarray:
    """Sample a channel at times t (F,) -> (F,C)."""
    times = ch.times
    k = len(times)
    cubic = ch.interpolation == "CUBICSPLINE"
    vals = ch.values[:, 1] if cubic else ch.values
    if k == 1:
        return np.broadcast_to(vals[0], (len(t), vals.shape[-1])).copy()
    if ch.interpolation == "STEP":
        idx = np.clip(np.searchsorted(times, t, side="right") - 1, 0, k - 1)
        return vals[idx].copy()
    i0 = np.clip(np.searchsorted(times, t, side="right") - 1, 0, k - 2)
    i1 = i0 + 1
    t0, t1 = times[i0], times[i1]
    dt = t1 - t0
    s = np.clip((t - t0) / np.where(dt > 0, dt, 1.0), 0.0, 1.0).astype(np.float32)
    if cubic:
        dt = dt[:, None].astype(np.float32)
        s2 = (s * s)[:, None]
        s3 = (s * s * s)[:, None]
        sv = s[:, None]
        out_t0 = ch.values[i0, 2]
        in_t1 = ch.values[i1, 0]
        res = (
            (2 * s3 - 3 * s2 + 1) * vals[i0]
            + (s3 - 2 * s2 + sv) * dt * out_t0
            + (-2 * s3 + 3 * s2) * vals[i1]
            + (s3 - s2) * dt * in_t1
        )
        if ch.path == "rotation":
            res = _normalize_quats(res)
        return res.astype(np.float32)
    if ch.path == "rotation":
        return slerp(vals[i0], vals[i1], s).astype(np.float32)
    sv = s[:, None]
    return (vals[i0] * (1 - sv) + vals[i1] * sv).astype(np.float32)


# --------------------------------------------------------------------------- model


class Model:
    """Parsed glTF asset."""

    def __init__(self) -> None:
        self.nodes: list[Node] = []
        self.meshes: list[Mesh] = []
        self.skins: list[Skin] = []
        self.materials: list[Material] = []
        self.textures: list[Texture] = []
        self.images: list[Image] = []
        self.animations: list[Animation] = []
        self.roots: list[int] = []
        self.order: list[int] = []  # parents before children
        self._baked: dict[int, BakedAnimation] = {}
        self._rest: BakedAnimation | None = None

    # ---- animation helpers -------------------------------------------------
    @property
    def animation_names(self) -> list[str]:
        return [a.name for a in self.animations]

    def find_animation(self, *candidates: str) -> int | None:
        """Return index of the first animation matching any candidate.

        Candidates are tried in priority order; each is matched exactly, then
        case-insensitively, then allowing a variant prefix/suffix such as
        "01_Cafe_Idle" or "Normal_Idle01".
        """
        names = self.animation_names
        lowered = [n.lower() for n in names]
        for c in candidates:
            if c in names:
                return names.index(c)
            cl = c.lower()
            if cl in lowered:
                return lowered.index(cl)
            for i, n in enumerate(lowered):
                if n.endswith("_" + cl) or (n.startswith(cl) and n[len(cl):].lstrip("_").isdigit()):
                    return i
        return None

    def _local_trs(self, frames: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        n = len(self.nodes)
        t = np.empty((frames, n, 3), np.float32)
        r = np.empty((frames, n, 4), np.float32)
        s = np.empty((frames, n, 3), np.float32)
        for i, node in enumerate(self.nodes):
            t[:, i] = node.translation
            r[:, i] = node.rotation
            s[:, i] = node.scale
        return t, r, s

    def _compose_world(self, local: np.ndarray) -> np.ndarray:
        world = np.empty_like(local)
        for i in self.order:
            p = self.nodes[i].parent
            world[:, i] = local[:, i] if p < 0 else world[:, p] @ local[:, i]
        return world

    def rest_pose(self) -> BakedAnimation:
        if self._rest is None:
            t, r, s = self._local_trs(1)
            local = trs_to_matrices(t, r, s)
            for i, node in enumerate(self.nodes):
                if node.matrix is not None:
                    local[:, i] = node.matrix
            weights = {
                i: self.meshes[n.mesh].default_weights[None, :].astype(np.float32)
                for i, n in enumerate(self.nodes)
                if n.mesh is not None and len(self.meshes[n.mesh].default_weights)
            }
            self._rest = BakedAnimation("rest", 0.0, BAKE_FPS, self._compose_world(local), weights)
        return self._rest

    def bake(self, index: int) -> BakedAnimation:
        """Bake animation `index` into world matrices (cached)."""
        if index in self._baked:
            return self._baked[index]
        anim = self.animations[index]
        frames = max(1, int(math.ceil(anim.duration * BAKE_FPS)) + 1)
        times = np.minimum(np.arange(frames, dtype=np.float32) / BAKE_FPS, anim.duration)
        t, r, s = self._local_trs(frames)
        rest = self.rest_pose()
        weights: dict[int, np.ndarray] = {
            n: np.repeat(w, frames, axis=0) for n, w in rest.weights.items()
        }
        animated_matrix_nodes: set[int] = set()
        for ch in anim.channels:
            vals = sample_channel(ch, times)
            if ch.path == "translation":
                t[:, ch.node] = vals
            elif ch.path == "rotation":
                r[:, ch.node] = _normalize_quats(vals)
            elif ch.path == "scale":
                s[:, ch.node] = vals
            elif ch.path == "weights":
                cur = weights.get(ch.node)
                count = vals.shape[1]
                if cur is None or cur.shape[1] != count:
                    cur = np.zeros((frames, count), np.float32)
                cur[:] = vals
                weights[ch.node] = cur
                continue
            animated_matrix_nodes.add(ch.node)
        local = trs_to_matrices(t, r, s)
        for i, node in enumerate(self.nodes):
            if node.matrix is not None and i not in animated_matrix_nodes:
                local[:, i] = node.matrix
        baked = BakedAnimation(anim.name, anim.duration, BAKE_FPS, self._compose_world(local), weights)
        self._baked[index] = baked
        return baked

    # ---- skinning / bounds -------------------------------------------------
    def joint_matrices(self, skin: int, world: np.ndarray) -> np.ndarray:
        sk = self.skins[skin]
        return world[sk.joints] @ sk.inverse_bind

    def draw_nodes(self) -> list[int]:
        """Nodes with meshes reachable from the scene roots."""
        reachable = set(self.order)
        return [i for i, n in enumerate(self.nodes) if n.mesh is not None and i in reachable]

    def pose_points(
        self, world: np.ndarray, weights: dict[int, np.ndarray], max_points: int = 1500
    ) -> np.ndarray:
        """Approximate skinned vertex positions (subsampled) for bounds estimation."""
        pts = []
        for ni in self.draw_nodes():
            node = self.nodes[ni]
            mesh = self.meshes[node.mesh]
            w_morph = weights.get(ni, mesh.default_weights)
            for prim in mesh.primitives:
                v = len(prim.positions)
                if v == 0:
                    continue
                step = max(1, v // max_points)
                pos = prim.positions[::step].copy()
                for k, delta in enumerate(prim.morph_positions):
                    if k < len(w_morph) and w_morph[k]:
                        pos += delta[::step] * w_morph[k]
                hom = np.concatenate([pos, np.ones((len(pos), 1), np.float32)], axis=1)
                if node.skin is not None and prim.joints is not None:
                    jm = self.joint_matrices(node.skin, world)
                    joints = prim.joints[::step].astype(np.int64)
                    wts = prim.weights[::step]
                    joints = np.clip(joints, 0, len(jm) - 1)
                    mats = np.einsum("vk,vkij->vij", wts, jm[joints])
                    out = np.einsum("vij,vj->vi", mats, hom)
                    keep = wts.sum(axis=1) > 1e-4
                    out = out[keep]
                else:
                    out = hom @ world[ni].T
                # Collapsed geometry (e.g. hidden mouth shapes or scale-0 props) is ignored.
                pts.append(out[:, :3])
        if not pts:
            return np.zeros((1, 3), np.float32)
        return np.concatenate(pts, axis=0)

    def animation_bounds(self, baked: BakedAnimation, samples: int = 12) -> tuple[float, float, float]:
        """Return (radius around Y axis, y_min, y_max) over sampled frames."""
        frames = baked.frame_count
        idxs = sorted(set(np.linspace(0, frames - 1, min(samples, frames)).astype(int).tolist()))
        radius, y_min, y_max = 0.0, math.inf, -math.inf
        for f in idxs:
            pts = self.pose_points(baked.world[f], {n: w[f] for n, w in baked.weights.items()})
            # Robust bounds: drop extreme outliers (stray collapsed verts at origin are harmless).
            r = np.sqrt(pts[:, 0] ** 2 + pts[:, 2] ** 2)
            radius = max(radius, float(np.percentile(r, 99.9)))
            y_min = min(y_min, float(np.percentile(pts[:, 1], 0.05)))
            y_max = max(y_max, float(np.percentile(pts[:, 1], 99.95)))
        if not math.isfinite(y_min):
            y_min, y_max = 0.0, 1.0
        return radius, y_min, y_max


# --------------------------------------------------------------------------- parsing


class _Reader:
    def __init__(self, doc: dict, buffers: list[bytes]) -> None:
        self.doc = doc
        self.buffers = buffers
        self._cache: dict[int, np.ndarray] = {}

    def buffer_view(self, index: int) -> tuple[memoryview, int]:
        bv = self.doc["bufferViews"][index]
        buf = self.buffers[bv["buffer"]]
        start = bv.get("byteOffset", 0)
        length = bv["byteLength"]
        if start + length > len(buf):
            raise GLTFError(f"bufferView {index} out of range")
        return memoryview(buf)[start : start + length], bv.get("byteStride", 0)

    def accessor(self, index: int) -> np.ndarray:
        if index in self._cache:
            return self._cache[index]
        acc = self.doc["accessors"][index]
        ctype = acc["componentType"]
        dtype = np.dtype(_COMPONENT_DTYPES[ctype]).newbyteorder("<")
        ncomp = _TYPE_SIZES[acc["type"]]
        count = acc["count"]
        if "bufferView" in acc:
            view, stride = self.buffer_view(acc["bufferView"])
            offset = acc.get("byteOffset", 0)
            elem = ncomp * dtype.itemsize
            if stride and stride != elem:
                raw = np.frombuffer(view, np.uint8, count=(count - 1) * stride + elem, offset=offset)
                rows = np.lib.stride_tricks.as_strided(raw, shape=(count, elem), strides=(stride, 1))
                arr = np.frombuffer(rows.tobytes(), dtype).reshape(count, ncomp)
            else:
                arr = np.frombuffer(view, dtype, count=count * ncomp, offset=offset).reshape(count, ncomp)
            arr = arr.copy()
        else:
            arr = np.zeros((count, ncomp), dtype)
        sparse = acc.get("sparse")
        if sparse:
            idx_info, val_info = sparse["indices"], sparse["values"]
            n = sparse["count"]
            iview, _ = self.buffer_view(idx_info["bufferView"])
            idt = np.dtype(_COMPONENT_DTYPES[idx_info["componentType"]]).newbyteorder("<")
            sidx = np.frombuffer(iview, idt, count=n, offset=idx_info.get("byteOffset", 0))
            vview, _ = self.buffer_view(val_info["bufferView"])
            svals = np.frombuffer(vview, dtype, count=n * ncomp, offset=val_info.get("byteOffset", 0))
            arr[sidx.astype(np.int64)] = svals.reshape(n, ncomp)
        if acc.get("normalized") and ctype != 5126:
            div = float(np.iinfo(dtype).max)
            arr = np.maximum(arr.astype(np.float32) / div, -1.0)
        if acc["type"] == "MAT4":
            # glTF matrices are column-major.
            arr = arr.reshape(count, 4, 4).transpose(0, 2, 1)
        self._cache[index] = arr
        return arr


def _parse_glb(data: bytes) -> tuple[dict, bytes | None]:
    if len(data) < 20:
        raise GLTFError("file too small")
    magic, version, length = struct.unpack_from("<4sII", data, 0)
    if magic != GLB_MAGIC:
        raise GLTFError("not a GLB file")
    if version != 2:
        raise GLTFError(f"unsupported glTF version {version}")
    if length > len(data):
        raise GLTFError("truncated GLB file")
    offset = 12
    doc = None
    binary = None
    while offset + 8 <= length:
        clen, ctype = struct.unpack_from("<II", data, offset)
        offset += 8
        chunk = data[offset : offset + clen]
        if len(chunk) != clen:
            raise GLTFError("truncated GLB chunk")
        if ctype == CHUNK_JSON:
            doc = json.loads(chunk.decode("utf-8"))
        elif ctype == CHUNK_BIN and binary is None:
            binary = chunk
        offset += (clen + 3) & ~3
    if doc is None:
        raise GLTFError("GLB has no JSON chunk")
    return doc, binary


def _decode_uri(uri: str) -> bytes:
    if uri.startswith("data:"):
        header, _, payload = uri.partition(",")
        if ";base64" in header:
            return base64.b64decode(payload)
        from urllib.parse import unquote_to_bytes

        return unquote_to_bytes(payload)
    raise GLTFError("external buffers/images are not supported (use .glb)")


def load_bytes(data: bytes) -> Model:
    """Parse GLB (or embedded .gltf JSON) bytes into a Model."""
    if data[:4] == GLB_MAGIC:
        doc, binary = _parse_glb(data)
    else:
        try:
            doc, binary = json.loads(data.decode("utf-8")), None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GLTFError("not a glTF file") from exc

    if not str(doc.get("asset", {}).get("version", "")).startswith("2"):
        raise GLTFError("only glTF 2.0 is supported")
    required = set(doc.get("extensionsRequired", []))
    if required:
        raise GLTFError(f"unsupported required extensions: {', '.join(sorted(required))}")

    buffers: list[bytes] = []
    for i, b in enumerate(doc.get("buffers", [])):
        if "uri" in b:
            buffers.append(_decode_uri(b["uri"]))
        elif i == 0 and binary is not None:
            buffers.append(binary)
        else:
            raise GLTFError("missing binary buffer")
    reader = _Reader(doc, buffers)
    model = Model()

    # images / textures / materials
    for img in doc.get("images", []):
        if "bufferView" in img:
            view, _ = reader.buffer_view(img["bufferView"])
            model.images.append(Image(bytes(view), img.get("mimeType", "image/png")))
        elif "uri" in img:
            model.images.append(Image(_decode_uri(img["uri"]), img.get("mimeType", "image/png")))
        else:
            model.images.append(Image(b"", "image/png"))
    samplers = doc.get("samplers", [])
    for tex in doc.get("textures", []):
        smp = samplers[tex["sampler"]] if "sampler" in tex else {}
        model.textures.append(
            Texture(
                image=tex.get("source", -1),
                wrap_s=smp.get("wrapS", 10497),
                wrap_t=smp.get("wrapT", 10497),
                mag_filter=smp.get("magFilter"),
                min_filter=smp.get("minFilter"),
            )
        )
    for mat in doc.get("materials", []):
        pbr = mat.get("pbrMetallicRoughness", {})
        bct = pbr.get("baseColorTexture")
        model.materials.append(
            Material(
                name=mat.get("name", ""),
                base_color=tuple(pbr.get("baseColorFactor", (1.0, 1.0, 1.0, 1.0))),
                texture=bct["index"] if bct else None,
                texcoord=bct.get("texCoord", 0) if bct else 0,
                alpha_mode=mat.get("alphaMode", "OPAQUE"),
                alpha_cutoff=float(mat.get("alphaCutoff", 0.5)),
                double_sided=bool(mat.get("doubleSided", False)),
            )
        )

    # meshes
    for mesh in doc.get("meshes", []):
        prims = []
        target_count = 0
        for p in mesh.get("primitives", []):
            if p.get("mode", MODE_TRIANGLES) != MODE_TRIANGLES:
                continue
            attrs = p["attributes"]
            if "POSITION" not in attrs:
                continue
            pos = reader.accessor(attrs["POSITION"]).astype(np.float32)
            v = len(pos)
            normals = (
                reader.accessor(attrs["NORMAL"]).astype(np.float32)
                if "NORMAL" in attrs
                else np.zeros((v, 3), np.float32)
            )
            uvs = (
                reader.accessor(attrs["TEXCOORD_0"]).astype(np.float32)
                if "TEXCOORD_0" in attrs
                else np.zeros((v, 2), np.float32)
            )
            joints = weights = None
            if "JOINTS_0" in attrs and "WEIGHTS_0" in attrs:
                joints = reader.accessor(attrs["JOINTS_0"]).astype(np.float32)
                weights = reader.accessor(attrs["WEIGHTS_0"]).astype(np.float32)
                total = weights.sum(axis=1, keepdims=True)
                weights = np.where(total > 1e-6, weights / np.where(total > 1e-6, total, 1.0), weights)
            if "indices" in p:
                idx = reader.accessor(p["indices"]).reshape(-1).astype(np.uint32)
            else:
                idx = np.arange(v, dtype=np.uint32)
            if len(idx) % 3:
                idx = idx[: len(idx) - len(idx) % 3]
            if len(idx) and int(idx.max()) >= v:
                raise GLTFError(f"mesh '{mesh.get('name')}' has out-of-range indices")
            morphs = [
                reader.accessor(t["POSITION"]).astype(np.float32)
                if "POSITION" in t
                else np.zeros((v, 3), np.float32)
                for t in p.get("targets", [])
            ]
            target_count = max(target_count, len(morphs))
            prims.append(Primitive(pos, normals, uvs, joints, weights, idx, p.get("material"), morphs))
        default_w = np.array(mesh.get("weights", [0.0] * target_count), np.float32)
        if len(default_w) < target_count:
            default_w = np.pad(default_w, (0, target_count - len(default_w)))
        model.meshes.append(Mesh(mesh.get("name", ""), prims, default_w))

    # skins
    for skin in doc.get("skins", []):
        joints = np.array(skin["joints"], np.int64)
        if "inverseBindMatrices" in skin:
            ibm = reader.accessor(skin["inverseBindMatrices"]).astype(np.float32)
        else:
            ibm = np.tile(np.eye(4, dtype=np.float32), (len(joints), 1, 1))
        model.skins.append(Skin(joints, ibm))

    # nodes
    for i, nd in enumerate(doc.get("nodes", [])):
        node = Node(name=nd.get("name", f"node{i}"), mesh=nd.get("mesh"), skin=nd.get("skin"))
        node.children = list(nd.get("children", []))
        if "matrix" in nd:
            node.matrix = np.array(nd["matrix"], np.float32).reshape(4, 4).T
        if "translation" in nd:
            node.translation = np.array(nd["translation"], np.float32)
        if "rotation" in nd:
            node.rotation = _normalize_quats(np.array(nd["rotation"], np.float32))
        if "scale" in nd:
            node.scale = np.array(nd["scale"], np.float32)
        model.nodes.append(node)
    for i, node in enumerate(model.nodes):
        for c in node.children:
            if not 0 <= c < len(model.nodes) or model.nodes[c].parent not in (-1, i):
                raise GLTFError("invalid node hierarchy")
            model.nodes[c].parent = i

    scenes = doc.get("scenes", [])
    if scenes:
        model.roots = list(scenes[doc.get("scene", 0)].get("nodes", []))
    else:
        model.roots = [i for i, n in enumerate(model.nodes) if n.parent < 0]
    seen: set[int] = set()
    stack = list(reversed(model.roots))
    while stack:
        i = stack.pop()
        if i in seen:
            raise GLTFError("cycle in node hierarchy")
        seen.add(i)
        model.order.append(i)
        stack.extend(reversed(model.nodes[i].children))
    # Nodes outside the scene still get world matrices (e.g. joints referenced by skins).
    for i, node in enumerate(model.nodes):
        if i not in seen and node.parent < 0:
            sub = [i]
            while sub:
                j = sub.pop()
                if j not in seen:
                    seen.add(j)
                    model.order.append(j)
                    sub.extend(model.nodes[j].children)

    # animations
    for ai, anim in enumerate(doc.get("animations", [])):
        channels = []
        duration = 0.0
        for ch in anim.get("channels", []):
            target = ch.get("target", {})
            if "node" not in target or target.get("path") not in ("translation", "rotation", "scale", "weights"):
                continue
            smp = anim["samplers"][ch["sampler"]]
            times = reader.accessor(smp["input"]).reshape(-1).astype(np.float32)
            values = reader.accessor(smp["output"]).astype(np.float32)
            interp = smp.get("interpolation", "LINEAR")
            k = len(times)
            if k == 0:
                continue
            if target["path"] == "weights":
                per_key = values.size // (k * (3 if interp == "CUBICSPLINE" else 1))
                values = values.reshape(-1, per_key)
            if interp == "CUBICSPLINE":
                values = values.reshape(k, 3, values.shape[-1])
            else:
                values = values.reshape(k, values.shape[-1])
            channels.append(Channel(target["node"], target["path"], times, values, interp))
            duration = max(duration, float(times[-1]))
        model.animations.append(Animation(anim.get("name") or f"Animation {ai + 1}", duration, channels))

    return model


def load_file(path: str) -> Model:
    with open(path, "rb") as fh:
        return load_bytes(fh.read())
