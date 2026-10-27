"""Draw the SVG files kicad-cli exports (symbols, footprints) with plain wx
drawing calls.

wx.svg can't be used: KiCad's Windows build ships wxPython without it
("No module named 'wx.svg._nanosvg'"). KiCad's SVG output is simple: groups
with a fill/stroke style, rect, circle and path elements (lines, arcs,
curves); text is drawn as stroked paths and the <text> elements are
invisible. That is all this module handles.
"""

import math
import re
import xml.etree.ElementTree as ET

import wx

_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_CMD = re.compile(r"[MmLlHhVvAaCcSsQqTtZz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def _style(el, inherited):
    st = dict(inherited)
    for key in ("fill", "stroke", "stroke-width", "fill-opacity", "stroke-opacity", "opacity"):
        if el.get(key) is not None:
            st[key] = el.get(key)
    for part in (el.get("style") or "").split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            st[k.strip()] = v.strip()
    return st


def _colour(value, opacity):
    if not value or value == "none":
        return None
    value = value.strip()
    if value.startswith("#") and len(value) in (4, 7):
        if len(value) == 4:
            value = "#" + "".join(c * 2 for c in value[1:])
        r, g, b = (int(value[i:i + 2], 16) for i in (1, 3, 5))
    elif value.startswith("rgb("):
        r, g, b = (int(float(v)) for v in _NUM.findall(value)[:3])
    else:
        c = wx.Colour(value)
        if not c.IsOk():
            return None
        r, g, b = c.Red(), c.Green(), c.Blue()
    try:
        a = float(opacity)
    except (TypeError, ValueError):
        a = 1.0
    if a <= 0:
        return None
    # Blend onto white ourselves: the bitmap has no alpha channel.
    return wx.Colour(*(round(c * a + 255 * (1 - a)) for c in (r, g, b)))


def _arc_points(x1, y1, rx, ry, phi, large, sweep, x2, y2):
    """Points along an SVG elliptical arc (endpoint form, SVG spec F.6)."""
    if rx == 0 or ry == 0:
        return [(x2, y2)]
    rx, ry = abs(rx), abs(ry)
    cp, sp = math.cos(math.radians(phi)), math.sin(math.radians(phi))
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    x1p, y1p = cp * dx + sp * dy, -sp * dx + cp * dy
    lam = (x1p / rx) ** 2 + (y1p / ry) ** 2
    if lam > 1:
        rx, ry = rx * math.sqrt(lam), ry * math.sqrt(lam)
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    co = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        co = -co
    cxp, cyp = co * rx * y1p / ry, -co * ry * x1p / rx
    cx = cp * cxp - sp * cyp + (x1 + x2) / 2
    cy = sp * cxp + cp * cyp + (y1 + y2) / 2

    def ang(ux, uy, vx, vy):
        a = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
        return a

    t1 = ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dt = ang((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dt > 0:
        dt -= 2 * math.pi
    elif sweep and dt < 0:
        dt += 2 * math.pi
    n = max(4, int(abs(dt) / (math.pi / 24)))
    pts = []
    for i in range(1, n + 1):
        t = t1 + dt * i / n
        x, y = rx * math.cos(t), ry * math.sin(t)
        pts.append((cp * x - sp * y + cx, sp * x + cp * y + cy))
    return pts


def _bezier(p0, p1, p2, p3, n=12):
    pts = []
    for i in range(1, n + 1):
        t = i / n
        u = 1 - t
        pts.append((u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
                    u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1]))
    return pts


def _path_polylines(d):
    """SVG path data -> list of (points, closed)."""
    tokens = _CMD.findall(d)
    out, pts = [], []
    x = y = sx = sy = 0.0
    cmd = None
    last_ctrl = None
    i = 0

    def nums(k):
        nonlocal i
        vals = [float(v) for v in tokens[i:i + k]]
        i += k
        return vals

    while i < len(tokens):
        if tokens[i].isalpha():
            cmd = tokens[i]
            i += 1
            if cmd in "Zz":
                if pts:
                    out.append((pts, True))
                pts, x, y = [], sx, sy
                continue
        if cmd is None:
            break
        rel = cmd.islower()
        c = cmd.upper()
        try:
            if c == "M":
                nx, ny = nums(2)
                if rel:
                    nx, ny = nx + x, ny + y
                if pts:
                    out.append((pts, False))
                x, y, sx, sy = nx, ny, nx, ny
                pts = [(x, y)]
                cmd = "l" if rel else "L"  # extra pairs after M are lines
            elif c == "L":
                nx, ny = nums(2)
                x, y = (nx + x, ny + y) if rel else (nx, ny)
                pts.append((x, y))
            elif c == "H":
                (nx,) = nums(1)
                x = nx + x if rel else nx
                pts.append((x, y))
            elif c == "V":
                (ny,) = nums(1)
                y = ny + y if rel else ny
                pts.append((x, y))
            elif c == "A":
                rx, ry, phi, large, sweep, nx, ny = nums(7)
                if rel:
                    nx, ny = nx + x, ny + y
                pts += _arc_points(x, y, rx, ry, phi, int(large), int(sweep), nx, ny)
                x, y = nx, ny
            elif c in "CS":
                if c == "C":
                    x1, y1, x2, y2, nx, ny = nums(6)
                    if rel:
                        x1, y1, x2, y2 = x1 + x, y1 + y, x2 + x, y2 + y
                else:
                    x2, y2, nx, ny = nums(4)
                    if rel:
                        x2, y2 = x2 + x, y2 + y
                    x1, y1 = (2 * x - last_ctrl[0], 2 * y - last_ctrl[1]) if last_ctrl else (x, y)
                if rel:
                    nx, ny = nx + x, ny + y
                pts += _bezier((x, y), (x1, y1), (x2, y2), (nx, ny))
                last_ctrl = (x2, y2)
                x, y = nx, ny
                continue
            elif c in "QT":
                if c == "Q":
                    qx, qy, nx, ny = nums(4)
                    if rel:
                        qx, qy = qx + x, qy + y
                else:
                    nx, ny = nums(2)
                    qx, qy = (2 * x - last_ctrl[0], 2 * y - last_ctrl[1]) if last_ctrl else (x, y)
                if rel:
                    nx, ny = nx + x, ny + y
                c1 = (x + 2 / 3 * (qx - x), y + 2 / 3 * (qy - y))
                c2 = (nx + 2 / 3 * (qx - nx), ny + 2 / 3 * (qy - ny))
                pts += _bezier((x, y), c1, c2, (nx, ny))
                last_ctrl = (qx, qy)
                x, y = nx, ny
                continue
            else:
                i += 1
        except ValueError:
            break
        last_ctrl = None
    if pts:
        out.append((pts, False))
    return out


def render(data, w, h, background=(255, 255, 255), supersample=2):
    """An SVG (bytes) drawn centred on a w x h bitmap without transparency."""
    root = ET.fromstring(data)
    vb = [float(v) for v in _NUM.findall(root.get("viewBox") or "")]
    if len(vb) != 4 or vb[2] <= 0 or vb[3] <= 0:
        raise ValueError("SVG without a usable viewBox")
    W, H = w * supersample, h * supersample
    scale = min(W / vb[2], H / vb[3])
    ox = (W - vb[2] * scale) / 2 - vb[0] * scale
    oy = (H - vb[3] * scale) / 2 - vb[1] * scale

    def P(px, py):
        return wx.Point(round(px * scale + ox), round(py * scale + oy))

    bmp = wx.Bitmap(W, H, 24)
    dc = wx.MemoryDC(bmp)
    dc.SetBackground(wx.Brush(wx.Colour(*background)))
    dc.Clear()

    def pen_brush(st):
        stroke = _colour(st.get("stroke"), float(st.get("stroke-opacity", 1)) * float(st.get("opacity", 1)))
        fill = _colour(st.get("fill", "#000000"), float(st.get("fill-opacity", 1)) * float(st.get("opacity", 1)))
        if stroke:
            width = max(1, round(float(_NUM.findall(st.get("stroke-width", "0.1"))[0]) * scale))
            pen = wx.Pen(stroke, width)
            pen.SetCap(wx.CAP_ROUND)
            pen.SetJoin(wx.JOIN_ROUND)
        else:
            pen = wx.TRANSPARENT_PEN
        return pen, (wx.Brush(fill) if fill else wx.TRANSPARENT_BRUSH), stroke, fill

    def walk(el, inherited):
        tag = el.tag.rsplit("}", 1)[-1]
        if tag in ("title", "desc", "text", "defs", "style", "metadata"):
            return
        st = _style(el, inherited)
        if tag in ("svg", "g"):
            for child in el:
                walk(child, st)
            return
        pen, brush, stroke, fill = pen_brush(st)
        if not stroke and not fill:
            return
        dc.SetPen(pen)
        dc.SetBrush(brush)
        g = lambda k: float(el.get(k) or 0)  # noqa: E731
        if tag == "rect":
            dc.DrawRectangle(P(g("x"), g("y")), wx.Size(round(g("width") * scale), round(g("height") * scale)))
        elif tag == "circle":
            dc.DrawCircle(P(g("cx"), g("cy")), max(1, round(g("r") * scale)))
        elif tag == "ellipse":
            c = P(g("cx") - g("rx"), g("cy") - g("ry"))
            dc.DrawEllipse(c, wx.Size(round(2 * g("rx") * scale), round(2 * g("ry") * scale)))
        elif tag == "line":
            dc.DrawLine(P(g("x1"), g("y1")), P(g("x2"), g("y2")))
        elif tag in ("polyline", "polygon", "path"):
            if tag == "path":
                parts = _path_polylines(el.get("d") or "")
            else:
                v = [float(n) for n in _NUM.findall(el.get("points") or "")]
                parts = [(list(zip(v[0::2], v[1::2])), tag == "polygon")]
            for pts, closed in parts:
                ipts = [P(px, py) for px, py in pts]
                if len(ipts) == 1:
                    ipts.append(ipts[0])
                if fill and len(ipts) > 2:
                    dc.DrawPolygon(ipts)  # draws the outline with the pen too
                elif stroke:
                    dc.DrawLines(ipts + ([ipts[0]] if closed else []))

    walk(root, {"fill": "#000000"})
    dc.SelectObject(wx.NullBitmap)
    if supersample == 1:
        return bmp
    # Draw large and shrink: smooth edges without anti-aliasing support.
    return bmp.ConvertToImage().Scale(w, h, wx.IMAGE_QUALITY_HIGH).ConvertToBitmap()
