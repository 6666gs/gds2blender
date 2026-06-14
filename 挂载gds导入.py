import bpy
import bmesh
import gdspy
import os
import time
import json

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
    sub_z_start: bpy.props.FloatProperty(name="基底顶面高度", default=-0.22, step=10)
    sub_thickness: bpy.props.FloatProperty(name="基底厚度", default=5.0, min=0.1, step=10)
    sub_color: bpy.props.FloatVectorProperty(
        name="基底颜色", subtype='COLOR', size=4,
        default=(0.15, 0.15, 0.15, 1.0), min=0.0, max=1.0
    )
    sub_pad_xmin: bpy.props.FloatProperty(name="X- 增量", default=10.0, min=0.0, step=10)
    sub_pad_xmax: bpy.props.FloatProperty(name="X+ 增量", default=10.0, min=0.0, step=10)
    sub_pad_ymin: bpy.props.FloatProperty(name="Y- 增量", default=10.0, min=0.0, step=10)
    sub_pad_ymax: bpy.props.FloatProperty(name="Y+ 增量", default=10.0, min=0.0, step=10)

    struct_name: bpy.props.StringProperty(
        name="结构名称",
        description="本次生成结构的根物体名称",
        default="GDS_Chip"
    )
    overwrite_same: bpy.props.BoolProperty(
        name="覆盖同名结构",
        description="勾选：若已存在同名结构则删除重建（定向更新某个结构，其它结构不受影响）；取消：自动编号新建",
        default=True
    )

    struct_location: bpy.props.FloatVectorProperty(
        name="结构位置 (微米 XYZ)",
        description="本次生成结构整体放置坐标，单位微米，与版图坐标对齐（内部按 scale 换算到 Blender 世界单位）",
        size=3,
        subtype='XYZ',
        default=(0.0, 0.0, 0.0),
        step=10
    )

    layers: bpy.props.CollectionProperty(type=GDSLayerItem)

# ==========================================
# 1.5 配置存取 (旁路 JSON)
# ==========================================
def _sidecar_path(filepath):
    """配置文件 = GDS 路径 + .layers.json"""
    return bpy.path.abspath(filepath) + ".layers.json"

def _color4(c):
    """转成长度为 4 的 RGBA 元组"""
    c = list(c)
    if len(c) < 4:
        c = c + [1.0] * (4 - len(c))
    return tuple(c[:4])

def _config_from_props(props):
    return {
        "schema": 1,
        "source": os.path.basename(bpy.path.abspath(props.filepath)),
        "scale": props.scale,
        "struct_location": list(props.struct_location),
        "substrate": {
            "use": props.use_substrate,
            "z_start": props.sub_z_start,
            "thickness": props.sub_thickness,
            "color": list(props.sub_color),
            "pad": [props.sub_pad_xmin, props.sub_pad_xmax,
                    props.sub_pad_ymin, props.sub_pad_ymax],
        },
        "layers": [
            {
                "layer": it.layer_num,
                "datatype": it.datatype_num,
                "name": it.layer_name,
                "active": it.is_active,
                "dir": it.extrude_dir,
                "z_start": it.z_start,
                "thickness": it.thickness,
                "color": list(it.color),
            }
            for it in props.layers
        ],
    }

def _apply_config(props, data):
    """把配置 dict 套用到 props。返回 (ok, message)。
    校验：当前已载入层的 (层号, 数据类型) 集合 必须与配置完全一致，否则失败。"""
    cur = {(it.layer_num, it.datatype_num) for it in props.layers}
    saved = {(l["layer"], l["datatype"]) for l in data.get("layers", [])}
    if cur != saved:
        return False, (f"层不匹配：当前 {len(cur)} 层，配置 {len(saved)} 层"
                       f"（按 层号/数据类型 集合比较）")

    if "scale" in data:
        props.scale = data["scale"]

    loc = data.get("struct_location")
    if loc and len(loc) == 3:
        props.struct_location = tuple(loc)

    sub = data.get("substrate")
    if sub:
        props.use_substrate = sub.get("use", props.use_substrate)
        props.sub_z_start = sub.get("z_start", props.sub_z_start)
        props.sub_thickness = sub.get("thickness", props.sub_thickness)
        if "color" in sub:
            props.sub_color = _color4(sub["color"])
        pad = sub.get("pad")
        if pad and len(pad) == 4:
            props.sub_pad_xmin, props.sub_pad_xmax, props.sub_pad_ymin, props.sub_pad_ymax = pad

    by_key = {(l["layer"], l["datatype"]): l for l in data.get("layers", [])}
    for it in props.layers:
        l = by_key.get((it.layer_num, it.datatype_num))
        if not l:
            continue
        it.is_active = l.get("active", it.is_active)
        if "dir" in l:
            it.extrude_dir = l["dir"]
        it.z_start = l.get("z_start", it.z_start)
        it.thickness = l.get("thickness", it.thickness)
        if "name" in l:
            it.layer_name = l["name"]
        if "color" in l:
            it.color = _color4(l["color"])
    return True, f"已套用配置（{len(saved)} 层）"

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

            # 自动尝试套用旁路配置（若存在）
            auto_path = _sidecar_path(props.filepath)
            if os.path.exists(auto_path):
                try:
                    with open(auto_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    ok, msg = _apply_config(props, data)
                    if ok:
                        self.report({'INFO'}, f"成功读取 {len(props.layers)} 个层，并自动套用配置")
                    else:
                        self.report({'WARNING'}, f"读取 {len(props.layers)} 层；配置未套用 - {msg}")
                except Exception as e:
                    self.report({'WARNING'}, f"读取 {len(props.layers)} 层；配置解析失败: {e}")
            else:
                self.report({'INFO'}, f"成功读取 {len(props.layers)} 个层！")
        except Exception as e:
            self.report({'ERROR'}, f"读取失败: {e}")
            
        return {'FINISHED'}

# ==========================================
# 2.5 配置 保存 / 载入 Operator
# ==========================================
class GDS_OT_SaveConfig(bpy.types.Operator):
    bl_idname = "gds.save_config"
    bl_label = "保存配置"
    bl_description = "把当前各层参数 + 基底 + 缩放写入 GDS 旁的 .layers.json"

    def execute(self, context):
        props = context.scene.gds_props
        if not props.filepath:
            self.report({'ERROR'}, "请先指定 GDS 文件路径！")
            return {'CANCELLED'}
        if len(props.layers) == 0:
            self.report({'ERROR'}, "当前没有层可保存，请先读取 GDS！")
            return {'CANCELLED'}
        path = _sidecar_path(props.filepath)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(_config_from_props(props), f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.report({'ERROR'}, f"保存失败: {e}")
            return {'CANCELLED'}
        self.report({'INFO'}, f"配置已保存: {os.path.basename(path)}")
        return {'FINISHED'}

class GDS_OT_LoadConfig(bpy.types.Operator):
    bl_idname = "gds.load_config"
    bl_label = "载入配置"
    bl_description = "读取 GDS 旁的 .layers.json；层不一致则载入失败"

    def execute(self, context):
        props = context.scene.gds_props
        if not props.filepath:
            self.report({'ERROR'}, "请先指定 GDS 文件路径！")
            return {'CANCELLED'}
        if len(props.layers) == 0:
            self.report({'ERROR'}, "请先读取 GDS 层再载入配置！")
            return {'CANCELLED'}
        path = _sidecar_path(props.filepath)
        if not os.path.exists(path):
            self.report({'ERROR'}, f"找不到配置文件: {os.path.basename(path)}")
            return {'CANCELLED'}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            self.report({'ERROR'}, f"读取配置失败: {e}")
            return {'CANCELLED'}
        ok, msg = _apply_config(props, data)
        if not ok:
            self.report({'ERROR'}, f"载入失败 - {msg}")
            return {'CANCELLED'}
        self.report({'INFO'}, msg)
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
        
        base_name = props.struct_name.strip() or "GDS_Chip"
        if props.overwrite_same:
            # 定向更新：同名结构存在则删除重建，其它结构不受影响
            root_name = base_name
            if root_name in bpy.data.objects:
                self._remove_structure(bpy.data.objects[root_name])
        else:
            # 自动寻找未被占用的编号名称
            root_name = base_name
            idx = 1
            while root_name in bpy.data.objects:
                root_name = f"{base_name}_{idx:03d}"
                idx += 1

        chip_root = bpy.data.objects.new(root_name, None)
        bpy.context.collection.objects.link(chip_root)
        # 结构位置按微米输入，乘以 scale 换算到 Blender 世界单位，与版图坐标对齐
        chip_root.location = tuple(c * props.scale for c in props.struct_location)

        lib = gdspy.GdsLibrary(infile=props.filepath)
        top_cell = next((c for c in lib.top_level() if not c.name.startswith('$$$')), lib.top_level()[-1])
        
        bbox = top_cell.get_bounding_box()

        if props.use_substrate and bbox is not None:
            (xmin, ymin), (xmax, ymax) = bbox
            self.create_substrate(xmin, ymin, xmax, ymax, props, chip_root)

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
                # 刻蚀掩膜与基底保持相同边界
                rect = gdspy.Rectangle(
                    (xmin - props.sub_pad_xmin, ymin - props.sub_pad_ymin),
                    (xmax + props.sub_pad_xmax, ymax + props.sub_pad_ymax)
                )
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

    def _remove_structure(self, root_obj):
        """删除结构根物体及其全部子物体，并清理由此产生的孤立 mesh 与材质，
        避免反复重载同名结构导致 .blend 里残留 Mat_xxx.001 与孤立网格无限堆积。"""
        objs = list(root_obj.children) + [root_obj]
        meshes = set()
        mats = set()
        for o in objs:
            if o.type == 'MESH' and o.data is not None:
                meshes.add(o.data)
                for m in o.data.materials:
                    if m is not None:
                        mats.add(m)
        for o in objs:
            bpy.data.objects.remove(o, do_unlink=True)
        for me in meshes:
            if me.users == 0:
                bpy.data.meshes.remove(me)
        for ma in mats:
            if ma.users == 0:
                bpy.data.materials.remove(ma)

    def create_substrate(self, xmin, ymin, xmax, ymax, props, parent):
        name = "GDS_Substrate"
        mesh = bpy.data.meshes.new(name)
        obj = bpy.data.objects.new(name, mesh)
        bpy.context.collection.objects.link(obj)
        obj.parent = parent

        s = props.scale
        z0 = props.sub_z_start * s

        verts = [
            ((xmin - props.sub_pad_xmin) * s, (ymin - props.sub_pad_ymin) * s, z0),
            ((xmax + props.sub_pad_xmax) * s, (ymin - props.sub_pad_ymin) * s, z0),
            ((xmax + props.sub_pad_xmax) * s, (ymax + props.sub_pad_ymax) * s, z0),
            ((xmin - props.sub_pad_xmin) * s, (ymax + props.sub_pad_ymax) * s, z0)
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
        cfg_row = box.row(align=True)
        cfg_row.operator("gds.save_config", icon='FILE_TICK')
        cfg_row.operator("gds.load_config", icon='IMPORT')
        
        layout.separator()
        layout.prop(props, "scale")
        
        sub_box = layout.box()
        sub_box.label(text="底层基底 (Substrate) 设置:", icon='MOD_THICKNESS')
        sub_box.prop(props, "use_substrate")
        if props.use_substrate:
            sub_box.prop(props, "sub_z_start")
            sub_box.prop(props, "sub_thickness")
            sub_box.prop(props, "sub_color")
            sub_box.separator()
            sub_box.label(text="基底外扩增量 (微米):")
            col = sub_box.column(align=True)
            col.prop(props, "sub_pad_ymax", text="Y+")
            row = col.row(align=True)
            row.prop(props, "sub_pad_xmin", text="X-")
            row.prop(props, "sub_pad_xmax", text="X+")
            col.prop(props, "sub_pad_ymin", text="Y-")
        
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
            layout.prop(props, "struct_name")
            layout.prop(props, "struct_location")
            layout.prop(props, "overwrite_same", icon='DUPLICATE')
            layout.scale_y = 1.5
            layout.operator("gds.generate_3d", icon='MOD_BUILD')

classes = (GDSLayerItem, GDSSceneProperties, GDS_OT_LoadLayers, GDS_OT_SaveConfig, GDS_OT_LoadConfig, GDS_OT_Generate3D, GDS_PT_MainPanel)
def register():
    for cls in classes: bpy.utils.register_class(cls)
    bpy.types.Scene.gds_props = bpy.props.PointerProperty(type=GDSSceneProperties)
def unregister():
    for cls in reversed(classes): bpy.utils.unregister_class(cls)
    del bpy.types.Scene.gds_props
if __name__ == "__main__": register()