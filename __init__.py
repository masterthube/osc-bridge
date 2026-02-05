bl_info = {
    "name": "OSC Bridge",
    "author": "Gemini & Freekx",
    "version": (1, 1, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > OSC Bridge",
    "description": "Universal OSC receiver for Objects, Lights, Materials, World, and GeoNodes.",
    "category": "Animation",
}

import bpy
import socket
import threading
import struct

# --- GLOBALS ---
OSC_DATA = {} 
UDP_IP, UDP_PORT, BUFFER_SIZE = "127.0.0.1", 9000, 8192
LISTENING = False
SERVER_THREAD = None
SOCK = None

def parse_packet(data):
    try:
        if data.startswith(b'#bundle'):
            pos = 16
            while pos < len(data):
                size = struct.unpack('>i', data[pos:pos+4])[0]
                pos += 4
                parse_packet(data[pos:pos+size])
                pos += size
        elif data.startswith(b'/'):
            addr_end = data.find(b'\x00')
            address = data[:addr_end].decode('utf-8')
            tag_end = data.find(b'\x00', (addr_end + 4) & ~3)
            tags = data[(addr_end + 4) & ~3:tag_end].decode('utf-8')
            if 'f' in tags:
                val_pos = (tag_end + 4) & ~3
                val = struct.unpack('>f', data[val_pos:val_pos+4])[0]
                OSC_DATA[address] = {"val": val}
    except: pass

def get_osc(addr):
    return OSC_DATA.get(addr, {}).get("val", 0.0)

def get_target_datapath(ch):
    """Returns the actual data block based on user selection"""
    tp = ch.target_type
    if tp == 'OBJECT': return ch.ptr_obj
    if tp == 'MATERIAL': return ch.ptr_mat
    if tp == 'LIGHT': return ch.ptr_light
    if tp == 'WORLD': return ch.ptr_world
    if tp == 'SCENE': return bpy.context.scene
    return None

def osc_recording_handler(scene):
    props = scene.osc_tool
    if props.ui_mode != 'RECORD' or not props.is_recording or not bpy.context.screen.is_animation_playing:
        return
    for ch in props.record_channels:
        if not ch.enabled or ch.osc_address not in OSC_DATA: continue
        val = OSC_DATA[ch.osc_address]["val"]
        if abs(val - ch.last_val) < ch.thinning: continue
        
        try:
            target = get_target_datapath(ch)
            if not target: continue
            
            # Universal Resolve
            resolved = target.path_resolve(ch.path)
            if hasattr(resolved, "__len__"): # If it's an array (location, color)
                resolved[ch.index] = val
            else: # If it's a single float (energy, alpha)
                exec(f"target.{ch.path} = {val}")
                
            target.keyframe_insert(data_path=ch.path, index=ch.index)
            ch.last_val = val
        except: pass

# --- OPERATORS ---
class OSC_OT_Toggle(bpy.types.Operator):
    bl_idname = "osc.toggle"
    bl_label = "Toggle Server"
    def execute(self, context):
        global LISTENING, SERVER_THREAD, SOCK
        if not LISTENING:
            LISTENING = True
            SERVER_THREAD = threading.Thread(target=self.run_server, daemon=True)
            SERVER_THREAD.start()
            bpy.app.timers.register(self.redraw)
        else: LISTENING = False
        return {'FINISHED'}

    def run_server(self):
        global SOCK
        SOCK = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        SOCK.bind((UDP_IP, UDP_PORT))
        SOCK.settimeout(0.5)
        while LISTENING:
            try:
                data, _ = SOCK.recvfrom(BUFFER_SIZE)
                parse_packet(data)
            except: continue
        if SOCK: SOCK.close()

    def redraw(self):
        for area in bpy.context.screen.areas: area.tag_redraw()
        return 0.03 if LISTENING else None

class OSC_OT_CleanDrivers(bpy.types.Operator):
    bl_idname = "osc.clean_drivers"
    bl_label = "Kill OSC Drivers"
    def execute(self, context):
        # This scans ALL data blocks for OSC drivers
        count = 0
        search_areas = [bpy.data.objects, bpy.data.materials, bpy.data.lights, bpy.data.worlds, [bpy.context.scene]]
        for area in search_areas:
            for item in area:
                if item.animation_data:
                    for d in reversed(item.animation_data.drivers):
                        if "osc(" in d.driver.expression:
                            item.driver_remove(d.data_path, d.array_index); count += 1
        self.report({'INFO'}, f"Cleaned {count} OSC drivers.")
        return {'FINISHED'}

class OSC_OT_ChannelControl(bpy.types.Operator):
    bl_idname = "osc.channel_ctrl"
    bl_label = "Channel Control"
    target_mode: bpy.props.StringProperty()
    action: bpy.props.EnumProperty(items=[('ADD', "Add", ""), ('REMOVE', "Remove", "")])
    index: bpy.props.IntProperty(default=-1)
    def execute(self, context):
        props = context.scene.osc_tool
        coll = props.live_channels if self.target_mode == 'LIVE' else props.record_channels
        if self.action == 'ADD': coll.add()
        else: coll.remove(self.index)
        return {'FINISHED'}

class OSC_OT_ApplyDriver(bpy.types.Operator):
    bl_idname = "osc.apply_driver"
    bl_label = "Apply Driver"
    idx: bpy.props.IntProperty()
    def execute(self, context):
        ch = context.scene.osc_tool.live_channels[self.idx]
        target = get_target_datapath(ch)
        if target:
            try:
                d = target.driver_add(ch.path, ch.index).driver
                d.expression = f'osc("{ch.osc_address}")'
            except: self.report({'ERROR'}, "Invalid Path for this Data Type")
        return {'FINISHED'}

# --- UI & DATA ---
class OSCChannel(bpy.types.PropertyGroup):
    enabled: bpy.props.BoolProperty(default=True)
    osc_address: bpy.props.StringProperty(name="Addr", default="/val")
    target_type: bpy.props.EnumProperty(
        name="Type",
        items=[('OBJECT', "Object", "Transform/Modifiers"), 
               ('LIGHT', "Light", "Intensity/Color"),
               ('MATERIAL', "Material", "Shaders"), 
               ('WORLD', "World", "Environment"),
               ('SCENE', "Scene", "Custom Props")]
    )
    ptr_obj: bpy.props.PointerProperty(type=bpy.types.Object)
    ptr_mat: bpy.props.PointerProperty(type=bpy.types.Material)
    ptr_light: bpy.props.PointerProperty(type=bpy.types.Light)
    ptr_world: bpy.props.PointerProperty(type=bpy.types.World)
    
    path: bpy.props.StringProperty(name="Path", default="location")
    index: bpy.props.IntProperty(name="Idx", default=0)
    thinning: bpy.props.FloatProperty(name="Thinning", default=0.001, precision=4)
    last_val: bpy.props.FloatProperty()

class OSCToolProps(bpy.types.PropertyGroup):
    ui_mode: bpy.props.EnumProperty(items=[('LIVE', "LIVE TESTING", ""), ('RECORD', "RECORD BAKE", "")])
    is_recording: bpy.props.BoolProperty(name="Recording", default=False)
    live_channels: bpy.props.CollectionProperty(type=OSCChannel)
    record_channels: bpy.props.CollectionProperty(type=OSCChannel)
    ui_max: bpy.props.FloatProperty(name="Traffic Scale", default=5.0)

class OSC_PT_Panel(bpy.types.Panel):
    bl_label = "OSC Bridge Pro v1.1.0"
    bl_idname = "OSC_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "OSC Bridge"

    def draw(self, context):
        layout = self.layout
        tool = context.scene.osc_tool
        row = layout.row()
        row.operator("osc.toggle", text="STOP SERVER" if LISTENING else "START SERVER", 
                     icon='PAUSE' if LISTENING else 'PLAY', depress=LISTENING)
        layout.separator()
        layout.prop(tool, "ui_mode", expand=True)

        mode_list = tool.live_channels if tool.ui_mode == 'LIVE' else tool.record_channels
        
        if tool.ui_mode == 'LIVE':
            layout.operator("osc.clean_drivers", text="Kill OSC Drivers", icon='X')

        for i, ch in enumerate(mode_list):
            box = layout.box()
            row = box.row()
            row.prop(ch, "target_type", text="")
            
            # Dynamic Pointer based on type
            if ch.target_type == 'OBJECT': row.prop(ch, "ptr_obj", text="")
            elif ch.target_type == 'LIGHT': row.prop(ch, "ptr_light", text="")
            elif ch.target_type == 'MATERIAL': row.prop(ch, "ptr_mat", text="")
            elif ch.target_type == 'WORLD': row.prop(ch, "ptr_world", text="")
            
            op = row.operator("osc.channel_ctrl", text="", icon='PANEL_CLOSE', emboss=False)
            op.target_mode, op.action, op.index = tool.ui_mode, 'REMOVE', i
            
            col = box.column(align=True)
            col.prop(ch, "osc_address")
            r1 = col.row(); r1.prop(ch, "path"); r1.prop(ch, "index")
            
            if tool.ui_mode == 'LIVE':
                col.operator("osc.apply_driver", text="Apply Driver Link").idx = i
            else:
                col.prop(ch, "thinning")

        # Record Trigger
        if tool.ui_mode == 'RECORD':
            layout.prop(tool, "is_recording", text="RECORDING ON" if tool.is_recording else "ARM RECORDING", 
                        toggle=True, icon='REC' if tool.is_recording else 'RADIOBUT_OFF')

        # Add Button
        op = layout.operator("osc.channel_ctrl", text="Add New Channel", icon='ADD')
        op.target_mode, op.action = tool.ui_mode, 'ADD'

        if OSC_DATA:
            layout.separator()
            box = layout.box()
            box.label(text="Traffic Monitor", icon='NODE_SEL')
            for addr in sorted(OSC_DATA.keys()):
                val = OSC_DATA[addr]["val"]
                sbox = box.box()
                row = sbox.row(); row.label(text=addr); row.label(text=f"{val:.3f}")
                sbox.progress(factor=min(max(val / tool.ui_max, 0.0), 1.0))

# --- REGISTRATION ---
classes = (OSCChannel, OSCToolProps, OSC_OT_Toggle, OSC_OT_CleanDrivers, OSC_OT_ChannelControl, OSC_OT_ApplyDriver, OSC_PT_Panel)

def register():
    for cls in classes: bpy.utils.register_class(cls)
    bpy.types.Scene.osc_tool = bpy.props.PointerProperty(type=OSCToolProps)
    bpy.app.driver_namespace['osc'] = get_osc
    if osc_recording_handler not in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.append(osc_recording_handler)

def unregister():
    global LISTENING
    LISTENING = False
    if osc_recording_handler in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(osc_recording_handler)
    for cls in reversed(classes): bpy.utils.unregister_class(cls)
    del bpy.types.Scene.osc_tool

if __name__ == "__main__":
    register()