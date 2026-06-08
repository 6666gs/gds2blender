import bpy

print("--- 正在转换为带光照的物理线框 ---")

chip_root_name = "GDS_Chip_Root"
if chip_root_name in bpy.data.objects:
    chip_root = bpy.data.objects[chip_root_name]
    
    for child in chip_root.children:
        if child.type == 'MESH':
            # 防止重复添加
            mod_name = "Lit_Wireframe"
            if mod_name in [m.name for m in child.modifiers]:
                child.modifiers.remove(child.modifiers.get(mod_name))
                
            # 添加线框修改器
            wire_mod = child.modifiers.new(name=mod_name, type='WIREFRAME')
            
            # 【核心参数】：线框的粗细程度 (单位同你的版图单位)
            # 如果渲染出来线太粗或太细，请在这里修改数值！
            wire_mod.thickness = 0.5 
            
            # 替换原网格 (去掉实体面，让它变成纯镂空线框)
            wire_mod.use_replace = True 
            
            # 保持拐角处粗细均匀
            wire_mod.use_even_offset = True 
            # 包含边界线
            wire_mod.use_boundary = True 
            
            print(f"已为 {child.name} 生成实体线框。")
else:
    print("未找到 GDS_Chip_Root，请确认模型是否已生成。")
    
print("--- 转换完成！请按下 F12 查看渲染效果 ---")