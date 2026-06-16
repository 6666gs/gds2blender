# -*- coding: utf-8 -*-
"""
微透镜建模 —— ALPS FLGS3SQ11A LD-光纤耦合透镜（双非球面）
依据厂家图纸 FLGS3SQ11A.pdf + 非球面面型数据 aspherical data.pdf 建模。

结构：1x1mm 方形玻璃板(四角R0.1) + 两面中央各一个圆形非球面凸镜 + 平边框。
  第1面 sideA(LD侧, 左)：小而陡，r=+0.7543, k=-24.48，孔径~φ0.6
  第2面 sideB(Fiber侧, 右)：大而凸，r=-0.4821, k=-1.079 +高阶项，孔径~φ0.86
非球面方程： Z = c p^2 /(1+sqrt(1-c^2(k+1)p^2)) + d p^4 + e p^6 + f p^8 + g p^10
  c=1/r,  p=sqrt(x^2+y^2)

迭代闭环：
    & "D:\blender\blender.exe" --background --python "f:\gds2blender\microlens.py"
输出 f:\gds2blender\renders\：
    microlens_side.png  侧剖（光轴水平，看左右两个非球面凸镜）—— 对照照片
    microlens_face.png  正视（沿光轴看，看方形面+圆形镜区+平边框）
    microlens_persp.png 3/4 透视
"""

import bpy
import bmesh
import math
import os
from math import sqrt, sin, cos, pi
from mathutils import Vector

# ============================== 器件参数（mm, 来自图纸/PDF）==============================
SQ_HALF = 0.5          # 方形半边长（1x1mm）
CORNER_R = 0.10        # 四角圆角 R0.1
CENTER_THICK = 0.80    # 中心厚（apex到apex）

# 第1面 sideA (LD侧, 左, 朝 -X 外凸)
A_R, A_K = 0.754272, -24.475844
A_D, A_E, A_F, A_G = 6.405734e-03, -1.917347e-01, 0.0, 0.0
A_APER = 0.30          # 物理镜区半径 ~φ0.6

# 第2面 sideB (Fiber侧, 右, 朝 +X 外凸)
B_R, B_K = -0.482068, -1.078601
B_D, B_E, B_F, B_G = -2.983551e-01, 5.189425e-01, 8.911923e-01, -1.516072e+00
B_APER = 0.43          # 物理镜区半径 ~φ0.86

GLASS_IOR = 1.80       # L-LAH84 高折射玻璃 (n≈1.778@1550nm, 可见略高)
FROST_ROUGH = 0.50     # 侧壁毛玻璃粗糙度（越大越磨砂）

U = 100.0              # mm→Blender 单位放大（便于复用相机/灯光量级）
BACK_OVERLAP = 0.12    # 镜冠实体伸入本体的深度(mm)，保证布尔并集干净
DOME_RINGS = 56
DOME_SEG = 128

# ============================== 渲染 / 导出 ==============================
RENDER_SAMPLES = 64
RES = 900
OUT_DIR = r"f:\gds2blender\renders"
EXPORT_DIR = r"f:\gds2blender\exports"
DO_RENDER = True       # 是否出图
DO_EXPORT = True       # 是否导出模型文件
# =====================================================================


def asphere_sag(p, r, k, d, e, f, g):
    """偶次非球面矢高 Z(p)。"""
    c = 1.0 / r
    p2 = p * p
    inside = 1.0 - c * c * (k + 1.0) * p2
    base = c * p2 / (1.0 + sqrt(inside))
    return base + d * p2**2 + e * p2**3 + f * p2**4 + g * p2**5


def reset_scene():
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    for coll in (bpy.data.meshes, bpy.data.materials, bpy.data.lights, bpy.data.cameras):
        for db in list(coll):
            if db.users == 0:
                coll.remove(db)


def make_rounded_square_prism(x0, x1):
    """沿 X 轴的方形棱柱(四角R圆角)，X 跨 [x0,x1]，截面 1x1mm。"""
    # 截面外轮廓（Y-Z 平面），带圆角
    pts = []
    S, r = SQ_HALF, CORNER_R
    corners = [(S - r, S - r), (-(S - r), S - r), (-(S - r), -(S - r)), (S - r, -(S - r))]
    start_ang = [0.0, pi / 2, pi, 1.5 * pi]
    for (cy, cz), a0 in zip(corners, start_ang):
        for n in range(9):  # 每角 9 段
            a = a0 + (pi / 2) * n / 8.0
            pts.append((cy + r * cos(a), cz + r * sin(a)))

    mesh = bpy.data.meshes.new("PrismMesh")
    bm = bmesh.new()
    vs0 = [bm.verts.new((x0 * U, y * U, z * U)) for (y, z) in pts]
    bm.faces.new(vs0)  # x0 端盖
    bm.verts.ensure_lookup_table()
    nrm = bmesh.ops.extrude_face_region(bm, geom=bm.faces[:])
    verts_new = [e for e in nrm['geom'] if isinstance(e, bmesh.types.BMVert)]
    bmesh.ops.translate(bm, vec=((x1 - x0) * U, 0, 0), verts=verts_new)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new("Microlens", mesh)
    bpy.context.collection.objects.link(obj)
    return obj


def make_dome(name, coeffs, x_apex, r_ap, d_in):
    """中心非球面凸镜实体（镜冠+伸入本体的封闭实体），供布尔并集。"""
    r, k, d, e, f, g = coeffs
    verts = [(x_apex * U, 0.0, 0.0)]  # 0: apex
    for i in range(1, DOME_RINGS + 1):
        p = r_ap * i / DOME_RINGS
        sx = x_apex + asphere_sag(p, r, k, d, e, f, g)
        for j in range(DOME_SEG):
            a = 2 * pi * j / DOME_SEG
            verts.append((sx * U, p * cos(a) * U, p * sin(a) * U))
    x_base = x_apex + asphere_sag(r_ap, r, k, d, e, f, g)
    x_back = x_base + d_in * BACK_OVERLAP
    back_ring0 = len(verts)
    for j in range(DOME_SEG):
        a = 2 * pi * j / DOME_SEG
        verts.append((x_back * U, r_ap * cos(a) * U, r_ap * sin(a) * U))
    back_center = len(verts)
    verts.append((x_back * U, 0.0, 0.0))

    faces = []
    for j in range(DOME_SEG):  # apex 扇面
        faces.append((0, 1 + j, 1 + (j + 1) % DOME_SEG))
    for i in range(1, DOME_RINGS):  # 环带
        r0, r1 = 1 + (i - 1) * DOME_SEG, 1 + i * DOME_SEG
        for j in range(DOME_SEG):
            j2 = (j + 1) % DOME_SEG
            faces.append((r0 + j, r1 + j, r1 + j2, r0 + j2))
    last0 = 1 + (DOME_RINGS - 1) * DOME_SEG  # 镜冠边→后环（圆柱壁）
    for j in range(DOME_SEG):
        j2 = (j + 1) % DOME_SEG
        faces.append((last0 + j, back_ring0 + j, back_ring0 + j2, last0 + j2))
    for j in range(DOME_SEG):  # 后盖扇面
        j2 = (j + 1) % DOME_SEG
        faces.append((back_center, back_ring0 + j2, back_ring0 + j))

    mesh = bpy.data.meshes.new(name + "Mesh")
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    return obj, x_base


def build_lens():
    # apex 位置
    xA = -CENTER_THICK / 2.0   # sideA apex (左)
    xB = +CENTER_THICK / 2.0   # sideB apex (右)
    domeA, baseA = make_dome("_domeA",
                             (A_R, A_K, A_D, A_E, A_F, A_G), xA, A_APER, +1)
    domeB, baseB = make_dome("_domeB",
                             (B_R, B_K, B_D, B_E, B_F, B_G), xB, B_APER, -1)
    # 本体棱柱：X 从 baseA 到 baseB（两平边框平面之间）
    prism = make_rounded_square_prism(baseA, baseB)  # 传 mm，内部乘 U
    print("[lens] baseA=%.4f  baseB=%.4f  thickness=%.4f mm" % (baseA, baseB, baseB - baseA))

    # 两种材质：光学面=清透抛光，侧壁=毛玻璃
    clear_mat = make_glass("LensClear", 0.0)
    frost_mat = make_glass("LensFrosted", FROST_ROUGH)
    # 棱柱：端盖(n边形，光学面/平边框)=clear[0]；侧壁(四边形)=frosted[1]
    prism.data.materials.clear()
    prism.data.materials.append(clear_mat)
    prism.data.materials.append(frost_mat)
    n_wall = 0
    for poly in prism.data.polygons:
        if len(poly.vertices) > 4:      # 端盖 n 边形 → 光学面/平边框
            poly.material_index = 0
        else:                           # 侧壁四边形 → 毛玻璃
            poly.material_index = 1
            n_wall += 1
    print("[lens] frosted side-wall faces =", n_wall)
    # 两个镜冠：清透
    for dome in (domeA, domeB):
        dome.data.materials.clear()
        dome.data.materials.append(clear_mat)

    bpy.context.view_layer.objects.active = prism
    for dome in (domeA, domeB):
        m = prism.modifiers.new("u_" + dome.name, 'BOOLEAN')
        m.operation = 'UNION'
        m.object = dome
        m.solver = 'EXACT'
    bpy.ops.object.select_all(action='DESELECT')
    prism.select_set(True)
    bpy.context.view_layer.objects.active = prism
    for m in list(prism.modifiers):
        bpy.ops.object.modifier_apply(modifier=m.name)

    bpy.ops.object.select_all(action='DESELECT')
    domeA.select_set(True)
    domeB.select_set(True)
    bpy.ops.object.delete(use_global=False)

    bpy.context.view_layer.objects.active = prism
    prism.select_set(True)
    try:
        bpy.ops.object.shade_smooth_by_angle(angle=math.radians(25))
    except Exception:
        bpy.ops.object.shade_smooth()
    return prism


def _principled(mat):
    for n in mat.node_tree.nodes:
        if n.type == 'BSDF_PRINCIPLED':
            return n
    return None


def make_glass(name, roughness):
    """玻璃材质：roughness=0 抛光清透；roughness大 = 毛玻璃。"""
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    b = _principled(mat)

    def setv(n, val):
        if n in b.inputs:
            b.inputs[n].default_value = val
    setv("Base Color", (0.93, 0.97, 1.0, 1.0))
    setv("Roughness", roughness)
    setv("Transmission Weight", 1.0)
    setv("Transmission", 1.0)
    setv("IOR", GLASS_IOR)
    return mat


def setup_world():
    world = bpy.context.scene.world or bpy.data.worlds.new("World")
    bpy.context.scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs[0].default_value = (0.045, 0.05, 0.06, 1.0)
        bg.inputs[1].default_value = 0.6


def add_area_light(name, loc, power, size, color=(1, 1, 1)):
    ld = bpy.data.lights.new(name, 'AREA')
    ld.energy, ld.size, ld.color = power, size, color
    ob = bpy.data.objects.new(name, ld)
    ob.location = loc
    bpy.context.collection.objects.link(ob)
    ob.rotation_euler = (Vector((0, 0, 0)) - Vector(loc)).to_track_quat('-Z', 'Y').to_euler()


def setup_lights():
    add_area_light("Key", (140, -120, 160), 8e5, 120, (1.0, 0.97, 0.92))
    add_area_light("Fill", (-160, -60, 80), 2.5e5, 160, (0.85, 0.9, 1.0))
    add_area_light("Rim", (-40, 160, 120), 4e5, 140, (1.0, 1.0, 1.0))


def add_ground():
    bpy.ops.mesh.primitive_plane_add(size=2000, location=(0, 0, -SQ_HALF * U - 2))
    g = bpy.context.active_object
    g.name = "Ground"
    mat = bpy.data.materials.new("GroundMat")
    mat.use_nodes = True
    b = _principled(mat)
    b.inputs["Base Color"].default_value = (0.06, 0.065, 0.075, 1.0)
    b.inputs["Roughness"].default_value = 0.5
    g.data.materials.append(mat)


def make_camera(name, loc, ortho=False, ortho_scale=140.0):
    cd = bpy.data.cameras.new(name)
    cam = bpy.data.objects.new(name, cd)
    bpy.context.collection.objects.link(cam)
    cam.location = loc
    if ortho:
        cd.type = 'ORTHO'
        cd.ortho_scale = ortho_scale
    cam.rotation_euler = (Vector((0, 0, 0)) - Vector(loc)).to_track_quat('-Z', 'Y').to_euler()
    return cam


def setup_render():
    scene = bpy.context.scene
    scene.render.engine = 'CYCLES'
    try:
        prefs = bpy.context.preferences.addons['cycles'].preferences
        for dt in ('OPTIX', 'CUDA', 'HIP', 'ONEAPI'):
            try:
                prefs.compute_device_type = dt
                prefs.get_devices()
                if any(d.type == dt for d in prefs.devices):
                    for d in prefs.devices:
                        d.use = True
                    scene.cycles.device = 'GPU'
                    print("[render] GPU =", dt)
                    break
            except Exception:
                continue
    except Exception:
        pass
    scene.cycles.samples = RENDER_SAMPLES
    scene.cycles.use_denoising = True
    scene.render.resolution_x = scene.render.resolution_y = RES
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = False
    try:
        scene.view_settings.view_transform = 'Standard'
    except Exception:
        pass


def render_to(cam, path):
    scene = bpy.context.scene
    scene.camera = cam
    scene.render.filepath = path
    bpy.ops.render.render(write_still=True)
    print("[render] wrote", path)


def report(obj):
    d = obj.dimensions
    print("[report] dims XYZ (units) = (%.2f, %.2f, %.2f)" % (d.x, d.y, d.z))
    print("[report] verts=%d faces=%d" % (len(obj.data.vertices), len(obj.data.polygons)))


def export_models(lens):
    os.makedirs(EXPORT_DIR, exist_ok=True)
    base = os.path.join(EXPORT_DIR, "FLGS3SQ11A")
    # 1) .blend 全场景（U 单位，渲染就绪，材质/灯光/相机齐全）
    bpy.ops.wm.save_as_mainfile(filepath=base + ".blend")
    print("[export] saved", base + ".blend")
    # 2) 网格导出：单独选中透镜并缩放回真实 mm（1 单位 = 1mm）
    bpy.ops.object.select_all(action='DESELECT')
    lens.select_set(True)
    bpy.context.view_layer.objects.active = lens
    lens.scale = (1.0 / U, 1.0 / U, 1.0 / U)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    d = lens.dimensions
    print("[export] lens real size (mm) = (%.3f, %.3f, %.3f)" % (d.x, d.y, d.z))

    def _try(label, fn):
        try:
            fn()
            print("[export] wrote", label)
        except Exception as e:
            print("[export] %s FAIL: %s" % (label, e))

    _try(base + ".stl", lambda: bpy.ops.wm.stl_export(
        filepath=base + ".stl", export_selected_objects=True))
    _try(base + ".obj", lambda: bpy.ops.wm.obj_export(
        filepath=base + ".obj", export_selected_objects=True))
    _try(base + ".glb", lambda: bpy.ops.export_scene.gltf(
        filepath=base + ".glb", export_format='GLB', use_selection=True))


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    reset_scene()
    lens = build_lens()  # 材质(清透+毛玻璃侧壁)已在内部分配
    report(lens)
    setup_world()
    setup_lights()
    add_ground()
    setup_render()

    # 侧剖：相机沿 +Y 看（X水平=光轴, Z竖直），看左右两个非球面凸镜
    cam_side = make_camera("CamSide", (0, -320, 0), ortho=True, ortho_scale=150)
    # 正视：相机沿 +X 看，看方形面+圆形镜区+平边框
    cam_face = make_camera("CamFace", (-320, 0, 0), ortho=True, ortho_scale=150)
    # 3/4 透视
    cam_persp = make_camera("CamPersp", (150, -190, 120), ortho=False)

    if DO_RENDER:
        render_to(cam_side, os.path.join(OUT_DIR, "microlens_side.png"))
        render_to(cam_face, os.path.join(OUT_DIR, "microlens_face.png"))
        render_to(cam_persp, os.path.join(OUT_DIR, "microlens_persp.png"))

    if DO_EXPORT:
        export_models(lens)  # 注意：导出会把透镜缩放到 mm，故放在渲染之后


if __name__ == "__main__":
    main()
