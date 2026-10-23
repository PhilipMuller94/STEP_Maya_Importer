"""External Open CASCADE worker. Run with ordinary Python, not mayapy.

The mesh handoff uses millimetres and world-space body placements. No Maya
modules or CAD binaries are loaded into the other process.
"""

import argparse
import json
import math
from pathlib import Path
import sys
import time

if __package__:
    from .options import DEFAULT_OPTIONS, ImportOptions
    from .archive import write_archive
else:  # Keep direct execution of converter.py working in the external process.
    from options import DEFAULT_OPTIONS, ImportOptions
    from archive import write_archive


def _sew_surfaces(surfaces):
    """Group contiguous patches without replacing their original CAD trims."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS_Compound
    from OCP.TopTools import TopTools_IndexedMapOfShape

    def faces_of(shape):
        faces = TopExp_Explorer(shape, TopAbs_FACE)
        while faces.More():
            yield faces.Current()
            faces.Next()

    sewing = BRepBuilderAPI_Sewing(1e-7)
    for surface in surfaces:
        sewing.Add(surface)
    try:
        sewing.Perform()
        result = sewing.SewedShape()
        if result.IsNull():
            return surfaces
        # Sewing can change trims that the CAD mesher previously handled well.
        # Use its face history only to discover connected groups, then retain
        # every original surface, orientation and triangulation in those groups.
        face_map, components = TopTools_IndexedMapOfShape(), {}
        for component, body in enumerate(_bodies(result, sew_surfaces=False)):
            for face in faces_of(body):
                components[face_map.Add(face)] = component
        parents = list(range(len(surfaces)))

        def root(index):
            while parents[index] != index:
                parents[index] = parents[parents[index]]
                index = parents[index]
            return index

        owners = {}
        for index, surface in enumerate(surfaces):
            for face in faces_of(surface):
                modified = (sewing.ModifiedSubShape(face)
                            if sewing.IsModifiedSubShape(face) else face)
                for mapped in faces_of(modified):
                    component = components.get(face_map.FindIndex(mapped))
                    if component is not None:
                        previous = owners.setdefault(component, index)
                        parents[root(index)] = root(previous)
        groups = {}
        for index, surface in enumerate(surfaces):
            groups.setdefault(root(index), []).append(surface)
        bodies, builder = [], BRep_Builder()
        for group in groups.values():
            if len(group) == 1:
                bodies.append(group[0])
            else:
                compound = TopoDS_Compound()
                builder.MakeCompound(compound)
                for surface in group:
                    builder.Add(compound, surface)
                bodies.append(compound)
        return bodies
    except RuntimeError:
        pass  # An unsewable CAD patch must not prevent the original import.
    return surfaces


def _bodies(shape, sew_surfaces=True):
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID
    from OCP.TopoDS import TopoDS_Iterator

    if shape.ShapeType() in (TopAbs_SOLID, TopAbs_SHELL, TopAbs_FACE):
        yield shape
        return
    children, iterator = [], TopoDS_Iterator(shape, True, True)
    while iterator.More():
        children.append(iterator.Value())
        iterator.Next()
    patches = [i for i, child in enumerate(children)
               if child.ShapeType() == TopAbs_FACE or
               (child.ShapeType() == TopAbs_SHELL and not BRep_Tool.IsClosed_s(child))]
    if sew_surfaces and len(patches) > 1:
        connected = _sew_surfaces([children[i] for i in patches])
        patch_indices = set(patches)
        for i, child in enumerate(children):
            if i == patches[0]:
                yield from connected
            elif i not in patch_indices:
                yield from _bodies(child, sew_surfaces)
    else:
        for child in children:
            yield from _bodies(child, sew_surfaces)


def _surface_area(face):
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    properties = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, properties)
    return abs(properties.Mass())


def _pack_uv_shells(uvs, ranges):
    """Place CAD parameter charts in separate grid cells, with a small gutter."""
    if not ranges:
        return
    columns = math.ceil(math.sqrt(len(ranges)))
    cell = 1.0 / columns
    for shell, (start, end) in enumerate(ranges):
        values = uvs[start:end]
        low_u, high_u = min(u for u, v in values), max(u for u, v in values)
        low_v, high_v = min(v for u, v in values), max(v for u, v in values)
        span_u, span_v = high_u - low_u, high_v - low_v
        scale = cell * 0.96 / max(span_u, span_v, 1e-30)
        offset_u = (shell % columns + 0.5) * cell - span_u * scale * 0.5
        offset_v = (shell // columns + 0.5) * cell - span_v * scale * 0.5
        uvs[start:end] = [(offset_u + (u - low_u) * scale, offset_v + (v - low_v) * scale)
                         for u, v in values]


def _tessellate(shape, linear, angular, indexed_normals=False):
    from OCP.BRep import BRep_Tool
    from OCP.BRepLib import BRepLib_ToolTriangulatedShape
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    mesher = BRepMesh_IncrementalMesh(shape, linear, False, math.radians(angular), True)
    if not mesher.IsDone():
        raise RuntimeError("Open CASCADE could not tessellate a body")

    vertices, triangles, normals = [], [], []
    degenerate_faces = 0
    sliver_faces, sliver_area = 0, 0.0
    normal_ids, normal_map = [], {}
    uvs, uv_ids, uv_ranges = [], [], []
    vertex_map = {}
    faces = TopExp_Explorer(shape, TopAbs_FACE)
    while faces.More():
        face = TopoDS.Face_s(faces.Current())
        location = TopLoc_Location()
        mesh = BRep_Tool.Triangulation_s(face, location)
        if mesh is None or mesh.NbTriangles() == 0:
            area = _surface_area(face)
            if area <= 1e-14:  # square of OCCT's 1e-7 mm confusion tolerance
                degenerate_faces += 1
                faces.Next()
                continue
            # Some imported trims need a finer local mesh. Never silently drop
            # a face with measurable area.
            BRepMesh_IncrementalMesh(face, linear * 0.25, False, math.radians(angular) * 0.5, True)
            mesh = BRep_Tool.Triangulation_s(face, location)
            if mesh is None or mesh.NbTriangles() == 0:
                if area <= linear * linear:
                    sliver_faces += 1
                    sliver_area += area
                    faces.Next()
                    continue
                raise RuntimeError("A CAD face with area {:.8g} mm2 could not be tessellated".format(
                    area))
        BRepLib_ToolTriangulatedShape.ComputeNormals_s(face, mesh)
        transform = location.Transformation()
        reversed_face = face.Orientation() == TopAbs_REVERSED
        reverse_winding = reversed_face != transform.IsNegative()
        sign = -1.0 if reversed_face else 1.0
        identity = location.IsIdentity()
        node_count = mesh.NbNodes()
        local_ids, local_normals = [0] * (node_count + 1), [None] * (node_count + 1)
        has_uvs = mesh.HasUVNodes()
        if not has_uvs:
            # Tessellated-only faces may lack a CAD parameter chart. Use the
            # dominant local plane while retaining the surface's separate shell.
            points = [mesh.Node(i) for i in range(1, node_count + 1)]
            coordinates = [(p.X(), p.Y(), p.Z()) for p in points]
            spans = [max(p[axis] for p in coordinates) - min(p[axis] for p in coordinates)
                     for axis in range(3)]
            axes = sorted(range(3), key=spans.__getitem__, reverse=True)[:2]
        local_uv_ids = [-1] * (node_count + 1)
        uv_start = len(uvs)
        for i in range(1, node_count + 1):
            point = mesh.Node(i)
            if not identity:
                point = point.Transformed(transform)
            xyz = (point.X(), point.Y(), point.Z())
            # Share matching boundary samples within this body only. Never merge
            # vertices from separate solids or assembly instances.
            key = tuple(round(component, 7) for component in xyz)
            vertex_id = vertex_map.get(key)
            if vertex_id is None:
                vertex_id = vertex_map[key] = len(vertices)
                vertices.append(xyz)
            local_ids[i] = vertex_id
            normal = mesh.Normal(i)
            if not identity:
                normal = normal.Transformed(transform)
            value = (sign * normal.X(), sign * normal.Y(), sign * normal.Z())
            if indexed_normals:
                normal_id = normal_map.get(value)
                if normal_id is None:
                    normal_id = normal_map[value] = len(normals)
                    normals.append(value)
                local_normals[i] = normal_id
            else:
                local_normals[i] = value
        face_start = len(triangles)
        for i in range(1, mesh.NbTriangles() + 1):
            a, b, c = mesh.Triangle(i).Get()
            if reverse_winding:
                b, c = c, b
            va, vb, vc = local_ids[a], local_ids[b], local_ids[c]
            if va == vb or vb == vc or vc == va:
                # CAD poles may use duplicate samples. Welding collapses these
                # zero-area triangles; they have no polygonal surface to import.
                continue
            triangles.append((va, vb, vc))
            corner_uvs = []
            for node in (a, b, c):
                uv_id = local_uv_ids[node]
                if uv_id == -1:
                    if has_uvs:
                        uv = mesh.UVNode(node)
                        value_uv = (uv.X(), uv.Y())
                    else:
                        point = coordinates[node - 1]
                        value_uv = (point[axes[0]], point[axes[1]])
                    if not all(math.isfinite(component) for component in value_uv):
                        raise RuntimeError("A CAD surface has non-finite UV coordinates")
                    uv_id = local_uv_ids[node] = len(uvs)
                    uvs.append(value_uv)
                corner_uvs.append(uv_id)
            uv_ids.append(tuple(corner_uvs))
            if indexed_normals:
                normal_ids.append((local_normals[a], local_normals[b], local_normals[c]))
            else:
                normals.append((local_normals[a], local_normals[b], local_normals[c]))
        if len(triangles) == face_start:
            area = _surface_area(face)
            if area <= 1e-14:
                degenerate_faces += 1
            elif area <= linear * linear:
                # The CAD mesher produced only repeated points for this sliver.
                # Its area is below a square of the requested detail tolerance;
                # report the omission rather than rejecting a large assembly.
                sliver_faces += 1
                sliver_area += area
            else:
                raise RuntimeError("A CAD face with area {:.8g} mm2 collapsed during vertex welding".format(area))
        else:
            uv_ranges.append((uv_start, len(uvs)))
        faces.Next()
    if not triangles and not (degenerate_faces or sliver_faces):
        raise RuntimeError("A body contains no tessellated faces")
    _pack_uv_shells(uvs, uv_ranges)
    result = {"vertices": vertices, "triangles": triangles, "normals": normals,
              "uvs": uvs, "uv_ids": uv_ids, "uv_shells": len(uv_ranges)}
    if degenerate_faces:
        result["degenerate_faces"] = degenerate_faces
    if sliver_faces:
        result.update(sliver_faces=sliver_faces, sliver_area_mm2=sliver_area)
    if indexed_normals:
        result["normal_ids"] = normal_ids
    return result


def convert(source, destination, linear=DEFAULT_OPTIONS.linear_deflection,
            angular=DEFAULT_OPTIONS.angular_deflection, binary=False, status=None, shape_cache=None):
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_Reader

    source = Path(source).resolve()
    if not source.is_file():
        raise ValueError("STEP file does not exist: {}".format(source))
    options = ImportOptions(linear_deflection=linear, angular_deflection=angular)
    timings = {}

    def notify(message, fraction):
        if status:
            status(message, fraction)

    shape = None
    if shape_cache and Path(shape_cache).is_file():
        from OCP.BinTools import BinTools
        from OCP.TopoDS import TopoDS_Shape
        notify("Loading cached CAD surfaces...", 0.12)
        cached_shape = TopoDS_Shape()
        try:
            if BinTools.Read_s(cached_shape, str(shape_cache)) and not cached_shape.IsNull():
                shape = cached_shape
        except Exception:
            pass
    if shape is None:
        reader = STEPControl_Reader()
        Interface_Static.SetCVal_s("xstep.cascade.unit", "MM")
        notify("Reading STEP file...", 0.02)
        start = time.perf_counter()
        if reader.ReadFile(str(source)) != IFSelect_RetDone:
            raise RuntimeError("Open CASCADE could not read this STEP file")
        timings["read_s"] = time.perf_counter() - start
        reader.SetSystemLengthUnit(1.0)  # OCCT scale factor for millimetres
        roots = reader.NbRootsForTransfer()
        notify("Translating CAD surfaces...", 0.12)
        start = time.perf_counter()
        transferred = reader.TransferRoots()
        timings["transfer_s"] = time.perf_counter() - start
        if not roots or transferred != roots:
            raise RuntimeError("Only {} of {} STEP roots could be transferred".format(transferred, roots))
        shape = reader.OneShape()
        if shape.IsNull():
            raise RuntimeError("STEP transfer produced no shape")
        if shape_cache:
            from OCP.BinTools import BinTools
            path = Path(shape_cache)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            if BinTools.Write_s(shape, str(temporary)):
                temporary.replace(path)
        # Drop STEP entity/transfer bookkeeping before building a large mesh.
        del reader
    notify("Connecting loose CAD surface patches...", 0.24)
    bodies = list(_bodies(shape))
    if not bodies:
        raise RuntimeError("No surfaces or solids found; wire-only STEP files cannot produce polygon meshes")
    start = time.perf_counter()

    def meshes():
        for index, body in enumerate(bodies):
            notify("Tessellating body {} / {}...".format(index + 1, len(bodies)),
                   0.25 + 0.6 * index / len(bodies))
            mesh = _tessellate(body, options.linear_deflection, options.angular_deflection, binary)
            mesh["name"] = "Body_{:03d}".format(index + 1)
            yield mesh

    if binary:
        result = write_archive(destination, source, meshes())
        result["timings"] = dict(timings, mesh_and_write_s=time.perf_counter() - start)
        notify("Conversion complete", 0.88)
        return result
    meshes = [mesh for mesh in meshes() if mesh["triangles"]]
    if not meshes:
        raise RuntimeError("No polygon surfaces remain after excluding degenerate CAD geometry")
    payload = {"version": 1, "units": "mm", "source": str(source), "meshes": meshes}
    with open(destination, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, separators=(",", ":"), allow_nan=False)
    return payload


def main():
    parser = argparse.ArgumentParser(description="Tessellate STEP solids into mesh JSON")
    parser.add_argument("source")
    parser.add_argument("destination")
    parser.add_argument("--linear", type=float, default=DEFAULT_OPTIONS.linear_deflection, help="Deflection in millimetres")
    parser.add_argument("--angular", type=float, default=DEFAULT_OPTIONS.angular_deflection, help="Deflection in degrees")
    parser.add_argument("--binary", action="store_true", help="Write a streaming mesh archive for Maya")
    parser.add_argument("--status", help="Write conversion progress to this JSON file")
    parser.add_argument("--shape-cache", help="Reuse transferred CAD surfaces for new detail settings")
    args = parser.parse_args()
    try:
        def status(message, fraction):
            if args.status:
                path = Path(args.status)
                temporary = path.with_suffix(".tmp")
                try:
                    temporary.write_text(json.dumps({"message": message, "fraction": fraction}), encoding="utf-8")
                    temporary.replace(path)
                except OSError:
                    # Windows may briefly lock the status file while Maya reads
                    # it. Progress reporting must never abort CAD conversion.
                    pass

        result = convert(args.source, args.destination, args.linear, args.angular, args.binary, status, args.shape_cache)
    except ImportError as exc:
        print("CAD dependency unavailable: {}. Install requirements-converter.txt in this Python environment."
              .format(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print("STEP conversion failed: {}".format(exc), file=sys.stderr)
        return 1
    if args.binary:
        print(json.dumps(result))
    else:
        print("Converted {} bodies, {} triangles".format(
            len(result["meshes"]), sum(len(mesh["triangles"]) for mesh in result["meshes"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
