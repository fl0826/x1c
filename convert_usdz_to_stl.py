"""USDZ 转 STL 工具

用法:
    python convert_usdz_to_stl.py model.usdz
    python convert_usdz_to_stl.py model.usdz -o out.stl

依赖（装在 fnn 环境里）:
    conda activate fnn      # 需要 trimesh + usd-core + numpy

关于单位：USDZ 里的坐标是「USD 单位」，切片器按「毫米」读。
本脚本按文件里的 metersPerUnit 换算，并把 upAxis 统一成 Z 向上。
不做这两步会得到一个尺寸小 1000 倍、且躺在打印板上的模型。
"""

import sys
from pathlib import Path

# Windows 控制台默认不是 UTF-8，直接 print 中文/符号会崩
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def convert_usdz_to_stl(usdz_file: Path, output_stl: Path = None):
    """转换 USDZ 到 STL，返回结果字典（失败返回 None）。"""

    try:
        import numpy as np
        import trimesh
    except ImportError as exc:
        print(f"❌ 缺少依赖: {exc.name}")
        print("\n本功能需要 trimesh + numpy，请先激活已安装它们的环境:")
        print("  conda activate fnn")
        return None

    try:
        from pxr import Usd, UsdGeom
    except ImportError:
        print("❌ 缺少 USD 库（读取 USDZ 必需）")
        print("\n安装方法: pip install usd-core")
        print("\n备选方案:")
        print("  - Blender: 文件 → 导入 → USD，再导出 STL")
        print("  - 在线转换: https://products.aspose.app/3d/conversion/usdz-to-stl")
        return None

    usdz_file = Path(usdz_file)
    if output_stl is None:
        output_stl = usdz_file.with_suffix(".stl")
    output_stl = Path(output_stl)

    try:
        print(f"\n读取: {usdz_file.name}")
        print("=" * 60)

        stage = Usd.Stage.Open(str(usdz_file))
        if not stage:
            print("❌ 无法打开 USDZ 文件")
            return None

        # 单位与朝向：这两个不处理，输出就会错得离谱
        meters_per_unit = UsdGeom.GetStageMetersPerUnit(stage)   # 缺省 0.01（厘米）
        up_axis = UsdGeom.GetStageUpAxis(stage)                  # 缺省 Y
        to_mm = meters_per_unit * 1000.0

        print("✅ USD 文件已加载")
        print(f"   单位 metersPerUnit = {meters_per_unit}"
              f"（1 USD 单位 = {to_mm:.4g} mm）")
        print(f"   朝向 upAxis = {up_axis}")

        xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())

        vertex_parts: list = []
        face_parts: list = []
        vertex_total = 0

        for prim in stage.Traverse():
            if not prim.IsA(UsdGeom.Mesh):
                continue

            mesh = UsdGeom.Mesh(prim)
            points = mesh.GetPointsAttr().Get()
            indices = mesh.GetFaceVertexIndicesAttr().Get()
            counts = mesh.GetFaceVertexCountsAttr().Get()

            if points is None or indices is None or counts is None:
                continue
            if len(points) == 0 or len(indices) == 0:
                continue

            verts = np.array(points, dtype=np.float64)

            # 应用父级变换链：扫描件常把缩放/平移放在父节点上，
            # 只取原始坐标会丢掉这部分。
            world = xform_cache.GetLocalToWorldTransform(prim)
            matrix = np.array([[world[r][c] for c in range(4)] for r in range(4)])
            if not np.allclose(matrix, np.eye(4)):
                homogeneous = np.hstack([verts, np.ones((len(verts), 1))])
                verts = (homogeneous @ matrix.T)[:, :3]

            vertex_parts.append(verts)
            offset = vertex_total
            vertex_total += len(verts)

            # 多边形按扇形三角化：原实现只处理 3/4 边，>4 边会被静默丢弃
            cursor = 0
            for count in counts:
                if count < 3:
                    cursor += count
                    continue
                loop = [int(indices[cursor + k]) + offset for k in range(count)]
                for k in range(1, count - 1):
                    face_parts.append([loop[0], loop[k], loop[k + 1]])
                cursor += count

        if not vertex_parts or not face_parts:
            print("❌ 未找到网格数据")
            return None

        verts = np.vstack(vertex_parts)
        faces = np.array(face_parts, dtype=np.int64)

        # 换算到毫米
        verts = verts * to_mm

        # Y-up → Z-up：(x, y, z) → (x, -z, y)
        if str(up_axis).upper() == "Y":
            verts = np.column_stack([verts[:, 0], -verts[:, 2], verts[:, 1]])
            print("   已把 Y 轴向上转为 Z 轴向上")

        mesh = trimesh.Trimesh(vertices=verts, faces=faces, process=False)
        extents = tuple(float(v) for v in mesh.extents)

        print("\n网格信息:")
        print(f"  顶点数: {len(verts):,}")
        print(f"  面片数: {len(faces):,}")
        print(f"  尺寸:   {extents[0]:.1f} × {extents[1]:.1f} × {extents[2]:.1f} mm")

        # 尺寸自检：这一步能拦住"单位没换算"这类错误
        if max(extents) <= 0:
            print("\n❌ 尺寸为 0，无法导出（模型可能是空的或单位换算失败）")
            return None
        if max(extents) < 1.0:
            print(f"\n⚠️  最大尺寸只有 {max(extents):.3f} mm，明显偏小。")
            print("   多半是单位没换算对，直接打印会得到一个几乎看不见的薄片。")
        elif max(extents) > 256.0:
            print(f"\n⚠️  最大尺寸 {max(extents):.1f} mm 超过 X1C 的 256mm 打印范围，")
            print("   需要在切片软件里缩小。")

        print(f"\n导出: {output_stl.name}")
        mesh.export(str(output_stl))

        size_mb = output_stl.stat().st_size / 1024 / 1024
        print("\n" + "=" * 60)
        print("✅ 转换成功!")
        print("=" * 60)
        print(f"  输出: {output_stl}")
        print(f"  大小: {size_mb:.2f} MB")
        print(f"  体积: {mesh.volume / 1000:.2f} cm³")
        if not mesh.is_watertight:
            print("  ⚠️  网格不封闭（扫描件常见），切片器会自动修补")
        print("\n下一步: 在 Bambu Studio 中导入并切片")

        return {
            "output": output_stl,
            "vertices": len(verts),
            "faces": len(faces),
            "extents_mm": extents,
            "volume_cm3": mesh.volume / 1000.0,
            "meters_per_unit": meters_per_unit,
            "up_axis": str(up_axis),
        }

    except Exception as exc:  # noqa: BLE001
        import traceback

        print(f"\n❌ 转换失败: {exc}")
        traceback.print_exc()
        return None


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        print("用法: python convert_usdz_to_stl.py model.usdz [-o 输出.stl]")
        return 1

    usdz_file = Path(args[0])
    output = Path(args[1]) if len(args) > 1 else None

    if not usdz_file.exists():
        print(f"❌ 文件不存在: {usdz_file}")
        return 1

    if usdz_file.suffix.lower() != ".usdz":
        print(f"❌ 只支持 .usdz 文件（当前: {usdz_file.suffix}）")
        return 1

    return 0 if convert_usdz_to_stl(usdz_file, output) else 1


if __name__ == "__main__":
    sys.exit(main())
