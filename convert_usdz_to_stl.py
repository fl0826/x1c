"""USDZ → STL。

用法:
    python convert_usdz_to_stl.py model.usdz
    输出: model.stl

处理三件容易错的事：
  * metersPerUnit：iPhone 扫描出来的单位是米，不换算会小 1000 倍
  * upAxis：Y 向上的模型要转成 Z 向上（打印机的方向）
  * 多边形：4 边以上要扇形三角化，否则 trimesh 会丢面
"""

import sys
from pathlib import Path

# Windows 控制台编码（幂等：重复 import 也不会崩）
if sys.platform == 'win32':
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass


def convert_usdz_to_stl(usdz_file, output_stl=None):
    """转换并返回 {"stl", "vertices", "faces", "extents_mm"}；失败返回 None。"""
    path = Path(usdz_file)
    if not path.is_file():
        print(f"❌ 文件不存在: {path}")
        return None

    try:
        import numpy as np
        import trimesh
    except ImportError:
        print("❌ 缺少 trimesh：pip install trimesh")
        return None

    try:
        from pxr import Gf, Usd, UsdGeom
    except ImportError:
        print("❌ 缺少 USD 库：pip install usd-core")
        return None

    if output_stl is None:
        output_stl = path.with_suffix('.stl')

    stage = Usd.Stage.Open(str(path))
    if not stage:
        print(f"❌ 打不开: {path}")
        return None

    # 单位：米 → 毫米
    meters = UsdGeom.GetStageMetersPerUnit(stage)
    scale = (meters if meters else 0.01) * 1000.0
    up_is_y = UsdGeom.GetStageUpAxis(stage) == "Y"
    cache = UsdGeom.XformCache(Usd.TimeCode.Default())

    vertices: list[tuple[float, float, float]] = []
    faces: list[list[int]] = []

    for prim in stage.Traverse():
        if not prim.IsA(UsdGeom.Mesh):
            continue
        mesh = UsdGeom.Mesh(prim)
        points = mesh.GetPointsAttr().Get()
        indices = mesh.GetFaceVertexIndicesAttr().Get()
        counts = mesh.GetFaceVertexCountsAttr().Get()
        if not points or not indices or not counts:
            continue

        matrix = cache.GetLocalToWorldTransform(prim)
        offset = len(vertices)
        for point in points:
            world = matrix.Transform(Gf.Vec3d(point[0], point[1], point[2]))
            x, y, z = world[0], world[1], world[2]
            if up_is_y:                      # Y 向上 → Z 向上
                x, y, z = x, -z, y
            vertices.append((x * scale, y * scale, z * scale))

        cursor = 0
        for count in counts:
            polygon = [indices[cursor + i] + offset for i in range(count)]
            for k in range(1, count - 1):    # 扇形三角化
                faces.append([polygon[0], polygon[k], polygon[k + 1]])
            cursor += count

    if not vertices or not faces:
        print("❌ 没有找到网格数据")
        return None

    mesh = trimesh.Trimesh(vertices=np.array(vertices), faces=np.array(faces))
    output_stl = Path(output_stl)
    mesh.export(str(output_stl))
    extents = [round(float(v), 2) for v in mesh.extents]

    print(f"✅ {path.name} → {output_stl.name}")
    print(f"   顶点 {len(vertices):,} · 面片 {len(faces):,}")
    print(f"   尺寸 {extents[0]} × {extents[1]} × {extents[2]} mm")
    return {
        "stl": str(output_stl),
        "vertices": len(vertices),
        "faces": len(faces),
        "extents_mm": extents,
    }


def main():
    if len(sys.argv) < 2:
        print("用法: python convert_usdz_to_stl.py model.usdz")
        return 1

    source = Path(sys.argv[1])
    if not source.exists():
        print(f"❌ 文件不存在: {source}")
        return 1
    if source.suffix.lower() not in ('.usdz', '.usda', '.usd'):
        print(f"❌ 只支持 .usdz / .usda / .usd")
        return 1

    return 0 if convert_usdz_to_stl(source) else 1


if __name__ == "__main__":
    sys.exit(main())
