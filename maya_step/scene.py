"""Maya API 2.0 scene creation. Public import is an undoable plug-in command."""

from itertools import chain, repeat
import math
import re

from maya import cmds
from maya.api import OpenMaya as om


def _name(text):
    text = re.sub(r"[^A-Za-z0-9_]", "_", text)
    return ("STEP_" + text) if not text or text[0].isdigit() else text


def create_geometry(payload, options, progress=None):
    from pathlib import Path

    root_transform = om.MFnTransform()
    root = root_transform.create()
    try:
        root_fn = om.MFnDependencyNode(root)
        root_fn.setName(_name(Path(payload["source"]).stem) + "_STEP")
        source_attr = om.MFnTypedAttribute().create("stepSource", "stepSrc", om.MFnData.kString)
        root_fn.addAttribute(source_attr)
        root_fn.findPlug("stepSource", False).setString(payload["source"])
        selection = om.MSelectionList()
        selection.add("initialShadingGroup")
        shading = om.MFnSet(selection.getDependNode(0))
        target_up = cmds.upAxis(query=True, axis=True).upper()
        factor = 0.1 * options.scale  # Maya API positions are always centimetres
        # Apply units and up-axis once on the group, rather than transforming
        # millions of points and normal vectors in Python.
        root_transform.setScale((factor, factor, factor))
        if options.source_up != target_up:
            angle = -math.pi / 2 if options.source_up == "Z" else math.pi / 2
            root_transform.setRotation(om.MEulerRotation(angle, 0, 0), om.MSpace.kTransform)
        total = len(payload["meshes"])
        for index, mesh in enumerate(payload["meshes"]):
            if progress:
                progress("Creating body {} / {}...".format(index + 1, total),
                         0.9 + 0.1 * index / total)
            parent = om.MFnTransform().create(root)
            om.MFnDependencyNode(parent).setName(_name(mesh["name"]))
            points = om.MPointArray(mesh["vertices"])
            counts = [3] * len(mesh["triangles"])
            connects = list(chain.from_iterable(mesh["triangles"]))
            mesh_fn = om.MFnMesh()
            mesh_fn.create(points, counts, connects, parent=parent)
            mesh_fn.setName(om.MFnDependencyNode(parent).name() + "Shape")
            if "normal_ids" in mesh:
                # The packed handoff shares normals across triangle corners.
                # Transform each unique normal once, then reuse its vector at
                # triangle corners for Maya's face-vertex normal API.
                pool = [om.MVector(*normal) for normal in mesh["normals"]]
                normals = [pool[i] for ids in mesh["normal_ids"] for i in ids]
            else:
                # Compatibility with the standalone JSON converter.
                normal_vectors, normals = {}, []
                for triangle in mesh["normals"]:
                    for normal in triangle:
                        key = tuple(normal)
                        vector = normal_vectors.get(key)
                        if vector is None:
                            vector = normal_vectors[key] = om.MVector(*normal)
                        normals.append(vector)
            face_ids = list(chain.from_iterable(repeat(i, 3) for i in range(len(counts))))
            mesh_fn.setFaceVertexNormals(normals, face_ids, connects)
            if mesh.get("uvs"):
                mesh_fn.setUVs([uv[0] for uv in mesh["uvs"]], [uv[1] for uv in mesh["uvs"]], "map1")
                mesh_fn.assignUVs(counts, list(chain.from_iterable(mesh["uv_ids"])), "map1")
            shading.addMember((mesh_fn.getPath(), om.MObject.kNullObj))
        return root
    except BaseException:
        removal = om.MDagModifier()
        removal.deleteNode(root)
        removal.doIt()
        raise


def ensure_plugin_loaded():
    """Shared loader for the dialog and programmatic imports."""
    from .bridge import ROOT

    plugin = str(ROOT / "plug-ins" / "step_importer.py")
    if not cmds.pluginInfo(plugin, query=True, loaded=True):
        cmds.loadPlugin(plugin, quiet=True)


def import_step(path, options=None):
    """Import a STEP file and return the root transform's full DAG path."""
    from .options import DEFAULT_OPTIONS

    ensure_plugin_loaded()
    return cmds.stepImportFile(str(path), (options or DEFAULT_OPTIONS).to_string())
