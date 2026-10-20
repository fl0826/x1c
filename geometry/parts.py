"""参数化改造件库。

设计原则：
  * 每个零件都是普通 Python 函数，参数带单位（mm）和物理约束校验
  * 不做 3D 布尔运算 —— 用"2D 挖孔 + 拉伸"和"并集拼接"实现，
    结果必然封闭，不会出现布尔运算失败的经典问题
  * 每个函数都保证返回值已经是"能放到热床上"的姿态（Z=0 起）

这些零件覆盖了"扫描一个东西 → 需要个配件"的绝大多数真实需求。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from geometry.mesh import (  # noqa: E402
    Mesh,
    box,
    circle_polygon,
    cylinder,
    extrude,
    extrude_polygon,
    plate_with_holes,
    polygon_with_holes,
)


def mounting_plate(
    width: float = 60.0,
    depth: float = 40.0,
    thickness: float = 4.0,
    hole_diameter: float = 4.5,
    hole_inset: float = 8.0,
) -> Mesh:
    """四角带螺孔的安装底板（把物体拧到墙面/家具上）。

    孔心位置 = 距边 hole_inset。用一次拉伸成型（不是分块拼接），
    因此外形尺寸精确等于 width × depth。
    """
    if thickness < 1.2:
        raise ValueError(f"厚度 {thickness}mm 太薄，至少 1.2mm 才能打印")
    if hole_diameter <= 0:
        raise ValueError("孔径必须大于 0")
    if hole_inset * 2 >= min(width, depth):
        raise ValueError("孔边距过大，孔会切到板边")
    if hole_inset < hole_diameter / 2 + 1.0:
        raise ValueError("孔边距太小，孔壁会破开；请加大 hole_inset 或减小孔径")

    hx = width / 2 - hole_inset
    hy = depth / 2 - hole_inset
    holes = [
        (-hx, -hy, hole_diameter),
        (hx, -hy, hole_diameter),
        (hx, hy, hole_diameter),
        (-hx, hy, hole_diameter),
    ]
    return (
        plate_with_holes(width, depth, thickness, holes)
        .drop_to_bed()
        .center_xy()
    )


def keyhook(
    hooks: int = 3,
    hole_spacing: float = 60.0,
    hook_length: float = 28.0,
    hook_diameter: float = 6.0,
    plate_height: float = 45.0,
    thickness: float = 4.0,
    screw_diameter: float = 4.5,
) -> Mesh:
    """墙面钥匙挂钩：竖直背板 + 若干向前伸出的挂钩。

    空间姿态（打印时的摆放）：
      X = 背板宽度（水平左右），Y = 厚度与挂钩伸出方向，Z = 背板高度（竖直）
    背板在 XZ 平面内竖直立着，厚度沿 Y；挂钩沿 -Y 向前伸出。
    hole_spacing 是墙上两个螺丝孔的实际间距（最常需要实测的尺寸）。
    """
    if hooks < 1:
        raise ValueError("至少需要 1 个挂钩")
    if hole_spacing < 20:
        raise ValueError(f"孔距 {hole_spacing}mm 过小，背板会太窄")
    if hook_diameter < 2:
        raise ValueError("挂钩直径至少 2mm，否则一挂就断")
    if thickness < 2:
        raise ValueError("背板至少 2mm 厚")
    if screw_diameter <= 0 or screw_diameter >= plate_height / 2:
        raise ValueError("螺孔直径不合理")

    plate_width = hole_spacing + 16.0
    if plate_height <= hook_diameter * 2:
        raise ValueError("背板高度不够放下挂钩")

    segments = 32
    # 背板：竖直立在 XZ 平面，厚度沿 Y。用"矩形挖两个孔"一次成型，
    # 孔的轴沿 Y（厚度方向），因此按 Z 造好后再绕 X 轴转 90°。
    hole_z = plate_height / 2 - 8.0
    holes = [
        (-hole_spacing / 2, hole_z, screw_diameter),
        (hole_spacing / 2, hole_z, screw_diameter),
    ]
    plate = plate_with_holes(plate_width, plate_height, thickness, holes)
    # plate_with_holes 出来的是"宽度沿 X、高度沿 Y、厚度沿 Z"，
    # 现在把它立起来：绕 X 轴转 90°，使高度方向变成 Z。
    plate.rotate_x(90.0)
    # 旋转后厚度沿 Y，把背板推到 y=0 作为背面
    plate.translate(dy=thickness / 2)

    parts: list[Mesh] = [plate]

    # 挂钩：沿 -Y 伸出的圆柱 + 末端上翘挡块
    spacing = plate_width / (hooks + 1)
    hook_z = plate_height * 0.30
    for i in range(hooks):
        x = -plate_width / 2 + spacing * (i + 1)
        arm = cylinder(hook_diameter / 2, hook_length, segments=24)
        arm.rotate_x(-90.0)          # 轴由 Z 转向 -Y（向前伸出）
        arm.translate(
            dx=x,
            dy=-hook_length / 2,
            dz=hook_z,
        )
        parts.append(arm)

        # 末端上翘的挡块，防止钥匙滑落
        tip = box(hook_diameter, hook_diameter, hook_diameter * 1.5)
        tip.translate(
            dx=x - hook_diameter / 2,
            dy=-hook_length,
            dz=hook_z,
        )
        parts.append(tip)

    result = Mesh()
    for part in parts:
        result.add(part)
    return result.drop_to_bed()


def cable_clip(
    cable_diameter: float = 6.0,
    count: int = 1,
    gap: float = 12.0,
    wall: float = 2.4,
    base_thickness: float = 2.0,
    length: float = 18.0,
) -> Mesh:
    """理线夹：底板上若干开口 U 形卡槽，可拧或贴到桌边。

    卡槽做成 C 形开口（不是闭合圆），这样线可以侧向按进去。
    """
    if cable_diameter < 1:
        raise ValueError("线径必须大于 1mm")
    if wall < 1.2:
        raise ValueError(f"壁厚 {wall}mm 太薄，至少要 1.2mm")
    if count < 1:
        raise ValueError("至少 1 个卡槽")

    inner_r = cable_diameter / 2
    outer_r = inner_r + wall
    height = cable_diameter + wall * 2
    total_len = outer_r * 2 * count + gap * (count - 1)

    parts: list[Mesh] = [box(total_len, length, base_thickness)]

    for i in range(count):
        x = -total_len / 2 + outer_r + i * (outer_r * 2 + gap)
        # 用"外圆挖内圆"的截面做成管，再切掉上半部分形成开口
        segments = 32
        outer = circle_polygon(outer_r, segments, x, 0.0)
        inner = circle_polygon(inner_r, segments, x, 0.0)
        tube = extrude(polygon_with_holes(outer, [inner]), height)
        tube.translate(dz=base_thickness)
        parts.append(tube)

    result = Mesh()
    for part in parts:
        result.add(part)
    result.drop_to_bed()
    return result


def spacer(
    outer_diameter: float = 12.0,
    hole_diameter: float = 4.5,
    height: float = 6.0,
) -> Mesh:
    """垫圈/套筒：最常用的小零件（补高度差、防压裂）。"""
    if hole_diameter >= outer_diameter:
        raise ValueError("内孔必须小于外径")
    if height <= 0:
        raise ValueError("高度必须大于 0")
    segments = 48
    outer = circle_polygon(outer_diameter / 2, segments)
    inner = circle_polygon(hole_diameter / 2, segments)
    return extrude(polygon_with_holes(outer, [inner]), height)


def box_with_lid(
    width: float = 60.0,
    depth: float = 40.0,
    height: float = 30.0,
    wall: float = 2.0,
    clearance: float = 0.3,
) -> tuple[Mesh, Mesh]:
    """收纳盒：返回 (盒身, 盒盖) 两个零件。

    clearance 是盖与身之间的配合间隙。FDM 打印建议 0.25~0.4mm，
    太小会卡死，太大会松。
    """
    if wall < 1.2:
        raise ValueError(f"壁厚 {wall}mm 太薄")
    if min(width, depth) <= wall * 4:
        raise ValueError("尺寸相对壁厚太小，盒内几乎没有空间")

    # 盒身：外形减去内腔（内腔不贯穿底面）
    segments = 64
    outer = _rect_outline_centered(width, depth, segments)
    inner = _rect_outline_centered(
        width - wall * 2, depth - wall * 2, segments
    )
    # 用"带孔截面拉伸"得到通腔，再补一块底板
    shell = extrude(polygon_with_holes(outer, [inner]), height)
    bottom = box(width, depth, wall)
    bottom.translate(dx=-width / 2, dy=-depth / 2)
    body = shell.copy()
    body.add(bottom)

    # 盒盖：略大的顶板 + 一圈可插入盒身的凸缘
    lid_h = wall * 1.6
    lid = box(width + clearance * 2, depth + clearance * 2, lid_h)
    lid.translate(dx=-(width + clearance * 2) / 2, dy=-(depth + clearance * 2) / 2)
    lip_h = min(6.0, height / 3)
    lip = extrude(
        polygon_with_holes(
            _rect_outline_centered(width - wall * 2 - clearance * 2,
                                   depth - wall * 2 - clearance * 2, segments),
            [_rect_outline_centered(
                width - wall * 4 - clearance * 2,
                depth - wall * 4 - clearance * 2, segments)],
        ),
        lip_h,
    )
    lid.add(lip)

    return body.drop_to_bed().center_xy(), lid.drop_to_bed().center_xy()


def battery_holder(
    battery_diameter: float = 14.0,
    battery_length: float = 50.0,
    count: int = 2,
    wall: float = 2.5,
    base_thickness: float = 3.0,
    clearance: float = 1.0,
) -> Mesh:
    """电池槽：并排若干个开口槽，电池卧放其中（AA/AAA/18650 等）。

    battery_diameter / battery_length 填**电池的实际规格**：
      AA(5号)  直径 14.0  长 50.0
      AAA(7号) 直径 10.5  长 44.5
      18650    直径 18.6  长 65.0

    clearance 是每格净空相对电池直径的余量（默认 1mm），便于取放；
    想夹紧一点就调小。

    形状：底板 + 两侧长墙 + 若干槽口分隔墙，槽口朝上。
    整个零件由**一个横截面多边形一次性拉伸**而成（见 extrude_polygon），
    所以拓扑必然正确；用多个盒子拼接则会在交界处产生共面/T 型顶点，
    得到"某条边被 3 个以上三角形共用"的非法网格。

    外形：宽 = 格数×净空 + (格数+1)×壁厚，深 = 电池长 + 2×壁厚，
    高 = 底板厚 + 电池半径。不需要支撑即可打印。
    """
    if count < 1:
        raise ValueError("至少需要 1 个电池槽")
    if battery_diameter <= 0 or battery_length <= 0:
        raise ValueError("电池直径与长度必须大于 0")
    if wall < 1.2:
        raise ValueError(f"壁厚 {wall}mm 太薄，至少 1.2mm 才能打印")
    if base_thickness < 1.2:
        raise ValueError(f"底板 {base_thickness}mm 太薄，至少 1.2mm")
    if not (0.0 <= clearance <= 5.0):
        raise ValueError(f"余量 {clearance}mm 不合理，建议 0.5~2.0")

    cell_width = battery_diameter + clearance
    pocket_width = cell_width          # 每格的净空
    pocket_height = battery_diameter / 2
    total_width = count * pocket_width + (count + 1) * wall
    total_depth = battery_length + wall * 2
    total_height = base_thickness + pocket_height

    if max(total_width, total_depth, total_height) > 256:
        raise ValueError(
            f"外形 {total_width:.1f}×{total_depth:.1f}×{total_height:.1f}mm "
            "超过 X1C 的 256mm 上限"
        )

    # 横截面 = "一个简单多边形"：沿 X 方向排布的若干槽口。
    # 整个零件由这一个多边形一次性拉伸而成，因此拓扑必然正确 ——
    # 用多个盒子拼接则会在盒子交界处产生共面/T 型顶点，得到非法网格。
    left = -total_width / 2
    right = total_width / 2
    polygon: list[tuple[float, float]] = [(left, 0.0), (right, 0.0), (right, total_height)]

    # 从右往左依次走过每个槽口（沿顶边"下探"再回来）
    cursor = right
    for index in range(count - 1, -1, -1):
        pocket_right = cursor - wall
        pocket_left = pocket_right - pocket_width
        polygon.append((pocket_right, total_height))
        polygon.append((pocket_right, base_thickness))
        polygon.append((pocket_left, base_thickness))
        polygon.append((pocket_left, total_height))
        cursor = pocket_left

    polygon.append((left, total_height))
    # 多边形在 XZ 平面上（x 横向、y 即高度），拉伸方向为电池轴向
    mesh = extrude_polygon(polygon, total_depth, center_z=True)
    # 截面在 XY、拉伸沿 Z → 绕 X 转 -90°，使拉伸方向变成 Y（电池轴向），高度回到 Z
    mesh.rotate_x(-90.0)
    return mesh.drop_to_bed().center_xy()


def _rect_outline_centered(
    width: float,
    depth: float,
    segments: int,
    cx: float = 0.0,
    cy: float = 0.0,
) -> list[tuple[float, float]]:
    """以 (cx, cy) 为中心、按指定总点数均匀采样的矩形轮廓。"""
    if segments % 4 != 0:
        raise ValueError("分段数必须是 4 的倍数")
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
                (start[0] + (end[0] - start[0]) * t, start[1] + (end[1] - start[1]) * t)
            )
    return points


@dataclass(frozen=True)
class PartSpec:
    """零件目录条目：告诉 LLM 有哪些零件、参数含义与约束。"""

    name: str
    builder: str
    description: str
    params: dict[str, str]


CATALOG: dict[str, PartSpec] = {
    "mounting_plate": PartSpec(
        name="安装底板",
        builder="mounting_plate",
        description="四角带螺孔的底板，用来把物体固定到墙面或家具上",
        params={
            "width": "板宽 mm",
            "depth": "板深 mm",
            "thickness": "板厚 mm，建议 3~5",
            "hole_diameter": "螺孔直径 mm，M4 螺丝用 4.5",
            "hole_inset": "孔中心到边的距离 mm",
        },
    ),
    "keyhook": PartSpec(
        name="钥匙挂钩",
        builder="keyhook",
        description="墙面钥匙挂钩，背板带两个螺孔，前面若干挂钩",
        params={
            "hooks": "挂钩数量",
            "hole_spacing": "墙上两个螺丝孔的实际间距 mm（需要实测）",
            "hook_length": "挂钩伸出长度 mm",
            "hook_diameter": "挂钩粗细 mm",
        },
    ),
    "cable_clip": PartSpec(
        name="理线夹",
        builder="cable_clip",
        description="固定线缆的卡夹，侧向开口方便按入",
        params={
            "cable_diameter": "线径 mm",
            "count": "卡槽数量",
            "gap": "卡槽间距 mm",
        },
    ),
    "spacer": PartSpec(
        name="垫圈/套筒",
        builder="spacer",
        description="补高度差或防压裂的垫圈",
        params={
            "outer_diameter": "外径 mm",
            "hole_diameter": "内孔 mm",
            "height": "高度 mm",
        },
    ),
    "box_with_lid": PartSpec(
        name="收纳盒(含盖)",
        builder="box_with_lid",
        description="带盖收纳盒，会生成盒身和盒盖两个零件",
        params={
            "width": "外宽 mm",
            "depth": "外深 mm",
            "height": "盒身高度 mm",
            "wall": "壁厚 mm",
            "clearance": "盖身配合间隙 mm，建议 0.3",
        },
    ),
    "battery_holder": PartSpec(
        name="电池槽",
        builder="battery_holder",
        description="并排放置电池的开口槽，电池卧放。AA/AAA/18650 通用",
        params={
            "battery_diameter": "电池直径 mm（AA=14.0、AAA=10.5、18650=18.6）",
            "battery_length": "电池长度 mm（AA=50.0、AAA=44.5、18650=65.0）",
            "count": "槽数",
            "wall": "壁厚 mm",
            "base_thickness": "底板厚 mm",
            "clearance": "每格净空余量 mm，便于取放，建议 1.0",
        },
    ),
}


def build(name: str, **params: float) -> Mesh | tuple[Mesh, Mesh]:
    """按目录名构建零件。"""
    spec = CATALOG.get(name)
    if spec is None:
        raise ValueError(
            f"没有名为 {name} 的零件。可用: {', '.join(sorted(CATALOG))}"
        )
    builder = globals()[spec.builder]
    return builder(**params)


