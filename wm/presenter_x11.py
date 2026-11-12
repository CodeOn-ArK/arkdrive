"""Host-only XRender compositor using python-xlib and system X11/Xrender.

PNG decode/upload is native-size; XRender picture transforms are installed
only when placement changes. No recursive encoding or per-frame resampling.
"""
import ctypes as C
import base64
import re
import struct
import zlib


def host_display(name):
    # Require explicit local display, including screen suffix validation.
    match = re.fullmatch(r':([0-9]+)(?:\.([0-9]+))?', name or '')
    if not match or int(match[1]) == 0:
        raise ValueError('explicit_nonzero_host_display_required')
    return name


def decode_png(data):
    """Decode WM-A's bounded RGB8/filter-0 PNG dialect, no image dependency."""
    if data[:8] != b'\x89PNG\r\n\x1a\n' or len(data) > 64*1024*1024:
        raise ValueError('invalid_feed_png')
    offset, chunks, size, ended = 8, [], None, False
    while offset+12 <= len(data):
        length, = struct.unpack_from('!I', data, offset)
        kind = data[offset+4:offset+8]
        body = data[offset+8:offset+8+length]
        if offset+12+length > len(data) or zlib.crc32(kind+body) != struct.unpack_from('!I', data, offset+8+length)[0]:
            raise ValueError('invalid_feed_png')
        if kind == b'IHDR':
            w, h, depth, color, compression, filtering, interlace = struct.unpack('!2I5B', body)
            if (depth, color, compression, filtering, interlace) != (8, 2, 0, 0, 0) or not 0 < w*h <= 16*1024*1024:
                raise ValueError('unsupported_feed_png')
            size = w, h
        elif kind == b'IDAT':
            chunks.append(body)
        elif kind == b'IEND':
            ended = True
            break
        offset += length+12
    if size is None or not ended:
        raise ValueError('invalid_feed_png')
    w, h = size
    limit = (w*3+1)*h
    dec = zlib.decompressobj()
    raw = dec.decompress(b''.join(chunks), limit+1)
    if len(raw) != limit or not dec.eof:
        raise ValueError('invalid_feed_png')
    pixels = bytearray(w*h*4)
    for y in range(h):
        start = y*(w*3+1)
        if raw[start] != 0:
            raise ValueError('unsupported_feed_png_filter')
        row = raw[start+1:start+1+w*3]
        dst = y*w*4
        pixels[dst:dst+w*4:4] = row[2::3]
        pixels[dst+1:dst+w*4:4] = row[1::3]
        pixels[dst+2:dst+w*4:4] = row[0::3]
    return w, h, bytes(pixels)


_keymap = None


def key_name(keysym):
    global _keymap
    if _keymap is None:
        _keymap = C.CDLL('libxkbcommon.so.0')
        _keymap.xkb_keysym_get_name.argtypes = [C.c_uint32, C.c_char_p, C.c_size_t]
        _keymap.xkb_keysym_get_name.restype = C.c_int
    name = C.create_string_buffer(128)
    count = _keymap.xkb_keysym_get_name(keysym, name, len(name))
    return name.value.decode('ascii') if 0 < count < len(name) else None


class Renderer:
    def __init__(self, connection, window, display_name):
        from Xlib import X
        self.X, self.d, self.window = X, connection, window
        self.screen = connection.screen()
        if self.screen.root_depth != 24 or connection.display.info.image_byte_order != 0:
            raise ValueError('presenter_requires_rgb24_lsb')
        visual = next(v for depth in self.screen.allowed_depths for v in depth.visuals
                      if v.visual_id == self.screen.root_visual)
        if (visual.red_mask, visual.green_mask, visual.blue_mask) != (0xff0000, 0xff00, 0xff):
            raise ValueError('presenter_requires_rgb24_lsb')
        self.x11, self.render = C.CDLL('libX11.so.6'), C.CDLL('libXrender.so.1')
        p, ul, i = C.c_void_p, C.c_ulong, C.c_int
        def fn(lib, name, args, result):
            f = getattr(lib, name)
            f.argtypes, f.restype = args, result
            return f
        self.close_display = fn(self.x11, 'XCloseDisplay', [p], i)
        open_display = fn(self.x11, 'XOpenDisplay', [C.c_char_p], p)
        self.flush = fn(self.x11, 'XSync', [p, i], i)
        default_screen = fn(self.x11, 'XDefaultScreen', [p], i)
        default_visual = fn(self.x11, 'XDefaultVisual', [p, i], p)
        find_format = fn(self.render, 'XRenderFindVisualFormat', [p, p], p)
        standard_format = fn(self.render, 'XRenderFindStandardFormat', [p, i], p)
        query = fn(self.render, 'XRenderQueryExtension', [p, C.POINTER(i), C.POINTER(i)], i)
        self.create = fn(self.render, 'XRenderCreatePicture', [p, ul, p, ul, p], ul)
        self.free = fn(self.render, 'XRenderFreePicture', [p, ul], None)
        self.set_transform = fn(self.render, 'XRenderSetPictureTransform', [p, ul, p], None)
        self.composite = fn(self.render, 'XRenderComposite', [p, i, ul, ul, ul, i, i, i, i, i, i, C.c_uint, C.c_uint], None)
        self.rd = open_display(host_display(display_name).encode())
        if not self.rd:
            raise ValueError('host_display_unavailable')
        if not query(self.rd, C.byref(i()), C.byref(i())):
            self.close_display(self.rd)
            raise ValueError('host_xrender_required')
        self.fmt = find_format(self.rd, default_visual(self.rd, default_screen(self.rd)))
        connection.sync()
        self.cursor_format = standard_format(self.rd, 0)  # PictStandardARGB32
        self.cursors = {}
        self.dest = self.create(self.rd, window.id, self.fmt, 0, None)
        self.flush(self.rd, 0)
        self.surfaces = {}
        self.gc = window.create_gc(foreground=0xffffff, background=0x171c25)
        self.version = None
        self.transforms = {}

    def upload(self, incarnation, frame):
        from pathlib import Path
        source = frame['frame']
        old = self.surfaces.get(incarnation)
        if old and old['frame'] == source['frame']:
            return
        w, h, pixels = decode_png(Path(source['path']).read_bytes())
        if (w, h) != (source['rect']['w'], source['rect']['h']):
            raise ValueError('feed_size_mismatch')
        if old and old['size'] != (w, h):
            self.drop(incarnation)
            old = None
        if old is None:
            pixmap = self.window.create_pixmap(w, h, 24)
            self.d.sync()
            picture = self.create(self.rd, pixmap.id, self.fmt, 0, None)
            self.flush(self.rd, 0)
            old = self.surfaces[incarnation] = dict(pixmap=pixmap, picture=picture, size=(w, h), frame=None, transform=None)
        # Bound PutImage requests below the server maximum request size.
        rows = max(1, min(h, (self.d.display.info.max_request_length*4-128)//(w*4)))
        for y in range(0, h, rows):
            count = min(rows, h-y)
            old['pixmap'].put_image(self.gc, 0, y, w, count, self.X.ZPixmap, 24, 0, pixels[y*w*4:(y+count)*w*4])
        self.d.sync()  # upload complete before the render connection reads it
        old['frame'] = source['frame']
        self.transform(incarnation, self.transforms.get(incarnation))

    def cursor(self, incarnation, cursor):
        image = cursor and cursor.get('image')
        if not image:
            return
        old = self.cursors.get(incarnation)
        if old and old['image'] == image:
            return
        w, h = image['width'], image['height']
        pixels = base64.b64decode(image['pixels'], validate=True)
        if image['format'] != 'argb32-le' or not 0 < w*h <= 128*128 or len(pixels) != w*h*4:
            raise ValueError('invalid_cursor_image')
        self.drop_cursor(incarnation)
        pixmap = self.window.create_pixmap(w, h, 32)
        gc = pixmap.create_gc()
        pixmap.put_image(gc, 0, 0, w, h, self.X.ZPixmap, 32, 0, pixels)
        gc.free()
        self.d.sync()
        picture = self.create(self.rd, pixmap.id, self.cursor_format, 0, None)
        self.cursors[incarnation] = dict(image=image, pixmap=pixmap, picture=picture, transform=None)
        self.transform_cursor(incarnation, self.transforms.get(incarnation))

    def transform_cursor(self, incarnation, t):
        source = self.cursors.get(incarnation)
        if source and t and source['transform'] != t:
            matrix = (C.c_int32*9)(int(t.native_w/t.w*65536), 0, 0,
                                  0, int(t.native_h/t.h*65536), 0, 0, 0, 65536)
            self.set_transform(self.rd, source['picture'], C.byref(matrix))
            source['transform'] = t

    def drop_cursor(self, incarnation):
        source = self.cursors.pop(incarnation, None)
        if source:
            self.free(self.rd, source['picture'])
            self.flush(self.rd, 0)
            source['pixmap'].free()

    def transform(self, incarnation, transform):
        source = self.surfaces.get(incarnation)
        if not source or transform is None or source['transform'] == transform:
            return
        matrix = (C.c_int32*9)(int(transform.native_w/transform.w*65536), 0, 0,
                              0, int(transform.native_h/transform.h*65536), 0, 0, 0, 65536)
        self.set_transform(self.rd, source['picture'], C.byref(matrix))
        source['transform'] = transform

    def layout(self, scene):
        if self.version == scene.version:
            return
        self.version, self.transforms = scene.version, dict(scene.transforms)
        for incarnation, transform in self.transforms.items():
            self.transform(incarnation, transform)
            self.transform_cursor(incarnation, transform)
        for incarnation in set(self.surfaces)-set(scene.transforms):
            self.drop(incarnation)
        self.flush(self.rd, 0)

    def draw_surface(self, incarnation, t, rect=None):
        source = self.surfaces.get(incarnation)
        if not source:
            return
        x, y, w, h = t.x, t.y, t.w, t.h
        sx = sy = 0
        if rect:
            sx, sy = int(rect['x']*t.w/t.native_w), int(rect['y']*t.h/t.native_h)
            w = min(t.w-sx, max(1, int(rect['w']*t.w/t.native_w)))
            h = min(t.h-sy, max(1, int(rect['h']*t.h/t.native_h)))
            x, y = x+sx, y+sy
        if w > 0 and h > 0:
            self.composite(self.rd, 1, source['picture'], 0, self.dest, sx, sy, 0, 0, x, y, w, h)

    def text(self, x, y, value):
        self.window.draw_text(self.gc, x, y, value.encode('ascii', 'replace')[:180])

    def draw(self, scene, status):
        self.layout(scene)
        self.window.clear_area()
        self.d.sync()
        for i, t in scene.transforms.items():
            self.draw_surface(i, t)
        # Repaint the parent's native dialog region on top of child surfaces.
        root = scene.root
        if root in scene.transforms:
            for region in scene.modal(root):
                self.draw_surface(root, scene.transforms[root], region)
        self.flush(self.rd, 0)
        self.text(8, 20, '[Takeover]   [Resume]   [Collapse]   [Focus view]   [Parent]')
        attention_count = sum(bool(scene.records[i].get('attention')) for i in scene.chips)
        self.text(8, 44, status + (f' | chips: {len(scene.chips)}, attention: {attention_count} (scroll)' if scene.chips else ''))
        for i, t in scene.transforms.items():
            if i != root and scene.modal(root):
                continue
            self.gc.change(foreground=0x65d6ad if i == scene.selected else 0x8291aa)
            self.window.rectangle(self.gc, t.x, max(0, t.y-1), t.w, t.h)
            if i != root:
                self.window.fill_rectangle(self.gc, t.x, max(48, t.y-24), min(t.w, 340), 23)
                self.gc.change(foreground=0x10141c)
                self.text(t.x+4, t.y-8, f'{scene.records[i]["session"]} drag title | right-drag resize')
            cursor = scene.frames.get(i, {}).get('cursor') or {}
            position = cursor.get('position') or {}
            if 'x' in position:
                source = self.cursors.get(i)
                hotspot = cursor.get('hotspot') or dict(x=0, y=0)
                x = t.x+int((position['x']-hotspot['x'])*t.w/t.native_w)
                y = t.y+int((position['y']-hotspot['y'])*t.h/t.native_h)
                if source:
                    image = source['image']
                    width, height = max(1, int(image['width']*t.w/t.native_w)), max(1, int(image['height']*t.h/t.native_h))
                    sx, sy = max(0, t.x-x), max(0, t.y-y)
                    width = min(width-sx, t.x+t.w-max(t.x,x))
                    height = min(height-sy, t.y+t.h-max(t.y,y))
                    if width > 0 and height > 0:
                        self.d.sync()
                        self.composite(self.rd, 3, source['picture'], 0, self.dest, sx, sy, 0, 0,
                                       max(t.x,x), max(t.y,y), width, height)
                        self.flush(self.rd, 0)
                else:
                    self.gc.change(foreground=0xffcf65)
                    self.window.line(self.gc, x-4, y, x+4, y)
                    self.window.line(self.gc, x, y-4, x, y+4)
        self.gc.change(foreground=0xffffff)
        for cx, cy, i in scene.chip_views():
            attention = scene.records[i].get('attention', [])
            self.text(cx, cy+16,
                      f'[{scene.records[i]["session"]} {",".join(sorted({a["need"] for a in attention})) if attention else "running"}]')
        self.d.flush()

    def drop(self, incarnation):
        self.drop_cursor(incarnation)
        surface = self.surfaces.pop(incarnation, None)
        if surface:
            self.free(self.rd, surface['picture'])
            self.flush(self.rd, 0)
            surface['pixmap'].free()

    def close(self):
        for i in list(self.surfaces):
            self.drop(i)
        self.free(self.rd, self.dest)
        self.close_display(self.rd)
        self.gc.free()
