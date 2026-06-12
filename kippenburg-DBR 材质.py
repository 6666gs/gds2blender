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
ASPECT = 0.5  # 高/宽 比
PREFIX = "FIG_"  # 脚本创建对象的统一前缀，保证幂等重建

# ── 性能/质量：先用草稿模式快速预览，满意后把 DRAFT 改成 False 出高清 ──
DRAFT = True
TARGET_DPI = 150 if DRAFT else 600  # 草稿≈1063×531；出版 600dpi≈4252×2126
RENDER_SAMPLES = 64 if DRAFT else 1024  # 采样数；配合降噪，草稿 64 已足够干净

# ── 画面整体曝光（单位=档/EV），负值更暗。太亮就调更负(如 -4)，太暗就调大 ──
EXPOSURE = -3.0

# ── 地面/背景模式 ──
#   'STUDIO'        : 浅灰影棚地面 + 底部柔和接触阴影，直接渲染出“放在盒子里、很真实”的成片
#   'SHADOW_CATCHER': 透明背景只接阴影，方便后期把器件抠到论文白底上合成
GROUND_MODE = 'STUDIO'

# ── 斑驳颗粒密度：整条器件(对角线)上大约有多少颗噪点 ──
#   关键：GDS 单位常是微米/纳米，物体坐标极大，固定噪声 Scale 会让颗粒细到看不见
#   （渲染成一整片）。这里按【场景尺度】自适应换算，单位再大也能看见颗粒。
#   看不见颗粒 → 调大；颗粒太碎/太噪 → 调小。
GRAIN_DENSITY = 260.0
_SCENE_REF = 1.0  # 由 main() 在得到 diag 后写入；add_grain 用它把密度换算到场景尺度


def mm_to_px(mm, dpi):
    return int(round(mm / 25.4 * dpi))


RES_X = mm_to_px(TARGET_WIDTH_MM, TARGET_DPI)
RES_Y = int(RES_X * ASPECT)


# ── 工具：按候选名设置 BSDF 输入，兼容不同版本接口名 ─────────────
#   同时按 socket 的 name 和 identifier 匹配：identifier 始终是英文规范名，
#   即使 Blender 界面是中文（开启"翻译→新建数据"）也能命中。
def _set(bsdf, names, value):
    if bsdf is None:
        return False
    names = [names] if isinstance(names, str) else names
    for n in names:
        for sock in bsdf.inputs:
            if sock.name == n or sock.identifier == n:
                sock.default_value = value
                return True
    return False


# ── 工具：按候选名取得 socket 对象本体（用于“连线”，而非仅设默认值）──
def _socket(node, names):
    if node is None:
        return None
    names = [names] if isinstance(names, str) else names
    for n in names:
        for sock in node.inputs:
            if sock.name == n or sock.identifier == n:
                return sock
    return None


# ── 工具：按节点类型查找，避免界面语言/"翻译新建数据"导致节点名不是英文 ──
#   node.type 始终是英文枚举（如 'BSDF_PRINCIPLED'），不受 UI 语言影响。
def _node_of_type(nodes, type_name):
    for node in nodes:
        if node.type == type_name:
            return node
    return None


# ── 工具：尽量启用 GPU 渲染（N卡 OptiX/CUDA、A卡 HIP、Intel oneAPI），失败回退 CPU ──
def enable_gpu(scene):
    try:
        prefs = bpy.context.preferences.addons['cycles'].preferences
    except (KeyError, AttributeError):
        scene.cycles.device = 'CPU'
        return "CPU（未找到 Cycles 偏好）"
    for backend in ('OPTIX', 'CUDA', 'HIP', 'ONEAPI', 'METAL'):
        try:
            prefs.compute_device_type = backend
        except (TypeError, AttributeError):
            continue  # 当前系统不支持该后端
        try:
            prefs.get_devices()
        except Exception:
            try:
                prefs.refresh_devices()
            except Exception:
                pass
        gpus = [d for d in prefs.devices if d.type == backend]
        if gpus:
            for d in prefs.devices:
                d.use = (d.type == backend)  # 仅启用该后端的 GPU 设备
            scene.cycles.device = 'GPU'
            return f"GPU（{backend}）：" + "，".join(g.name for g in gpus)
    scene.cycles.device = 'CPU'
    return "CPU（未检测到受支持的 GPU；核显多数走 CPU）"


# ── 1. 渲染设置：Cycles + 去噪 + 透明背景 + 16bit PNG ────────────
def setup_render(scene):
    scene.render.engine = 'CYCLES'
    print("Render device:", enable_gpu(scene))
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
    r.film_transparent = (GROUND_MODE != 'STUDIO')  # STUDIO 用不透明浅灰背景；否则透明便于合成
    if hasattr(r, 'filter_size'):
        r.filter_size = 1.5  # 抗锯齿宽度

    img = r.image_settings
    img.file_format = 'PNG'
    img.color_mode = 'RGBA'
    img.color_depth = '16'

    scene.view_settings.view_transform = 'Standard'  # 示意图：颜色平实准确
    scene.view_settings.look = 'None'
    scene.view_settings.exposure = EXPOSURE  # 整体压暗：灯光过曝白成一片时往更负调


# ── 2. 世界环境：冷白低强度补光，避免全黑反射 ──────────────────
def setup_world(scene):
    world = scene.world or bpy.data.worlds.new(PREFIX + "World")
    scene.world = world
    world.use_nodes = True
    bg = _node_of_type(world.node_tree.nodes, 'BACKGROUND')
    if bg:
        if GROUND_MODE == 'STUDIO':
            _set(bg, "Color", (0.86, 0.88, 0.92, 1.0))  # 浅灰：影棚白盒氛围 + 金属反射
            _set(bg, "Strength", 0.4)  # 别太亮，否则环境光会把底部阴影“冲淡”到看不见
        else:
            _set(bg, "Color", (0.90, 0.92, 0.95, 1.0))
            _set(bg, "Strength", 0.4)


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


# ── 6b. 影棚地面：浅灰大平面，直接承接柔和接触阴影 → “放在盒子里、很真实”──
#   平面很大 + 浅灰世界背景 → 远处渐隐为背景色，看不到地平线，呈无缝白盒。
def add_studio_ground(center, diag, z_bottom, color=(0.85, 0.86, 0.88)):
    bpy.ops.mesh.primitive_plane_add(
        size=diag * 40,  # 足够大，铺满画面、无缝
        location=(center.x, center.y, z_bottom - diag * 0.003),  # 略低于器件底，避免共面闪烁
    )
    plane = bpy.context.active_object
    plane.name = PREFIX + "StudioGround"
    mat, b = _new_material(PREFIX + "StudioGround")
    _set(b, "Base Color", (*color, 1.0))
    _set(b, "Roughness", 0.7)  # 哑光地面：阴影柔、不抢反射
    _set(b, "Metallic", 0.0)
    plane.data.materials.append(mat)
    return plane


# ── 7. 材质库 ───────────────────────────────────────────────
def _new_material(name):
    if old := bpy.data.materials.get(name):
        bpy.data.materials.remove(old)
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = _node_of_type(nt.nodes, 'BSDF_PRINCIPLED')
    if bsdf is None:  # 极少数情况默认节点缺失：自建并接到材质输出（用索引连，避免名字依赖）
        bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
        out = _node_of_type(nt.nodes, 'OUTPUT_MATERIAL') or nt.nodes.new("ShaderNodeOutputMaterial")
        nt.links.new(bsdf.outputs[0], out.inputs[0])
    return mat, bsdf


def mat_sio2_blue():  # 深蓝 SiO₂ 板体（底部埋层/薄膜底包层）：光泽 + 清漆 + 微颗粒
    mat, b = _new_material(PREFIX + "SiO2_Blue")
    _set(b, ["Coat Weight", "Clearcoat"], 0.4)
    _set(b, ["Coat Roughness", "Clearcoat Roughness"], 0.05)
    add_grain(  # 介质：很淡的颗粒，仅在光泽面上隐约可见
        mat, b, base_color=(0.01, 0.06, 0.32),  # 深海军蓝：和亮紫 TFLN 拉开
        dark=0.7, light=1.6, rough=(0.14, 0.24),
        cells=300.0, bump_cells=1100.0, bump_strength=0.05,
    )
    return mat


def mat_sio2_clad_clear():  # 透明 SiO₂ 上包层：玻璃质感 + 可见玻璃边缘
    mat, b = _new_material(PREFIX + "SiO2_Clad_Clear")
    _set(b, "Base Color", (0.90, 0.95, 1.0, 1.0))             # 近无色，极淡冷调
    _set(b, ["Transmission Weight", "Transmission"], 1.0)      # 全透射 → 玻璃
    _set(b, "Roughness", 0.04)                                # 越小越清澈
    _set(b, "IOR", 1.46)                                      # 熔融石英折射率
    if hasattr(mat, "use_screen_refraction"):
        mat.use_screen_refraction = True  # EEVEE 折射；Cycles 无害

    # 让“玻璃边缘”可见：透明背景下纯玻璃没有可反射的环境，边缘会消失。
    #   用 Layer Weight 的 Fresnel（正面≈0、掠射边缘≈1）→ ColorRamp 收窄成
    #   “只有很边缘才亮”，喂给自发光 → 给玻璃勾出一圈冷白边线（透明背景下也看得见）。
    nt = mat.node_tree
    lw = nt.nodes.new("ShaderNodeLayerWeight")
    lw.inputs["Blend"].default_value = 0.35  # 越大边线越宽
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    cr = ramp.color_ramp
    cr.elements[0].position = 0.60
    cr.elements[0].color = (0.0, 0.0, 0.0, 1.0)              # 主体不发光（保持透明清澈）
    cr.elements[1].position = 0.92
    cr.elements[1].color = (0.55, 0.75, 1.0, 1.0)           # 边缘冷白蓝
    nt.links.new(lw.outputs["Fresnel"], ramp.inputs["Fac"])
    if (em := _socket(b, ["Emission Color", "Emission"])) is not None:
        nt.links.new(ramp.outputs["Color"], em)
    _set(b, "Emission Strength", 1.5)                        # 边线亮度；太抢眼就调小

    # 提示：想要“平贴覆盖”而非折射玻璃，把上面 Transmission 改 0 并启用 Alpha：
    #   _set(b, "Alpha", 0.25); mat.blend_method = 'BLEND'   # blend_method 仅 EEVEE 需要
    return mat


def mat_tfln_violet():  # TFLN 薄层：亮紫 + 微自发光 + 微颗粒
    mat, b = _new_material(PREFIX + "TFLN_Violet")
    _set(b, ["Emission Color", "Emission"], (0.45, 0.15, 0.95, 1.0))
    _set(b, "Emission Strength", 0.6)
    add_grain(
        mat, b, base_color=(0.45, 0.15, 0.95),
        dark=0.78, light=1.18, rough=(0.24, 0.4),
        cells=320.0, bump_cells=1100.0, bump_strength=0.07,
    )
    return mat


def mat_si_dark():  # Si 衬底：深灰、半哑光 + 颗粒（晶圆/芯片基底质感）
    mat, b = _new_material(PREFIX + "Si_Dark")
    _set(b, "Metallic", 0.2)
    add_grain(
        mat, b, base_color=(0.06, 0.06, 0.07),
        dark=0.6, light=1.9, rough=(0.48, 0.66),
        cells=260.0, bump_cells=900.0, bump_strength=0.12,
    )
    return mat


def mat_gold():  # Au 电极：金属金
    mat, b = _new_material(PREFIX + "Gold_Au")
    _set(b, "Base Color", (1.0, 0.76, 0.30, 1.0))
    _set(b, "Metallic", 1.0)
    _set(b, "Roughness", 0.38)  # 略糙：顶光反射铺成一片亮金，电极不再是暗面（太亮就调回 0.28）
    return mat


# ── 工具：给 Principled BSDF 叠加“噪声斑驳”——同时扰动 [基色明暗] [粗糙度] [凹凸] ──
#   关键修复：用 Object 坐标【经 Mapping 缩放】，颗粒密度按【场景尺度】自适应。
#   GDS 单位常是微米/纳米、物体坐标极大，固定噪声 Scale 会让颗粒细到看不见（渲染成一片）。
#   cells = 整条器件(对角线)上的颗粒数（默认取 GRAIN_DENSITY）；看不见就调大、太碎就调小。
def add_grain(
    mat,
    bsdf,
    base_color,
    dark=0.5,
    light=1.3,
    rough=(0.25, 0.6),
    cells=None,
    bump_cells=900.0,
    bump_strength=0.25,
):
    nt = mat.node_tree
    r, g, bl = base_color[:3]
    clamp = lambda x: max(0.0, min(1.0, x))
    cells = GRAIN_DENSITY if cells is None else cells
    scale = cells / _SCENE_REF if _SCENE_REF else cells  # 物体坐标→颗粒密度，随尺度自适应

    # Object 坐标 → Mapping 统一缩放，使后面所有噪声密度都按场景尺度走
    tc = nt.nodes.new("ShaderNodeTexCoord")
    mapping = nt.nodes.new("ShaderNodeMapping")
    mapping.inputs["Scale"].default_value = (scale, scale, scale)
    if (mv := _socket(mapping, "Vector")) is not None:
        nt.links.new(tc.outputs["Object"], mv)

    # 主噪声（密度=cells）→ 同时驱动基色明暗与粗糙度
    n1 = nt.nodes.new("ShaderNodeTexNoise")
    n1.inputs["Scale"].default_value = 1.0
    if "Detail" in n1.inputs:
        n1.inputs["Detail"].default_value = 2.0
    if (s := _socket(n1, "Vector")) is not None:
        nt.links.new(mapping.outputs["Vector"], s)

    # 基色：noise → ColorRamp（暗↔亮两色）→ Base Color，做出明暗斑驳
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    cr = ramp.color_ramp
    cr.elements[0].position = 0.30
    cr.elements[0].color = (clamp(r * dark), clamp(g * dark), clamp(bl * dark), 1.0)
    cr.elements[1].position = 0.72
    cr.elements[1].color = (clamp(r * light), clamp(g * light), clamp(bl * light), 1.0)
    nt.links.new(n1.outputs["Fac"], ramp.inputs["Fac"])
    if (bc := _socket(bsdf, "Base Color")) is not None:
        nt.links.new(ramp.outputs["Color"], bc)

    # 粗糙度：noise → MapRange → Roughness，做出忽亮忽哑的斑驳高光
    mr = nt.nodes.new("ShaderNodeMapRange")
    mr.inputs["To Min"].default_value = rough[0]
    mr.inputs["To Max"].default_value = rough[1]
    nt.links.new(n1.outputs["Fac"], mr.inputs["Value"])
    if (rg := _socket(bsdf, "Roughness")) is not None:
        nt.links.new(mr.outputs["Result"], rg)

    # 凹凸：更细的 noise（密度=bump_cells）→ Bump → Normal，把高光真正“打碎”成颗粒
    if bump_strength > 0:
        n2 = nt.nodes.new("ShaderNodeTexNoise")
        n2.inputs["Scale"].default_value = max(bump_cells / cells, 0.001)
        if "Detail" in n2.inputs:
            n2.inputs["Detail"].default_value = 4.0
        if (s2 := _socket(n2, "Vector")) is not None:
            nt.links.new(mapping.outputs["Vector"], s2)
        bump = nt.nodes.new("ShaderNodeBump")
        bump.inputs["Strength"].default_value = bump_strength
        nt.links.new(n2.outputs["Fac"], bump.inputs["Height"])
        if (nrm := _socket(bsdf, "Normal")) is not None:
            nt.links.new(bump.outputs["Normal"], nrm)
    return mat


def mat_gold_grainy():  # 镀金顶面（RSOA/submount 顶电极、外腔芯片 Au 共用）：明显颗粒斑驳
    mat, b = _new_material(PREFIX + "Gold_Grainy")
    _set(b, "Metallic", 1.0)
    add_grain(
        mat,
        b,
        base_color=(1.0, 0.74, 0.28),
        dark=0.45,  # 暗斑：偏暗的金
        light=1.15,  # 亮斑：偏亮的金（会被 clamp 到 1）
        rough=(0.25, 0.62),  # 粗糙度斑驳区间
        cells=240.0,  # 颗粒数（看不见→调大 GRAIN_DENSITY 或这里）
        bump_cells=850.0,
        bump_strength=0.32,  # 凹凸强度：嫌太花就调小
    )
    return mat


def mat_submount_gray():  # RSOA 灰色底座/衬底：中灰 + 明显颗粒斑驳（陶瓷/镀层质感）
    mat, b = _new_material(PREFIX + "Submount_Gray")
    _set(b, "Metallic", 0.3)
    add_grain(
        mat,
        b,
        base_color=(0.42, 0.43, 0.46),
        dark=0.6,  # 暗斑
        light=1.35,  # 亮斑
        rough=(0.4, 0.72),  # 整体偏哑光，斑驳更像粗糙衬底
        cells=200.0,  # 衬底颗粒可稍大块
        bump_cells=700.0,
        bump_strength=0.38,  # 衬底凹凸可稍强
    )
    return mat


def mat_waveguide_cyan():  # 波导 + 光栅点阵：自发光青色
    mat, b = _new_material(PREFIX + "Waveguide_Cyan")
    _set(b, "Base Color", (0.10, 0.85, 1.0, 1.0))
    _set(b, ["Emission Color", "Emission"], (0.10, 0.85, 1.0, 1.0))
    _set(b, "Emission Strength", 4.0)
    return mat


def mat_rsoa_red():  # InP RSOA 增益区波导：红色自发光（自发辐射）
    mat, b = _new_material(PREFIX + "RSOA_Red")
    _set(b, "Base Color", (0.60, 0.02, 0.02, 1.0))
    _set(b, ["Emission Color", "Emission"], (1.0, 0.06, 0.03, 1.0))
    _set(b, "Emission Strength", 5.0)  # 想更亮/更晕就调大
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
            if obj.data.users > 1:  # 共享网格先转单用户，避免连带改到别的物体
                obj.data = obj.data.copy()
            obj.data.materials.clear()
            obj.data.materials.append(mat)
            n += 1
    print(f"  [{keyword}] -> {n} object(s)")


def assign_material(obj_names, mat):
    """按【精确物体名】把【同一个】材质指派给一个或多个物体，替换其全部材质槽。

    obj_names: 单个物体名字符串，或物体名列表，例如：
        wg = mat_waveguide_cyan()                 # 只调用工厂【一次】，拿到材质对象
        assign_material("Layer 41/0.001", wg)     # 单个
        assign_material(["Layer 41/0.001", "Layer 42/0.001"], wg)  # 多个共用

    ⚠ 多物体共用同一材质时，务必只调用一次材质工厂函数（mat_xxx()）并复用返回的
      mat 对象。【不要】对每个物体各调一次工厂——工厂内部会先 remove 掉同名旧材质，
      而 remove 会把该材质从【所有】已指派的物体上解绑，导致只有最后一个物体留住材质。
    """
    names = [obj_names] if isinstance(obj_names, str) else list(obj_names)
    for obj_name in names:
        obj = bpy.data.objects.get(obj_name)
        if obj is None:
            print(f"  ⚠ 找不到物体：{obj_name!r}")
            continue
        if obj.type != 'MESH':
            print(f"  ⚠ {obj_name!r} 不是网格，跳过")
            continue
        # 网格数据被多物体共享时先转单用户，避免改一个连带改了另一个
        if obj.data.users > 1:
            obj.data = obj.data.copy()
        obj.data.materials.clear()
        obj.data.materials.append(mat)
        print(f"  ✓ {obj_name!r} ← {mat.name}")


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

    global _SCENE_REF
    _SCENE_REF = diag  # 让 add_grain 的颗粒密度按场景尺度走（GDS 单位再大也看得见颗粒）

    # ── 灯光布局（针对“RSOA 端比 TFLN 端亮”“金电极偏暗、衬底过曝”重做）──
    #   器件是沿 X 的长条：RSOA 在 -X 端，TFLN 在 +X 端。
    #   要点：主光【居中、抬高、加大】→ 全长照度均匀 + 水平金属能反射它而变亮。
    #   调节速查：
    #     · 整体太亮/太暗 → 改文件顶部 EXPOSURE（更负=更暗），或等比缩放下面 energy。
    #     · 衬底仍过曝而金属偏暗 → 调小 Fill 能量、保持 Key 顶光。
    #     · 哪端偏暗 → 把 Key 的 X 往那端挪一点，或加大 Rim 能量。

    # 主光（Key）：X 居中（保证全长均匀）、偏【后】上方并向前下倾。
    #   ← 解决“看不到底部阴影”：光从器件后上方来 → 把柔和阴影投到器件【前方】地面
    #     （朝相机一侧），加上世界环境光调暗，阴影就清晰可见、器件像“放进盒子里”。
    #   ← 仍保持 X 居中，RSOA/TFLN 两端照度均匀；面积适中 → 阴影够柔又留得住。
    add_area_light(
        "Key",
        center + Vector((0.0, 0.7, 1.9)) * diag,  # X 居中、偏后、抬高
        (math.radians(-26), 0, 0),  # 向前下倾 → 阴影投向 −Y（相机侧）
        diag * 2.0,  # 适中面积：够柔又留得住接触阴影
        e * 1300,
    )
    # 补光（Fill）：相机侧(−Y)正面柔光，照亮朝相机的前立面；弱一些，别填掉阴影。
    add_area_light(
        "Fill",
        center + Vector((-0.4, -1.6, 0.8)) * diag,
        (math.radians(74), 0, math.radians(-8)),
        diag * 2.2,
        e * 300,
    )
    # 轮廓光（Rim）：从 TFLN 端(+X)侧后方勾金属边，并给远端补亮、平衡左右。
    add_area_light(
        "Rim",
        center + Vector((1.0, 0.9, 1.2)) * diag,
        (math.radians(120), 0, math.radians(20)),
        diag * 1.4,
        e * 500,
    )

    add_camera(center, diag)
    if GROUND_MODE == 'STUDIO':
        add_studio_ground(center, diag, mins.z)  # 浅灰地面 + 接触阴影，成片“在盒子里”
    else:
        add_shadow_catcher(center, diag, mins.z)  # 透明背景只接阴影，便于后期合成

    # 把关键字改成你工程里的实际物体名！
    # apply_material("SiO2", mat_sio2_blue())
    # apply_material("TFLN", mat_tfln_violet())
    # apply_material("Si_sub", mat_si_dark())  # 给硅衬底起含 Si_sub 的唯一名
    # apply_material("Au", mat_gold())
    # apply_material("electrode", mat_gold())
    # apply_material("RSOA", mat_gold_grainy())
    # apply_material("submount", mat_submount_gray())
    # apply_material("waveguide", mat_waveguide_cyan())
    # apply_material("grating", mat_waveguide_cyan())
    # apply_material("probe", mat_probe_dark())

    # 用你大纲里的真实名字，一一对应。
    # ★ 多个物体共用同一材质：把工厂函数的返回值【先存进变量】，再传给 assign_material，
    #   并用列表一次性指给多个物体；切勿对每个物体各调一次 mat_xxx()（见 assign_material 注释）。
    sio2_blue = mat_sio2_blue()           # 底部 SiO₂ 埋层/薄膜底包层 → 蓝色介质
    sio2_clad = mat_sio2_clad_clear()     # 上方 SiO₂ 包层 → 透明玻璃（带可见边缘）
    wg_cyan = mat_waveguide_cyan()        # 外腔 LNOI 波导/光栅 → 青色自发光
    rsoa_red = mat_rsoa_red()             # RSOA 增益区波导 → 红色自发光
    gold_grainy = mat_gold_grainy()       # 所有金电极（RSOA/submount/外腔芯片）→ 斑驳金
    submount = mat_submount_gray()        # submount 灰色衬底 → 斑驳灰

    assign_material("GDS_Substrate.002", sio2_blue)
    assign_material("Layer 41/0.001", wg_cyan)
    assign_material("Layer 46/0.001", gold_grainy)  # 外腔芯片 Au 电极 → 斑驳金（原为光面金）

    # —— 新增材质的指派示例（把名字换成你工程里的真实物体名；多个名字用列表）——
    # ★ 斑驳金一份材质可同时指给多块电极（RSOA 顶电极 / submount 顶电极 / 外腔芯片 Au）：
    # assign_material(["RSOA_TopElectrode", "Submount_Electrode", "Layer 46/0.001"], gold_grainy)
    # assign_material("Submount", submount)                           # submount 斑驳灰衬底
    # assign_material(["LNOI_Cladding", "SiO2_Top.001"], sio2_clad)   # 透明上包层（带可见边缘）
    # assign_material("RSOA_Waveguide", rsoa_red)                     # RSOA 红色自发光波导
    # assign_material(["Layer 41/0.001", "Layer 41/0.002"], wg_cyan)  # 同一青色材质给多段波导

    print(f"FIG setup done. Resolution: {RES_X} x {RES_Y}")


main()
