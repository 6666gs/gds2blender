import bpy
import bmesh
import gdspy
import os
import time

# ==========================================
# 1. 数据结构
# ==========================================
class GDSLayerItem(bpy.types.PropertyGroup):
    layer_name: bpy.props.StringProperty(name="层名称")
    layer_num: bpy.props.IntProperty()
    datatype_num: bpy.props.IntProperty()
    
    is_active: bpy.props.BoolProperty(name="启用", default=True)
    
    extrude_dir: bpy.props.EnumProperty(
        name="方向",
        items=[
            ('UP', '向上 (生长/沉积)', '向上挤出厚度'),
            ('DOWN', '向下 (刻蚀/挖槽)', '生成反转掩膜并向下挤出')
        ],
        default='UP'
    )
    
    z_start: bpy.props.FloatProperty(name="Z起点", default=0.0, step=10)
    thickness: bpy.props.FloatProperty(name="厚度", default=0.22, min=0.0, step=1)
    color: bpy.props.FloatVectorProperty(
        name="颜色", subtype='COLOR', size=4, 
        default=(0.5, 0.5, 0.5, 1.0), min=0.0, max=1.0
    )

class GDSSceneProperties(bpy.types.PropertyGroup):
    filepath: bpy.props.StringProperty(name="GDS 文件", subtype='FILE_PATH')
    scale: bpy.props.FloatProperty(name="缩放 (微米)", default=1.0, min=0.001)
    
    use_substrate: bpy.props.BoolProperty(name="生成基底 (Substrate)", default=True)
    sub_z_start: bpy.props.FloatProperty(name="基底顶面高度", default=-0.22, step=10) # 默认下降，留出刻蚀层空间
    sub_thickness: bpy.props.FloatProperty(name="基底厚度", default=5.0, min=0.1, step=10)
    sub_color: bpy.props.FloatVectorProperty(
        name="基底颜色", subtype='COLOR', size=4, 
        default=(0.15, 0.15, 0.15, 1.0), min=0.0, max=1.0 
    )
    
    layers: bpy.props.CollectionProperty(type=GDSLayerItem)

# ==========================================
# 2. 读取 GDS 
# ==========================================
class GDS_OT_LoadLayers(bpy.types.Operator):
    bl_idname = "gds.load_layers"
    bl_label = "1. 读取并分析 GDS 层"
    
    def execute(self, context):
        props = context.scene.gds_props
        if not os.path.exists(props.filepath):
            self.report({'ERROR'}, "找不到文件！")
            return {'CANCELLED'}
            
        props.layers.clear()
        
        try:
            lib = gdspy.GdsLibrary(infile=props.filepath)
            top_cell = next((c for c in lib.top_level() if not c.name.startswith('$$$')), lib.top_level()[-1])
            layers_dict = top_cell.get_polygons(by_spec=True)
            
            current_z = 0.0 
            for spec in sorted(layers_dict.keys()):
                l_num, d_type = spec
                item = props.layers.add()
                item.layer_name = f"Layer {l_num}/{d_type}"
                item.layer_num = l_num
                item.datatype_num = d_type
                item.z_start = current_z
                item.extrude_dir = 'UP' 
                
                if l_num == 40 and d_type == 0:
                    item.color = (0.15, 0.15, 0.25, 1.0)
                    item.thickness = 0.22
                
                current_z += item.thickness + 0.1 
                
            self.report({'INFO'}, f"成功读取 {len(props.layers)} 个层！")
        except Exception as e:
            self.report({'ERROR'}, f"读取失败: {e}")
            
        return {'FINISHED'}

# ==========================================
# 3. 核心加速器与布尔运算
# ==========================================
class GDS_OT_Generate3D(bpy.types.Operator):
    bl_idname = "gds.generate_3d"
    bl_label = "2. 极速生成 3D 结构"
    
    def execute(self, context):
        props = context.scene.gds_props
        if not os.path.exists(props.filepath): return {'CANCELLED'}

        start_time = time.time()
        
        root_name = "GDS_Chip_Root"
        if root_name in bpy.data.objects:
            root_obj = bpy.data.objects[root_name]
            for child in root_obj.children:
                bpy.data.objects.remove(child, do_unlink=True)
            bpy.data.objects.remove(root_obj, do_unlink=True)
            
        chip_root = bpy.data.objects.new(root_name, None)
        bpy.context.collection.objects.link(chip_root)

        lib = gdspy.GdsLibrary(infile=props.filepath)
        top_cell = next((c for c in lib.top_level() if not c.name.startswith('$$$')), lib.top_level()[-1])
        
        # 定义基底外扩边缘 (微米)
        GDS_PAD = 10.0
        bbox = top_cell.get_bounding_box()
        
        if props.use_substrate and bbox is not None:
            (xmin, ymin), (xmax, ymax) = bbox
            self.create_substrate(xmin, ymin, xmax, ymax, GDS_PAD, props, chip_root)

        all_polys = top_cell.get_polygons(by_spec=True)
        
        for item in props.layers:
            if not item.is_active: continue
            spec = (item.layer_num, item.datatype_num)
            if spec not in all_polys: continue
            
            raw_polys = all_polys[spec]
            target_polys = raw_polys
            
            # --- 核心更新：2D 掩膜反转刻蚀法 ---
            if item.extrude_dir == 'DOWN' and bbox is not None:
                (xmin, ymin), (xmax, ymax) = bbox
                # 创建一个和基底完全一样大的虚拟外框
                rect = gdspy.Rectangle((xmin - GDS_PAD, ymin - GDS_PAD), (xmax + GDS_PAD, ymax + GDS_PAD))
                try:
                    print(f"[{item.layer_name}] 正在计算 2D 刻蚀掩膜 (这可能需要几秒钟)...")
                    # 使用 gdspy 高效运算：外框 减去 波导形状
                    inverse_polys = gdspy.boolean(rect, raw_polys, 'not')
                    if inverse_polys is not None:
                        target_polys = inverse_polys
                except Exception as e:
                    print(f"[{item.layer_name}] 布尔反转失败: {e}")
                    
            obj = self.create_mesh_fast(item, target_polys, props.scale)
            if obj:
                obj.parent = chip_root

        end_time = time.time()
        self.report({'INFO'}, f"渲染完成！耗时: {end_time - start_time:.2f} 秒")
        return {'FINISHED'}

    def create_substrate(self, xmin, ymin, xmax, ymax, pad, props, parent):
        name = "GDS_Substrate"
        mesh = bpy.data.meshes.new(name)
        obj = bpy.data.objects.new(name, mesh)
        bpy.context.collection.objects.link(obj)
        obj.parent = parent
        
        s = props.scale
        z0 = props.sub_z_start * s
        
        verts = [
            ((xmin - pad) * s, (ymin - pad) * s, z0),
            ((xmax + pad) * s, (ymin - pad) * s, z0),
            ((xmax + pad) * s, (ymax + pad) * s, z0),
            ((xmin - pad) * s, (ymax + pad) * s, z0)
        ]
        faces = [[0, 1, 2, 3]]
        
        mesh.from_pydata(verts, [], faces)
        mesh.update()
        
        if props.sub_thickness > 0:
            bm = bmesh.new()
            bm.from_mesh(mesh)
            res = bmesh.ops.extrude_face_region(bm, geom=bm.faces[:])
            extruded_verts = [v for v in res['geom'] if isinstance(v, bmesh.types.BMVert)]
            bmesh.ops.translate(bm, vec=(0, 0, -props.sub_thickness * s), verts=extruded_verts)
            bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
            bm.to_mesh(mesh)
            bm.free()
            
        mat = bpy.data.materials.new(name="Mat_Substrate")
        mat.use_nodes = True
        nodes = mat.node_tree.nodes
        principled = nodes.get("Principled BSDF")
        if not principled:
            nodes.clear()
            principled = nodes.new(type='ShaderNodeBsdfPrincipled')
            output = nodes.new(type='ShaderNodeOutputMaterial')
            mat.node_tree.links.new(principled.outputs[0], output.inputs[0])
            
        principled.inputs['Base Color'].default_value = props.sub_color
        mat.diffuse_color = props.sub_color
        obj.data.materials.append(mat)

    def create_mesh_fast(self, layer_item, poly_data, scale):
        name = layer_item.layer_name
        all_verts = []
        all_faces = []
        vert_idx_offset = 0
        poly_list = poly_data.polygons if hasattr(poly_data, 'polygons') else poly_data
        
        for poly in poly_list:
            if len(poly) > 1 and tuple(poly[0]) == tuple(poly[-1]):
                poly = poly[:-1]
            num_verts = len(poly)
            if num_verts < 3: continue 
            for x, y in poly:
                all_verts.append((x * scale, y * scale, layer_item.z_start * scale))
            face_indices = list(range(vert_idx_offset, vert_idx_offset + num_verts))
            all_faces.append(face_indices)
            vert_idx_offset += num_verts

        if not all_verts: return None

        mesh = bpy.data.meshes.new(name)
        mesh.from_pydata(all_verts, [], all_faces)
        mesh.update()
        
        obj = bpy.data.objects.new(name, mesh)
        bpy.context.collection.objects.link(obj)
        
        if layer_item.thickness > 0:
            bm = bmesh.new()
            bm.from_mesh(mesh)
            geom_to_extrude = bm.faces[:]
            res = bmesh.ops.extrude_face_region(bm, geom=geom_to_extrude)
            
            extrude_dist = layer_item.thickness * scale
            if layer_item.extrude_dir == 'DOWN':
                extrude_dist = -extrude_dist 
                
            extruded_verts = [v for v in res['geom'] if isinstance(v, bmesh.types.BMVert)]
            bmesh.ops.translate(bm, vec=(0, 0, extrude_dist), verts=extruded_verts)
            bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
            bm.to_mesh(mesh)
            bm.free()

        mat = bpy.data.materials.new(name=f"Mat_{name}")
        mat.use_nodes = True
        nodes = mat.node_tree.nodes
        principled = nodes.get("Principled BSDF")
        if not principled:
            nodes.clear()
            principled = nodes.new(type='ShaderNodeBsdfPrincipled')
            output = nodes.new(type='ShaderNodeOutputMaterial')
            mat.node_tree.links.new(principled.outputs[0], output.inputs[0])
            
        principled.inputs['Base Color'].default_value = layer_item.color
        mat.diffuse_color = layer_item.color 
        obj.data.materials.append(mat)
        
        return obj

# ==========================================
# 4. UI 面板绘制
# ==========================================
class GDS_PT_MainPanel(bpy.types.Panel):
    bl_label = "GDS 极速导入器 (真实刻蚀版)"
    bl_idname = "GDS_PT_main_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "GDS Importer" 

    def draw(self, context):
        layout = self.layout
        props = context.scene.gds_props

        box = layout.box()
        box.prop(props, "filepath")
        box.operator("gds.load_layers", icon='FILE_REFRESH')
        
        layout.separator()
        layout.prop(props, "scale")
        
        sub_box = layout.box()
        sub_box.label(text="底层基底 (Substrate) 设置:", icon='MOD_THICKNESS')
        sub_box.prop(props, "use_substrate")
        if props.use_substrate:
            sub_box.prop(props, "sub_z_start")
            sub_box.prop(props, "sub_thickness")
            sub_box.prop(props, "sub_color")
        
        if len(props.layers) > 0:
            layout.separator()
            layout.label(text="版图各层级参数配置:", icon='MATERIAL')
            
            for item in props.layers:
                box = layout.box()
                row = box.row(align=True)
                row.prop(item, "is_active", text="")
                row.label(text=item.layer_name)
                row.prop(item, "color", text="")
                
                if item.is_active:
                    col = box.column(align=True)
                    col.prop(item, "extrude_dir") 
                    col.prop(item, "z_start")
                    col.prop(item, "thickness")

            layout.separator()
            layout.scale_y = 1.5
            layout.operator("gds.generate_3d", icon='MOD_BUILD')

classes = (GDSLayerItem, GDSSceneProperties, GDS_OT_LoadLayers, GDS_OT_Generate3D, GDS_PT_MainPanel)
def register():
    for cls in classes: bpy.utils.register_class(cls)
    bpy.types.Scene.gds_props = bpy.props.PointerProperty(type=GDSSceneProperties)
def unregister():
    for cls in reversed(classes): bpy.utils.unregister_class(cls)
    del bpy.types.Scene.gds_props
if __name__ == "__main__": register()