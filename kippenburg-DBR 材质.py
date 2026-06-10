"""
Blender (bpy) 脚本：为 TFLN/InP 集成器件示意图配置渲染、相机、影棚灯光、
阴影承接面与一套可复用材质库，复刻"光泽蓝介质 + 金属金 + 自发光青波导"的影棚质感。

用法
  1. 打开含模型几何的 .blend → Scripting 工作区 → 新建脚本粘贴运行；
     或命令行： blender your_model.blend --python this_script.py
  2. 脚本只创建/更新 "FIG_" 前缀的对象与材质，不触碰你的模型几何。
  3. 末尾 apply_material() 按"物体名包含关键字"把材质指过去——按你的命名改关键字。
  4. 取景：数字键盘 0 进相机视角，微调 FIG_Camera 的位置/旋转。
兼容 Blender 3.6 / 4.x（Principled BSDF 接口名差异已做兼容）。
"""

import bpy, math
from mathutils import Vector

# ── 0. 排版参数（按论文版面改这里） ──────────────────────────────
TARGET_WIDTH_MM = 180.0  # 双栏宽；单栏用 ~88
TARGET_DPI = 600
ASPECT = 0.5  # 高/宽 比
RENDER_SAMPLES = 1024
PREFIX = "FIG_"  # 脚本创建对象的统一前缀，保证幂等重建


def mm_to_px(mm, dpi):
    return int(round(mm / 25.4 * dpi))


RES_X = mm_to_px(TARGET_WIDTH_MM, TARGET_DPI)
RES_Y = int(RES_X * ASPECT)


# ── 工具：按候选名设置 BSDF 输入，兼容不同版本接口名 ─────────────
def _set(bsdf, names, value):
    names = [names] if isinstance(names, str) else names
    for n in names:
        if n in bsdf.inputs:
            bsdf.inputs[n].default_value = value
            return True
    return False


# ── 1. 渲染设置：Cycles + 去噪 + 透明背景 + 16bit PNG ────────────
def setup_render(scene):
    scene.render.engine = 'CYCLES'
    try:
        scene.cycles.device = 'GPU'  # 需先在 偏好设置 里启用 GPU
    except Exception:
        pass
    cyc = scene.cycles
    cyc.samples = RENDER_SAMPLES
    cyc.use_denoising = True
    try:
        cyc.denoiser = 'OPENIMAGEDENOISE'  # CPU/任意显卡可用；N卡可改 'OPTIX'
    except Exception:
        pass
    cyc.use_adaptive_sampling = True

    r = scene.render
    r.resolution_x, r.resolution_y = RES_X, RES_Y
    r.resolution_percentage = 100
    r.film_transparent = True  # 透明背景 → 后期合成
    if hasattr(r, 'filter_size'):
        r.filter_size = 1.5  # 抗锯齿宽度

    img = r.image_settings
    img.file_format = 'PNG'
    img.color_mode = 'RGBA'
    img.color_depth = '16'

    scene.view_settings.view_transform = 'Standard'  # 示意图：颜色平实准确
    scene.view_settings.look = 'None'


# ── 2. 世界环境：冷白低强度补光，避免全黑反射 ──────────────────
def setup_world(scene):
    world = scene.world or bpy.data.worlds.new(PREFIX + "World")
    scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs["Color"].default_value = (0.90, 0.92, 0.95, 1.0)
        bg.inputs["Strength"].default_value = 0.4


# ── 3. 场景包围盒（用于按模型尺度自适应放相机/灯，避免写死单位） ──
def scene_bounds():
    mins, maxs = Vector((1e9,) * 3), Vector((-1e9,) * 3)
    found = False
    for obj in bpy.context.scene.objects:
        if obj.type != 'MESH' or obj.name.startswith(PREFIX):
            continue
        found = True
        for c in obj.bound_box:
            w = obj.matrix_world @ Vector(c)
            mins = Vector(map(min, mins, w))
            maxs = Vector(map(max, maxs, w))
    if not found:
        return Vector((-1, -1, -0.2)), Vector((1, 1, 0.2))
    return mins, maxs


def _purge_prefixed():
    for obj in list(bpy.data.objects):
        if obj.name.startswith(PREFIX):
            bpy.data.objects.remove(obj, do_unlink=True)


# ── 4. 三点影棚柔光（能量随尺度平方缩放，单位无关） ─────────────
def add_area_light(name, location, rotation, size, energy):
    light = bpy.data.lights.new(PREFIX + name, type='AREA')
    light.size, light.energy = size, energy
    obj = bpy.data.objects.new(PREFIX + name, light)
    obj.location, obj.rotation_euler = location, rotation
    bpy.context.collection.objects.link(obj)
    return obj


# ── 5. 相机：左前上方俯视（RSOA 端更近），中长焦低畸变 ──────────
def add_camera(center, diag):
    cam_data = bpy.data.cameras.new(PREFIX + "Camera")
    cam_data.type, cam_data.lens = 'PERSP', 70  # 想要严格正交可改 'ORTHO'
    cam = bpy.data.objects.new(PREFIX + "Camera", cam_data)
    cam.location = center + Vector((-1.1, -1.4, 0.9)) * diag
    bpy.context.collection.objects.link(cam)

    target = bpy.data.objects.new(PREFIX + "CamTarget", None)
    target.location = center
    bpy.context.collection.objects.link(target)
    con = cam.constraints.new('TRACK_TO')
    con.target, con.track_axis, con.up_axis = target, 'TRACK_NEGATIVE_Z', 'UP_Y'
    bpy.context.scene.camera = cam
    return cam


# ── 6. 阴影承接面：白底只接阴影，配合透明背景出软接触阴影 ──────
def add_shadow_catcher(center, diag, z_bottom):
    bpy.ops.mesh.primitive_plane_add(
        size=diag * 6, location=(center.x, center.y, z_bottom)
    )
    plane = bpy.context.active_object
    plane.name = PREFIX + "ShadowCatcher"
    plane.is_shadow_catcher = True
    return plane


# ── 7. 材质库 ───────────────────────────────────────────────
def _new_material(name):
    if old := bpy.data.materials.get(name):
        bpy.data.materials.remove(old)
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    return mat, mat.node_tree.nodes.get("Principled BSDF")


def mat_sio2_blue():  # 蓝色 SiO₂ 板体：光泽 + 清漆高光
    mat, b = _new_material(PREFIX + "SiO2_Blue")
    _set(b, "Base Color", (0.05, 0.22, 0.85, 1.0))
    _set(b, "Roughness", 0.18)
    _set(b, ["Coat Weight", "Clearcoat"], 0.4)
    _set(b, ["Coat Roughness", "Clearcoat Roughness"], 0.05)
    return mat


def mat_tfln_violet():  # TFLN 薄层：亮紫 + 微自发光，做出"亮线"感
    mat, b = _new_material(PREFIX + "TFLN_Violet")
    _set(b, "Base Color", (0.45, 0.15, 0.95, 1.0))
    _set(b, "Roughness", 0.3)
    _set(b, ["Emission Color", "Emission"], (0.45, 0.15, 0.95, 1.0))
    _set(b, "Emission Strength", 0.6)
    return mat


def mat_si_dark():  # Si 衬底：深灰、半哑光
    mat, b = _new_material(PREFIX + "Si_Dark")
    _set(b, "Base Color", (0.06, 0.06, 0.07, 1.0))
    _set(b, "Metallic", 0.2)
    _set(b, "Roughness", 0.55)
    return mat


def mat_gold():  # Au 电极：金属金
    mat, b = _new_material(PREFIX + "Gold_Au")
    _set(b, "Base Color", (1.0, 0.76, 0.30, 1.0))
    _set(b, "Metallic", 1.0)
    _set(b, "Roughness", 0.28)
    return mat


def mat_gold_grainy():  # RSOA 镀金顶面：噪声驱动粗糙度，做颗粒质感
    mat, b = _new_material(PREFIX + "Gold_Grainy")
    _set(b, "Base Color", (1.0, 0.74, 0.28, 1.0))
    _set(b, "Metallic", 1.0)
    nt = mat.node_tree
    noise = nt.nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = 220.0
    mr = nt.nodes.new("ShaderNodeMapRange")
    mr.inputs["To Min"].default_value = 0.22
    mr.inputs["To Max"].default_value = 0.5
    nt.links.new(noise.outputs["Fac"], mr.inputs["Value"])
    nt.links.new(mr.outputs["Result"], b.inputs["Roughness"])
    return mat


def mat_submount_gray():  # RSOA 灰色底座：浅灰光泽
    mat, b = _new_material(PREFIX + "Submount_Gray")
    _set(b, "Base Color", (0.62, 0.63, 0.66, 1.0))
    _set(b, "Metallic", 0.3)
    _set(b, "Roughness", 0.35)
    return mat


def mat_waveguide_cyan():  # 波导 + 光栅点阵：自发光青色
    mat, b = _new_material(PREFIX + "Waveguide_Cyan")
    _set(b, "Base Color", (0.10, 0.85, 1.0, 1.0))
    _set(b, ["Emission Color", "Emission"], (0.10, 0.85, 1.0, 1.0))
    _set(b, "Emission Strength", 4.0)
    return mat


def mat_probe_dark():  # 探针：深色金属
    mat, b = _new_material(PREFIX + "Probe_Dark")
    _set(b, "Base Color", (0.04, 0.04, 0.05, 1.0))
    _set(b, "Metallic", 1.0)
    _set(b, "Roughness", 0.2)
    return mat


# ── 8. 按物体名关键字批量指派材质 ──────────────────────────────
#   注意：是"子串包含"匹配。别用 "si" 这种会撞上 "SiO2" 的关键字；
#   建议给各物体起唯一名字（或在此改成你的实际命名）。
def apply_material(keyword, mat):
    n = 0
    for obj in bpy.context.scene.objects:
        if obj.type != 'MESH' or obj.name.startswith(PREFIX):
            continue
        if keyword.lower() in obj.name.lower():
            obj.data.materials.clear()
            obj.data.materials.append(mat)
            n += 1
    print(f"  [{keyword}] -> {n} object(s)")


# ── 主流程 ──────────────────────────────────────────────────
def main():
    scene = bpy.context.scene
    _purge_prefixed()
    setup_render(scene)
    setup_world(scene)

    mins, maxs = scene_bounds()
    center = (mins + maxs) / 2.0
    diag = (maxs - mins).length or 2.0
    e = diag * diag  # 灯光能量 ∝ 尺度²，保持单位无关的照度

    add_area_light(
        "Key",
        center + Vector((-1.0, -1.2, 1.6)) * diag,
        (math.radians(40), 0, math.radians(-30)),
        diag * 1.2,
        e * 1200,
    )
    add_area_light(
        "Fill",
        center + Vector((1.3, -0.8, 0.8)) * diag,
        (math.radians(60), 0, math.radians(40)),
        diag * 1.6,
        e * 400,
    )
    add_area_light(
        "Rim",
        center + Vector((0.2, 1.4, 1.0)) * diag,
        (math.radians(120), 0, 0),
        diag * 1.0,
        e * 700,
    )

    add_camera(center, diag)
    add_shadow_catcher(center, diag, mins.z)

    # 把关键字改成你工程里的实际物体名！
    apply_material("SiO2", mat_sio2_blue())
    apply_material("TFLN", mat_tfln_violet())
    apply_material("Si_sub", mat_si_dark())  # 给硅衬底起含 Si_sub 的唯一名
    apply_material("Au", mat_gold())
    apply_material("electrode", mat_gold())
    apply_material("RSOA", mat_gold_grainy())
    apply_material("submount", mat_submount_gray())
    apply_material("waveguide", mat_waveguide_cyan())
    apply_material("grating", mat_waveguide_cyan())
    apply_material("probe", mat_probe_dark())

    print(f"FIG setup done. Resolution: {RES_X} x {RES_Y}")


main()
