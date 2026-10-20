"""三角网格：数据结构、STL 读写、几何校验。

为什么自己实现而不用 trimesh：
  本机 pip 被沙箱阻断，装不了第三方几何库；而参数化改造件
  （支架、卡扣、盒子、旋钮）的几何本身很简单，纯 Python 足够。
  好处是零依赖、能在任何机器上跑、逻辑全部可单元测试。

设计取舍：
  * 只支持三角网格（打印机切片只需要三角网格）
  * 只支持二进制 STL 输出（Bambu Studio 与打印机都吃这个）
  * 校验项按"会导致打印失败"的顺序排列，不追求几何学完备
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from pathlib import Path

Vec3 = tuple[float, float, float]
Tri = tuple[Vec3, Vec3, Vec3]


@dataclass
class Mesh:
    """一组三角形。

    提供 transform / boolean 之外的常用操作；布尔运算在 parts 里
    用"并集 = 拼接 + 去重"的方式实现（对打印件够用，且不会失败）。
    """

    triangles: list[Tri] = field(default_factory=list)

    # ---------- 基础 ----------

    def add(self, other: "Mesh") -> "Mesh":
        self.triangles.extend(other.triangles)
        return self

    def copy(self) -> "Mesh":
        return Mesh(list(self.triangles))

    def __len__(self) -> int:
        return len(self.triangles)

    def bounds(self) -> tuple[Vec3, Vec3]:
        if not self.triangles:
            raise ValueError("空网格没有包围盒")
        xs: list[float] = []
        ys: list[float] = []
        zs: list[float] = []
        for tri in self.triangles:
            for x, y, z in tri:
                xs.append(x)
                ys.append(y)
                zs.append(z)
        return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))

    def size(self) -> Vec3:
        lo, hi = self.bounds()
        return (hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2])

    def volume(self) -> float:
        """用有向体积求和算体积（发散的封闭网格结果是精确的）。"""
        total = 0.0
        for (ax, ay, az), (bx, by, bz), (cx, cy, cz) in self.triangles:
            total += (
                ax * (by * cz - bz * cy)
                - ay * (bx * cz - bz * cx)
                + az * (bx * cy - by * cx)
            )
        return abs(total) / 6.0

    # ---------- 变换 ----------

    def translate(self, dx: float = 0.0, dy: float = 0.0, dz: float = 0.0) -> "Mesh":
        self.triangles = [
            tuple((x + dx, y + dy, z + dz) for x, y, z in tri)  # type: ignore[misc]
            for tri in self.triangles
        ]
        return self

    def scale(self, sx: float, sy: float | None = None, sz: float | None = None) -> "Mesh":
        sy = sx if sy is None else sy
        sz = sx if sz is None else sz
        self.triangles = [
            tuple((x * sx, y * sy, z * sz) for x, y, z in tri)  # type: ignore[misc]
            for tri in self.triangles
        ]
        return self

    def rotate_z(self, degrees: float) -> "Mesh":
        cos_a, sin_a = _cos_sin(degrees)
        self.triangles = [
            tuple((x * cos_a - y * sin_a, x * sin_a + y * cos_a, z) for x, y, z in tri)  # type: ignore[misc]
            for tri in self.triangles
        ]
        return self

    def rotate_x(self, degrees: float) -> "Mesh":
        cos_a, sin_a = _cos_sin(degrees)
        self.triangles = [
            tuple((x, y * cos_a - z * sin_a, y * sin_a + z * cos_a) for x, y, z in tri)  # type: ignore[misc]
            for tri in self.triangles
        ]
        return self

    def rotate_y(self, degrees: float) -> "Mesh":
        cos_a, sin_a = _cos_sin(degrees)
        self.triangles = [
            tuple((x * cos_a + z * sin_a, y, -x * sin_a + z * cos_a) for x, y, z in tri)  # type: ignore[misc]
            for tri in self.triangles
        ]
        return self

    def drop_to_bed(self) -> "Mesh":
        """把模型最低点移到 Z=0（打印必须坐在热床上）。"""
        lo, _hi = self.bounds()
        return self.translate(dz=-lo[2])

    def center_xy(self) -> "Mesh":
        """把模型在 XY 平面上居中（放到打印板中间）。"""
        lo, hi = self.bounds()
        return self.translate(dx=-(lo[0] + hi[0]) / 2, dy=-(lo[1] + hi[1]) / 2)

    # ---------- STL ----------

    # STL 存的是 32 位浮点，而孔的关键坐标来自三角函数与射线求交。
    # 同一个数学值走不同计算路径会差出 1e-6 级误差（例如 5.795555 与 5.7955549），
    # 直接按单个坐标四舍五入会落到相邻格点（5.79555 / 5.79556），
    # 本该重合的顶点就分家了，读回来边配不上、封闭网格被误判成不封闭。
    # 因此这里做"带容差的顶点归并"：先把顶点投到网格格子，再在邻居格子里
    # 找已在容差内的顶点并复用，保证相近的点一定映射到同一个坐标。
    QUANTIZE_DIGITS = 5

    def quantize(self, digits: int | None = None) -> "Mesh":
        """把顶点归并到统一网格，消除浮点误差导致的"顶点本该重合却没重合"。

        容差 1e-5 mm 对 3D 打印毫无影响（喷嘴直径 0.4mm），
        但能保证 STL 往返前后的拓扑一致。
        """
        d = self.QUANTIZE_DIGITS if digits is None else digits
        cell: dict[tuple[int, int, int], Vec3] = {}

        def canonical(x: float, y: float, z: float) -> Vec3:
            # +0.0 把 -0.0 归一为 0.0：字典键比较时 -0.0 与 0.0 不相等
            base = (
                int(math.floor(x * 10**d + 0.5)),
                int(math.floor(y * 10**d + 0.5)),
                int(math.floor(z * 10**d + 0.5)),
            )
            tolerance = 10 ** (-d)
            # 在 3×3 邻域里找已有顶点
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        found = cell.get((base[0] + dx, base[1] + dy, base[2] + dz))
                        if found is None:
                            continue
                        if (
                            abs(found[0] - x) <= tolerance
                            and abs(found[1] - y) <= tolerance
                            and abs(found[2] - z) <= tolerance
                        ):
                            return found
            snapped = (round(x, d) + 0.0, round(y, d) + 0.0, round(z, d) + 0.0)
            cell[base] = snapped
            return snapped

        self.triangles = [
            tuple(canonical(x, y, z) for x, y, z in tri)  # type: ignore[misc]
            for tri in self.triangles
        ]
        return self

    def to_stl_binary(self, path: Path, header: str = "x1c-printpilot") -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 先量化再写，保证读回来拓扑一致
        self.quantize()
        with path.open("wb") as fh:
            fh.write(header.encode("ascii", "replace")[:80].ljust(80, b"\0"))
            fh.write(struct.pack("<I", len(self.triangles)))
            for tri in self.triangles:
                fh.write(struct.pack("<3f", *_normal(tri)))
                for x, y, z in tri:
                    fh.write(struct.pack("<3f", x, y, z))
                # 属性字节数，Bambu Studio 忽略该字段
                fh.write(struct.pack("<H", 0))
        return path

    @classmethod
    def from_stl_binary(cls, path: Path) -> "Mesh":
        path = Path(path)
        data = path.read_bytes()
        if len(data) < 84:
            raise ValueError(f"{path} 不是合法的二进制 STL（文件太短）")
        count = struct.unpack("<I", data[80:84])[0]
        expected = 84 + count * 50
        if len(data) < expected:
            raise ValueError(
                f"{path} 数据不完整：声明 {count} 个三角形需要 {expected} 字节，"
                f"实际只有 {len(data)} 字节"
            )
        triangles: list[Tri] = []
        offset = 84
        for _ in range(count):
            # 每个三角形 50 字节：法线 3f(12) + 顶点 9f(36) + 属性 2
            values = struct.unpack("<9f", data[offset + 12:offset + 48])
            triangles.append(
                (
                    (values[0], values[1], values[2]),
                    (values[3], values[4], values[5]),
                    (values[6], values[7], values[8]),
                )
            )
            offset += 50
        # 读回来也量化一次：float32 的 2.48528 会变成 2.4852800369262695，
        # 不归一的话与内存中的值仍不相等。
        return cls(triangles).quantize()

    # ---------- 校验 ----------

    def validate(self, min_wall_mm: float = 0.8) -> list[str]:
        """检查会导致打印失败的问题，返回问题列表（空列表 = 通过）。

        只做能在纯 Python 里可靠判定、且确实影响打印的检查。
        """
        problems: list[str] = []
        if not self.triangles:
            return ["网格为空"]

        lo, hi = self.bounds()
        size = (hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2])
        for name, value in zip(("X", "Y", "Z"), size):
            if value <= 0:
                problems.append(f"{name} 方向尺寸为 {value:.3f}mm，模型是平的或反向的")
        if size[2] < min_wall_mm:
            problems.append(
                f"高度仅 {size[2]:.2f}mm，小于最小壁厚 {min_wall_mm}mm，打印机会压扁"
            )

        # 退化三角形（零面积）会让部分切片器报错或产生空洞
        degenerate = sum(1 for tri in self.triangles if _area(tri) < 1e-9)
        if degenerate:
            problems.append(f"存在 {degenerate} 个零面积三角形，可能导致切片异常")

        # 边界盒超过 X1C 成型尺寸就必须分件或缩放
        if size[0] > 256 or size[1] > 256 or size[2] > 256:
            problems.append(
                f"尺寸 {size[0]:.1f}×{size[1]:.1f}×{size[2]:.1f}mm 超过 X1C 的 256mm 上限"
            )

        problems.extend(self._watertight_problems())
        return problems

    def _watertight_problems(self) -> list[str]:
        """用"每条边恰好被两个三角形共用"判断是否封闭。

        非封闭网格切片后会漏面或直接失败，是打印失败的常见原因。
        """
        edge_count: dict[tuple[Vec3, Vec3], int] = {}
        for tri in self.triangles:
            for i in range(3):
                a = _round(tri[i])
                b = _round(tri[(i + 1) % 3])
                key = (a, b) if a <= b else (b, a)
                edge_count[key] = edge_count.get(key, 0) + 1

        boundary = sum(1 for count in edge_count.values() if count == 1)
        overused = sum(1 for count in edge_count.values() if count > 2)
        problems: list[str] = []
        if boundary:
            problems.append(
                f"网格不封闭：有 {boundary} 条边只属于一个三角形（模型有开口）"
            )
        if overused:
            problems.append(
                f"网格非法：有 {overused} 条边被 3 个以上三角形共用（自相交或重复面）"
            )
        return problems

    def describe(self) -> dict[str, object]:
        size = self.size()
        return {
            "三角形数": len(self.triangles),
            "尺寸mm": tuple(round(v, 2) for v in size),
            "体积mm3": round(self.volume(), 2),
        }


# ---------------------------------------------------------------------------
# 基础体
# ---------------------------------------------------------------------------


def box(width: float, depth: float, height: float, center: bool = False) -> Mesh:
    """长方体。默认一角在原点，center=True 时以原点为中心。"""
    x0, y0, z0 = (-width / 2, -depth / 2, -height / 2) if center else (0.0, 0.0, 0.0)
    x1, y1, z1 = x0 + width, y0 + depth, z0 + height

    v = [
        (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),  # 底面
        (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),  # 顶面
    ]
    # 顶点顺序保证法线朝外（STL 用右手法则）
    faces = [
        (0, 3, 2), (0, 2, 1),  # 底（朝 -Z）
        (4, 5, 6), (4, 6, 7),  # 顶（朝 +Z）
        (0, 1, 5), (0, 5, 4),  # 前（朝 -Y）
        (1, 2, 6), (1, 6, 5),  # 右（朝 +X）
        (2, 3, 7), (2, 7, 6),  # 后（朝 +Y）
        (3, 0, 4), (3, 4, 7),  # 左（朝 -X）
    ]
    return Mesh([(v[a], v[b], v[c]) for a, b, c in faces])


def cylinder(
    radius: float,
    height: float,
    segments: int = 48,
    center: bool = False,
) -> Mesh:
    """圆柱。默认底面在 Z=0 且轴心在原点。"""
    if segments < 3:
        raise ValueError("圆柱至少需要 3 个分段")
    z0 = -height / 2 if center else 0.0
    z1 = z0 + height
    triangles: list[Tri] = []

    bottom_center = (0.0, 0.0, z0)
    top_center = (0.0, 0.0, z1)
    ring_bottom = [
        (radius * math.cos(2 * math.pi * i / segments),
         radius * math.sin(2 * math.pi * i / segments),
         z0)
        for i in range(segments)
    ]
    ring_top = [(x, y, z1) for x, y, _ in ring_bottom]

    for i in range(segments):
        j = (i + 1) % segments
        # 侧面
        triangles.append((ring_bottom[i], ring_bottom[j], ring_top[j]))
        triangles.append((ring_bottom[i], ring_top[j], ring_top[i]))
        # 底面（法线朝 -Z）
        triangles.append((bottom_center, ring_bottom[j], ring_bottom[i]))
        # 顶面（法线朝 +Z）
        triangles.append((top_center, ring_top[i], ring_top[j]))
    return Mesh(triangles)


def circle_polygon(
    radius: float,
    segments: int = 48,
    cx: float = 0.0,
    cy: float = 0.0,
) -> list[tuple[float, float]]:
    """逆时针的圆多边形。"""
    return [
        (
            cx + radius * math.cos(2 * math.pi * i / segments),
            cy + radius * math.sin(2 * math.pi * i / segments),
        )
        for i in range(segments)
    ]


def rect_polygon(width: float, depth: float) -> list[tuple[float, float]]:
    """以原点为中心的逆时针矩形。"""
    hw, hd = width / 2, depth / 2
    return [(-hw, -hd), (hw, -hd), (hw, hd), (-hw, hd)]


def polygon_with_holes(
    outer: list[tuple[float, float]],
    holes: list[list[tuple[float, float]]],
) -> list[Vec3]:
    """把"外轮廓 + 若干孔"三角化成 2D 三角形（Z=0）。

    这是纯 Python 里最实用的挖孔方式：不做 3D 布尔运算，
    而是在截面层面直接生成正确的三角剖分，再拉伸成实体。
    结果是封闭网格（每条边恰好被两个三角形共用）。

    要求：孔完全落在外轮廓内且互不相交。外轮廓与孔都必须是逆时针。
    """
    if not outer:
        raise ValueError("外轮廓不能为空")
    tris: list[list[tuple[float, float]]] = []

    if not holes:
        # 简单扇形三角化（仅适用于凸外轮廓，例如矩形）
        for i in range(1, len(outer) - 1):
            tris.append([outer[0], outer[i], outer[i + 1]])
        return [_to3d(t) for t in tris]

    if len(holes) != 1:
        raise ValueError("当前实现只支持单个孔；多个孔请分件建模后并集")

    hole = holes[0]
    n = len(hole)
    if len(outer) != n:
        raise ValueError(
            f"外轮廓点数({len(outer)})必须与孔点数({n})相同，"
            "以便一一配对生成环形带；请用相同的分段数生成两者"
        )
    for index, point in enumerate(hole):
        if not _point_in_polygon(point, outer):
            raise ValueError(f"孔的第 {index} 个点落在轮廓外，无法挖孔")

    # 环形带：外i, 外i+1, 内i+1 / 外i, 内i+1, 内i
    for i in range(n):
        j = (i + 1) % n
        tris.append([outer[i], outer[j], hole[j]])
        tris.append([outer[i], hole[j], hole[i]])
    return [_to3d(t) for t in tris]


def extrude(
    profile_2d: list[Vec3],
    height: float,
    center_z: bool = False,
) -> Mesh:
    """把 2D 三角形列表沿 Z 拉伸成封闭实体。

    profile_2d 里每个三角形由 3 个 Z=0 的点构成（逆时针）。
    生成：底面（反向）+ 顶面 + 侧面。侧面用"边界边"配对得到。
    """
    if height <= 0:
        raise ValueError("拉伸高度必须大于 0")
    z0 = -height / 2 if center_z else 0.0
    z1 = z0 + height

    tris: list[Tri] = []
    # 有向边计数：内部边会以 (p,q) 和 (q,p) 各出现一次。
    directed: dict[tuple[tuple[float, float], tuple[float, float]], int] = {}

    for tri in profile_2d:
        (ax, ay, _az), (bx, by, _bz), (cx, cy, _cz) = tri
        a = _qp(ax, ay)
        b = _qp(bx, by)
        c = _qp(cx, cy)
        # 顶面（保留原方向，法线朝 +Z）
        tris.append(((ax, ay, z1), (bx, by, z1), (cx, cy, z1)))
        # 底面（反向，法线朝 -Z）
        tris.append(((ax, ay, z0), (cx, cy, z0), (bx, by, z0)))
        for p, q in ((a, b), (b, c), (c, a)):
            directed[(p, q)] = directed.get((p, q), 0) + 1

    # 只有"恰好出现一次且反向边不存在"的有向边才是真正的边界。
    # 之前用 count != 1 加反向判断，会把重复的同向边也当成边界，
    # 结果是同一堵墙被生成两次（表现为某条边被 4 个三角形共用）。
    for (p, q), count in directed.items():
        if count != 1:
            continue
        if directed.get((q, p), 0) != 0:
            continue
        tris.append(((p[0], p[1], z0), (q[0], q[1], z0), (q[0], q[1], z1)))
        tris.append(((p[0], p[1], z0), (q[0], q[1], z1), (p[0], p[1], z1)))
    return Mesh(tris)


def _qp(x: float, y: float, digits: int = 6) -> tuple[float, float]:
    """把 2D 点量化，避免浮点误差让本该相同的点匹配不上。"""
    return (round(x, digits), round(y, digits))


def find_t_junctions(
    profile_2d: list[Vec3],
) -> list[tuple[tuple[float, float], tuple[float, float], tuple[float, float]]]:
    """找出轮廓里"某条边界边的中间落着另一个顶点"的情况（T 型顶点）。

    T 型顶点会让这条长边与两条短边重叠，挤出时就会多生成墙面，
    是最难定位的一类几何缺陷。返回 (长边起点, 长边终点, 中间点)。
    """
    directed: dict[tuple[tuple[float, float], tuple[float, float]], int] = {}
    vertices: set[tuple[float, float]] = set()
    for tri in profile_2d:
        pts = [_qp(p[0], p[1]) for p in tri]
        vertices.update(pts)
        for i in range(3):
            key = (pts[i], pts[(i + 1) % 3])
            directed[key] = directed.get(key, 0) + 1

    boundary = [
        (p, q)
        for (p, q), count in directed.items()
        if count == 1 and directed.get((q, p), 0) == 0
    ]

    problems: list[tuple[tuple[float, float], tuple[float, float], tuple[float, float]]] = []
    for p, q in boundary:
        for vertex in vertices:
            if vertex == p or vertex == q:
                continue
            if _point_on_segment(vertex, p, q):
                problems.append((p, q, vertex))
    return problems


def _point_on_segment(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
    tol: float = 1e-6,
) -> bool:
    """判断点是否落在线段内部（不含端点）。"""
    px, py = point
    ax, ay = start
    bx, by = end
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    if abs(cross) > tol * max(1.0, abs(bx - ax) + abs(by - ay)):
        return False
    dot = (px - ax) * (bx - ax) + (py - ay) * (by - ay)
    length_sq = (bx - ax) ** 2 + (by - ay) ** 2
    return tol < dot < length_sq - tol


def _to3d(tri: list[tuple[float, float]]) -> Vec3:
    (ax, ay), (bx, by), (cx, cy) = tri
    return ((ax, ay, 0.0), (bx, by, 0.0), (cx, cy, 0.0))  # type: ignore[return-value]


def _point_in_polygon(
    point: tuple[float, float],
    polygon: list[tuple[float, float]],
) -> bool:
    """射线法判断点是否在多边形内。"""
    x, y = point
    inside = False
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            cross = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < cross:
                inside = not inside
    return inside


def plate_with_hole(
    width: float,
    depth: float,
    height: float,
    hole_diameter: float,
    segments: int = 48,
) -> Mesh:
    """矩形平板，中心一个贯穿圆孔（多孔请用 plate_with_holes）。"""
    r = hole_diameter / 2
    if r <= 0:
        raise ValueError("孔径必须大于 0")
    if r >= min(width, depth) / 2:
        raise ValueError(
            f"孔径 {hole_diameter}mm 对 {width}×{depth}mm 的板过大，孔会切到外壁"
        )
    return plate_with_holes(
        width, depth, height, [(0.0, 0.0, hole_diameter)],
        hole_segments=segments,
    )


def rect_with_holes(
    width: float,
    depth: float,
    holes: list[tuple[float, float, float]],
    hole_segments: int = 32,
    margin: float = 2.0,
) -> list[Vec3]:
    """矩形范围内挖多个圆孔，返回精确三角化的 2D 三角形。

    holes: [(中心x, 中心y, 直径), ...]，坐标以矩形中心为原点。

    做法：在每对相邻孔心之间、以及孔心与外边界之间画一条"切割线"，
    把矩形切成若干小矩形；每个小矩形要么是实心，要么正好套住一个孔。
    套住孔的小矩形用 polygon_with_holes 精确挖孔。
    这样避免做通用多边形布尔运算，同时保证结果是封闭网格。
    """
    if not holes:
        profile = polygon_with_holes(
            _rect_outline_with_segments(width, depth, 4), []
        )
        return profile

    hw, hd = width / 2, depth / 2
    for cx, cy, diameter in holes:
        if abs(cx) + diameter / 2 > hw or abs(cy) + diameter / 2 > hd:
            raise ValueError(
                f"孔(中心 {cx},{cy} 直径 {diameter})超出 {width}×{depth} 的板面"
            )

    # 每条轴上切割线放在相邻孔心的中点。切割线不穿过孔心，
    # 因此孔必须是"分离"的：相邻孔边缘之间要留出间隙，
    # 否则两个孔会落进同一格（无法用单孔环面表达）。
    for axis, name in ((0, "X"), (1, "Y")):
        # 按该轴坐标分组，只比较"确实是不同孔"且该轴坐标相邻的情况。
        # 同一坐标上的多个孔（例如上下对齐的两个孔）在轴上不构成相邻，
        # 直接比较会把间距当成 0 而误报。
        entries = sorted(
            {(round(h[axis], 6), round(h[2], 6)) for h in holes},
            key=lambda item: item[0],
        )
        for (left_pos, left_d), (right_pos, right_d) in zip(entries, entries[1:]):
            gap = right_pos - left_pos - (left_d + right_d) / 2
            if gap < 1.0:
                raise ValueError(
                    f"相邻孔在 {name} 方向太近（净间距 {gap:.2f}mm），请加大孔间距"
                )

    x_cuts = _axis_cuts([h[0] for h in holes], -hw, hw, margin)
    y_cuts = _axis_cuts([h[1] for h in holes], -hd, hd, margin)
    x_list = sorted(x_cuts)
    y_list = sorted(y_cuts)

    def cell_index(value: float, cuts: list[float]) -> int:
        for i in range(len(cuts) - 1):
            if cuts[i] <= value <= cuts[i + 1]:
                return i
        return len(cuts) - 2

    cell_of: dict[tuple[int, int], tuple[float, float, float]] = {}
    for cx, cy, diameter in holes:
        key = (cell_index(cx, x_list), cell_index(cy, y_list))
        if key in cell_of:
            raise ValueError("存在落入同一格子的孔，请加大孔间距")
        cell_of[key] = (cx, cy, diameter)

    triangles: list[Vec3] = []
    for xi in range(len(x_list) - 1):
        for yi in range(len(y_list) - 1):
            x0, x1 = x_list[xi], x_list[xi + 1]
            y0, y1 = y_list[yi], y_list[yi + 1]
            w, d = x1 - x0, y1 - y0
            if w <= 1e-9 or d <= 1e-9:
                continue
            ccx = (x0 + x1) / 2
            ccy = (y0 + y1) / 2
            hole = cell_of.get((xi, yi))
            if hole is None:
                triangles.extend(_rect_triangles(x0, y0, x1, y1))
            else:
                cx, cy, diameter = hole
                # 孔环 = 方框挖圆。方框与圆不同心，不能用"环形带"配对，
                # 否则方框的四条边会被重复使用（非法网格）。
                # 正确做法：把方框的四个角各自扇形连到对应象限的圆弧上。
                triangles.extend(
                    _frame_with_hole(x0, y0, x1, y1, cx, cy,
                                     diameter / 2, max(8, hole_segments))
                )
    return triangles


def _axis_cuts(
    values: list[float], low: float, high: float, margin: float
) -> list[float]:
    """为一条轴生成切割坐标：只放在相邻孔心的中点。

    切割线**不穿过孔心** —— 那样孔会正好压在格子边界上，孔环的射线
    可能与边界平行而无法生成。放在中点则每个孔完整落在自己的格子里。
    代价是相邻两孔之间需要留出足够的间距（由调用方校验）。
    """
    ordered = sorted(set(values))
    cuts: set[float] = {low, high}
    for left, right in zip(ordered, ordered[1:]):
        mid = (left + right) / 2
        if low < mid < high:
            cuts.add(mid)
        elif low < left < high:
            # 两个孔几乎重合时中点可能落在板外，退而取孔心附近
            cuts.add(left + margin)
    return sorted(cuts)


def _frame_with_hole(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    cx: float,
    cy: float,
    radius: float,
    segments: int,
) -> list[Vec3]:
    """方框内挖一个圆孔（圆心可不在方框中心）。

    做法：把方框边界按"与圆周相同的角度"采样，得到与圆周点数一致的
    外圈，然后外圈-内圈一一配对生成环形带。
    这样既不依赖角块拼接（容易重叠或留缝），也不依赖角点对齐，
    每条边恰好被两个三角形共用。

    注意：不能用"方框四角 + 扇形"的方式 —— 方框边与圆弧之间会留下
    两个直角区域，补它们时极易产生重叠。
    """
    if segments < 12 or segments % 4 != 0:
        raise ValueError("圆的分段数必须是 4 的倍数且不少于 12")
    # 注意用 <=：切割线穿过孔心时，孔的边缘会正好贴在格子边界上（相切），
    # 这是允许的；用严格不等式会把这种正常情形误判为越界。
    if not (cx - radius >= x0 - 1e-9 and cx + radius <= x1 + 1e-9):
        raise ValueError("孔在 X 方向超出格子范围")
    if not (cy - radius >= y0 - 1e-9 and cy + radius <= y1 + 1e-9):
        raise ValueError("孔在 Y 方向超出格子范围")

    def ray_to_box(angle: float) -> tuple[float, float]:
        """从孔心沿给定角度射出，与方框边界的交点。

        圆周的每个点都沿同一角度射到方框上，因此两点一一对应，
        配对后不会出现重叠或空隙。
        """
        dx = math.cos(angle)
        dy = math.sin(angle)
        best_t: float | None = None
        if abs(dx) > 1e-12:
            for edge_x in (x0, x1):
                t = (edge_x - cx) / dx
                if t > 0:
                    y = cy + t * dy
                    if y0 - 1e-9 <= y <= y1 + 1e-9:
                        best_t = t if best_t is None else min(best_t, t)
        if abs(dy) > 1e-12:
            for edge_y in (y0, y1):
                t = (edge_y - cy) / dy
                if t > 0:
                    x = cx + t * dx
                    if x0 - 1e-9 <= x <= x1 + 1e-9:
                        best_t = t if best_t is None else min(best_t, t)
        if best_t is None:
            raise ValueError("射线未与方框相交，孔心可能不在方框内")
        return (cx + best_t * dx, cy + best_t * dy)

    inner = [
        (
            cx + radius * math.cos(2 * math.pi * i / segments),
            cy + radius * math.sin(2 * math.pi * i / segments),
        )
        for i in range(segments)
    ]
    outer = [
        ray_to_box(2 * math.pi * i / segments) for i in range(segments)
    ]

    triangles: list[Vec3] = []
    for i in range(segments):
        j = (i + 1) % segments
        a = inner[i]
        b = inner[j]
        c = outer[j]
        d = outer[i]
        triangles.append(((a[0], a[1], 0.0), (b[0], b[1], 0.0), (c[0], c[1], 0.0)))
        triangles.append(((a[0], a[1], 0.0), (c[0], c[1], 0.0), (d[0], d[1], 0.0)))
    return triangles


def _densify_outline(
    outline: list[tuple[float, float]],
    xs: list[float],
    ys: list[float],
) -> list[tuple[float, float]]:
    """在轮廓的轴对齐线段上补入邻接坐标点，消除 T 型顶点。"""
    result: list[tuple[float, float]] = []
    n = len(outline)
    for i in range(n):
        start = outline[i]
        end = outline[(i + 1) % n]
        result.append(start)
        if abs(start[1] - end[1]) < 1e-9:  # 水平段
            low, high = sorted((start[0], end[0]))
            for value in sorted(xs):
                if low + 1e-9 < value < high - 1e-9:
                    result.append((value, start[1]))
        elif abs(start[0] - end[0]) < 1e-9:  # 竖直段
            low, high = sorted((start[1], end[1]))
            for value in sorted(ys):
                if low + 1e-9 < value < high - 1e-9:
                    result.append((start[0], value))
    return result


def _rect_triangles(
    x0: float, y0: float, x1: float, y1: float
) -> list[Vec3]:
    """实心小矩形的两个三角形。"""
    return [
        ((x0, y0, 0.0), (x1, y0, 0.0), (x1, y1, 0.0)),
        ((x0, y0, 0.0), (x1, y1, 0.0), (x0, y1, 0.0)),
    ]


def plate_with_holes(
    width: float,
    depth: float,
    height: float,
    holes: list[tuple[float, float, float]],
    hole_segments: int = 32,
) -> Mesh:
    """矩形平板，挖多个贯通圆孔，一次拉伸成型。

    这是"安装底板/多孔支架"的标准构造，避免了分块拼接带来的尺寸误差。
    """
    profile = rect_with_holes(width, depth, holes, hole_segments)
    return extrude(profile, height)


def _signed_area(polygon: list[tuple[float, float]]) -> float:
    total = 0.0
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return total / 2


def _is_convex(
    prev: tuple[float, float],
    current: tuple[float, float],
    nxt: tuple[float, float],
    counter_clockwise: bool,
) -> bool:
    ax, ay = prev
    bx, by = current
    cx, cy = nxt
    cross = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    return cross > 0 if counter_clockwise else cross < 0


def _triangle_contains(
    point: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
) -> bool:
    """判断点是否严格位于三角形内部（用重心符号法）。"""
    def cross(o, p, q):
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])

    d1 = cross(a, b, point)
    d2 = cross(b, c, point)
    d3 = cross(c, a, point)
    has_neg = d1 < 0 or d2 < 0 or d3 < 0
    has_pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (has_neg and has_pos)


def ear_clip(polygon: list[tuple[float, float]]) -> list[Vec3]:
    """把任意**简单多边形**三角化（耳切法），返回 Z=0 上的三角形列表。

    为什么需要它：零件如果由多个盒子拼出来，盒子之间必然产生共面或
    T 型顶点（"某条边被 3 个以上三角形共用"，属非法网格）。
    把整个零件定义成**一个多边形再一次性拉伸**，拓扑天然正确。
    这覆盖了一大类托盘/支架/梳状件。

    要求：多边形不自交（可以有凹角，不必是凸的）。凹多边形正是它的价值所在。
    """
    if len(polygon) < 3:
        raise ValueError("多边形至少需要 3 个顶点")

    vertices = list(polygon)
    # 统一成逆时针
    if _signed_area(vertices) < 0:
        vertices.reverse()
    counter_clockwise = True

    indices = list(range(len(vertices)))
    triangles: list[Vec3] = []
    guard = 0
    max_iterations = len(vertices) ** 2 + 16

    while len(indices) > 3:
        guard += 1
        if guard > max_iterations:
            raise ValueError("多边形三角化失败：可能存在自交或重复顶点")

        ear_found = False
        count = len(indices)
        for position in range(count):
            prev_i = indices[(position - 1) % count]
            curr_i = indices[position]
            next_i = indices[(position + 1) % count]
            prev = vertices[prev_i]
            curr = vertices[curr_i]
            nxt = vertices[next_i]

            if not _is_convex(prev, curr, nxt, counter_clockwise):
                continue
            # 耳内不能含其他顶点
            others = [
                vertices[i] for i in indices
                if i not in (prev_i, curr_i, next_i)
            ]
            if any(_triangle_contains(p, prev, curr, nxt) for p in others):
                continue

            triangles.append(
                ((prev[0], prev[1], 0.0), (curr[0], curr[1], 0.0), (nxt[0], nxt[1], 0.0))
            )
            indices.pop(position)
            ear_found = True
            break

        if not ear_found:
            raise ValueError("多边形三角化失败：找不到可用的耳（可能自交）")

    a, b, c = (vertices[i] for i in indices)
    triangles.append(((a[0], a[1], 0.0), (b[0], b[1], 0.0), (c[0], c[1], 0.0)))
    return triangles


def extrude_polygon(
    polygon: list[tuple[float, float]],
    height: float,
    center_z: bool = False,
) -> Mesh:
    """把一个简单多边形沿 Z 拉伸成封闭实体。

    这是"由单块材料构成的零件"的通用构造方式：
    整个零件一个多边形 → 一次性拉伸 → 拓扑必然正确，不会有拼接产生的问题。
    """
    profile = ear_clip(polygon)
    return extrude(profile, height, center_z=center_z)


def _rect_outline_with_segments(
    width: float,
    depth: float,
    segments: int,
    cx: float = 0.0,
    cy: float = 0.0,
) -> list[tuple[float, float]]:
    """按指定总点数在矩形边界上均匀取点（逆时针），可指定中心。

    点数必须与圆孔分段数一致，才能与圆孔一一配对成环形带。
    """
    if segments % 4 != 0:
        raise ValueError("分段数必须是 4 的倍数，矩形四条边才能均分")
    per_side = segments // 4
    hw, hd = width / 2, depth / 2
    corners = [
        (cx - hw, cy - hd),
        (cx + hw, cy - hd),
        (cx + hw, cy + hd),
        (cx - hw, cy + hd),
    ]
    points: list[tuple[float, float]] = []
    for side in range(4):
        start = corners[side]
        end = corners[(side + 1) % 4]
        for k in range(per_side):
            t = k / per_side
            points.append(
                (
                    start[0] + (end[0] - start[0]) * t,
                    start[1] + (end[1] - start[1]) * t,
                )
            )
    return points


def through_hole_box(
    width: float,
    depth: float,
    height: float,
    hole_diameter: float,
    hole_z: float | None = None,
    axis: str = "z",
) -> Mesh:
    """带贯穿圆孔的长方体（axis 指定孔轴方向）。

    hole_z 仅为兼容旧签名保留；孔沿 axis 贯穿整个形体。
    """
    mesh = plate_with_hole(width, depth, height, hole_diameter)
    if axis == "z":
        return mesh
    if axis == "x":
        return mesh.rotate_y(90.0)
    if axis == "y":
        return mesh.rotate_x(-90.0)
    raise ValueError(f"不支持的轴: {axis}")


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _cos_sin(degrees: float) -> tuple[float, float]:
    radians = math.radians(degrees)
    return math.cos(radians), math.sin(radians)


def _normal(tri: Tri) -> Vec3:
    (ax, ay, az), (bx, by, bz), (cx, cy, cz) = tri
    ux, uy, uz = bx - ax, by - ay, bz - az
    vx, vy, vz = cx - ax, cy - ay, cz - az
    nx = uy * vz - uz * vy
    ny = uz * vx - ux * vz
    nz = ux * vy - uy * vx
    length = math.sqrt(nx * nx + ny * ny + nz * nz)
    if length < 1e-12:
        return (0.0, 0.0, 0.0)
    return (nx / length, ny / length, nz / length)


def _area(tri: Tri) -> float:
    (ax, ay, az), (bx, by, bz), (cx, cy, cz) = tri
    ux, uy, uz = bx - ax, by - ay, bz - az
    vx, vy, vz = cx - ax, cy - ay, cz - az
    nx = uy * vz - uz * vy
    ny = uz * vx - ux * vz
    nz = ux * vy - uy * vx
    return math.sqrt(nx * nx + ny * ny + nz * nz) / 2


def _round(v: Vec3, digits: int = 6) -> Vec3:
    return (round(v[0], digits), round(v[1], digits), round(v[2], digits))

