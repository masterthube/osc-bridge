bl_info = {
    "name": "OSC Bridge",
    "author": "Gemini & Freekx",
    "version": (1, 0, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > OSC Bridge",
    "description": "Real-time OSC live-linking and keyframe baking from TouchDesigner/OSC sources.",
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

# --- OSC PARSING ---
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

# --- BAKE HANDLER ---
def osc_recording_handler(scene):
    props = scene.osc_tool
    if props.ui_mode != 'RECORD' or not props.is_recording or not bpy.context.screen.is_animation_playing:
        return
    for ch in props.record_channels:
        if not ch.enabled or ch.osc_address not in OSC_DATA: continue
        val = OSC_DATA[ch.osc_address]["val"]
        
        # Simple noise thinning
        if abs(val - ch.last_val) < ch.thinning: continue
        
        try:
            target = ch.target_obj if ch.target_type == 'OBJECT' else ch.target_mat
            if not target: continue
            if ch.target_type == 'OBJECT':
                target.path_resolve(ch.path)[ch.index] = val
            else: 
                target.node_tree.path_resolve(ch.path).default_value = val
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
        count = 0
        for obj in bpy.data.objects:
            if obj.animation_data:
                for d in reversed(obj.animation_data.drivers):
                    if "osc(" in d.driver.expression:
                        obj.driver_remove(d.data_path, d.array_index); count += 1
        for mat in bpy.data.materials:
            if mat.node_tree and mat.node_tree.animation_data:
                for d in reversed(mat.node_tree.animation_data.drivers):
                    if "osc(" in d.driver.expression:
                        mat.node_tree.driver_remove(d.data_path, d.array_index); count += 1
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
        if ch.target_obj:
            try:
                d = ch.target_obj.driver_add(ch.path, ch.index).driver
                d.expression = f'osc("{ch.osc_address}")'
            except: self.report({'ERROR'}, "Invalid Path")
        return {'FINISHED'}

# --- UI & DATA ---
class OSCChannel(bpy.types.PropertyGroup):
    enabled: bpy.props.BoolProperty(default=True)
    osc_address: bpy.props.StringProperty(name="Addr", default="/val")
    target_type: bpy.props.EnumProperty(items=[('OBJECT', "Object", ""), ('MATERIAL', "Shader", "")])
    target_obj: bpy.props.PointerProperty(type=bpy.types.Object)
    target_mat: bpy.props.PointerProperty(type=bpy.types.Material)
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
    bl_label = "OSC Bridge Pro"
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

        if tool.ui_mode == 'LIVE':
            box = layout.box()
            box.operator("osc.clean_drivers", text="Kill OSC Drivers", icon='X')
            for i, ch in enumerate(tool.live_channels):
                cbox = box.box()
                row = cbox.row()
                row.prop(ch, "osc_address", text="")
                row.prop(ch, "target_obj", text="")
                op = row.operator("osc.channel_ctrl", text="", icon='PANEL_CLOSE', emboss=False)
                op.target_mode, op.action, op.index = 'LIVE', 'REMOVE', i
                col = cbox.column(align=True)
                r1 = col.row(); r1.prop(ch, "path"); r1.prop(ch, "index")
                col.operator("osc.apply_driver", text="Apply Driver Link").idx = i
            op = layout.operator("osc.channel_ctrl", text="Add Patch", icon='ADD')
            op.target_mode, op.action = 'LIVE', 'ADD'
        else:
            box = layout.box()
            row = box.row()
            row.prop(tool, "is_recording", text="RECORDING ON" if tool.is_recording else "ARM RECORDING", 
                     toggle=True, icon='REC' if tool.is_recording else 'RADIOBUT_OFF')
            for i, ch in enumerate(tool.record_channels):
                cbox = box.box()
                row = cbox.row(); row.prop(ch, "target_type", expand=True)
                op = row.operator("osc.channel_ctrl", text="", icon='PANEL_CLOSE', emboss=False)
                op.target_mode, op.action, op.index = 'RECORD', 'REMOVE', i
                col = cbox.column(align=True)
                col.prop(ch, "osc_address")
                if ch.target_type == 'OBJECT': col.prop(ch, "target_obj")
                else: col.prop(ch, "target_mat")
                r2 = col.row(); r2.prop(ch, "path"); r2.prop(ch, "index")
                col.prop(ch, "thinning")
            op = layout.operator("osc.channel_ctrl", text="Add Bake Channel", icon='ADD')
            op.target_mode, op.action = 'RECORD', 'ADD'

        if OSC_DATA:
            layout.separator()
            box = layout.box()
            box.label(text="Traffic Monitor", icon='NODE_SEL')
            box.prop(tool, "ui_max")
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