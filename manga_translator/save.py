import os
import platform
import shutil
import functools
from PIL import Image
from abc import abstractmethod
from .rendering.gimp_render import gimp_render

from .utils import Context


class FormatNotSupportedException(Exception):
    def __init__(self, fmt: str):
        super().__init__(f'Format {fmt} is not supported.')

OUTPUT_FORMATS = {}
def register_format(format_cls):
    for fmt in format_cls.SUPPORTED_FORMATS:
        if fmt in OUTPUT_FORMATS:
            raise Exception(f'Tried to register multiple ExportFormats for "{fmt}"')
        OUTPUT_FORMATS[fmt] = format_cls()
    return format_cls

class ExportFormat():
    SUPPORTED_FORMATS = []

    # Subclasses will be auto registered
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        register_format(cls)

    def save(self, result: Image.Image, dest: str, ctx: Context):
        self._save(result, dest, ctx)

    @abstractmethod
    def _save(self, result: Image.Image, dest: str, ctx: Context):
        pass

def save_result(result: Image.Image, dest: str, ctx: Context):
    _, ext = os.path.splitext(dest)
    ext = ext[1:]
    if ext not in OUTPUT_FORMATS:
        raise FormatNotSupportedException(ext)

    format_handler: ExportFormat = OUTPUT_FORMATS[ext]
    format_handler.save(result, dest, ctx)


# -- Helper Functions

def _find_font_file(basename):
    """Search fontconfig's known font files for one whose basename matches."""
    if shutil.which("fc-list") is None:
        return None
    try:
        result = subprocess.run(
            ["fc-list", "--format", "%{file}\n"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line and os.path.basename(line) == basename:
            return line
    return None


@functools.lru_cache(maxsize=None)
def _resolve_font_name(font):
    """
    GIMP looks fonts up by family name (via fontconfig), not by filename. If
    `font` looks like a font file (.ttf/.otf/...) and we're on Linux with
    fontconfig available, return the family name fontconfig reports for that
    file - this is exactly what GIMP will match. Otherwise (already a family
    name, non-Linux, no fontconfig, or lookup fails) return `font` unchanged
    and let the in-script resolve-font fallback handle it.
    """
    if not font:
        return font
    if platform.system() != "Linux":
        return font
    if not font.lower().endswith((".ttf", ".otf", ".ttc", ".otc", ".pfb")):
        return font
    if shutil.which("fc-scan") is None:
        return font

    # The input may be a full path or a bare filename. fc-scan needs a path.
    path = font if os.path.isfile(font) else _find_font_file(os.path.basename(font))
    if not path:
        return font

    try:
        result = subprocess.run(
            ["fc-scan", "--format", "%{family}\n", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return font

    # fc-scan may print several families (faces / localized names); the first
    # entry on the first line is the primary family GIMP indexes it under.
    for line in result.stdout.splitlines():
        line = line.strip()
        if line:
            return line.split(",")[0].strip() or font
    return font


# -- Format Implementations

class ImageFormat(ExportFormat):
    SUPPORTED_FORMATS = ['png', 'webp']

    def _save(self, result: Image.Image, dest: str, ctx: Context):
        result.save(dest)

class JPGFormat(ExportFormat):
    SUPPORTED_FORMATS = ['jpg', 'jpeg']

    def _save(self, result: Image.Image, dest: str, ctx: Context):
        result = result.convert('RGB')
        # Certain versions of PIL only support JPEG but not JPG
        result.save(dest, quality=ctx.save_quality, format='JPEG')

class GIMPFormat(ExportFormat):
    SUPPORTED_FORMATS = ['xcf', 'psd', 'pdf']

    def _save(self, result: Image.Image, dest: str, ctx: Context):
        gimpCtx = ctx
        gimpCtx.gimp_font = _resolve_font_name(ctx.gimp_font)

        gimp_render(dest, gimpCtx)

# class KraFormat(ExportFormat):
#     SUPPORTED_FORMATS = ['kra']

#     def _save(self, result: Image.Image, dest: str, ctx: Context):
#         ...

# class SvgFormat(TranslationExportFormat):
#     SUPPORTED_FORMATS = ['svg']

