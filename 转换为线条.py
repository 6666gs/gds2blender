import bpy

# ── 配置 ──────────────────────────────────────────────────────────
WIRE_THICKNESS = 0.5            # 线框粗细（版图单位）；线太粗/太细就改这里
MODIFIER_NAME = "Lit_Wireframe"
# 目标结构根物体名称。留空 "" = 自动识别（推荐），优先级如下：
#   ① 填了名字且存在 → 用它（可填导入器面板里的「结构名称」，默认 "GDS_Chip"）
#   ② 否则有选中物体 → 处理选中物体所在的结构
#   ③ 否则           → 处理场景里所有 GDS 结构（带网格子物体的空物体）
TARGET_ROOT = ""


def mesh_descendants(obj):
    """递归收集 obj 之下的所有 MESH 物体。"""
    out = []
    for child in obj.children:
        if child.type == 'MESH':
            out.append(child)
        out.extend(mesh_descendants(child))
    return out


def apply_wireframe(mesh_obj):
    """给单个网格加（或重建）线框修改器。"""
    old = mesh_obj.modifiers.get(MODIFIER_NAME)
    if old:
        mesh_obj.modifiers.remove(old)
    mod = mesh_obj.modifiers.new(name=MODIFIER_NAME, type='WIREFRAME')
    mod.thickness = WIRE_THICKNESS
    mod.use_replace = True        # 替换实体面 → 纯镂空线框
    mod.use_even_offset = True    # 拐角处粗细均匀
    mod.use_boundary = True       # 包含边界线
    print(f"  已为 {mesh_obj.name} 生成实体线框。")


def resolve_roots():
    """按优先级决定要处理哪些结构根物体，返回 (根物体列表, 说明)。"""
    # ① 指定名字且存在
    if TARGET_ROOT:
        root = bpy.data.objects.get(TARGET_ROOT)
        if root:
            return [root], f"目标结构：{root.name}"
        print(f"⚠ 未找到名为 {TARGET_ROOT!r} 的物体，改用自动模式。")
    # ② 选中物体所在结构（向上找到顶层父物体；忽略不含网格的无关选择）
    roots = []
    for obj in bpy.context.selected_objects:
        top = obj
        while top.parent:
            top = top.parent
        if top not in roots and (top.type == 'MESH' or mesh_descendants(top)):
            roots.append(top)
    if roots:
        return roots, "目标：当前选中物体所在的结构"
    # ③ 兜底：所有顶层空物体且带网格子物体（即导入器生成的结构根）
    roots = [o for o in bpy.data.objects
             if o.type == 'EMPTY' and o.parent is None and mesh_descendants(o)]
    return roots, "目标：场景中全部 GDS 结构"


print("--- 正在转换为带光照的物理线框 ---")
roots, note = resolve_roots()
if not roots:
    print("未找到任何结构。请先运行「挂载gds导入.py」生成模型，"
          "或在 TARGET_ROOT 填入「结构名称」、或先选中目标结构后重跑。")
else:
    print(note)
    total = 0
    for root in roots:
        meshes = ([root] if root.type == 'MESH' else []) + mesh_descendants(root)
        if not meshes:
            print(f"[{root.name}] 没有网格子物体，跳过。")
            continue
        print(f"[{root.name}] 共 {len(meshes)} 个网格：")
        for m in meshes:
            apply_wireframe(m)
            total += 1
    print(f"--- 完成！共处理 {total} 个网格，按 F12 查看渲染效果 ---")
