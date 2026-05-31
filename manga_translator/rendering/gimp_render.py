import tempfile
import subprocess
import math
import cv2
import platform
import glob
import os

from ..utils import Context

# convert alignment/direction to gimp's values
alignment_to_justification = {
    "left": "TEXT-JUSTIFY-LEFT",
    "right": "TEXT-JUSTIFY-RIGHT",
    "center": "TEXT-JUSTIFY-CENTER",
}
direction_to_base_direction = {
    "h": "TEXT-DIRECTION-LTR",
    "v": "TEXT-DIRECTION-TTB-LTR-UPRIGHT",
    "hr": "TEXT-DIRECTION-RTL",
    "vr": "TEXT-DIRECTION-TTB-RTL-UPRIGHT",
}

# GIMP 3.0 changes vs 2.10 (Script-Fu / PDB "v3"), reflected below:
#   - Fonts are resource OBJECTS now, not strings. A font NAME (family name, NOT
#     a .ttf filename) must be resolved with (gimp-font-get-by-name "...") before
#     being passed to text procedures. That call returns NULL for an unknown
#     name, which then makes gimp-text-layer-new fail; resolve-font (defined in
#     the script below) falls back to the current context font in that case.
#   - The unit argument of gimp-text-layer-new takes the UNIT-PIXEL constant.
#   - gimp-image-add-layer was removed; use gimp-image-insert-layer
#     (image layer parent position). parent 0 = main stack, position 0 = top.
#   - File procedures take a single filename (GFile), no second "raw" filename.
#   - gimp-xcf-save / gimp-file-save take a VECTOR of drawables, not one drawable.
#   - gimp-image-get-layers returns a single array; in the default (v2) dialect
#     that array is wrapped in a list, so (vector-ref (car ...) 0) gets a layer.
# This script uses the default (v2) Script-Fu dialect, hence the (car ...) wraps
# around single-value PDB returns.

text_init_template = '( text{n} ( car ( gimp-text-layer-new image "{text}" ( resolve-font "{default_font}" ) {text_size} UNIT-PIXEL ) ) )'
font_template = '( gimp-text-layer-set-font text{n} ( resolve-font "{font}" ) )'
angle_template = "( gimp-item-transform-rotate text{n} {angle} TRUE 0 0 )"

text_template = """
    ( gimp-image-insert-layer image text{n} 0 0 )
    ( gimp-text-layer-set-color text{n} (list {color}) )
    ( gimp-item-set-name text{n} "{name}" )
    ( gimp-layer-set-offsets text{n} {position} )
    ( gimp-text-layer-resize text{n} {size} )
    ( gimp-text-layer-set-language text{n} "{language}" )
    ( gimp-text-layer-set-letter-spacing text{n} {letter_spacing} )
    ( gimp-text-layer-set-line-spacing text{n} {line_spacing} )
    ( gimp-text-layer-set-base-direction text{n} {base_direction} )
    ( gimp-text-layer-set-justification text{n} {justify} )
    {font}
    {angle}
"""

save_templates = {
    "xcf": '( gimp-xcf-save RUN-NONINTERACTIVE image (vector background_layer) "{out_file}" )',
    # gimp-file-save picks the exporter from the extension; this is the robust
    # replacement for the renamed/reworked file-psd-save / file-pdf-save plugins.
    "psd": '( gimp-file-save RUN-NONINTERACTIVE image (vector background_layer) "{out_file}" )',
    "pdf": '( gimp-file-save RUN-NONINTERACTIVE image (vector background_layer) "{out_file}" )',
}

create_mask = '( inpainting ( car ( gimp-file-load-layer RUN-NONINTERACTIVE image "{mask_file}" ) ) )'
rename_mask = '( gimp-image-insert-layer image inpainting 0 0 ) ( gimp-item-set-name inpainting "mask" )'

# resolve-font turns a font NAME into a font resource, falling back to the
# current context font when the name does not resolve (gimp-font-get-by-name
# returns NULL). The check tolerates whichever way this build represents a
# missing resource (#f, 0 or a negative id); a valid font is always a positive
# id. Without this a bad font name aborts the whole script.
script_template = """
( begin
( define ( resolve-font name )
    ( let ( ( f ( car ( gimp-font-get-by-name name ) ) ) )
        ( if ( and f ( or ( not ( number? f ) ) ( > f 0 ) ) )
            f
            ( car ( gimp-context-get-font ) ) ) ) )
( let* (
            ( image ( car ( gimp-file-load RUN-NONINTERACTIVE "{input_file}" ) ) )
            ( layer-list (gimp-image-get-layers image))
            ( background_layer (vector-ref (car layer-list) 0))
            {create_mask}
            {text_init}
        )
    {rename_mask}
    ( gimp-item-set-name background_layer "original image" )
    ( gimp-item-set-lock-content background_layer TRUE )
    ( gimp-item-set-lock-position background_layer TRUE )
    {text}
    {save}
    ( gimp-quit 0 )
)
)"""


def gimp_render(out_file, ctx: Context):
    input_file = os.path.join(tempfile.gettempdir(), ".gimp_input.png")
    mask_file = os.path.join(tempfile.gettempdir(), ".gimp_mask.png")

    extension = out_file.split(".")[-1]

    ctx.upscaled.save(input_file)

    # If there is no text on the page, gimp_mask will be None and there is no
    # need to add it as a layer.
    if ctx.gimp_mask is not None:
        cv2.imwrite(mask_file, ctx.gimp_mask)
    else:
        ctx.text_regions = []

    filtered_text_regions = [
        text_region for text_region in ctx.text_regions if text_region.translation != ""
    ]

    text_init = "\n".join(
        [
            text_init_template.format(
                n=n,
                text=text_region.translation.replace('"', '\\"'),
                text_size=text_region.font_size,
                default_font=ctx.gimp_font
                + (" Bold" if text_region.bold else "")
                + (" Italic" if text_region.italic else ""),
            )
            for n, text_region in enumerate(filtered_text_regions)
        ]
    )

    text = "".join(
        [
            text_template.format(
                n=n,
                color=" ".join([str(value) for value in text_region.fg_colors]),
                name=" ".join(text_region.text).replace('"', '\\"'),
                position=str(text_region.xywh[0]) + " " + str(text_region.xywh[1]),
                size=str(text_region.xywh[2]) + " " + str(text_region.xywh[3]),
                justify=alignment_to_justification[text_region.alignment],
                font=font_template.format(n=n, font=text_region.font_family)
                if text_region.font_family != ""
                else "",
                # rotated text is weird in gimp so we don't do it unless it's over 10 degrees
                angle=angle_template.format(n=n, angle=math.radians(text_region.angle))
                if abs(text_region.angle) > 10
                else "",
                language=text_region.target_lang,
                line_spacing=text_region.line_spacing,
                letter_spacing=text_region.letter_spacing,
                base_direction=direction_to_base_direction[text_region.direction],
            )
            for n, text_region in enumerate(filtered_text_regions)
        ]
    )

    # scheme script to be ran by gimp
    full_script = script_template.format(
        input_file=input_file.replace("\\", "\\\\"),
        text_init=text_init,
        text=text,
        extension=extension,
        save=save_templates[extension].format(out_file=out_file.replace("\\", "\\\\")),
        create_mask=(
            create_mask.format(mask_file=mask_file.replace("\\", "\\\\"))
            if ctx.gimp_mask is not None
            else ""
        ),
        rename_mask=(rename_mask if ctx.gimp_mask is not None else ""),
    )

    gimp_batch(full_script)

    # Delete Files
    os.unlink(input_file)

    # Deleting file only if it exists
    if os.path.exists(mask_file):
        os.unlink(mask_file)


def gimp_console_executable():
    executable = "gimp"
    if platform.system() == "Windows":
        gimp_dir = os.getenv("LOCALAPPDATA") + "\\Programs\\GIMP 3\\bin\\"
        executables = glob.glob(gimp_dir + "gimp-console-3.*.exe")
        if len(executables) > 0:
            return executables[0]
        # may be in program files
        gimp_dir = os.getenv("ProgramFiles") + "\\GIMP 3\\bin\\"
        executables = glob.glob(gimp_dir + "gimp-console-3.*.exe")
        if len(executables) == 0:
            print("error: gimp not found in directory:", gimp_dir)
            return
        executable = executables[0]
    return executable


def gimp_batch(script, timeout=300):
    """
    Run a gimp script in batch mode. Quit gimp after running the script and on
    errors. Raise an exception if there is a GIMP error.

    A timeout is enforced because on GIMP 3.0 a Script-Fu error that happens
    before the script reaches (gimp-quit 0) does NOT make GIMP exit: it keeps
    running idle as a background process, so this call would otherwise block
    forever. stdin is also detached so GIMP can never block waiting on it.
    """
    try:
        result = subprocess.run(
            [gimp_console_executable(), "-i", "-b", script, "-b", "(gimp-quit 0)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        # subprocess.run kills the process on timeout; surface a clear error.
        print("=== Output")
        print(e.stdout or "")
        print("=== Error")
        print(e.stderr or "")
        raise Exception(
            "GIMP did not exit within %ds. This usually means a Script-Fu "
            "command errored before (gimp-quit 0) was reached - check the GIMP "
            "output above (a frequent cause is a font name GIMP cannot resolve)."
            % timeout
        )

    print("=== Output")
    print(result.stdout)

    print("=== Error")
    print(result.stderr)

    if "Error:" in result.stderr:
        raise Exception("GIMP Execution error")

    # return result
