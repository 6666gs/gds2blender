import bpy, math
from mathutils import Vector

WIRE_PREFIX = "FIG_BondWire"


def _lerp(a, b, t):
    return a + (b - a) * t


def _arch(t, apex):
    """0→apex→1 处分别为 0→最高→0 的拱形；apex<0.5 偏向起点，更像真实球楔焊。"""
    if t <= apex:
        arg = (t / apex) * (math.pi / 2) if apex > 0 else math.pi / 2
    else:
        arg = (
            math.pi / 2 + ((t - apex) / (1 - apex)) * (math.pi / 2)
            if apex < 1
            else math.pi / 2
        )
    return math.sin(arg)


def get_gold_material():
    mat = bpy.data.materials.get("FIG_Gold_Au")
    if mat:
        return mat
    mat = bpy.data.materials.new("FIG_Gold_Au")
    mat.use_nodes = True
    b = mat.node_tree.nodes.get("Principled BSDF")
    b.inputs["Base Color"].default_value = (1.0, 0.78, 0.34, 1.0)
    b.inputs["Metallic"].default_value = 1.0
    b.inputs["Roughness"].default_value = 0.25
    return mat


def make_bond_wire(start, end, height, radius, apex=0.4, segments=32, name="wire"):
    """从 start 拱到 end 的一根金线：POLY 曲线 + 圆形 bevel = 光滑实心管。"""
    start, end = Vector(start), Vector(end)
    curve = bpy.data.curves.new(f"{WIRE_PREFIX}_{name}", type='CURVE')
    curve.dimensions = '3D'
    curve.bevel_depth = radius  # ← 线半径，决定金线粗细
    curve.bevel_resolution = 4  # 截面圆滑度
    curve.use_fill_caps = True  # 封住两端

    spline = curve.splines.new('POLY')
    spline.points.add(segments)  # 默认已有 1 个点
    for i in range(segments + 1):
        t = i / segments
        p = _lerp(start, end, t)
        p.z += height * _arch(t, apex)
        spline.points[i].co = (p.x, p.y, p.z, 1.0)  # 第 4 个分量是权重

    obj = bpy.data.objects.new(f"{WIRE_PREFIX}_{name}", curve)
    obj.data.materials.append(get_gold_material())
    bpy.context.collection.objects.link(obj)
    return obj


def make_bond_wire_array(
    railA_start,
    railA_end,
    railB_start,
    railB_end,
    count,
    height,
    radius,
    apex=0.4,
    parent=None,
):
    """在 A、B 两条焊盘轨道之间均匀生成 count 根平行金线。"""
    A0, A1 = Vector(railA_start), Vector(railA_end)
    B0, B1 = Vector(railB_start), Vector(railB_end)
    for i in range(count):
        t = 0.0 if count == 1 else i / (count - 1)
        a, b = _lerp(A0, A1, t), _lerp(B0, B1, t)
        h = height * (1.0 + 0.04 * math.sin(i * 1.7))  # 轻微高度变化，避免全等
        w = make_bond_wire(a, b, h, radius, apex=apex, name=f"{i:02d}")
        if parent:
            w.parent = parent


def purge_wires():
    for obj in list(bpy.data.objects):
        if obj.name.startswith(WIRE_PREFIX):
            bpy.data.objects.remove(obj, do_unlink=True)


# ─── 用法：把下面坐标/数值改成你模型里的真实值 ───────────────
purge_wires()
rsoa = bpy.data.objects.get("RSOA")  # 没有就保持 None；用于让金线随 RSOA 一起移动

make_bond_wire_array(
    railA_start=(-0.90, 0.02, 0.10),  # 焊盘 A 起点
    railA_end=(-0.90, 0.10, 0.10),  # 焊盘 A 终点
    railB_start=(-0.70, 0.02, 0.10),  # 焊盘 B 起点
    railB_end=(-0.70, 0.10, 0.10),  # 焊盘 B 终点
    count=6,
    height=0.05,  # 拱顶离焊盘的高度
    radius=0.004,  # 金线半径
    apex=0.4,  # 拱顶偏向起点
    parent=rsoa,
)
