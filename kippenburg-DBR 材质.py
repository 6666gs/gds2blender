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
#   ★ 这是“整体亮度总开关”，每次出图先调它。波导改为“纯受光实心”后不再自发光，
#     场景靠三盏 softbox 把金属/波导打亮，目标是“干净、立体、深灰底衬托”，
#     一般落在 -1.2 ~ -0.3；太暗就往 0 调，过曝白片就往更负调。
EXPOSURE = -2

# ── 背景风格（三选一）──────────────────────────────────────────
#   'FLAT_DARK'     : 中性深灰【平】背景 + 同色地面(当前附图的样子，干净衬托) ← 默认
#   'DARK_GRADIENT' : 深蓝【径向】渐变 + 自带暗角(发光氛围最强，配合 Glare 辉光)
#   'LIGHT_STUDIO'  : 浅灰影棚(金属反射最均匀，最像论文白底配图)
BACKGROUND_STYLE = 'LIGHT_STUDIO'
# 深灰平背景的灰度/亮度：嫌背景太暗就调大，太亮就调小（与 EXPOSURE 配合微调）
FLAT_BG_GRAY = 0.045
FLAT_BG_STRENGTH = 1.0

# ── 出图用哪个机位：1=出光特写(外腔出光口在右下、离镜头最近)；2=波导俯视(高角度看清整体走向) ──
#   两个相机都会创建好；想临时切换预览：在视图里选中目标相机 → Ctrl+小键盘0。
ACTIVE_CAMERA = 2

# ── 地面/背景模式 ──
#   'STUDIO'        : 实色地面 + 底部柔和接触阴影，直接渲染出“放在盒子里、很真实”的成片
#   'SHADOW_CATCHER': 透明背景只接阴影，方便后期把器件抠到论文白底上合成
GROUND_MODE = 'STUDIO'

# ── 斑驳颗粒：两个总开关，所有“斑驳材质”(电极/submount/Si/介质)都【真的】读它们 ──
#   ★ 旧版 bug：各材质把 cells 写死，改这里没反应。现已全部改为读这两个值，立即生效。
#
#   GRAIN_DENSITY = 颗粒【密度/细度】：整条器件(对角线)上大约多少颗噪点。
#     越大 → 颗粒越【细】(消“马赛克大色块”就调大，如 700~1200)；越小 → 颗粒越粗。
#   GRAIN_STRENGTH = 颗粒【强度】总开关：0.0 = 完全光滑无噪点；1.0 = 很明显。
#     嫌斑驳/马赛克太重就往 0 调(如 0.3)；想要明显磨砂质感就往 1 调。
GRAIN_DENSITY = 200.0
GRAIN_STRENGTH = 1
_SCENE_REF = 1.0  # 由 main() 在得到 diag 后写入；add_grain 用它把密度换算到场景尺度

# ── 波导外观（纯受光实心：靠高光显立体，不自发光）─────────────────
#   WAVEGUIDE_ROUGHNESS：越小越像湿润玻璃(高光尖锐、立体感强)；越大越哑光。
#   WAVEGUIDE_GLOW：波导内部辉光强度，0 = 纯受光(默认，你选的)；想要一点“在发光”可给 1~3。
#   WAVEGUIDE_BEVEL：False = 保持【有棱角】的矩形脊(默认，你选的)；
#     True = 加圆角+平滑着色变“圆润管状”。改 False 后重跑会自动移除已加的圆角、恢复硬边。
WAVEGUIDE_ROUGHNESS = 0.16
WAVEGUIDE_GLOW = 0.0
WAVEGUIDE_BEVEL = False
WAVEGUIDE_BEVEL_FRAC = 0.35  # 圆角宽度占波导厚度的比例（仅 BEVEL=True 时生效）
# RSOA 增益区(注入光源头)：默认给一点淡红辉光以示“光源”，不想要就设 0
RSOA_GLOW = 1.5


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


# ── 工具：把“值”优先写到节点属性，失败再写到同名输入 socket ─────────
#   关键：Blender 4.4+/5.0 把合成器 Glare 的 threshold/size/mix 等从“节点属性”
#   迁移成了“输入 socket”。先试 setattr(属性)，不行再走 _set(socket)，
#   于是 3.6 / 4.x / 5.0 都能命中同一参数。
def _prop_or_socket(node, attr_names, socket_names, value):
    attr_names = [attr_names] if isinstance(attr_names, str) else attr_names
    for a in attr_names:
        if hasattr(node, a):
            try:
                setattr(node, a, value)
                return True
            except (TypeError, AttributeError, ValueError):
                pass
    return _set(node, socket_names, value)  # 退回到“当作输入 socket 的默认值”


# ── 工具：给“枚举”设值，且兼容不同版本的【写法】与【属性 or 菜单 socket】 ──
#   坑：Glare 类型在 4.x 是节点属性、值写 'FOG_GLOW'；在 5.0 变成菜单 socket、
#   值要写显示名 'Fog Glow'。这里逐个候选(原值 / 标题式带空格 / 大写下划线)试到成功，
#   全失败返回 False（不抛错）。
def _set_enum(node, attr_names, socket_names, value):
    attr_names = [attr_names] if isinstance(attr_names, str) else attr_names
    cands = [
        value,
        value.replace('_', ' ').title(),  # FOG_GLOW → Fog Glow
        value.replace(' ', '_').upper(),  # Fog Glow → FOG_GLOW
    ]
    for a in attr_names:
        if hasattr(node, a):
            for v in cands:
                try:
                    setattr(node, a, v)
                    return True
                except (TypeError, ValueError):
                    pass
    sock = _socket(node, socket_names)
    if sock is not None:
        for v in cands:
            try:
                sock.default_value = v
                return True
            except (TypeError, ValueError):
                pass
    return False


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
                d.use = d.type == backend  # 仅启用该后端的 GPU 设备
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
    r.film_transparent = (
        GROUND_MODE != 'STUDIO'
    )  # STUDIO 用不透明浅灰背景；否则透明便于合成
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
            _set(
                bg, "Strength", 1.0
            )  # 提到 1.0：让“一整片环境”成为金属均匀高光的主要来源
        else:
            _set(bg, "Color", (0.90, 0.92, 0.95, 1.0))
            _set(bg, "Strength", 0.4)


# ── 2b. 深色径向渐变世界：中心略亮、四周压暗 → 自带暗角，发光波导/谐振最跳 ──
#   关键：背景压暗、几何仍被 softbox 三点光打亮 → “暗场里的亮线”，配合 Compositor
#   的 Glare 辉光，波导像真的在发光/谐振。金属的均匀感这时主要靠 softbox 大柔光（反射
#   一大片柔光）而非环境，所以下面 strength 给得很低（0.25 左右）。
def setup_world_dark(
    scene, core=(0.04, 0.05, 0.08), edge=(0.008, 0.010, 0.018), strength=0.25
):
    world = scene.world or bpy.data.worlds.new(PREFIX + "World")
    scene.world = world
    world.use_nodes = True
    nt = world.node_tree
    bg = _node_of_type(nt.nodes, 'BACKGROUND')
    if bg is None:
        bg = nt.nodes.new("ShaderNodeBackground")
        out = _node_of_type(nt.nodes, 'OUTPUT_WORLD') or nt.nodes.new(
            "ShaderNodeOutputWorld"
        )
        nt.links.new(bg.outputs[0], out.inputs[0])
    tc = nt.nodes.new("ShaderNodeTexCoord")
    # 把屏幕空间(Window, 0~1)重定心+放大：Mapping 算 Location + Scale*Vector，
    #   取 Scale=1.4、Location=-0.7 → 画面中心落到原点（球形渐变此处最亮）、四角到边缘（最暗）。
    #   不重定心的话，球形渐变是从坐标原点(画面角落)起算 → 亮斑会跑到角上。
    mapping = nt.nodes.new("ShaderNodeMapping")
    if (mloc := _socket(mapping, "Location")) is not None:
        mloc.default_value = (-0.7, -0.7, 0.0)
    if (msca := _socket(mapping, "Scale")) is not None:
        msca.default_value = (1.4, 1.4, 1.4)
    grad = nt.nodes.new("ShaderNodeTexGradient")
    grad.gradient_type = 'SPHERICAL'  # 球形渐变：中心 1、四周 0 → 径向暗角
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    cr = ramp.color_ramp
    cr.elements[0].position = 0.0
    cr.elements[0].color = (*edge, 1.0)  # 四周：近黑深蓝
    cr.elements[1].position = 1.0
    cr.elements[1].color = (*core, 1.0)  # 中心：略亮深蓝
    # 用屏幕空间（Window）让暗角跟随画面而非世界坐标；没有 Window 就退回 Generated
    out_names = [o.name for o in tc.outputs]
    src = tc.outputs["Window"] if "Window" in out_names else tc.outputs["Generated"]
    if (mv := _socket(mapping, "Vector")) is not None:
        nt.links.new(src, mv)
    if (gv := _socket(grad, "Vector")) is not None:
        nt.links.new(mapping.outputs["Vector"], gv)
    nt.links.new(grad.outputs["Color"], ramp.inputs["Fac"])
    if (bc := _socket(bg, "Color")) is not None:
        nt.links.new(ramp.outputs["Color"], bc)
    _set(bg, "Strength", strength)


# ── 2c. 中性深灰【平】背景：单色世界，最干净、衬托金属与波导（当前默认）──
#   配合同色地面，画面四周渐隐为同一深灰，复现“放在深灰影棚里”的干净观感。
def setup_world_flat(scene, gray=0.045, strength=1.0):
    world = scene.world or bpy.data.worlds.new(PREFIX + "World")
    scene.world = world
    world.use_nodes = True
    bg = _node_of_type(world.node_tree.nodes, 'BACKGROUND')
    if bg:
        _set(bg, "Color", (gray, gray, gray, 1.0))
        _set(bg, "Strength", strength)


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
    if hasattr(light, "spread"):
        light.spread = math.radians(
            120
        )  # 略收窄发散角（默认180°）→ 高光更聚拢、过渡更柔
    obj = bpy.data.objects.new(PREFIX + name, light)
    obj.location, obj.rotation_euler = location, rotation
    bpy.context.collection.objects.link(obj)
    return obj


# ── 5. 相机（多机位）：用球坐标显式控制取景，按 name 建独立相机 ──────────
#   器件长轴沿 X：−X = RSOA 端，+X = 外腔(出光/锥形耦合)端。
#   重要：本相机 up 锁定世界 +Y，所以【世界 +X 永远落在画面右半边】；azimuth 只决定
#         哪一端在【前景(近/下)】。于是：
#     · az≈55  → −X(RSOA)端前景、落【左下】（原斜视默认）
#     · az≈135 → +X(出光)端前景、落【右下】、离镜头最近 → 机位1
#     · az≈90  + 大仰角 → 长轴横躺画面、从正前上方俯看 → 机位2 看清波导走向
#   elevation_deg 仰角：小=低斜角(夸张前景/出光特写)，大=俯视(看清版图、电极少遮挡波导)
#   lens 焦距：小→透视强/纵深明显；大→更接近正交。use_ortho=True → 正交工程版图感。
def add_camera(
    name,
    center,
    diag,
    elevation_deg=36.0,
    azimuth_deg=55.0,
    dist_mult=2.0,
    lens=50.0,
    use_ortho=False,
    ortho_scale_mult=1.25,
    use_dof=False,
    dof_fstop=2.8,
    set_active=False,
):
    cam_data = bpy.data.cameras.new(PREFIX + "Camera_" + name)
    if use_ortho:
        cam_data.type = 'ORTHO'
        cam_data.ortho_scale = diag * ortho_scale_mult
    else:
        cam_data.type, cam_data.lens = 'PERSP', lens
    cam = bpy.data.objects.new(PREFIX + "Camera_" + name, cam_data)
    bpy.context.collection.objects.link(cam)

    el, az = math.radians(elevation_deg), math.radians(azimuth_deg)
    offset = Vector(
        (
            -math.cos(el) * math.cos(az),
            -math.cos(el) * math.sin(az),
            math.sin(el),
        )
    ) * (dist_mult * diag)
    cam.location = center + offset

    target = bpy.data.objects.new(PREFIX + "CamTarget_" + name, None)
    target.location = center
    bpy.context.collection.objects.link(target)
    con = cam.constraints.new('TRACK_TO')
    con.target, con.track_axis, con.up_axis = target, 'TRACK_NEGATIVE_Z', 'UP_Y'
    if use_dof and hasattr(cam_data, "dof"):
        cam_data.dof.use_dof = True
        cam_data.dof.focus_object = target
        cam_data.dof.aperture_fstop = dof_fstop
    if set_active:
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


# ── 6b. 影棚地面：大平面，承接柔和接触阴影 → “放在盒子里、很真实”──
#   平面很大 + 世界背景同色 → 远处渐隐为背景色，看不到地平线，无缝。
#   深色背景模式：传入深色 + 较低 roughness → 地面像“暗色玻璃台面”，会淡淡反出
#   发光波导/光束（很酷）；浅灰模式：默认浅灰哑光，柔和接触阴影。
def add_studio_ground(center, diag, z_bottom, color=(0.85, 0.86, 0.88), roughness=0.7):
    bpy.ops.mesh.primitive_plane_add(
        size=diag * 40,  # 足够大，铺满画面、无缝
        location=(
            center.x,
            center.y,
            z_bottom - diag * 0.003,
        ),  # 略低于器件底，避免共面闪烁
    )
    plane = bpy.context.active_object
    plane.name = PREFIX + "StudioGround"
    mat, b = _new_material(PREFIX + "StudioGround")
    _set(b, "Base Color", (*color, 1.0))
    _set(b, "Roughness", roughness)  # 小→镜面反出发光更明显；大→哑光、阴影更柔
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
    if (
        bsdf is None
    ):  # 极少数情况默认节点缺失：自建并接到材质输出（用索引连，避免名字依赖）
        bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
        out = _node_of_type(nt.nodes, 'OUTPUT_MATERIAL') or nt.nodes.new(
            "ShaderNodeOutputMaterial"
        )
        nt.links.new(bsdf.outputs[0], out.inputs[0])
    return mat, bsdf


def mat_sio2_blue():  # 深蓝 SiO₂ 板体（底部埋层/薄膜底包层）：光泽 + 清漆 + 微颗粒
    mat, b = _new_material(PREFIX + "SiO2_Blue")
    _set(b, ["Coat Weight", "Clearcoat"], 0.4)
    _set(b, ["Coat Roughness", "Clearcoat Roughness"], 0.05)
    add_grain(  # 介质：很淡的颗粒，仅在光泽面上隐约可见
        mat,
        b,
        base_color=(0.01, 0.06, 0.32),  # 深海军蓝：和亮紫 TFLN 拉开
        dark=0.85,
        light=1.25,
        rough=(0.14, 0.22),
        density_mult=1.0,
        bump=0.03,
    )
    return mat


def mat_sio2_clad_clear():  # 透明 SiO₂ 上包层：玻璃质感 + 可见玻璃边缘
    mat, b = _new_material(PREFIX + "SiO2_Clad_Clear")
    _set(b, "Base Color", (0.90, 0.95, 1.0, 1.0))  # 近无色，极淡冷调
    _set(b, ["Transmission Weight", "Transmission"], 1.0)  # 全透射 → 玻璃
    _set(b, "Roughness", 0.04)  # 越小越清澈
    _set(b, "IOR", 1.46)  # 熔融石英折射率
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
    cr.elements[0].color = (0.0, 0.0, 0.0, 1.0)  # 主体不发光（保持透明清澈）
    cr.elements[1].position = 0.92
    cr.elements[1].color = (0.55, 0.75, 1.0, 1.0)  # 边缘冷白蓝
    nt.links.new(lw.outputs["Fresnel"], ramp.inputs["Fac"])
    if (em := _socket(b, ["Emission Color", "Emission"])) is not None:
        nt.links.new(ramp.outputs["Color"], em)
    _set(b, "Emission Strength", 1.5)  # 边线亮度；太抢眼就调小

    # 提示：想要“平贴覆盖”而非折射玻璃，把上面 Transmission 改 0 并启用 Alpha：
    #   _set(b, "Alpha", 0.25); mat.blend_method = 'BLEND'   # blend_method 仅 EEVEE 需要
    return mat


def mat_tfln_violet():  # TFLN 薄层：亮紫受光板 + 很淡颗粒（波导已不发光，这里也压低自发光）
    mat, b = _new_material(PREFIX + "TFLN_Violet")
    _set(b, ["Emission Color", "Emission"], (0.45, 0.15, 0.95, 1.0))
    _set(b, "Emission Strength", 0.2)  # 仅给暗部一点底色避免发灰；不喧宾夺主
    add_grain(
        mat,
        b,
        base_color=(0.45, 0.15, 0.95),
        dark=0.85,
        light=1.12,
        rough=(0.26, 0.4),
        density_mult=1.0,
        bump=0.04,
    )
    return mat


def mat_si_dark():  # Si 衬底：深灰、半哑光 + 颗粒（晶圆/芯片基底质感）
    mat, b = _new_material(PREFIX + "Si_Dark")
    _set(b, "Metallic", 0.2)
    add_grain(
        mat,
        b,
        base_color=(0.06, 0.06, 0.07),
        dark=0.65,
        light=1.6,
        rough=(0.45, 0.62),
        density_mult=1.0,
        bump=0.08,
    )
    return mat


def mat_gold():  # Au 电极：金属金
    mat, b = _new_material(PREFIX + "Gold_Au")
    _set(b, "Base Color", (1.0, 0.76, 0.30, 1.0))
    _set(b, "Metallic", 1.0)
    _set(
        b, "Roughness", 0.38
    )  # 略糙：顶光反射铺成一片亮金，电极不再是暗面（太亮就调回 0.28）
    return mat


# ── 工具：给 Principled BSDF 叠加“噪声斑驳”——同时扰动 [基色明暗] [粗糙度] [凹凸] ──
#   两个总开关：GRAIN_DENSITY(细度) + GRAIN_STRENGTH(强度)，本函数把它们换算到场景尺度。
#   · density_mult：本材质相对总密度的倍率(衬底想更粗块就 <1)。
#   · dark/light/rough/bump：满强度(strength=1)时的明暗、粗糙度区间与凹凸量；实际按
#     strength 线性缩放——strength=0 退化为“纯色 + 单一粗糙度”(完全光滑、无噪点)。
#   消“马赛克”的关键：高 Detail 多倍频 + 放宽 ColorRamp 过渡 + 细密 bump，颗粒细腻不成块。
def add_grain(
    mat,
    bsdf,
    base_color,
    dark=0.6,
    light=1.25,
    rough=(0.3, 0.5),
    density_mult=1.0,
    bump=0.18,
    strength=None,
):
    nt = mat.node_tree
    r, g, bl = base_color[:3]
    clamp = lambda x: max(0.0, min(1.0, x))
    s = GRAIN_STRENGTH if strength is None else strength
    cells = GRAIN_DENSITY * density_mult
    rough_mid = 0.5 * (rough[0] + rough[1])

    # strength≈0：纯色 + 单一粗糙度，跳过整套噪声节点（真正“完全光滑”）
    if s <= 0.001:
        _set(bsdf, "Base Color", (clamp(r), clamp(g), clamp(bl), 1.0))
        _set(bsdf, "Roughness", rough_mid)
        return mat

    # 满强度的对比/凹凸按 strength 线性缩放 → 这个旋钮真的控制“斑驳强弱”
    e_dark = 1.0 - (1.0 - dark) * s
    e_light = 1.0 + (light - 1.0) * s
    e_rmin = rough_mid + (rough[0] - rough_mid) * s
    e_rmax = rough_mid + (rough[1] - rough_mid) * s
    e_bump = bump * s

    scale = cells / _SCENE_REF if _SCENE_REF else cells  # 物体坐标→密度，随尺度自适应

    # Object 坐标 → Mapping 统一缩放，使后面所有噪声密度都按场景尺度走
    tc = nt.nodes.new("ShaderNodeTexCoord")
    mapping = nt.nodes.new("ShaderNodeMapping")
    mapping.inputs["Scale"].default_value = (scale, scale, scale)
    if (mv := _socket(mapping, "Vector")) is not None:
        nt.links.new(tc.outputs["Object"], mv)

    # 主噪声：高 Detail + 适度 Roughness → 细腻多倍频，不再是大块（消马赛克的关键）
    n1 = nt.nodes.new("ShaderNodeTexNoise")
    n1.inputs["Scale"].default_value = 1.0
    if "Detail" in n1.inputs:
        n1.inputs["Detail"].default_value = 6.0
    if "Roughness" in n1.inputs:
        n1.inputs["Roughness"].default_value = 0.6
    if (sV := _socket(n1, "Vector")) is not None:
        nt.links.new(mapping.outputs["Vector"], sV)

    # 基色：noise → ColorRamp（暗↔亮两色）→ Base Color。过渡放宽(0.20↔0.80) → 不再硬切色块
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    cr = ramp.color_ramp
    cr.elements[0].position = 0.20
    cr.elements[0].color = (
        clamp(r * e_dark),
        clamp(g * e_dark),
        clamp(bl * e_dark),
        1.0,
    )
    cr.elements[1].position = 0.80
    cr.elements[1].color = (
        clamp(r * e_light),
        clamp(g * e_light),
        clamp(bl * e_light),
        1.0,
    )
    nt.links.new(n1.outputs["Fac"], ramp.inputs["Fac"])
    if (bc := _socket(bsdf, "Base Color")) is not None:
        nt.links.new(ramp.outputs["Color"], bc)

    # 粗糙度：noise → MapRange → Roughness，做出忽亮忽哑的斑驳高光
    mr = nt.nodes.new("ShaderNodeMapRange")
    mr.inputs["To Min"].default_value = e_rmin
    mr.inputs["To Max"].default_value = e_rmax
    nt.links.new(n1.outputs["Fac"], mr.inputs["Value"])
    if (rg := _socket(bsdf, "Roughness")) is not None:
        nt.links.new(mr.outputs["Result"], rg)

    # 凹凸：更细的 noise（密度×3）→ Bump → Normal，把高光“打碎”成细颗粒（不再大块）
    if e_bump > 0:
        n2 = nt.nodes.new("ShaderNodeTexNoise")
        n2.inputs["Scale"].default_value = 3.0
        if "Detail" in n2.inputs:
            n2.inputs["Detail"].default_value = 6.0
        if (s2 := _socket(n2, "Vector")) is not None:
            nt.links.new(mapping.outputs["Vector"], s2)
        bmp = nt.nodes.new("ShaderNodeBump")
        bmp.inputs["Strength"].default_value = e_bump
        nt.links.new(n2.outputs["Fac"], bmp.inputs["Height"])
        if (nrm := _socket(bsdf, "Normal")) is not None:
            nt.links.new(bmp.outputs["Normal"], nrm)
    return mat


def mat_gold_grainy():  # 镀金电极（细腻磨砂金；斑驳强弱由 GRAIN_STRENGTH 总开关控制）
    mat, b = _new_material(PREFIX + "Gold_Grainy")
    _set(b, "Base Color", (1.0, 0.76, 0.32, 1.0))
    _set(b, "Metallic", 1.0)
    add_grain(
        mat,
        b,
        base_color=(1.0, 0.74, 0.30),
        dark=0.62,  # 暗斑（对比大幅收窄，避免“马赛克”大色块）
        light=1.12,  # 亮斑
        rough=(0.30, 0.48),  # 粗糙度斑驳区间收窄 → 高光柔和连续
        density_mult=1.0,  # 细度跟随 GRAIN_DENSITY；强弱跟随 GRAIN_STRENGTH
        bump=0.10,  # 凹凸已调小：满强度也只是细磨砂，不打碎成块
    )
    return mat


def mat_gold_smooth():  # 光滑均匀金（参考图金电极那种连续柔和渐变高光，无麻点热点）
    # ★ 消“刺眼麻点”的关键：去掉 add_grain 的 bump（不打碎法线）+ 用单一中等粗糙度。
    #   Roughness=0.40 是甜点：把单个光源的“小而强热点”模糊展开成“一大片低强度柔光”，
    #   既均匀又还像金。想更亮镜面感→调到 0.30；想更哑光→调到 0.50。
    mat, b = _new_material(PREFIX + "Gold_Smooth")
    _set(b, "Base Color", (1.0, 0.78, 0.34, 1.0))  # 略提亮，避免暗金
    _set(b, "Metallic", 1.0)
    _set(b, "Roughness", 0.40)
    return mat


def mat_submount_gray():  # RSOA 灰色底座/衬底：中灰 + 明显颗粒斑驳（陶瓷/镀层质感）
    mat, b = _new_material(PREFIX + "Submount_Gray")
    _set(b, "Metallic", 0.3)
    add_grain(
        mat,
        b,
        base_color=(0.42, 0.43, 0.46),
        dark=0.68,  # 暗斑
        light=1.25,  # 亮斑
        rough=(0.42, 0.62),  # 整体偏哑光，斑驳更像粗糙衬底
        density_mult=0.8,  # 衬底颗粒比电极略粗
        bump=0.12,  # 衬底凹凸可稍强（仍按 GRAIN_STRENGTH 缩放）
    )
    return mat


# ── 波导材质工厂（纯受光实心：青色玻璃光泽体，靠灯光高光显出圆润立体；可选极淡辉光）──
#   要“立体”而非“霓虹线”：给真实 Base Color + 低粗糙度 + 清漆，顶面接住主光形成一条
#   尖锐高光线 → 配合圆角(Bevel)读出圆润管状。WAVEGUIDE_GLOW=0 时完全不自发光。
def _lit_waveguide(name, color, rough, glow_color, glow):
    mat, b = _new_material(PREFIX + name)
    _set(b, "Base Color", (*color, 1.0))
    _set(b, "Metallic", 0.0)
    _set(b, "Roughness", rough)
    _set(b, ["Coat Weight", "Clearcoat"], 0.6)  # 清漆：湿润玻璃般的尖锐高光，最显立体
    _set(b, ["Coat Roughness", "Clearcoat Roughness"], 0.03)
    if glow > 0:  # 可选内部辉光：默认 0=纯受光；给一点能模拟“光在里面”
        _set(b, ["Emission Color", "Emission"], (*glow_color, 1.0))
        _set(b, "Emission Strength", glow)
    return mat


def mat_waveguide_cyan():  # 外腔 LNOI 波导/光栅：受光青色玻璃管（默认不发光，靠高光显立体）
    return _lit_waveguide(
        "Waveguide_Cyan",
        color=(0.05, 0.55, 0.72),
        rough=WAVEGUIDE_ROUGHNESS,
        glow_color=(0.10, 0.85, 1.0),
        glow=WAVEGUIDE_GLOW,
    )


def mat_rsoa_red():  # InP RSOA 增益区：受光红色实体 + 一点淡红辉光（注入光的“源头”）
    return _lit_waveguide(
        "RSOA_Red",
        color=(0.80, 0.07, 0.03),
        rough=0.22,
        glow_color=(1.0, 0.15, 0.05),
        glow=RSOA_GLOW,
    )


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


# ── 波导圆角开关：enable=True 加 Bevel+平滑读成“圆润管状”；False 移除并恢复【硬棱角】──
#   ★ 用【精确物体名】(与 assign_material 一致)，避免 "Layer 1/0" 子串误伤 "Layer 1/0.001"。
#   幂等：无条件调用，按 enable 决定加还是清除，反复跑都能正确切回当前选择的状态。
def round_waveguide_edges(obj_names, enable, width_frac=0.35, segments=3):
    names = [obj_names] if isinstance(obj_names, str) else list(obj_names)
    done = 0
    for nm in names:
        obj = bpy.data.objects.get(nm)
        if obj is None or obj.type != 'MESH':
            continue
        if enable:
            zs = [(obj.matrix_world @ Vector(c)).z for c in obj.bound_box]
            h = max(zs) - min(zs)
            if h <= 0:
                continue
            bev = obj.modifiers.get("FIG_Bevel") or obj.modifiers.new(
                "FIG_Bevel", 'BEVEL'
            )
            bev.width = h * width_frac
            bev.segments = segments
            bev.limit_method = 'ANGLE'
            bev.angle_limit = math.radians(30)
            bev.use_clamp_overlap = True
            for p in obj.data.polygons:  # 平滑着色让圆角顺滑
                p.use_smooth = True
            if hasattr(obj.data, "use_auto_smooth"):
                obj.data.use_auto_smooth = True
                obj.data.auto_smooth_angle = math.radians(45)
        else:
            # 恢复【硬棱角】：移除之前加的圆角修改器 + 关掉平滑着色（变回有棱有角的矩形脊）
            if (m := obj.modifiers.get("FIG_Bevel")) is not None:
                obj.modifiers.remove(m)
            for p in obj.data.polygons:
                p.use_smooth = False
            if hasattr(obj.data, "use_auto_smooth"):
                obj.data.use_auto_smooth = False
        done += 1
    print(
        f"  [波导{'圆角' if enable else '硬棱角'}] 已处理 {done}/{len(names)} 个波导物体"
    )


# ── 9. 合成器辉光（Cycles 无内置 bloom，发光的“光晕”必须在这里做）──────
#   原理：渲染出的是物理光强、不含镜头/眼球散射，发光像素不会自动外扩成光晕。
#   Glare(Fog Glow) 把高于 threshold 的亮像素向四周柔和扩散 → “亮线”变“在发光的线”。
#   threshold≈1.0：只让发光波导/谐振(>1)起晕，暗衬底不晕。这是观感提升最大的一步。
def _get_compositor_tree(scene):
    # 新 API（5.0+）：scene.node_tree 已移除，改用 scene.compositing_node_group
    if hasattr(scene, "compositing_node_group"):
        ng = scene.compositing_node_group
        if ng is None:
            ng = bpy.data.node_groups.new(PREFIX + "Compositor", "CompositorNodeTree")
            scene.compositing_node_group = ng
        return ng
    # 旧 API（3.6 / 4.x）：scene.use_nodes + scene.node_tree
    try:
        scene.use_nodes = True
    except Exception:
        pass
    return scene.node_tree


def setup_compositor_glow(
    scene, glare_type='FOG_GLOW', threshold=1.0, size=0.5, mix=0.0, quality='HIGH'
):
    """串一个 Glare 节点，让 > threshold 的发光像素产生柔和光晕。
    size：老版本是 2^size 像素核（给 8≈256px）；新版是相对图像比例 0~1（给 0.5）——两种都试。"""
    nt = _get_compositor_tree(scene)
    nodes, links = nt.nodes, nt.links

    rlayers = _node_of_type(nodes, 'R_LAYERS') or nodes.new("CompositorNodeRLayers")
    rlayers.location = (-400, 0)
    glare = _node_of_type(nodes, 'GLARE') or nodes.new("CompositorNodeGlare")
    glare.location = (0, 0)
    # 输出节点：Blender 5.0 移除了 Composite 节点 → 改用 Group Output（其第一个 Color 输入=最终结果）；
    #   3.6 / 4.x 仍是 Composite。先找现成的，没有再按版本新建。
    out_node = _node_of_type(nodes, 'COMPOSITE') or _node_of_type(nodes, 'GROUP_OUTPUT')
    if out_node is None:
        try:
            out_node = nodes.new("CompositorNodeComposite")  # 3.6 / 4.x
        except (RuntimeError, KeyError):
            out_node = nodes.new("NodeGroupOutput")  # 5.0+
    # 5.0+ 的 Group Output：必须保证节点组接口有一个 Color 输出 socket，节点才会有可连的输入
    if out_node.type == 'GROUP_OUTPUT' and hasattr(nt, "interface"):
        has_out = any(
            getattr(it, "in_out", None) == 'OUTPUT' for it in nt.interface.items_tree
        )
        if not has_out:
            nt.interface.new_socket(
                name="Image", in_out='OUTPUT', socket_type='NodeSocketColor'
            )
    out_node.location = (400, 0)

    # 参数：枚举用 _set_enum（兼容 'FOG_GLOW'/'Fog Glow' 两种写法、属性 or 菜单 socket）；
    #       数值用 _prop_or_socket（属性优先、退回 float socket）。
    _set_enum(glare, "glare_type", ["Glare Type", "Type"], glare_type)
    _set_enum(glare, "quality", ["Quality"], quality)
    _prop_or_socket(glare, "threshold", ["Threshold", "Highlights"], threshold)
    _prop_or_socket(glare, "mix", ["Mix", "Strength"], mix)
    if hasattr(glare, "size"):  # 老语义：int，核大小=2^size 像素
        try:
            glare.size = 8
        except (TypeError, ValueError):
            pass
    _set(glare, ["Size"], size)  # 新语义：相对图像比例 0~1

    img_out = rlayers.outputs.get("Image") or rlayers.outputs[0]
    g_in = _socket(glare, ["Image"]) or glare.inputs[0]
    links.new(img_out, g_in)
    g_out = glare.outputs.get("Image") or glare.outputs[0]
    c_in = _socket(out_node, ["Image"]) or (
        out_node.inputs[0] if len(out_node.inputs) else None
    )
    if c_in is not None:
        links.new(g_out, c_in)
    else:
        print("  [Compositor] ⚠ 输出节点无可连输入，跳过连线（请反馈给我）")
    # 调不出辉光时：取消下一行注释，看控制台真实 socket 名，补进上面候选列表即可。
    # print("  [Glare inputs]", [s.name for s in glare.inputs])
    print(f"  [Compositor] Glare({glare_type}) threshold={threshold} size={size}")
    return glare


def main():
    scene = bpy.context.scene
    _purge_prefixed()
    setup_render(scene)
    if BACKGROUND_STYLE == 'DARK_GRADIENT':
        setup_world_dark(scene)
    elif BACKGROUND_STYLE == 'LIGHT_STUDIO':
        setup_world(scene)
    else:  # 'FLAT_DARK'（默认）：中性深灰平背景
        setup_world_flat(scene, gray=FLAT_BG_GRAY, strength=FLAT_BG_STRENGTH)

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
    #   ★ softbox 化（面积加大、能量降一个量级）：镜面金属反射“一大片大柔光”→
    #     高光铺成柔和渐变而非刺眼热点。深背景下环境补光很弱，主要靠这三盏打亮金属，
    #     所以哪怕背景很暗，金属/衬底依然被照亮、有立体感。整体太暗→先调顶部 EXPOSURE。
    add_area_light(
        "Key",
        center + Vector((0.0, 0.5, 2.2)) * diag,  # X 居中、偏后、抬更高
        (math.radians(-30), 0, 0),  # 向前下倾 → 阴影投向 −Y（相机侧）
        diag * 5.0,  # 超大柔光罩：在镜面金属里成像大 → 一片柔和高光
        e * 350,
    )
    # 补光（Fill）：相机侧(−Y)正面大柔光，约 Key 一半，保留方向性立体感、别打成死平。
    add_area_light(
        "Fill",
        center + Vector((-0.3, -1.6, 0.7)) * diag,
        (math.radians(76), 0, math.radians(-6)),
        diag * 3.0,
        e * 150,
    )
    # 轮廓光（Rim）：从 TFLN 端(+X)侧后方勾金属边，并给远端补亮、平衡左右。
    add_area_light(
        "Rim",
        center + Vector((1.0, 0.9, 1.2)) * diag,
        (math.radians(120), 0, math.radians(20)),
        diag * 2.0,
        e * 200,
    )

    # ── 两个机位都建好，ACTIVE_CAMERA 决定渲染用哪个（视图里可 Ctrl+小键盘0 临时切换）──
    # 机位1「出光特写」：低斜角 + 出光(+X)端在右下、离镜头最近 → 突出出光/谐振
    add_camera(
        "1_OutputCloseup",
        center,
        diag,
        elevation_deg=30.0,
        azimuth_deg=135.0,
        dist_mult=1.9,
        lens=55.0,
        set_active=(ACTIVE_CAMERA == 1),
    )
    # 机位2「波导俯视」：高仰角 + 正前方(长轴横躺) → 越过金电极看清整体波导走向
    #   想要纯版图(无透视)就把 use_ortho 改 True；想更立体就把 elevation_deg 调小到 ~55
    add_camera(
        "2_WaveguideTop",
        center,
        diag,
        elevation_deg=70.0,
        azimuth_deg=90.0,
        dist_mult=2.2,
        lens=48.0,
        use_ortho=False,
        set_active=(ACTIVE_CAMERA == 2),
    )
    if GROUND_MODE == 'STUDIO':
        if BACKGROUND_STYLE == 'FLAT_DARK':
            # 中性深灰地面：与平背景同调，画面四周无缝渐隐；微反射衬出金属/波导立体
            g = FLAT_BG_GRAY * 1.3
            add_studio_ground(
                center, diag, mins.z, color=(g, g, g * 1.05), roughness=0.55
            )
        elif BACKGROUND_STYLE == 'DARK_GRADIENT':
            # 暗色玻璃台面：会淡淡反出波导/光束，很酷；roughness 调小→反射更清晰
            add_studio_ground(
                center, diag, mins.z, color=(0.015, 0.018, 0.03), roughness=0.42
            )
        else:  # LIGHT_STUDIO
            add_studio_ground(
                center, diag, mins.z
            )  # 浅灰地面 + 接触阴影，成片“在盒子里”
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
    sio2_blue = mat_sio2_blue()  # 底部 SiO₂ 埋层/薄膜底包层 → 蓝色介质
    sio2_clad = mat_sio2_clad_clear()  # 上方 SiO₂ 包层 → 透明玻璃（带可见边缘）
    wg_cyan = mat_waveguide_cyan()  # 外腔 LNOI 波导/光栅 → 青色自发光
    rsoa_red = mat_rsoa_red()  # RSOA 增益区波导 → 红色自发光
    gold_smooth = mat_gold_smooth()  # 金电极 → 光滑均匀金（消麻点热点，新默认）
    gold_grainy = mat_gold_grainy()  # （备用）斑驳金，想要颗粒感时换回它
    submount = mat_submount_gray()  # submount 灰色衬底 → 斑驳灰

    assign_material("Layer 1/0.001", sio2_blue)
    assign_material("Layer 2/0.001", mat_tfln_violet())
    assign_material("Layer 3/0.001", sio2_clad)
    assign_material("Layer 41/0", wg_cyan)

    assign_material("Layer 1/0", rsoa_red)
    assign_material("Layer 4/0", submount)
    assign_material(["Layer 46/0", "Layer 3/0", "Layer 2/0"], gold_grainy)

    # ★ 波导棱角/圆角：无条件调用，按 WAVEGUIDE_BEVEL 决定硬棱角(默认)还是圆润管状。
    #   只对青色波导(Layer 41/0)和 RSOA 增益区(Layer 1/0)做；若还有其它波导段，把名字加进列表。
    round_waveguide_edges(
        ["Layer 41/0", "Layer 1/0"], WAVEGUIDE_BEVEL, width_frac=WAVEGUIDE_BEVEL_FRAC
    )

    # ⚠ 修复：早期 P1 试用曾把 GDS_Substrate* 设为隐藏(hide_viewport/hide_render=True)，P1 删除
    #   后该隐藏状态仍残留在 .blend 里。这里强制恢复可见，保证渲染到完整模型衬底（幂等、可反复跑）。
    for _o in bpy.data.objects:
        if _o.name.startswith("GDS_Substrate"):
            _o.hide_viewport = False
            _o.hide_render = False
    assign_material(["GDS_Substrate", "GDS_Substrate.001"], mat_si_dark())

    # —— 控制台打印全部网格物体名，方便你核对/填写上面的 RSOA 波导名 ——
    print("  —— 全部网格物体名（非 FIG_ 前缀）——")
    for _o in bpy.context.scene.objects:
        if _o.type == 'MESH' and not _o.name.startswith(PREFIX):
            print(f"    · {_o.name!r}")

    # —— 新增材质的指派示例（把名字换成你工程里的真实物体名；多个名字用列表）——
    # ★ 斑驳金一份材质可同时指给多块电极（RSOA 顶电极 / submount 顶电极 / 外腔芯片 Au）：
    # assign_material(["RSOA_TopElectrode", "Submount_Electrode", "Layer 46/0.001"], gold_grainy)
    # assign_material("Submount", submount)                           # submount 斑驳灰衬底
    # assign_material(["LNOI_Cladding", "SiO2_Top.001"], sio2_clad)   # 透明上包层（带可见边缘）
    # assign_material("RSOA_Waveguide", rsoa_red)                     # RSOA 红色自发光波导
    # assign_material(["Layer 41/0.001", "Layer 41/0.002"], wg_cyan)  # 同一青色材质给多段波导

    # ★ 合成器辉光：波导已改纯受光，这里收敛为“仅给最亮高光一点柔和光晕”，干净不开花。
    #   想要更强的发光光晕氛围：调大 mix(→0 或正)、调低 threshold。
    setup_compositor_glow(scene, threshold=1.3, size=0.4, mix=-0.25)

    _bg_label = {
        'FLAT_DARK': '深灰平背景',
        'DARK_GRADIENT': '深蓝径向',
        'LIGHT_STUDIO': '浅灰影棚',
    }.get(BACKGROUND_STYLE, BACKGROUND_STYLE)
    print(
        f"FIG setup done. Resolution: {RES_X} x {RES_Y}  | "
        f"背景={_bg_label} | 波导=纯受光实心{'+圆角' if WAVEGUIDE_BEVEL else '(硬棱角)'} | "
        f"颗粒: 密度={GRAIN_DENSITY} 强度={GRAIN_STRENGTH} | 辉光=on"
    )


main()
