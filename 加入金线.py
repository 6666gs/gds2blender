"""
加入金线.py —— 在两排焊盘之间生成一排平行的金键合线（bond wire）。

═══════════════════════════════════════════════════════════════════════
一、这个脚本做什么
═══════════════════════════════════════════════════════════════════════
在「焊盘轨道 A」和「焊盘轨道 B」之间，均匀生成 count 根拱形金线；
每根金线 = 一条 POLY 曲线 + 圆形 bevel（实心圆管），材质为金属金。
常用于把 InP RSOA 芯片的电极焊盘连接到载体/外部电极。

═══════════════════════════════════════════════════════════════════════
二、坐标系说明（重要）
═══════════════════════════════════════════════════════════════════════
- 所有坐标都是 Blender 的【世界坐标】，单位即 Blender 单位
  （本工程整器件尺度约 1~2 个单位，可对照 材质脚本里的 diag）。
- 每个坐标写成 (X, Y, Z)：X=左右、Y=前后、Z=上下（高度）。
- 金线对象创建在世界原点且无旋转缩放，所以你填的坐标 = 在视口里
  看到的那个位置的坐标，不需要再做任何换算。

═══════════════════════════════════════════════════════════════════════
三、如何在 Blender 里确认坐标（三选一，方法 A 最准）
═══════════════════════════════════════════════════════════════════════
方法 A —— 3D 光标吸附法（推荐）
  1. 选中目标焊盘：物体模式选中该物体；或 Tab 进编辑模式框选它的顶点/面；
  2. 按 Shift+S → 选「Cursor to Selected（光标→所选）」，
     3D 光标就跳到焊盘中心；
  3. 按 N 打开右侧边栏 →「视图 / View」标签 →「3D 游标 / 3D Cursor」
     → Location，这三个数就是该焊盘的世界坐标，抄进下面 railA/railB。
  （懒人版：直接运行本脚本里的 print_cursor()，把坐标打印到控制台再复制。）

方法 B —— 读物体原点坐标
  1. 物体模式单击选中焊盘物体；
  2. 按 N →「条目 / Item」标签 → Transform → Location 即物体原点坐标
     （或运行 print_selected() 直接打印）。

方法 C —— 编辑模式看顶点中位点
  1. Tab 进编辑模式，选中焊盘上的几个顶点；
  2. 按 N →「条目 / Item」→ Median 显示所选顶点的平均坐标。
     注意：此处是相对物体原点的【局部】坐标，若该物体有位移/旋转，
     结果会和世界坐标不一致——这种情况请改用方法 A。

═══════════════════════════════════════════════════════════════════════
四、怎么填 railA / railB 参数
═══════════════════════════════════════════════════════════════════════
- railA 是一排焊盘所在的【一条直线】：
    railA_start = 这排里【第一个】焊盘的坐标，
    railA_end   = 这排里【最后一个】焊盘的坐标；
  脚本会在这条线上等距取 count 个点。
- railB 同理，是【对面】那排焊盘的首、尾坐标。
- 第 i 根金线 = 连接「A 线上第 i 点」↔「B 线上第 i 点」。
- 所以只要量出每排首尾两个焊盘的坐标，中间的会自动插值，不必逐个量。

═══════════════════════════════════════════════════════════════════════
五、运行方式
═══════════════════════════════════════════════════════════════════════
- Scripting 工作区粘贴运行；或命令行：blender your.blend --python 加入金线.py
- 反复运行安全：开头 purge_wires() 会先删掉上一轮的金线（FIG_BondWire 前缀）。
- 对坐标技巧：先把 count 改成 1 只跑一根，确认两端正好落在焊盘上，
  再把 count 改回实际根数。
"""

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


# ─── 量坐标小工具：在 Blender 的 Python 控制台里调用，把坐标打印出来复制 ───
def print_cursor():
    """打印 3D 光标的世界坐标。
    配合方法 A 用：先 Shift+S → Cursor to Selected 把光标吸到焊盘，再调用本函数。"""
    c = bpy.context.scene.cursor.location
    print(f"3D 光标世界坐标: ({c.x:.4f}, {c.y:.4f}, {c.z:.4f})")
    return (round(c.x, 4), round(c.y, 4), round(c.z, 4))


def print_selected():
    """打印当前选中物体的世界坐标（物体原点）。"""
    obj = bpy.context.active_object
    if obj is None:
        print("⚠ 当前没有选中物体")
        return None
    loc = obj.matrix_world.translation
    print(f"{obj.name} 世界坐标: ({loc.x:.4f}, {loc.y:.4f}, {loc.z:.4f})")
    return (round(loc.x, 4), round(loc.y, 4), round(loc.z, 4))


# ─── 用法：把下面坐标/数值改成你模型里的真实值 ───────────────
#   坐标怎么量？见文件顶部「三、如何在 Blender 里确认坐标」。
#   先量出 A 排首尾焊盘、B 排首尾焊盘共 4 个坐标，填进对应 4 行即可。
purge_wires()
rsoa = bpy.data.objects.get("RSOA")  # 没有就保持 None；用于让金线随 RSOA 一起移动

make_bond_wire_array(
    railA_start=(-0.90, 0.02, 0.10),  # 焊盘 A 排：第一个焊盘坐标
    railA_end=(-0.90, 0.10, 0.10),  # 焊盘 A 排：最后一个焊盘坐标
    railB_start=(-0.70, 0.02, 0.10),  # 焊盘 B 排：第一个焊盘坐标
    railB_end=(-0.70, 0.10, 0.10),  # 焊盘 B 排：最后一个焊盘坐标
    count=6,  # 金线根数（A、B 之间等距铺这么多根）
    height=0.05,  # 拱顶离焊盘的高度（金线弓起多高）
    radius=0.004,  # 金线半径（线越粗这个越大）
    apex=0.4,  # 拱顶偏向起点的位置：0~1，越小越靠近 A 端
    parent=rsoa,
)
