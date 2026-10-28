"""Off-screen OpenGL renderer (moderngl) for glTF character models.

Each frame is rendered into a multisampled framebuffer, resolved and read back
as premultiplied RGBA so the Qt layer can composite it into a translucent window.
GPU skinning uses a float texture for joint matrices (no uniform-count limits),
the look matches the wiki viewer: unlit textures plus an inverted-hull outline.
"""

from __future__ import annotations

import logging
import math
import os
import sys
from dataclasses import dataclass
from typing import Callable

import moderngl
import numpy as np

from .gltf import Model

log = logging.getLogger(__name__)

MAX_MORPHS = 4

VERTEX_SHADER = """
#version 330 core
in vec3 in_pos;
in vec3 in_normal;
in vec2 in_uv;
in vec4 in_joints;
in vec4 in_weights;
in vec3 in_m0;
in vec3 in_m1;
in vec3 in_m2;
in vec3 in_m3;

uniform sampler2D u_joint_tex;
uniform bool u_skinned;
uniform mat4 u_node;
uniform mat4 u_root;
uniform mat4 u_view_proj;
uniform vec4 u_morph;
uniform float u_outline_px;
uniform vec2 u_viewport;

out vec2 v_uv;

mat4 joint(float idx) {
    int i = int(idx + 0.5);
    return mat4(texelFetch(u_joint_tex, ivec2(0, i), 0),
                texelFetch(u_joint_tex, ivec2(1, i), 0),
                texelFetch(u_joint_tex, ivec2(2, i), 0),
                texelFetch(u_joint_tex, ivec2(3, i), 0));
}

void main() {
    vec3 p = in_pos + u_morph.x * in_m0 + u_morph.y * in_m1 + u_morph.z * in_m2 + u_morph.w * in_m3;
    mat4 m = u_node;
    if (u_skinned && dot(in_weights, vec4(1.0)) > 1e-4) {
        m = in_weights.x * joint(in_joints.x) + in_weights.y * joint(in_joints.y)
          + in_weights.z * joint(in_joints.z) + in_weights.w * joint(in_joints.w);
    }
    m = u_root * m;
    vec4 clip = u_view_proj * (m * vec4(p, 1.0));
    if (u_outline_px > 0.0) {
        vec2 n = (u_view_proj * vec4(mat3(m) * in_normal, 0.0)).xy;
        float len = length(n);
        if (len > 1e-6) {
            clip.xy += (n / len) * (u_outline_px * 2.0 / u_viewport) * clip.w;
        }
    }
    gl_Position = clip;
    v_uv = in_uv;
}
"""

FRAGMENT_SHADER = """
#version 330 core
in vec2 v_uv;
uniform sampler2D u_tex;
uniform bool u_has_tex;
uniform vec4 u_color;
uniform int u_alpha_mode;
uniform float u_cutoff;
uniform bool u_outline_pass;
uniform vec3 u_outline_color;
out vec4 f_color;

void main() {
    vec4 c = u_color;
    if (u_has_tex) c *= texture(u_tex, v_uv);
    if (u_alpha_mode == 1) {
        if (c.a < u_cutoff) discard;
        c.a = 1.0;
    } else if (u_alpha_mode == 0) {
        c.a = 1.0;
    } else if (c.a < 0.004) {
        discard;
    }
    if (u_outline_pass) {
        f_color = vec4(u_outline_color, 1.0);
        return;
    }
    f_color = vec4(c.rgb * c.a, c.a);
}
"""

ALPHA_MODES = {"OPAQUE": 0, "MASK": 1, "BLEND": 2}

GL_FILTERS = {
    9728: moderngl.NEAREST,
    9729: moderngl.LINEAR,
    9984: moderngl.NEAREST_MIPMAP_NEAREST,
    9985: moderngl.LINEAR_MIPMAP_NEAREST,
    9986: moderngl.NEAREST_MIPMAP_LINEAR,
    9987: moderngl.LINEAR_MIPMAP_LINEAR,
}


class RendererError(RuntimeError):
    pass


# --------------------------------------------------------------------------- matrices


def ortho(left: float, right: float, bottom: float, top: float, near: float, far: float) -> np.ndarray:
    m = np.eye(4, dtype=np.float32)
    m[0, 0] = 2.0 / (right - left)
    m[1, 1] = 2.0 / (top - bottom)
    m[2, 2] = -2.0 / (far - near)
    m[0, 3] = -(right + left) / (right - left)
    m[1, 3] = -(top + bottom) / (top - bottom)
    m[2, 3] = -(far + near) / (far - near)
    return m


def rot_x(angle: float) -> np.ndarray:
    c, s = math.cos(angle), math.sin(angle)
    m = np.eye(4, dtype=np.float32)
    m[1, 1], m[1, 2], m[2, 1], m[2, 2] = c, -s, s, c
    return m


def rot_y(angle: float) -> np.ndarray:
    c, s = math.cos(angle), math.sin(angle)
    m = np.eye(4, dtype=np.float32)
    m[0, 0], m[0, 2], m[2, 0], m[2, 2] = c, s, -s, c
    return m


def gl_mat(m: np.ndarray) -> bytes:
    """numpy (row-major, column-vector convention) -> GL column-major bytes."""
    return np.ascontiguousarray(m.T, dtype=np.float32).tobytes()


def srgb_encode(c: float) -> float:
    return c ** (1.0 / 2.2) if c > 0 else 0.0


# --------------------------------------------------------------------------- frame


@dataclass
class Frame:
    """Ortho camera framing in model units; anchor = model origin in pixels from top-left."""

    width: int
    height: int
    anchor_x: float
    anchor_y: float
    ppu: float  # pixels per model unit
    pitch: float  # radians, camera elevation

    @staticmethod
    def fit(radius: float, y_min: float, y_max: float, ppu: float, pitch: float, margin: int) -> "Frame":
        c, s = math.cos(pitch), math.sin(pitch)
        top = y_max * c + radius * s
        bottom = min(y_min, 0.0) * c - radius * s
        width = int(math.ceil(2 * radius * ppu)) + 2 * margin
        height = int(math.ceil((top - bottom) * ppu)) + 2 * margin
        return Frame(
            width=max(width, 8),
            height=max(height, 8),
            anchor_x=max(width, 8) / 2.0,
            anchor_y=margin + top * ppu,
            ppu=ppu,
            pitch=pitch,
        )

    def view_proj(self) -> np.ndarray:
        left = -self.anchor_x / self.ppu
        right = (self.width - self.anchor_x) / self.ppu
        top = self.anchor_y / self.ppu
        bottom = (self.anchor_y - self.height) / self.ppu
        return ortho(left, right, bottom, top, -100.0, 100.0) @ rot_x(self.pitch)


# --------------------------------------------------------------------------- GPU model


@dataclass
class _DrawItem:
    node: int
    mesh: int
    skin: int | None
    vao: moderngl.VertexArray
    material: int | None
    morph_count: int
    order: int  # 0 opaque, 1 mask, 2 blend


class GpuModel:
    """GPU resources for one Model. Shared between pets showing the same model."""

    def __init__(self, ctx: moderngl.Context, program: moderngl.Program, model: Model, textures: list):
        self.ctx = ctx
        self.model = model
        self._buffers: list[moderngl.Buffer] = []
        self._textures: dict[int, moderngl.Texture] = {}
        self.items: list[_DrawItem] = []
        self.joint_textures: list[moderngl.Texture] = []

        for ti, tex in enumerate(model.textures):
            img = textures[tex.image] if 0 <= tex.image < len(textures) else None
            if img is None:
                continue
            w, h, rgba = img
            t = ctx.texture((w, h), 4, rgba)
            t.repeat_x = tex.wrap_s != 33071
            t.repeat_y = tex.wrap_t != 33071
            mn = GL_FILTERS.get(tex.min_filter or 9987, moderngl.LINEAR_MIPMAP_LINEAR)
            mg = GL_FILTERS.get(tex.mag_filter or 9729, moderngl.LINEAR)
            if mn in (moderngl.NEAREST_MIPMAP_NEAREST, moderngl.LINEAR_MIPMAP_NEAREST,
                      moderngl.NEAREST_MIPMAP_LINEAR, moderngl.LINEAR_MIPMAP_LINEAR):
                t.build_mipmaps()
                try:
                    t.anisotropy = 4.0
                except Exception:  # extension not available
                    pass
            t.filter = (mn, mg)
            self._textures[ti] = t

        for skin in model.skins:
            jt = ctx.texture((4, max(1, len(skin.joints))), 4, dtype="f4")
            jt.filter = (moderngl.NEAREST, moderngl.NEAREST)
            self.joint_textures.append(jt)

        active = set(program)
        for ni in model.draw_nodes():
            node = model.nodes[ni]
            mesh = model.meshes[node.mesh]
            for prim in mesh.primitives:
                if len(prim.indices) == 0:
                    continue
                v = len(prim.positions)
                attrs: dict[str, np.ndarray] = {
                    "in_pos": prim.positions,
                    "in_normal": prim.normals,
                    "in_uv": prim.uvs,
                    "in_joints": prim.joints if prim.joints is not None else np.zeros((v, 4), np.float32),
                    "in_weights": prim.weights if prim.weights is not None else np.zeros((v, 4), np.float32),
                }
                for k in range(MAX_MORPHS):
                    attrs[f"in_m{k}"] = (
                        prim.morph_positions[k] if k < len(prim.morph_positions) else np.zeros((v, 3), np.float32)
                    )
                content = []
                for name, arr in attrs.items():
                    if name not in active:
                        continue
                    buf = ctx.buffer(np.ascontiguousarray(arr, dtype=np.float32).tobytes())
                    self._buffers.append(buf)
                    content.append((buf, f"{arr.shape[1]}f", name))
                ibo = ctx.buffer(np.ascontiguousarray(prim.indices, dtype=np.uint32).tobytes())
                self._buffers.append(ibo)
                vao = ctx.vertex_array(program, content, ibo, index_element_size=4)
                mat = model.materials[prim.material] if prim.material is not None else None
                order = ALPHA_MODES.get(mat.alpha_mode, 0) if mat else 0
                self.items.append(
                    _DrawItem(ni, node.mesh, node.skin if prim.joints is not None else None, vao,
                              prim.material, min(len(prim.morph_positions), MAX_MORPHS), order)
                )
        self.items.sort(key=lambda it: it.order)

    def texture_for(self, material: int | None) -> moderngl.Texture | None:
        if material is None:
            return None
        tex = self.model.materials[material].texture
        return self._textures.get(tex) if tex is not None else None

    def release(self) -> None:
        for it in self.items:
            it.vao.release()
        for b in self._buffers:
            b.release()
        for t in list(self._textures.values()) + self.joint_textures:
            t.release()
        self.items.clear()
        self._buffers.clear()
        self._textures.clear()
        self.joint_textures.clear()


# --------------------------------------------------------------------------- renderer


def _create_context() -> moderngl.Context:
    attempts: list[dict] = [{}]
    if sys.platform.startswith("linux"):
        attempts.append({"backend": "egl"})
    elif sys.platform == "win32":
        # Fall back to the Mesa software rasterizer bundled with Qt (opengl32sw.dll).
        for base in filter(None, [getattr(sys, "_MEIPASS", None), os.path.dirname(sys.executable)]):
            for rel in ("opengl32sw.dll", os.path.join("PySide6", "opengl32sw.dll")):
                path = os.path.join(base, rel)
                if os.path.exists(path):
                    attempts.append({"libgl": path})
    errors = []
    for kwargs in attempts:
        try:
            ctx = moderngl.create_standalone_context(require=330, **kwargs)
            log.info("OpenGL context: %s / %s (%s)", ctx.info.get("GL_VENDOR"),
                     ctx.info.get("GL_RENDERER"), ctx.info.get("GL_VERSION"))
            return ctx
        except Exception as exc:  # noqa: BLE001 - try next backend
            errors.append(f"{kwargs or 'default'}: {exc}")
            log.warning("OpenGL context attempt %s failed: %s", kwargs or "default", exc)
    raise RendererError("Could not create an OpenGL 3.3 context.\n" + "\n".join(errors))


class Renderer:
    def __init__(self, samples: int = 4) -> None:
        self.ctx = _create_context()
        self.program = self.ctx.program(vertex_shader=VERTEX_SHADER, fragment_shader=FRAGMENT_SHADER)
        self.samples = max(0, min(samples, self.ctx.info.get("GL_MAX_SAMPLES", 0) or 0))
        self._fbos: dict[tuple[int, int], tuple] = {}
        self._uniforms = {name: self.program[name] for name in self.program if isinstance(self.program[name], moderngl.Uniform)}
        self._set("u_joint_tex", 1)
        self._set("u_tex", 0)
        self._white = self.ctx.texture((1, 1), 4, b"\xff\xff\xff\xff")

    def _set(self, name: str, value) -> None:
        u = self._uniforms.get(name)
        if u is not None:
            u.value = value

    def _write(self, name: str, data: bytes) -> None:
        u = self._uniforms.get(name)
        if u is not None:
            u.write(data)

    def upload(self, model: Model, textures: list) -> GpuModel:
        return GpuModel(self.ctx, self.program, model, textures)

    def _targets(self, w: int, h: int):
        key = (w, h)
        fb = self._fbos.get(key)
        if fb is None:
            if len(self._fbos) > 24:
                for old in self._fbos.values():
                    for obj in old:
                        obj.release()
                self._fbos.clear()
            if self.samples:
                color = self.ctx.renderbuffer((w, h), 4, samples=self.samples)
                depth = self.ctx.depth_renderbuffer((w, h), samples=self.samples)
                ms = self.ctx.framebuffer([color], depth)
                rcolor = self.ctx.renderbuffer((w, h), 4)
                resolve = self.ctx.framebuffer([rcolor])
                fb = (ms, resolve, color, depth, rcolor)
            else:
                color = self.ctx.renderbuffer((w, h), 4)
                depth = self.ctx.depth_renderbuffer((w, h))
                ms = self.ctx.framebuffer([color], depth)
                fb = (ms, ms, color, depth)
            self._fbos[key] = fb
        return fb[0], fb[1]

    def render(
        self,
        gpu: GpuModel,
        world: np.ndarray,
        weights: dict[int, np.ndarray],
        frame: Frame,
        yaw: float,
        scale: float = 1.0,
        outline_px: float = 1.5,
        outline_color: tuple[float, float, float] = (0.0, 0.0, 0.0),
    ) -> tuple[int, int, np.ndarray]:
        """Render and return (w, h, rgba[h,w,4]) top-down, premultiplied alpha.

        `scale` is the device pixel ratio: the frame is rendered at higher resolution.
        """
        ctx = self.ctx
        model = gpu.model
        w = max(1, int(round(frame.width * scale)))
        h = max(1, int(round(frame.height * scale)))
        target, resolve = self._targets(w, h)
        target.use()
        ctx.viewport = (0, 0, w, h)
        ctx.clear(0.0, 0.0, 0.0, 0.0, depth=1.0)
        ctx.enable(moderngl.DEPTH_TEST)
        ctx.blend_func = moderngl.ONE, moderngl.ONE_MINUS_SRC_ALPHA

        for si, jt in enumerate(gpu.joint_textures):
            jm = model.joint_matrices(si, world)
            jt.write(np.ascontiguousarray(jm.transpose(0, 2, 1), dtype=np.float32).tobytes())

        self._write("u_view_proj", gl_mat(frame.view_proj()))
        self._write("u_root", gl_mat(rot_y(yaw)))
        self._set("u_viewport", (float(w), float(h)))
        self._set("u_outline_color", tuple(outline_color))

        def bind(it: _DrawItem, outline: bool) -> None:
            mat = model.materials[it.material] if it.material is not None else None
            tex = gpu.texture_for(it.material)
            (tex or self._white).use(0)
            self._set("u_has_tex", tex is not None)
            if mat:
                r, g, b, a = mat.base_color
                self._set("u_color", (srgb_encode(r), srgb_encode(g), srgb_encode(b), a))
                self._set("u_alpha_mode", ALPHA_MODES.get(mat.alpha_mode, 0))
                self._set("u_cutoff", mat.alpha_cutoff)
            else:
                self._set("u_color", (1.0, 1.0, 1.0, 1.0))
                self._set("u_alpha_mode", 0)
            if it.skin is not None:
                gpu.joint_textures[it.skin].use(1)
                self._set("u_skinned", True)
            else:
                self._set("u_skinned", False)
                self._write("u_node", gl_mat(world[it.node]))
            mw = weights.get(it.node)
            mv = [0.0] * MAX_MORPHS
            if mw is not None:
                for k in range(min(it.morph_count, len(mw))):
                    mv[k] = float(mw[k])
            self._set("u_morph", tuple(mv))
            self._set("u_outline_pass", outline)
            self._set("u_outline_px", outline_px * scale if outline else 0.0)

        if not gpu.joint_textures:
            self._white.use(1)

        # Outline pass: back faces of an expanded hull, opaque parts only.
        if outline_px > 0:
            ctx.disable(moderngl.BLEND)
            ctx.enable(moderngl.CULL_FACE)
            ctx.cull_face = "front"
            for it in gpu.items:
                if it.order != 0:
                    continue
                mat = model.materials[it.material] if it.material is not None else None
                if mat and "halo" in mat.name.lower():
                    continue
                bind(it, True)
                it.vao.render(moderngl.TRIANGLES)

        ctx.cull_face = "back"
        for it in gpu.items:
            mat = model.materials[it.material] if it.material is not None else None
            if mat is None or not mat.double_sided:
                ctx.enable(moderngl.CULL_FACE)
            else:
                ctx.disable(moderngl.CULL_FACE)
            if it.order == 2:
                ctx.enable(moderngl.BLEND)
                ctx.depth_mask = False
            else:
                ctx.disable(moderngl.BLEND)
                ctx.depth_mask = True
            bind(it, False)
            it.vao.render(moderngl.TRIANGLES)
        ctx.depth_mask = True
        ctx.disable(moderngl.CULL_FACE)
        ctx.disable(moderngl.BLEND)

        if resolve is not target:
            ctx.copy_framebuffer(resolve, target)
        data = resolve.read(components=4, alignment=1)
        rgba = np.frombuffer(data, np.uint8).reshape(h, w, 4)[::-1]
        return w, h, np.ascontiguousarray(rgba)

    def release(self) -> None:
        for fb in self._fbos.values():
            for obj in fb:
                obj.release()
        self._fbos.clear()
        self._white.release()
        self.program.release()
        self.ctx.release()


TextureDecoder = Callable[[bytes, str], "tuple[int, int, bytes] | None"]


def decode_textures(model: Model, decoder: TextureDecoder) -> list:
    """Decode all images of `model` into (w, h, rgba_bytes) tuples (thread-safe decoder required)."""
    out = []
    for img in model.images:
        try:
            out.append(decoder(img.data, img.mime) if img.data else None)
        except Exception:  # noqa: BLE001 - a broken texture should not kill the model
            log.exception("failed to decode texture")
            out.append(None)
    return out
