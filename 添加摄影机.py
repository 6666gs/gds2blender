import bpy
import math

print("--- 开始搭建摄影场景与摄影约束空间 ---")

# 1. 安全锁：强制切换回实体模式防止卡死
for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D':
        for space in area.spaces:
            if space.type == 'VIEW_3D':
                space.shading.type = 'SOLID'

# 2. 自动加入并设置摄像机
print("  -> 正在加入并设置摄像机...")
for obj in bpy.data.objects:
    if obj.type == 'CAMERA':
        bpy.data.objects.remove(obj, do_unlink=True)

bpy.ops.object.camera_add(location=(100, -150, 100)) 
camera = bpy.context.active_object
camera.name = "Photo_Camera"
camera.rotation_euler = (math.radians(60), 0, math.radians(45))
camera.data.clip_end = 100000 
bpy.context.scene.camera = camera

## 3. 创建摄影约束空间 ("容器")
#print("  -> 正在创建摄影约束空间 ('容器')...")
#trench_size = 500 
#trench_thickness = 100 

#bpy.ops.mesh.primitive_cube_add(size=trench_size, enter_editmode=False, align='WORLD', location=(0, 0, 0))
#trench_volume = bpy.context.active_object
#trench_volume.name = "Photo_Constraint_Volume"
#trench_volume.scale[2] = trench_thickness / trench_size

#chip_root_name = "GDS_Chip_Root"
#if chip_root_name in bpy.data.objects:
#    chip_root = bpy.data.objects[chip_root_name]
#    trench_volume.parent = chip_root

#    # --- 步骤 B: 应用布尔修改器实现裁剪 ---
#    print("  -> 正在为所有子模型应用布尔裁剪器...")
#    bool_mod_name = "Boolean_Clipping"
#    
#    for child in chip_root.children:
#        # 【核心修复】：只给 MESH 加修改器，且绝对不能是 trench_volume 自己！
#        if child.type == 'MESH' and child != trench_volume: 
#            if bool_mod_name in [mod.name for mod in child.modifiers]:
#                child.modifiers.remove(child.modifiers.get(bool_mod_name))
#            
#            bool_mod = child.modifiers.new(name=bool_mod_name, type='BOOLEAN')
#            bool_mod.operation = 'INTERSECT'
#            bool_mod.object = trench_volume
#            
#            # 自动适配新老版本的快速求解器
#            try:
#                bool_mod.solver = 'FAST'   # 兼容 Blender 4.0 及以下
#            except TypeError:
#                bool_mod.solver = 'FLOAT'  # 兼容 Blender 4.1 及以上
#            
#            # 关闭视口的实时显示，防卡死，只在最终 F12 渲染时生效
#            bool_mod.show_viewport = False 
#            bool_mod.show_render = True

## 将容器本身设为线框显示，不参与渲染
#trench_volume.display_type = 'WIRE'
#trench_volume.show_name = True
## 确保这个线框盒子本身在渲染时也是不可见的
#trench_volume.hide_render = True 

#print("--- 高级摄影场景与摄影约束搭建完成！ ---")