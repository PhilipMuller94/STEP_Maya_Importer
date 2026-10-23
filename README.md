# Maya STEP mesh importer

A Python plug-in that imports `.step` and `.stp` solids and surfaces as editable,
triangulated Maya polygon meshes. Includes a native import dialog, a **STEP** menu,
a **File > Import > STEP Mesh** translator, and undo/redo.

## Set up the CAD converter once

Run in PowerShell from this project directory, using **ordinary 64-bit Python
3.10 through 3.14**, not Maya's `mayapy`:

```powershell
python -m venv .venv-step
.\.venv-step\Scripts\python.exe -m pip install -r requirements-converter.txt
```

The CAD kernel runs in this separate environment, keeping its native libraries
out of Maya's process. Keep the project folder and `.venv-step` together. On
macOS/Linux use `.venv-step/bin/python` in the commands above.

## Run in Maya

Paste this into Maya's Script Editor, **Python** tab:

```python
import runpy
from maya import cmds

script = cmds.fileDialog2(
    fileMode=1, caption="Select this project's main.py",
    fileFilter="Python files (*.py)",
)
if script:
    runpy.run_path(script[0], run_name="__main__")
```

Select `main.py` from the downloaded project folder. In the import dialog,
choose a STEP file, set the detail tolerances, and click **Import geometry**.
The importer selects the resulting group; press **F** to frame it. A **STEP** menu
is also added for later use. Cancel/Esc stops conversion before scene creation.
Cancellation is also checked between bodies during mesh creation; partial
geometry is removed if the import is cancelled.

After updating this project's Python files, restart Maya once to load the new
modules, then use the same launch command.

Alternatively, load `plug-ins/step_importer.py` with Maya's Plug-in Manager
(Browse), then use **File > Import** with file type **STEP Mesh**. File > Import
uses the defaults below; use the custom dialog for adjustable settings. Opening
or referencing STEP files is not supported. Save the result as a Maya scene.

## Settings and geometry

| Setting | Default | Meaning |
| --- | --- | --- |
| Detail tolerance | 0.1 mm | Maximum chordal deflection requested from the CAD mesher; lower is finer |
| Angle tolerance | 20 degrees | Angular deflection; lower is finer |
| Scale multiplier | 1.0 | Additional scale after converting physical STEP units |
| CAD up axis | Z | Rotate into the current Maya Y-up or Z-up coordinate system |
| Reuse cached geometry | On | Reuse the converted mesh, or the translated CAD surfaces when detail settings change |
| Conversion timeout | 600 seconds | Available through the Python API |

STEP units are converted to millimetres in the worker. The parent group's scale
and rotation convert to Maya centimetres and the current up axis, avoiding a
Python transformation of every point. Physical size is independent of the
scene's display units. Each solid becomes a separate mesh under one group.
Loose surface patches within the same source compound are grouped by
coincident boundaries using a fixed 0.0000001 mm tolerance before tessellation.
Each connected group becomes one Maya object, with separate UV shells
for its CAD surfaces. Disconnected patches, closed shells, solids, and surfaces
in different source compounds remain separate objects. CAD sewing is used only
to discover connectivity; the original surfaces and trims are tessellated.
Grouping does not fill gaps or combine the whole assembly.
Assembly placements are baked into vertices; the original assembly hierarchy,
part names, colors, materials, PMI, and CAD construction history are not
imported. CAD boundaries remain sharp through face-vertex normals; curved faces
have smooth normals. Matching vertex positions within a body are welded using
coordinates rounded to 0.0000001 mm; watertight topology depends on the input and
the tessellation. Wire-only entities have no polygon representation and are
omitted. A file with no surfaces fails with a clear error.

UVs are created automatically in the **map1** UV set. Each CAD face/surface gets
an independent UV chart, with shared UVs between its connected triangles. Geometry stays
welded across surfaces while their UV boundaries remain separate; periodic
surfaces retain their parameter seams. Surface charts are placed in separate grid cells
inside 0-1 UV space **per mesh**, with a small gutter. Separate mesh objects each
have their own layout. A connected surface chart forms one UV shell. If a CAD
face's tessellation has disconnected patches, those patches retain separate UV
islands inside that chart; UVs are not artificially joined across the gaps.

The charts use the CAD surface's parameter coordinates. Their aspect in parameter
space is preserved during packing; equal texel density and distortion-free
artistic unwrapping are not guaranteed. Tessellated surfaces without parameter
coordinates receive a dominant-plane projection as a fallback. STEP material or
texture assignments are not imported.

This produces triangle meshes, not Maya NURBS or editable CAD features. Large
assemblies and very small tolerances can require substantial RAM; mesh creation
is synchronous after conversion, with cancellation checks between bodies.
Zero-area faces are omitted and reported. If the CAD mesher cannot represent a
sliver or produces only collapsed triangles, that face can be omitted when its
area is no greater than the square of the selected linear tolerance. The import
reports the number and total area of these slivers. Bodies containing only such
degenerate geometry are omitted and reported. Larger face failures abort the
import and remove any partial scene geometry.

## Large files and caching

The importer writes a binary mesh archive instead of expanding every triangle's
normals into JSON. The worker writes one body at a time, and Maya reads one body
at a time. This reduces peak memory and intermediate file size. Redraw is
suspended during interactive mesh creation.

The first conversion of a large STEP file can still take minutes because the CAD
kernel must parse and translate its surfaces. With **Reuse cached geometry**
enabled, subsequent imports skip that work. Changing mesh tolerances reuses a
separate cache of the translated CAD surfaces; changing scale or up axis reuses
the same mesh cache.

Mesh caches rebuild automatically after converter updates, including UV support
and surface grouping. The translated CAD cache remains reusable, so the long STEP
translation does not need to run again. Existing objects in a Maya scene are
unchanged; restart Maya and reimport to apply these updates.

Caches are local to `target/step-cache/`. Keys include the source's absolute path,
size and modification time, converter settings and code, and pinned CAD package
requirements. A changed source or detail setting triggers the appropriate
rebuild. Uncheck **Reuse cached geometry**, or pass `ImportOptions(use_cache=False)`,
to force a fresh conversion without storing its result. Cache files may be
deleted from that directory when no imports are running. They can take hundreds
of megabytes for a large assembly.

## Python API

```python
from maya_step.options import ImportOptions
from maya_step.scene import import_step

root = import_step(
    "path/to/part.step",  # Replace with your STEP file's path.
    ImportOptions(linear_deflection=0.05, angular_deflection=15,
                  source_up="Z", scale=1.0, timeout=900),
)
```

Choose another converter interpreter in the dialog, or set the environment
variable `STEP_IMPORT_PYTHON` to its absolute executable path before starting
Maya. Resolution order: environment variable, dialog's saved preference, project
`.venv-step`. The interpreter must contain the packages in
`requirements-converter.txt`. Do not install a package named `OCP`; the required
distribution is `cadquery-ocp`.

To tessellate outside Maya:

```powershell
.\.venv-step\Scripts\python.exe maya_step/converter.py part.step mesh.json --linear 0.1 --angular 20
```

## Distribution

The source distribution contains `main.py`, `maya_step/`, `plug-ins/`,
`requirements-converter.txt`, `README.md`, and `.gitignore`. Keep this folder
structure when installing or sharing the plug-in.

Each installation creates its own converter environment using the setup commands
above. Virtual environments, generated geometry caches, Python bytecode, and
editor settings are excluded from the source distribution. Caches are created
automatically when importing files.

Built against Autodesk's [file translator API](https://help.autodesk.com/cloudhelp/2024/ENU/MAYA-API-REF/cpp_ref/class_m_px_file_translator.html)
and [Open CASCADE meshing](https://www.occt3d.com/dev/doc/refman/html/class_b_rep_mesh___incremental_mesh.html),
using the [CadQuery OCP bindings](https://pypi.org/project/cadquery-ocp/7.9.3.1.1/).
Surface connectivity analysis uses Open CASCADE's
[sewing API](https://www.occt3d.com/dev/doc/refman/html/class_b_rep_builder_a_p_i___sewing.html).
