bl_info = {
    "name": "Sprite Baker",
    "author": "Eduardo Villar Arrechea",
    "version": (1, 0),
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar (N) > 'Pixel Art'",
    "description": "Addon to produce pixel art sprites from 3D models",
    "category": "Object",
}

# (final render resolution / resolution used to compute the outline)
# The outline must be computed at a higher resolution, overlaid on the render, and then downscaled.
# If the outline is computed at a very low resolution, it loses too much quality to look good.
# By default, the render resolution used for the outline is 5 times larger than the final resolution.
RATIO_RES_OUTLINE = 0.2

import bpy
import os
import tempfile
import shutil
from mathutils import Vector
from bpy_extras.io_utils import ImportHelper, ExportHelper


def convert_material_to_emission(mat):
    if not mat.use_nodes:
        return False

    nodes = mat.node_tree.nodes
    links = mat.node_tree.links

    output_node = None
    for n in nodes:
        if n.type == 'OUTPUT_MATERIAL' and n.is_active_output:
            output_node = n
            break
    if output_node is None:
        for n in nodes:
            if n.type == 'OUTPUT_MATERIAL':
                output_node = n
                break
    if output_node is None:
        return False

    principled = None
    for link in list(links):
        if link.to_node == output_node and link.to_socket.name == 'Surface':
            if link.from_node.type == 'BSDF_PRINCIPLED':
                principled = link.from_node
            break

    if principled is None:
        for link in list(links):
            if link.to_node == output_node and link.to_socket.name == 'Surface':
                if link.from_node.type == 'EMISSION':
                    return False

    emission = nodes.new(type='ShaderNodeEmission')
    emission.location = (output_node.location.x - 250, output_node.location.y)

    if principled:
        base_color_input = principled.inputs.get('Base Color')
        if base_color_input and base_color_input.is_linked:
            from_socket = base_color_input.links[0].from_socket
            links.new(from_socket, emission.inputs['Color'])
        elif base_color_input:
            emission.inputs['Color'].default_value = base_color_input.default_value
    else:
        emission.inputs['Color'].default_value = (0.8, 0.8, 0.8, 1.0)

    emission.inputs['Strength'].default_value = 1.0
    links.new(emission.outputs['Emission'], output_node.inputs['Surface'])

    if principled:
        nodes.remove(principled)

    return True


def convert_all_materials():
    counter = 0
    for mat in bpy.data.materials:
        if convert_material_to_emission(mat):
            counter += 1
    return counter


def disable_ao_and_black_world():
    scene = bpy.context.scene
    eevee = getattr(scene, "eevee", None)
    if eevee is not None and hasattr(eevee, "use_gtao"):
        eevee.use_gtao = False

    world = scene.world
    if world and world.use_nodes:
        for n in world.node_tree.nodes:
            if n.type == 'BACKGROUND':
                n.inputs['Color'].default_value = (0, 0, 0, 1)
                n.inputs['Strength'].default_value = 0.0


def configure_render(samples=1, filter_size=0.0):

    scene = bpy.context.scene
    render = scene.render

    render.film_transparent = True

    if hasattr(render, "filter_size"):
        render.filter_size = filter_size

    eevee = getattr(scene, "eevee", None)
    if eevee is not None:
        if hasattr(eevee, "taa_render_samples"):
            eevee.taa_render_samples = samples
        if hasattr(eevee, "taa_samples"):
            eevee.taa_samples = samples

    cycles = getattr(scene, "cycles", None)
    if cycles is not None and hasattr(cycles, "samples"):
        cycles.samples = samples



def enable_normal_pass():
    view_layer = bpy.context.view_layer
    if hasattr(view_layer, "use_pass_normal"):
        view_layer.use_pass_normal = True


def configure_compositor(posterize_steps=32, use_outline=False, outline_size=1, silhouette_outline_size=5):
    scene = bpy.context.scene

    tree = scene.compositing_node_group
    if tree is None:
        tree = bpy.data.node_groups.new("PixelArtComposite", "CompositorNodeTree")
        scene.compositing_node_group = tree

    nodes = tree.nodes
    links = tree.links

    for n in list(nodes):
        nodes.remove(n)

    # Also clear the interface sockets left over from previous runs
    for item in list(tree.interface.items_tree):
        tree.interface.remove(item)

    render_layers = nodes.new('CompositorNodeRLayers')
    render_layers.location = (-1350, 0)

    # General outline nodes
    filter_node = nodes.new('CompositorNodeFilter')
    filter_node.inputs["Type"].default_value = "Laplace"

    map_range_1 = nodes.new('ShaderNodeMapRange')
    map_range_1.data_type = 'FLOAT'
    map_range_1.interpolation_type = 'LINEAR'
    map_range_1.clamp = True
    map_range_1.inputs['From Min'].default_value = 0.0
    map_range_1.inputs['From Max'].default_value = 1.0
    map_range_1.inputs['To Min'].default_value = -1.0
    map_range_1.inputs['To Max'].default_value = 20.0

    dilate_node = nodes.new('CompositorNodeDilateErode')
    dilate_node.inputs["Type"].default_value = 'Steps'
    dilate_node.inputs["Size"].default_value = outline_size

    map_range_2 = nodes.new('ShaderNodeMapRange')
    map_range_2.data_type = 'FLOAT'
    map_range_2.interpolation_type = 'LINEAR'
    map_range_2.clamp = True
    map_range_2.inputs['From Min'].default_value = 1.0
    map_range_2.inputs['From Max'].default_value = 0.0
    map_range_2.inputs['To Min'].default_value = 0.0
    map_range_2.inputs['To Max'].default_value = 1.0

    # Silhouette outline nodes
    dilate_node_outline = nodes.new('CompositorNodeDilateErode')
    dilate_node_outline.inputs["Type"].default_value = 'Steps'
    dilate_node_outline.inputs["Size"].default_value = silhouette_outline_size

    invert_color_node_outline = nodes.new('CompositorNodeInvert')
    invert_color_node_outline.inputs["Factor"].default_value = 1.0

    set_alpha_node_outline = nodes.new('CompositorNodeSetAlpha')

    alpha_over_node = nodes.new('CompositorNodeAlphaOver')

    # Mix
    mix_node = nodes.new('ShaderNodeMix')
    mix_node.data_type = 'RGBA'
    mix_node.blend_type = 'MULTIPLY'
    mix_node.clamp_factor = True
    mix_node.clamp_result = True
    mix_node.inputs['Factor'].default_value = 1.0 if use_outline else 0.0

    # Scale
    scale_node = nodes.new('CompositorNodeScale')
    scale_node.inputs['X'].default_value = RATIO_RES_OUTLINE
    scale_node.inputs['Y'].default_value = RATIO_RES_OUTLINE
    scale_node.inputs['Interpolation'].default_value = 'Nearest'

    # Posterize
    posterize_node = nodes.new('CompositorNodePosterize')
    posterize_node.inputs['Steps'].default_value = posterize_steps

    # This is a Group Output, with a Color interface socket.
    tree.interface.new_socket(name="Image", in_out="OUTPUT", socket_type="NodeSocketColor")
    output_node = nodes.new('NodeGroupOutput')


    # LOCATIONS
    # General outline
    filter_node.location = (-900, -300)
    map_range_1.location = (-650, -300)
    dilate_node.location = (-400, -300)
    map_range_2.location = (-150, -300)
    mix_node.location = (100, -100)

    # Silhouette outline
    dilate_node_outline.location = (-900, 100)
    invert_color_node_outline.location = (-650, 100)
    set_alpha_node_outline.location = (-400, 100)

    # Output
    alpha_over_node.location = (350, 100)
    scale_node.location = (650, 100)
    posterize_node.location = (950, 100)
    output_node.location = (1250, 100)

    # LINKS
    # Silhouette outline
    links.new(render_layers.outputs['Alpha'], dilate_node_outline.inputs[0])
    links.new(dilate_node_outline.outputs[0], invert_color_node_outline.inputs['Color'])
    links.new(dilate_node_outline.outputs[0], set_alpha_node_outline.inputs['Alpha'])
    links.new(invert_color_node_outline.outputs[0], set_alpha_node_outline.inputs['Image'])
    links.new(set_alpha_node_outline.outputs[0], alpha_over_node.inputs['Background'])


    # General outline
    links.new(render_layers.outputs['Normal'], filter_node.inputs[0])
    links.new(filter_node.outputs[0], map_range_1.inputs['Value'])
    links.new(map_range_1.outputs['Result'], dilate_node.inputs[0])
    links.new(dilate_node.outputs[0], map_range_2.inputs['Value'])

    # Mix
    links.new(render_layers.outputs['Image'], mix_node.inputs['A'])
    links.new(map_range_2.outputs['Result'], mix_node.inputs['B'])
    links.new(mix_node.outputs['Result'], alpha_over_node.inputs['Foreground'])

    # Alpha Over
    links.new(alpha_over_node.outputs['Image'], scale_node.inputs['Image'])

    # Scale - Posterize
    # Link directly to the scale node if the outline is not used
    if not use_outline:
        links.new(render_layers.outputs['Image'], scale_node.inputs['Image'])

    links.new(scale_node.outputs['Image'], posterize_node.inputs['Image'])

    links.new(posterize_node.outputs['Image'], output_node.inputs['Image'])


def delete_lights():
    lights = [obj for obj in bpy.data.objects if obj.type == 'LIGHT']
    for light in lights:
        bpy.data.objects.remove(light, do_unlink=True)
    return len(lights)


def hex_to_rgb(hex_code):

    code = hex_code.strip()
    if code.startswith('#'):
        code = code[1:]

    if len(code) != 6:
        return None

    try:
        r = int(code[0:2], 16) / 255.0
        g = int(code[2:4], 16) / 255.0
        b = int(code[4:6], 16) / 255.0
    except ValueError:
        return None

    return (r, g, b)

def rgb_to_hex(r, g, b):

    r_int = max(0, min(255, round(r * 255)))
    g_int = max(0, min(255, round(g * 255)))
    b_int = max(0, min(255, round(b * 255)))
    return f"#{r_int:02x}{g_int:02x}{b_int:02x}"

def get_unique_path(path):
    """If 'path' already exists on disk, return a variant with an incremental
    numeric suffix (_1, _2, _3...) that does not exist yet."""

    if not os.path.exists(path):
        return path

    folder, file_name = os.path.split(path)
    base_name, extension = os.path.splitext(file_name)

    counter = 1
    while True:
        candidate = os.path.join(folder, f"{base_name}_{counter}{extension}")
        if not os.path.exists(candidate):
            return candidate
        counter += 1

def apply_palette_to_image(input_path, output_path, palette_rgb):
    """Loads an image, quantizes its colors to the given palette and saves it."""

    if not palette_rgb:
        return False, "The color palette is empty: add at least one color"

    try:
        img = bpy.data.images.load(input_path, check_existing=False)
    except RuntimeError as e:
        return False, f"Could not load the rendered image: {e}"

    try:
        width, height = img.size[0], img.size[1]
        channels = img.channels

        if width == 0 or height == 0:
            return False, "The rendered image is empty"

        pixels = list(img.pixels)

        for i in range(0, len(pixels), channels):
            alpha = pixels[i + 3] if channels >= 4 else 1.0

            if alpha <= 0.0:
                continue

            r, g, b = pixels[i], pixels[i + 1], pixels[i + 2]

            best_color = palette_rgb[0]
            best_distance = None

            for color in palette_rgb:
                dr = r - color[0]
                dg = g - color[1]
                db = b - color[2]
                distance = dr * dr + dg * dg + db * db

                if best_distance is None or distance < best_distance:
                    best_distance = distance
                    best_color = color

            pixels[i] = best_color[0]
            pixels[i + 1] = best_color[1]
            pixels[i + 2] = best_color[2]

        img.pixels = pixels
        img.filepath_raw = input_path
        img.file_format = 'PNG'
        img.save()

        return True, f"Palette applied and saved to: {input_path}"

    finally:
        bpy.data.images.remove(img)

def render_to_file(temp_path):
    """Renders the current scene image (active camera) to disk."""
    scene = bpy.context.scene

    original_filepath = scene.render.filepath

    try:
        scene.render.filepath = temp_path
        bpy.ops.render.render(write_still=True)

        return True, f"Render temporarily saved to: {temp_path}"
    except RuntimeError as e:
        return False, f"Render error: {e}"
    finally:
        scene.render.filepath = original_filepath

def resize_file(file_path, res_x, res_y):
    img = bpy.data.images.load(file_path)

    width, height = img.size

    # Center
    center_x = width // 2
    center_y = height // 2

    half_x = res_x // 2
    half_y = res_y // 2

    left = center_x - half_x
    right = center_x + half_x
    top = center_y - half_y
    bottom = center_y + half_y

    # Clamp
    left = max(0, left)
    right = min(width, right)
    top = max(0, top)
    bottom = min(height, bottom)

    new_width = right - left
    new_height = bottom - top

    pixels = img.pixels[:]

    def get_pixel(x, y):
        idx = (y * width + x) * 4
        return pixels[idx:idx+4]

    new_pixels = []

    for y in range(top, bottom):
        for x in range(left, right):
            new_pixels.extend(get_pixel(x, y))

    img.scale(new_width, new_height)

    img.pixels = new_pixels

    img.filepath_raw = file_path
    img.file_format = 'PNG'
    img.save()


def configure_camera(fix_camera_position=False, position=(0.0, 0.0, 5.0), fix_camera_rotation=False, rotation=(0.0, 0.0, 0.0)):
    scene = bpy.context.scene
    cam_obj = scene.camera

    if cam_obj is None:
        for obj in bpy.data.objects:
            if obj.type == 'CAMERA':
                cam_obj = obj
                break

    if cam_obj is None:
        cam_data = bpy.data.cameras.new("Camera")
        cam_obj = bpy.data.objects.new("Camera", cam_data)
        bpy.context.collection.objects.link(cam_obj)
        scene.camera = cam_obj

    if not fix_camera_position:
        cam_obj.location = Vector(position)

    if not fix_camera_rotation:
        cam_obj.rotation_euler = Vector(rotation)

    return cam_obj.name


def center_objects_at_origin():

    mesh_objects = [o for o in bpy.data.objects if o.type == 'MESH']
    if not mesh_objects:
        return False, "There are no mesh objects in the scene to center"

    total = Vector((0.0, 0.0, 0.0))
    for obj in mesh_objects:
        total += obj.matrix_world.translation

    center = total / len(mesh_objects)
    offset = -center

    # Find the root object (without a parent) of each mesh. A set is used so the same root object is not moved more than once if several meshes share a parent.
    root_objects = set()
    for obj in mesh_objects:
        root = obj
        while root.parent is not None:
            root = root.parent
        root_objects.add(root)

    for root in root_objects:
        root.location = root.location + offset

    return True, (
        f"{len(mesh_objects)} mesh(es) in {len(root_objects)} root object(s) "
        f"centered at the origin (offset by {tuple(round(v, 3) for v in offset)})"
    )



def frame_camera_to_objects(margin=1.0, objects=None, keep_aspect=False, scale_x=128, scale_y=64):
    scene = bpy.context.scene
    cam_obj = scene.camera

    if cam_obj is None or cam_obj.type != 'CAMERA':
        return False, "There is no active camera in the scene", scale_y

    if objects is None:
        objects = [o for o in bpy.data.objects if o.type == 'MESH']
    if not objects:
        return False, "There are no mesh objects in the scene", scale_y

    cam_data = cam_obj.data

    if cam_data.type != 'ORTHO':
        cam_data.type = 'ORTHO'
    cam_data.sensor_fit = 'HORIZONTAL'

    cam_matrix_inv = cam_obj.matrix_world.inverted()

    min_x = min_y = float('inf')
    max_x = max_y = float('-inf')

    for obj in objects:
        for corner in obj.bound_box:
            world_corner = obj.matrix_world @ Vector(corner)
            local_corner = cam_matrix_inv @ world_corner
            min_x = min(min_x, local_corner.x)
            max_x = max(max_x, local_corner.x)
            min_y = min(min_y, local_corner.y)
            max_y = max(max_y, local_corner.y)

    width = max_x - min_x
    height = max_y - min_y

    if width <= 0 or height <= 0:
        return False, "The object has no valid dimensions as seen from the camera", scale_y

    # The render resolution must be higher so the outline has enough quality;
    # downscaling is done later by the compositor
    object_aspect = width / height
    render_res_x = max(1, scale_x) / RATIO_RES_OUTLINE
    render_res_y = max(1, round(scale_x / object_aspect)) / RATIO_RES_OUTLINE

    scene.render.resolution_x = int(render_res_x)
    scene.render.resolution_y = int(render_res_y)

    cam_data.ortho_scale = width * margin

    message = (
        f"Object fully framed (width x height) | "
        f"Render resolution set to {render_res_x}x{render_res_y} | "
        f"ortho_scale={cam_data.ortho_scale:.3f}"
    )

    if keep_aspect:
        computed_scale_y = max(1, round(scale_x * (height / width)))
        message += f" | compositor scale_y computed automatically: {computed_scale_y}"
        return True, message, computed_scale_y

    return True, message, scale_y



def run_process(
    convert_materials,
    disable_ao,
    samples,
    filter_size,
    scale_x,
    scale_y,
    posterize_steps,
    delete_lights_flag,
    camera_pos,
    camera_rot,
    frame_flag,
    framing_margin=1.05,
    keep_aspect=False,
    center_objects_flag=True,
    use_outline=False,
    outline_size=1,
    silhouette_outline_size=5,
    fix_camera_position=False,
    fix_camera_rotation=False
):
    summary = []

    if convert_materials:
        counter = convert_all_materials()
        summary.append(f"Materials converted to Emission: {counter}")
    else:
        summary.append("Materials: unchanged (disabled)")

    if disable_ao:
        disable_ao_and_black_world()
        summary.append("AO disabled and World set to black")
    else:
        summary.append("AO/World: unchanged (disabled)")

    configure_render(samples=samples, filter_size=filter_size)
    summary.append(f"Render: transparent, samples={samples}, filter_size={filter_size}")

    if use_outline:
        enable_normal_pass()
        summary.append(f"Outline enabled (size={outline_size})")
    else:
        summary.append("Outline: disabled")

    configure_camera(fix_camera_position=fix_camera_position, position=camera_pos, fix_camera_rotation=fix_camera_rotation, rotation=camera_rot)

    if center_objects_flag:
        ok_center, msg_center = center_objects_at_origin()
        summary.append(msg_center)
    else:
        summary.append("Object centering: unchanged (disabled)")

    scale_y_final = scale_y

    if frame_flag:
        ok, msg, scale_y_result = frame_camera_to_objects(
            margin=framing_margin,
            keep_aspect=keep_aspect,
            scale_x=scale_x,
            scale_y=scale_y,
        )
        summary.append(msg)
        if ok and keep_aspect:
            scale_y_final = scale_y_result
    else:
        summary.append("Automatic framing: disabled")

    configure_compositor(
        posterize_steps=posterize_steps,
        use_outline=use_outline,
        outline_size=outline_size,
        silhouette_outline_size=silhouette_outline_size
    )
    summary.append(f"Compositor: Scale {scale_x}x{scale_y_final}, Posterize {posterize_steps}")

    if delete_lights_flag:
        n_lights = delete_lights()
        summary.append(f"Lights deleted: {n_lights}")
    else:
        summary.append("Lights: unchanged (disabled)")

    return summary


class PIXELART_PaletteColor(bpy.types.PropertyGroup):
    color: bpy.props.FloatVectorProperty(
        name="Color",
        subtype='COLOR_GAMMA',
        size=4,
        default=(1.0, 1.0, 1.0, 1.0),
        min=0.0,
        max=1.0,
    )


class PIXELART_Settings(bpy.types.PropertyGroup):
    convert_materials: bpy.props.BoolProperty(
        name="Convert materials to Emission",
        default=True,
    )
    disable_ao: bpy.props.BoolProperty(
        name="Disable AO and World",
        default=True,
    )
    samples: bpy.props.IntProperty(
        name="Samples",
        default=1,
        min=1,
        max=4096,
    )
    filter_size: bpy.props.FloatProperty(
        name="Filter Size",
        default=0.0,
        min=0.0,
        max=10.0,
    )
    scale_x: bpy.props.IntProperty(
        name="Scale X",
        default=128,
        min=1,
        max=8192,
    )
    scale_y: bpy.props.IntProperty(
        name="Scale Y",
        default=64,
        min=1,
        max=8192,
    )
    posterize_steps: bpy.props.IntProperty(
        name="Posterize Steps",
        default=32,
        min=2,
        max=1024,
    )
    delete_lights: bpy.props.BoolProperty(
        name="Delete lights",
        default=True,
    )
    center_objects: bpy.props.BoolProperty(
        name="Center objects at the origin",
        description="Moves all mesh objects so that the midpoint of their origins ends up at (0,0,0). It is automatically disabled after running, to avoid centering several times in a row",
        default=True,
    )
    fix_camera_position: bpy.props.BoolProperty(
        name="Lock camera position",
        description="The camera will keep its current position",
        default=False,
    )
    camera_position: bpy.props.FloatVectorProperty(
        name="Camera position",
        size=3,
        default=(0.0, 0.0, 5.0),
        subtype='XYZ',
    )
    fix_camera_rotation: bpy.props.BoolProperty(
        name="Lock camera rotation",
        description="The camera will keep its current rotation",
        default=False,
    )
    camera_rotation: bpy.props.FloatVectorProperty(
        name="Camera rotation",
        size=3,
        default=(0.0, 0.0, 0.0),
        subtype='EULER',
        unit='ROTATION',
    )
    frame_camera: bpy.props.BoolProperty(
        name="Frame camera to object",
        default=True,
    )
    framing_margin: bpy.props.FloatProperty(
        name="Framing margin",
        description="Padding multiplier around the object (1.0 = exact fit, no margin)",
        default=1.05,
        min=1.0,
        max=3.0,
    )
    keep_aspect: bpy.props.BoolProperty(
        name="Keep aspect ratio",
        description="If enabled, the compositor's Scale Y is computed automatically from Scale X and the real aspect ratio of the object framed by the camera",
        default=True,
    )
    use_outline: bpy.props.BoolProperty(
        name="Use outline",
        description="Generates an outline around the objects from the Normal pass, using edge detection in the compositor",
        default=True,
    )
    outline_size: bpy.props.IntProperty(
        name="General outline size",
        description="Thickness of the outline of all elements in the render",
        default=1
    )
    silhouette_outline_size: bpy.props.IntProperty(
        name="Silhouette outline size",
        description="Thickness of the outline around the overall shape of the render",
        default=5
    )
    palette_colors: bpy.props.CollectionProperty(type=PIXELART_PaletteColor)
    use_palette: bpy.props.BoolProperty(
        name="Use palette",
        default=False,
    )
    output_path: bpy.props.StringProperty(
        name="Output path",
        description="Path where the final image will be saved, already rendered and with the color palette applied",
        subtype='FILE_PATH',
        default="//baked_sprite.png",
        options={'PATH_SUPPORTS_BLEND_RELATIVE'},
    )
    show_general_options: bpy.props.BoolProperty(
        name="General Options",
        default=False
    )
    show_render_options: bpy.props.BoolProperty(
        name="Render Settings",
        default=False
    )
    show_camera_options: bpy.props.BoolProperty(
        name="Camera Options",
        default=False
    )
    show_palette: bpy.props.BoolProperty(
        name="Palette Options",
        default=False
    )


class PIXELART_OT_palette_add(bpy.types.Operator):
    bl_idname = "pixelart.palette_add"
    bl_label = "Add color"
    bl_description = "Adds a new color to the palette"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = context.scene.pixelart_settings
        item = s.palette_colors.add()
        item.color = (1.0, 1.0, 1.0, 1.0)
        return {'FINISHED'}


class PIXELART_OT_palette_remove_index(bpy.types.Operator):
    bl_idname = "pixelart.palette_remove_index"
    bl_label = "Remove color"
    bl_description = "Removes this color from the palette"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty(default=0)

    def execute(self, context):
        s = context.scene.pixelart_settings
        if 0 <= self.index < len(s.palette_colors):
            s.palette_colors.remove(self.index)
        return {'FINISHED'}


class PIXELART_OT_palette_import(bpy.types.Operator, ImportHelper):
    bl_idname = "pixelart.palette_import"
    bl_label = "Import palette"
    bl_description = (
        "Imports a palette from a .txt file: one hexadecimal color "
        "per line (with or without '#'), ending at a blank line "
        "or at the end of the file. Replaces the current palette"
    )
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".txt"
    filter_glob: bpy.props.StringProperty(default="*.txt", options={'HIDDEN'})

    def execute(self, context):
        s = context.scene.pixelart_settings

        try:
            with open(self.filepath, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except OSError as e:
            self.report({'ERROR'}, f"Could not read the file: {e}")
            return {'CANCELLED'}

        colors_read = []
        for line in lines:
            clean_line = line.strip()

            # A blank line marks the end of the palette, even if
            # more lines follow in the file.
            if clean_line == "":
                break

            rgb = hex_to_rgb(clean_line)
            if rgb is None:
                self.report(
                    {'WARNING'},
                    f"Line ignored (not a valid hexadecimal color): '{clean_line}'"
                )
                continue

            colors_read.append(rgb)

        if not colors_read:
            self.report({'ERROR'}, "No valid color was found in the file")
            return {'CANCELLED'}

        s.palette_colors.clear()
        for r, g, b in colors_read:
            item = s.palette_colors.add()
            item.color = (r, g, b, 1.0)

        self.report({'INFO'}, f"Palette imported: {len(colors_read)} color(s)")
        return {'FINISHED'}


class PIXELART_OT_palette_export(bpy.types.Operator, ExportHelper):
    bl_idname = "pixelart.palette_export"
    bl_label = "Export palette"
    bl_description = "Exports the current palette to a .txt file, one hexadecimal color per line"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".txt"
    filter_glob: bpy.props.StringProperty(default="*.txt", options={'HIDDEN'})

    def execute(self, context):
        s = context.scene.pixelart_settings

        if len(s.palette_colors) == 0:
            self.report({'ERROR'}, "The color palette is empty: there is nothing to export")
            return {'CANCELLED'}

        lines = [rgb_to_hex(*item.color[:3]) for item in s.palette_colors]

        try:
            with open(self.filepath, 'w', encoding='utf-8') as f:
                f.write("\n".join(lines) + "\n")
        except OSError as e:
            self.report({'ERROR'}, f"Could not write the file: {e}")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Palette exported ({len(lines)} color(s)) to: {self.filepath}")
        return {'FINISHED'}


class PIXELART_OT_run(bpy.types.Operator):
    bl_idname = "pixelart.run"
    bl_label = "Run"
    bl_description = "Runs the full process with the values configured in the panel"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = context.scene.pixelart_settings
        summary = run_process(
            convert_materials=s.convert_materials,
            disable_ao=s.disable_ao,
            samples=s.samples,
            filter_size=s.filter_size,
            scale_x=s.scale_x,
            scale_y=s.scale_y,
            posterize_steps=s.posterize_steps,
            delete_lights_flag=s.delete_lights,
            camera_pos=tuple(s.camera_position),
            camera_rot=tuple(s.camera_rotation),
            frame_flag=s.frame_camera,
            framing_margin=s.framing_margin,
            keep_aspect=s.frame_camera and s.keep_aspect,
            center_objects_flag=s.center_objects,
            use_outline=s.use_outline,
            outline_size=s.outline_size,
            silhouette_outline_size=s.silhouette_outline_size,
            fix_camera_position=s.fix_camera_position,
            fix_camera_rotation=s.fix_camera_rotation
        )

        if s.center_objects:
            s.center_objects = False

        self.report({'INFO'}, " | ".join(summary))
        return {'FINISHED'}

class PIXELART_OT_render_and_save(bpy.types.Operator):
    bl_idname = "pixelart.render_and_save"
    bl_label = "Render and save to path"
    bl_description = "Renders the scene with the current settings"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = context.scene.pixelart_settings
        scene = bpy.context.scene

        absolute_output_path = bpy.path.abspath(s.output_path)
        if not absolute_output_path:
            self.report({'ERROR'}, "Enter a valid output path")
            return {'CANCELLED'}

        if not os.path.isabs(absolute_output_path):
            if s.output_path.startswith("//"):
                self.report(
                    {'ERROR'},
                    "The path uses '//' but the .blend file has not been saved yet. "
                    "Save the project first or enter an absolute path."
                )
            else:
                self.report(
                    {'ERROR'},
                    f"The output path must be absolute or start with '//' "
                    f"(relative to the .blend file): '{s.output_path}'"
                )
            return {'CANCELLED'}

        output_folder = os.path.dirname(absolute_output_path)
        if output_folder and not os.path.isdir(output_folder):
            self.report(
                {'ERROR'},
                f"The output path folder does not exist: {output_folder}. "
                f"If you use '//', save the .blend file first so that a "
                f"relative path can be resolved, or enter an absolute path."
            )
            return {'CANCELLED'}


        if len(s.palette_colors) == 0 and s.use_palette:
            self.report({'ERROR'}, "The color palette is empty: add at least one color")
            return {'CANCELLED'}

        # Use a temporary folder to save the render and modify it before placing it at the desired path
        temp_folder = tempfile.gettempdir()
        temp_path = os.path.join(temp_folder, "pixelart_temp_render.png")

        ok_render, msg_render = render_to_file(temp_path)
        if not ok_render:
            self.report({'ERROR'}, msg_render)
            return {'CANCELLED'}

        # Crop the file dimensions caused by rendering at a larger size for the outline
        resize_file(temp_path, int(scene.render.resolution_x * RATIO_RES_OUTLINE), int(scene.render.resolution_y * RATIO_RES_OUTLINE))

        final_output_path = get_unique_path(absolute_output_path)

        # Apply the palette if desired
        if s.use_palette:
            palette_rgb = [tuple(item.color[:3]) for item in s.palette_colors]

            ok_palette, msg_palette = apply_palette_to_image(
                input_path=temp_path,
                output_path=final_output_path,
                palette_rgb=palette_rgb,
            )

            if not ok_palette:
                self.report({'ERROR'}, msg_palette)
                return {'CANCELLED'}

        # Move the file to the destination path
        shutil.move(temp_path, final_output_path)

        # Cleanup: the temporary file is no longer needed
        if os.path.exists(temp_path):
            os.remove(temp_path)

        if s.use_palette:
            self.report({'INFO'}, f"{msg_render} | {msg_palette}")
        else:
            self.report({'INFO'}, f"{msg_render}")

        return {'FINISHED'}


class PIXELART_PT_panel(bpy.types.Panel):
    bl_label = "Pixel Art Setup"
    bl_idname = "PIXELART_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Pixel Art"

    def draw(self, context):
        layout = self.layout
        s = context.scene.pixelart_settings

        box = layout.box()
        row = box.row()
        row.prop(s, "show_general_options", icon="TRIA_DOWN" if s.show_general_options else "TRIA_RIGHT", emboss=False)

        if s.show_general_options:
            col = box.column(align=True)
            col.prop(s, "convert_materials")
            col.prop(s, "disable_ao")
            col.prop(s, "delete_lights")
            col.prop(s, "center_objects")

        box = layout.box()
        row = box.row()
        row.prop(s, "show_render_options", icon="TRIA_DOWN" if s.show_render_options else "TRIA_RIGHT", emboss=False)

        if s.show_render_options:
            col = box.column(align=True)

            col.label(text="Render parameter settings")
            col.prop(s, "samples")
            col.prop(s, "filter_size")
            col.separator()


            col.label(text="Scale")
            col.prop(s, "scale_x")
            sub = col.row()
            sub.enabled = not (s.frame_camera and s.keep_aspect)
            sub.prop(s, "scale_y")
            col.separator()

            col.label(text="Posterize")
            col.prop(s, "posterize_steps")
            col.separator()

            col.label(text="Framing")
            col.prop(s, "frame_camera")
            sub = col.row()
            sub.enabled = s.frame_camera
            sub.prop(s, "framing_margin")
            sub = col.row()
            sub.enabled = s.frame_camera
            sub.prop(s, "keep_aspect")
            col.separator()

            col.label(text="Outline")
            col.prop(s, "use_outline")
            sub = col.column()
            sub.enabled = s.use_outline
            sub.prop(s, "outline_size")
            sub.prop(s, "silhouette_outline_size")

        box = layout.box()
        row = box.row()
        row.prop(s, "show_camera_options", icon="TRIA_DOWN" if s.show_camera_options else "TRIA_RIGHT", emboss=False)

        if s.show_camera_options:
            col = box.column(align=True)

            col.prop(s, "fix_camera_position")
            sub = col.column()
            sub.enabled = not s.fix_camera_position
            sub.prop(s, "camera_position")
            col.separator()

            col.prop(s, "fix_camera_rotation")
            sub = col.column()
            sub.enabled = not s.fix_camera_rotation
            sub.prop(s, "camera_rotation")

        box = layout.box()
        row = box.row()
        row.prop(s, "show_palette", icon="TRIA_DOWN" if s.show_palette else "TRIA_RIGHT", emboss=False)

        if s.show_palette:
            col = box.column(align=True)
            col.prop(s, "use_palette")
            col.separator()

            if len(s.palette_colors) == 0:
                col.label(text="(the palette is empty)")
            else:
                grid = col.grid_flow(row_major=True, columns=5, even_columns=True, even_rows=True)
                for idx, item in enumerate(s.palette_colors):
                    cell = grid.column(align=True)
                    cell.prop(item, "color", text="")
                    op = cell.operator("pixelart.palette_remove_index", text="", icon='X')
                    op.index = idx
            col.separator()

            col.operator("pixelart.palette_add", text="Add color", icon='ADD')
            col.separator()

            row_io = col.row(align=True)
            row_io.operator("pixelart.palette_import", text="Import palette", icon='IMPORT')
            row_io.operator("pixelart.palette_export", text="Export palette", icon='EXPORT')


        layout.separator()
        row = layout.row(align=True)
        col = layout.column(align=True)
        col.prop(s, "output_path")
        col.operator("pixelart.render_and_save", text="Render and save to path", icon='RENDER_STILL')
        col.operator("pixelart.run", text="Apply changes", icon='PLAY')


classes = (
    PIXELART_PaletteColor,
    PIXELART_Settings,
    PIXELART_OT_palette_add,
    PIXELART_OT_palette_remove_index,
    PIXELART_OT_palette_import,
    PIXELART_OT_palette_export,
    PIXELART_OT_run,
    PIXELART_OT_render_and_save,
    PIXELART_PT_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.pixelart_settings = bpy.props.PointerProperty(type=PIXELART_Settings)


def unregister():
    del bpy.types.Scene.pixelart_settings
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
