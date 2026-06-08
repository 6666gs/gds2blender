import bpy
import math

print("Setting up Scale-Independent Studio Lights...")

# 1. 安全锁：强制切换回实体模式防止卡死
for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D':
        for space in area.spaces:
            if space.type == 'VIEW_3D':
                space.shading.type = 'SOLID'

# 2. 引擎设置
try:
    bpy.context.scene.render.engine = 'BLENDER_EEVEE_NEXT'
except TypeError:
    bpy.context.scene.render.engine = 'BLENDER_EEVEE'

if hasattr(bpy.context.scene, "eevee"):
    if hasattr(bpy.context.scene.eevee, "use_ssr"): bpy.context.scene.eevee.use_ssr = True
    if hasattr(bpy.context.scene.eevee, "use_gtao"): bpy.context.scene.eevee.use_gtao = True
    if hasattr(bpy.context.scene.eevee, "use_bloom"): bpy.context.scene.eevee.use_bloom = True

# 3. 清理旧灯光和旧背景 (把刚才黑漆漆的灯删掉)
for obj in bpy.data.objects:
    if obj.type in ['LIGHT'] or obj.name == "Studio_Backdrop":
        bpy.data.objects.remove(obj, do_unlink=True)

# 4. 创建超大自适应背景底板 (扩大到 100,000 单位，确保能盖住巨型芯片)
bpy.ops.mesh.primitive_plane_add(size=100000, enter_editmode=False, align='WORLD', location=(0, 0, -100))
bg = bpy.context.active_object
bg.name = "Studio_Backdrop"

bg_mat = bpy.data.materials.new(name="Mat_Background")
bg_mat.use_nodes = True
bg_bsdf = bg_mat.node_tree.nodes.get("Principled BSDF")
if bg_bsdf:
    bg_bsdf.inputs['Base Color'].default_value = (0.05, 0.05, 0.08, 1) # 深蓝灰
    bg_bsdf.inputs['Roughness'].default_value = 0.8 
bg.data.materials.append(bg_mat)

# 5. 使用无视比例的【太阳光】 (Sun Light)
def add_sun(name, energy, rot, color):
    # 位置对于太阳光毫无意义，只受旋转角度影响
    bpy.ops.object.light_add(type='SUN', location=(0, 0, 100)) 
    light = bpy.context.active_object
    light.name = name
    light.data.energy = energy
    light.rotation_euler = rot
    light.data.color = color

# 主光源：从左前方打来的暖色阳光，提供主要照明
add_sun("Sun_Key", 4.0, (math.radians(45), 0, math.radians(-30)), (1.0, 0.95, 0.9))
# 轮廓光：从右后方打来的冷色强光，用来勾勒波导和金属的边缘反光 (极其重要)
add_sun("Sun_Rim", 3.0, (math.radians(-60), 0, math.radians(120)), (0.7, 0.85, 1.0))
# 顶光：垂直向下打的微弱中性光，照亮暗部细节
add_sun("Sun_Top", 1.5, (0, 0, 0), (1.0, 1.0, 1.0))

# 6. 设置微弱的全局环境光 (避免纯黑死角，给一点点夜空灰的底色)
if bpy.data.worlds:
    world_tree = bpy.data.worlds["World"].node_tree
    if world_tree and "Background" in world_tree.nodes:
        world_tree.nodes["Background"].inputs[0].default_value = (0.02, 0.02, 0.03, 1)

print("Studio Setup Complete! Safe to enter Rendered Mode.")