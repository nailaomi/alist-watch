# -*- coding: utf-8 -*-
"""Netwatch 应用图标的纯标准库渲染器。

不依赖 PIL / ImageMagick / cairo，只用 zlib + struct 手写 PNG。
这样容器里（python:3.12-alpine）也能生成图标，Bark 通知里的 icon
和浏览器 favicon 用的是同一份像素。

图形：靛蓝渐变圆角方块 + 白色仪表环（右侧留口）+ 心跳波 + 翠绿状态点。
"""
import math
import os
import struct
import zlib

BASE = 512


def _clamp(x, a=0.0, b=1.0):
    return a if x < a else (b if x > b else x)


def _smooth(e0, e1, x):
    t = _clamp((x - e0) / (e1 - e0))
    return t * t * (3 - 2 * t)


def _seg(px, py, x1, y1, x2, y2):
    """点到线段距离"""
    dx, dy = x2 - x1, y2 - y1
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else _clamp(((px - x1) * dx + (py - y1) * dy) / L)
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _rrect(px, py, cx, cy, hw, hh, r):
    qx = abs(px - cx) - (hw - r)
    qy = abs(py - cy) - (hh - r)
    return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - r


def _lerp(a, b, t):
    return a + (b - a) * t


def render(size=BASE, simple=False):
    """返回 (w, h, RGB bytes)。simple=True 时用更适合小尺寸的简化笔画。"""
    s = BASE
    cx = cy = s / 2.0
    R = s * 0.316          # 环半径
    rw = s * 0.064         # 环线宽
    pw = s * 0.056         # 心跳线宽
    corner = s * 0.225

    # 心跳折线（相对坐标 * s）：两端留空，不碰环
    if simple:
        # 小尺寸只留心跳：32px 下圆环会把图形糊成一团
        rw, pw = 0.0, s * 0.130
        pts = [(0.135, 0.500), (0.335, 0.500), (0.475, 0.235),
               (0.615, 0.765), (0.720, 0.500), (0.865, 0.500)]
    else:
        pts = [(0.290, 0.500), (0.360, 0.500), (0.406, 0.412),
               (0.466, 0.602), (0.528, 0.386), (0.590, 0.500), (0.710, 0.500)]
    pts = [(x * s, y * s) for x, y in pts]
    segs = list(zip(pts, pts[1:]))

    # 缺口：屏幕坐标下 (-96°, 0°) 扇形
    gap_c = -45.0
    gap_h = 0.0          # 0 = 完整圆环

    # 状态点：右下角独立徽标，外面套一圈底色把它和环分开
    dot_x, dot_y = s * 0.797, s * 0.797
    dot_r = 0.0 if simple else s * 0.055
    dot_halo = s * 0.023

    px = bytearray(size * size * 3)
    scale = s / float(size)
    aa = 1.6 * scale

    for j in range(size):
        for i in range(size):
            x = (i + 0.5) * scale
            y = (j + 0.5) * scale

            # ---- 背景：圆角方块 + 对角渐变 + 左上高光
            d = _rrect(x, y, cx, cy, s / 2.0, s / 2.0, corner)
            bg = 1.0 - _smooth(-aa, aa, d)
            if bg <= 0.0:
                o = (j * size + i) * 3
                px[o] = px[o + 1] = px[o + 2] = 0
                continue

            t = _clamp((x * 0.32 + y * 0.68) / s)
            r0 = _lerp(0x74, 0x37, t)     # #7464f7 -> #3730b8
            g0 = _lerp(0x60, 0x2c, t)
            b0 = _lerp(0xf8, 0xae, t)
            hl = math.hypot(x - s * 0.24, y - s * 0.14) / (s * 0.72)
            hl = max(0.0, 1.0 - hl) ** 2 * 0.20
            r0 = _lerp(r0, 255, hl)
            g0 = _lerp(g0, 255, hl)
            b0 = _lerp(b0, 255, hl)

            # ---- 前景遮罩
            fg = 0.0

            # 仪表环（带缺口）
            dr = math.hypot(x - cx, y - cy)
            ring = 1.0 - _smooth(rw / 2 - aa, rw / 2 + aa, abs(dr - R))
            if ring > 0.0 and rw > 0:
                if gap_h <= 0:
                    fg = max(fg, ring)
                else:
                    ang = math.degrees(math.atan2(y - cy, x - cx))
                    dd = abs(((ang - gap_c + 180) % 360) - 180)
                    cut = 1.0 - _smooth(gap_h - 1.2, gap_h + 1.2, dd)
                    fg = max(fg, ring * cut)

            # 心跳波
            if 0.20 * s < x < 0.80 * s and 0.34 * s < y < 0.66 * s:
                best = min(_seg(x, y, a[0], a[1], b[0], b[1]) for a, b in segs)
                pulse = 1.0 - _smooth(pw / 2 - aa, pw / 2 + aa, best)
                fg = max(fg, pulse)

            rr, gg, bb = r0, g0, b0
            if fg > 0.0:
                rr = _lerp(rr, 255.0, fg)
                gg = _lerp(gg, 255.0, fg)
                bb = _lerp(bb, 255.0, fg)

            # 状态点（翠绿）画在最上层，先挖一圈底色再填
            ddist = math.hypot(x - dot_x, y - dot_y)
            halo = 0.0
            if dot_r > 0:
                halo = 1.0 - _smooth(dot_r + dot_halo - aa, dot_r + dot_halo + aa, ddist)
            if halo > 0.0:
                rr = _lerp(rr, r0, halo)
                gg = _lerp(gg, g0, halo)
                bb = _lerp(bb, b0, halo)
            dot = 0.0 if dot_r <= 0 else 1.0 - _smooth(dot_r - aa, dot_r + aa, ddist)
            if dot > 0.0:
                rr = _lerp(rr, 0x3d, dot)
                gg = _lerp(gg, 0xe0, dot)
                bb = _lerp(bb, 0xa0, dot)

            o = (j * size + i) * 3
            px[o] = max(0, min(255, int(rr + 0.5)))
            px[o + 1] = max(0, min(255, int(gg + 0.5)))
            px[o + 2] = max(0, min(255, int(bb + 0.5)))
    return size, size, bytes(px)


def _png(w, h, rgb):
    raw = bytearray()
    stride = w * 3
    for y in range(h):
        raw.append(0)
        raw += rgb[y * stride:(y + 1) * stride]

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def _down(src, ssize, dsize):
    """整数倍/非整数倍都支持的面积平均缩放"""
    out = bytearray(dsize * dsize * 3)
    ratio = ssize / float(dsize)
    for j in range(dsize):
        y0 = int(j * ratio)
        y1 = min(ssize, max(y0 + 1, int((j + 1) * ratio)))
        for i in range(dsize):
            x0 = int(i * ratio)
            x1 = min(ssize, max(x0 + 1, int((i + 1) * ratio)))
            r = g = b = n = 0
            for yy in range(y0, y1):
                base = yy * ssize * 3
                for xx in range(x0, x1):
                    o = base + xx * 3
                    r += src[o]
                    g += src[o + 1]
                    b += src[o + 2]
                    n += 1
            o = (j * dsize + i) * 3
            out[o] = r // n
            out[o + 1] = g // n
            out[o + 2] = b // n
    return dsize, dsize, bytes(out)


_cache = {}
_master = []


def png(size=512):
    """返回指定尺寸的 PNG 字节；512 为主图，其余从主图缩放（带缓存）"""
    if size in _cache:
        return _cache[size]
    if size <= 64:
        data = _png(*render(size, simple=True))
    elif size < BASE:
        data = _png(*_down(render(BASE)[2], BASE, size))
    else:
        if not _master:
            _master.append(render(BASE))
        data = _png(*_master[0])
    _cache[size] = data
    return data


SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#7464f7"/><stop offset="1" stop-color="#3730b8"/></linearGradient>
</defs>
<rect width="512" height="512" rx="115" fill="url(#g)"/>
<path d="M406 256a150 150 0 1 0-150 150" fill="none" stroke="#fff" stroke-width="34"
 stroke-linecap="round"/>
<path d="M106 256h62l40-80 48 160 44-140 30 60h76" fill="none" stroke="#fff"
 stroke-width="30" stroke-linecap="round" stroke-linejoin="round"/>
<circle cx="358" cy="148" r="32" fill="#3de0a0"/>
</svg>"""

# ================================================================
# 网盘品牌图标
# ================================================================
# 没有 PIL，也就没有现成的官方素材。这里用 SDF 手画每个网盘的
# 识别性特征（品牌色 + 标志性形状），输出带透明圆角的 RGBA PNG，
# 网页 <img> 和 Bark 通知里的 icon / 状态图共用同一份像素。
LOGO_COLORS = {
    "115":     ("#4a90e2", "#1f5fbf"),
    "quark":   ("#6c63ff", "#4340c9"),
    "guangya": ("#ffc24a", "#ef8f00"),
    "aliyun":  ("#5b8cff", "#2b5fd9"),
    "tianyi":  ("#3ba0ff", "#0b6ad4"),
    "xunlei":  ("#4fc3f7", "#0288d1"),
    "pikpak":  ("#2ee6b0", "#00a884"),
    "baidu":   ("#5a6bff", "#2326c9"),
}


def _hex(h):
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _poly_d(px, py, pts):
    """多边形有向距离（负=在内部）"""
    d = 1e9
    inside = False
    n = len(pts)
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        dx, dy = xj - xi, yj - yi
        L = dx * dx + dy * dy
        t = 0.0 if L == 0 else _clamp(((px - xi) * dx + (py - yi) * dy) / L)
        dd = math.hypot(px - (xi + t * dx), py - (yi + t * dy))
        if dd < d:
            d = dd
        if (yi > py) != (yj > py):
            xc = (xj - xi) * (py - yi) / (yj - yi) + xi
            if px < xc:
                inside = not inside
        j = i
    return -d if inside else d


def _cloud(x, y, s):
    """云朵轮廓（115 / 阿里 / 百度 的底形）"""
    return min(
        math.hypot(x - 0.352 * s, y - 0.556 * s) - 0.132 * s,
        math.hypot(x - 0.512 * s, y - 0.428 * s) - 0.176 * s,
        math.hypot(x - 0.666 * s, y - 0.556 * s) - 0.124 * s,
        _rrect(x, y, 0.506 * s, 0.624 * s, 0.238 * s, 0.082 * s, 0.080 * s),
    )


def _arc_ring(x, y, s, cx, cy, r, half, a0, a1, w):
    """带角度范围的圆环（画信号弧 / 轨道）"""
    d = abs(math.hypot(x - cx * s, y - cy * s) - r * s) - w * s / 2.0
    ang = math.degrees(math.atan2(y - cy * s, x - cx * s))
    A = abs(((ang - a0 + 180) % 360) - 180)
    B = abs(((ang - a1 + 180) % 360) - 180)
    if A <= half or B <= half:
        return d
    # 角度外：退回点到端点的距离，避免端面被拉长
    best = 1e9
    for aa, lo, hi in ((a0, a0, a1),):
        pass
    for ang_deg in (a0 - half, a0 + half, a1 - half, a1 + half):
        ar = math.radians(ang_deg)
        px = cx * s + r * s * math.cos(ar)
        py = cy * s + r * s * math.sin(ar)
        dd = math.hypot(x - px, y - py) - w * s / 2.0
        if dd < best:
            best = dd
    return best


def _symbol(key, x, y, s):
    """返回符号的有向距离；负=白色笔画内部"""
    k = key
    if k == "115":
        # 云 + 云下一道横杠（115 的"盘"感）
        return min(_cloud(x, y, s),
                   _rrect(x, y, 0.506 * s, 0.748 * s, 0.172 * s, 0.028 * s, 0.028 * s))
    if k == "aliyun":
        # 云 + 向下箭头（阿里云盘的下载盘意象）
        d = _cloud(x, y, s)
        pts = [(0.506 * s, 0.905 * s), (0.388 * s, 0.688 * s),
               (0.462 * s, 0.688 * s), (0.462 * s, 0.556 * s),
               (0.550 * s, 0.556 * s), (0.550 * s, 0.688 * s),
               (0.624 * s, 0.688 * s)]
        return min(d, _poly_d(x, y, pts))
    if k == "baidu":
        # 云 + 右下小圆（避免和 115 撞脸）
        return min(_cloud(x, y, s),
                   math.hypot(x - 0.700 * s, y - 0.700 * s) - 0.072 * s)
    if k == "quark":
        # 原子：中心核 + 两条交叉椭圆轨道
        d = math.hypot(x - 0.5 * s, y - 0.5 * s) - 0.115 * s
        for ang, thick in ((0.0, 0.052), (58.0, 0.052)):
            ar = math.radians(ang)
            ca, sa = math.cos(ar), math.sin(ar)
            ux = (x - 0.5 * s) * ca + (y - 0.5 * s) * sa
            uy = -(x - 0.5 * s) * sa + (y - 0.5 * s) * ca
            # 椭圆环：归一化半径近似
            q = math.hypot(ux / (0.365 * s), uy / (0.170 * s))
            dd = abs(q - 1.0) * 0.210 * s - thick * s / 2.0
            if dd < d:
                d = dd
        return d
    if k == "guangya":
        # 鸭：圆头 + 三角嘴 + 身体弧
        body = math.hypot((x - 0.520 * s) / 1.10, (y - 0.610 * s) / 1.0) - 0.215 * s
        head = math.hypot(x - 0.545 * s, y - 0.395 * s) - 0.135 * s
        beak = _poly_d(x, y, [(0.428 * s, 0.392 * s), (0.276 * s, 0.438 * s),
                              (0.430 * s, 0.482 * s)])
        d = min(body, head, beak)
        eye = math.hypot(x - 0.596 * s, y - 0.362 * s) - 0.031 * s
        return max(d, -eye)
    if k == "tianyi":
        # 三条信号弧 + 实心点
        dot = math.hypot(x - 0.5 * s, y - 0.66 * s) - 0.088 * s
        d = dot
        for r, w in ((0.235, 0.062), (0.360, 0.056), (0.485, 0.050)):
            dd = _arc_ring(x, y, s, 0.5, 0.66, r, 46.0, -180.0, 0.0, w)
            if dd < d:
                d = dd
        return d
    if k == "xunlei":
        # 闪电
        return _poly_d(x, y, [
            (0.585 * s, 0.150 * s), (0.300 * s, 0.545 * s), (0.462 * s, 0.545 * s),
            (0.408 * s, 0.860 * s), (0.700 * s, 0.442 * s), (0.532 * s, 0.442 * s)])
    if k == "pikpak":
        # 播放三角（圆角靠外层 padding 保证）
        return _poly_d(x, y, [(0.372 * s, 0.268 * s), (0.372 * s, 0.732 * s),
                              (0.752 * s, 0.500 * s)])
    return math.hypot(x - 0.5 * s, y - 0.5 * s) - 0.30 * s


def render_logo(key, size=96):
    """品牌图标：渐变圆角方块 + 白色符号，返回 (w,h,RGBA bytes)"""
    s = float(size)
    c0 = _hex((LOGO_COLORS.get(key) or ("#6b7280", "#374151"))[0])
    c1 = _hex((LOGO_COLORS.get(key) or ("#6b7280", "#374151"))[1])
    corner = s * 0.285
    px = bytearray(size * size * 4)
    aa = max(0.6, s / 96.0 * 1.1)

    for j in range(size):
        for i in range(size):
            x = i + 0.5
            y = j + 0.5
            d = _rrect(x, y, s / 2.0, s / 2.0, s / 2.0, s / 2.0, corner)
            a = 1.0 - _smooth(-aa, aa, d)
            o = (j * size + i) * 4
            if a <= 0.002:
                continue
            t = _clamp((x * 0.30 + y * 0.70) / s)
            r = _lerp(c0[0], c1[0], t)
            g = _lerp(c0[1], c1[1], t)
            b = _lerp(c0[2], c1[2], t)
            hl = math.hypot(x - s * 0.26, y - s * 0.16) / (s * 0.78)
            hl = max(0.0, 1.0 - hl) ** 2 * 0.22
            r = _lerp(r, 255, hl)
            g = _lerp(g, 255, hl)
            b = _lerp(b, 255, hl)

            m = 1.0 - _smooth(-aa, aa, _symbol(key, x, y, s))
            if m > 0.0:
                r = _lerp(r, 255.0, m)
                g = _lerp(g, 255.0, m)
                b = _lerp(b, 255.0, m)

            px[o] = max(0, min(255, int(r + 0.5)))
            px[o + 1] = max(0, min(255, int(g + 0.5)))
            px[o + 2] = max(0, min(255, int(b + 0.5)))
            px[o + 3] = max(0, min(255, int(a * 255 + 0.5)))
    return size, size, bytes(px)


def _png_rgba(w, h, rgba):
    raw = bytearray()
    stride = w * 4
    for y in range(h):
        raw.append(0)
        raw += rgba[y * stride:(y + 1) * stride]

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


_logo_cache = {}


# ---------------- 官方图标：PNG 解码 + 缩放（纯标准库） ----------------
def _decode_png(data):
    """最小 PNG 解码器：8bit、非隔行，支持 0/2/3/4/6 色型，输出 RGBA"""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not png")
    pos, idat, plte, trns = 8, bytearray(), None, None
    w = h = bd = ct = None
    inter = 0
    while pos + 8 <= len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if tag == b"IHDR":
            w, h, bd, ct, _c, _f, inter = struct.unpack(">IIBBBBB", chunk)
        elif tag == b"PLTE":
            plte = chunk
        elif tag == b"tRNS":
            trns = chunk
        elif tag == b"IDAT":
            idat += chunk
        elif tag == b"IEND":
            break
    if bd != 8 or w is None:
        raise ValueError("unsupported png")
    nch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ct]
    raw = zlib.decompress(bytes(idat))
    out = bytearray(h * w * nch)

    def unfilter(line, prev, ft, stride):
        if ft == 1:
            for i in range(nch, stride):
                line[i] = (line[i] + line[i - nch]) & 255
        elif ft == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif ft == 3:
            for i in range(stride):
                a = line[i - nch] if i >= nch else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 255
        elif ft == 4:
            for i in range(stride):
                a = line[i - nch] if i >= nch else 0
                b = prev[i]
                c = prev[i - nch] if i >= nch else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 255

    if inter:
        # Adam7 隔行：7 个 pass 各自独立 filter，逐点写回
        p = 0
        for xs, ys, xstep, ystep in ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8),
                                     (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2),
                                     (0, 1, 1, 2)):
            pw = (w - xs + xstep - 1) // xstep
            ph = (h - ys + ystep - 1) // ystep
            if pw <= 0 or ph <= 0:
                continue
            pstride = pw * nch
            prev = bytearray(pstride)
            for y in range(ph):
                ft = raw[p]
                p += 1
                line = bytearray(raw[p:p + pstride])
                p += pstride
                unfilter(line, prev, ft, pstride)
                dy = ys + y * ystep
                for x in range(pw):
                    o1 = (dy * w + xs + x * xstep) * nch
                    out[o1:o1 + nch] = line[x * nch:(x + 1) * nch]
                prev = line
    else:
        stride = w * nch
        prev = bytearray(stride)
        p = 0
        for y in range(h):
            ft = raw[p]
            p += 1
            line = bytearray(raw[p:p + stride])
            p += stride
            unfilter(line, prev, ft, stride)
            out[y * stride:(y + 1) * stride] = line
            prev = line

    rgba = bytearray(w * h * 4)
    if ct == 6:
        rgba[:] = out
    elif ct == 2:
        rgba[0::4] = out[0::3]
        rgba[1::4] = out[1::3]
        rgba[2::4] = out[2::3]
        rgba[3::4] = b"\xff" * (w * h)
    elif ct == 3:
        for i in range(w * h):
            pi = out[i] * 3
            o = i * 4
            rgba[o] = plte[pi]
            rgba[o + 1] = plte[pi + 1]
            rgba[o + 2] = plte[pi + 2]
            rgba[o + 3] = trns[out[i]] if (trns and out[i] < len(trns)) else 255
    elif ct == 0:
        for i in range(w * h):
            v = out[i]
            o = i * 4
            rgba[o] = rgba[o + 1] = rgba[o + 2] = v
            rgba[o + 3] = 255
    else:
        for i in range(w * h):
            v = out[i * 2]
            o = i * 4
            rgba[o] = rgba[o + 1] = rgba[o + 2] = v
            rgba[o + 3] = out[i * 2 + 1]
    return w, h, bytes(rgba)


def _scale_rgba(sw, sh, src, dw, dh, ss=4):
    """4x4 子采样缩放：缩小走面积平均，放大近似双线性（预乘 alpha，边缘不发黑）"""
    out = bytearray(dw * dh * 4)
    N = ss * ss
    for j in range(dh):
        sy0 = j * sh / float(dh)
        sy1 = (j + 1) * sh / float(dh)
        for i in range(dw):
            sx0 = i * sw / float(dw)
            sx1 = (i + 1) * sw / float(dw)
            r = g = b = a = 0
            for yy in range(ss):
                syi = min(sh - 1, int(sy0 + (yy + 0.5) * (sy1 - sy0) / ss))
                base = syi * sw
                for xx in range(ss):
                    sxi = min(sw - 1, int(sx0 + (xx + 0.5) * (sx1 - sx0) / ss))
                    o = (base + sxi) * 4
                    al = src[o + 3]
                    if al:
                        r += src[o] * al
                        g += src[o + 1] * al
                        b += src[o + 2] * al
                        a += al
            o = (j * dw + i) * 4
            if a:
                out[o] = min(255, r // a)
                out[o + 1] = min(255, g // a)
                out[o + 2] = min(255, b // a)
                out[o + 3] = min(255, a // N)
    return dw, dh, bytes(out)


def _round_mask(w, h, rgba, rad):
    """统一裁成 iOS 风格的圆角方块（官方原图是直角的）"""
    for j in range(h):
        for i in range(w):
            d = _rrect(i + 0.5, j + 0.5, w / 2.0, h / 2.0, w / 2.0, h / 2.0, rad)
            a = 1.0 - _smooth(-0.85, 0.85, d)
            if a < 0.999:
                o = (j * w + i) * 4
                rgba[o + 3] = int(rgba[o + 3] * a)


_logo_src = {}
_logo_rgba_cache = {}


def _logo_source(key):
    """优先读 logos/<key>.png（官方图标，构建时已放进镜像）"""
    if key in _logo_src:
        return _logo_src[key]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logos", key + ".png")
    res = None
    try:
        with open(path, "rb") as f:
            res = _decode_png(f.read())
    except Exception:
        res = None
    _logo_src[key] = res
    return res


def logo_rgba(key, size=96):
    """品牌图标像素：官方 PNG → 缩放 + 圆角；没有文件就退回手绘 SDF"""
    ck = (key, size)
    hit = _logo_rgba_cache.get(ck)
    if hit is not None:
        return hit
    src = _logo_source(key)
    if src:
        w, h, rgba = src
        _, _, out = _scale_rgba(w, h, rgba, size, size)
        buf = bytearray(out)
        _round_mask(size, size, buf, size * 0.225)
        res = (size, size, bytes(buf))
    else:
        res = render_logo(key, size)
    _logo_rgba_cache[ck] = res
    return res


def logo_png(key, size=96):
    ck = (key, size)
    if ck not in _logo_cache:
        _logo_cache[ck] = _png_rgba(*logo_rgba(key, size))
    return _logo_cache[ck]


def _blit(buf, w, h, x0, y0, src, sw, sh):
    for y in range(sh):
        yy = y0 + y
        if yy < 0 or yy >= h:
            continue
        srow = y * sw * 4
        dbase = (yy * w + x0) * 3
        for x in range(sw):
            if x0 + x < 0 or x0 + x >= w:
                continue
            so = srow + x * 4
            a = src[so + 3]
            if a == 0:
                continue
            o = dbase + x * 3
            if a == 255:
                buf[o], buf[o + 1], buf[o + 2] = src[so], src[so + 1], src[so + 2]
            else:
                t = a / 255.0
                buf[o] = int(buf[o] + (src[so] - buf[o]) * t)
                buf[o + 1] = int(buf[o + 1] + (src[so + 1] - buf[o + 1]) * t)
                buf[o + 2] = int(buf[o + 2] + (src[so + 2] - buf[o + 2]) * t)


if __name__ == "__main__":
    import sys
    import time
    t0 = time.time()
    for sz in (512, 192, 32):
        with open("icon-%d.png" % sz, "wb") as f:
            f.write(png(sz))
        print("icon-%d.png" % sz)
    with open("icon.svg", "w", encoding="utf-8") as f:
        f.write(SVG)
    # 网盘图标 contact sheet，方便目视检查
    import os
    keys = list(LOGO_COLORS)
    cell = 128
    sheet = bytearray(bytes((246, 247, 251)) * (cell * len(keys) * cell))
    for i, k in enumerate(keys):
        _blit(sheet, cell * len(keys), cell, i * cell + 16, 16,
              logo_rgba(k, 96)[2], 96, 96)
    open("logos-sheet.png", "wb").write(_png(cell * len(keys), cell, bytes(sheet)))
    print("耗时 %.2fs" % (time.time() - t0))
