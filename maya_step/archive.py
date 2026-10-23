"""Streaming mesh handoff using numeric buffers, without pickle or Maya/CAD imports."""

from array import array
from itertools import chain
import json
from pathlib import Path
import struct
import sys

MAGIC = b"MAYASTEP\x03"
HEADER = struct.Struct("<I")


def _header(stream, data):
    encoded = json.dumps(data, separators=(",", ":"), allow_nan=False).encode("utf-8")
    stream.write(HEADER.pack(len(encoded)))
    stream.write(encoded)


def _read_header(stream):
    raw = stream.read(HEADER.size)
    if len(raw) != HEADER.size:
        raise ValueError("Incomplete STEP mesh archive")
    length, = HEADER.unpack(raw)
    if length > 1024 * 1024:
        raise ValueError("Invalid STEP mesh archive header")
    if not length:
        return None
    raw = stream.read(length)
    if len(raw) != length:
        raise ValueError("Incomplete STEP mesh archive header")
    return json.loads(raw)


def _buffer(stream, kind, rows):
    values = array(kind, chain.from_iterable(rows))
    if sys.byteorder != "little":
        values.byteswap()
    values.tofile(stream)


def write_archive(path, source, meshes):
    """Write each body immediately so the worker never retains the entire model."""
    count, triangles, degenerate_faces, sliver_faces, sliver_area, omitted = 0, 0, 0, 0, 0.0, 0
    with open(path, "wb") as stream:
        stream.write(MAGIC)
        _header(stream, {"source": str(source), "units": "mm", "version": 3})
        for mesh in meshes:
            degenerate_faces += mesh.get("degenerate_faces", 0)
            sliver_faces += mesh.get("sliver_faces", 0)
            sliver_area += mesh.get("sliver_area_mm2", 0.0)
            if not mesh["triangles"]:
                omitted += 1
                continue
            _header(stream, {"name": mesh["name"], "vertices": len(mesh["vertices"]),
                             "triangles": len(mesh["triangles"]), "normals": len(mesh["normals"]),
                             "uvs": len(mesh.get("uvs", ())), "uv_shells": mesh.get("uv_shells", 0),
                             "degenerate_faces": mesh.get("degenerate_faces", 0),
                             "sliver_faces": mesh.get("sliver_faces", 0),
                             "sliver_area_mm2": mesh.get("sliver_area_mm2", 0.0)})
            _buffer(stream, "d", mesh["vertices"])
            _buffer(stream, "I", mesh["triangles"])
            _buffer(stream, "d", mesh["normals"])
            _buffer(stream, "I", mesh["normal_ids"])
            if mesh.get("uvs"):
                _buffer(stream, "d", mesh["uvs"])
                _buffer(stream, "I", mesh["uv_ids"])
            count += 1
            triangles += len(mesh["triangles"])
        stream.write(HEADER.pack(0))
        if not count:
            raise ValueError("No polygon surfaces remain after excluding degenerate CAD geometry")
        result = {"bodies": count, "triangles": triangles}
        if degenerate_faces:
            result["degenerate_faces"] = degenerate_faces
        if sliver_faces:
            result.update(sliver_faces=sliver_faces, sliver_area_mm2=sliver_area)
        if omitted:
            result["omitted_bodies"] = omitted
        _header(stream, result)
    return result


class MeshArchive:
    """Read one body at a time; scanning headers does not load its numeric data."""

    def __init__(self, path, entries):
        self.path = Path(path)
        self.entries = entries

    def __len__(self):
        return len(self.entries)

    def __iter__(self):
        with self.path.open("rb") as stream:
            for offset, header in self.entries:
                stream.seek(offset)
                mesh = {"name": header["name"]}
                if header.get("degenerate_faces"):
                    mesh["degenerate_faces"] = header["degenerate_faces"]
                if header.get("sliver_faces"):
                    mesh.update(sliver_faces=header["sliver_faces"], sliver_area_mm2=header["sliver_area_mm2"])
                buffers = [
                        ("vertices", header["vertices"], "<ddd", 24),
                        ("triangles", header["triangles"], "<III", 12),
                        ("normals", header["normals"], "<ddd", 24),
                        ("normal_ids", header["triangles"], "<III", 12)]
                if header["uvs"]:
                    buffers.extend((("uvs", header["uvs"], "<dd", 16),
                                    ("uv_ids", header["triangles"], "<III", 12)))
                    mesh["uv_shells"] = header["uv_shells"]
                for key, count, format_string, size in buffers:
                    raw = stream.read(count * size)
                    if len(raw) != count * size:
                        raise ValueError("Incomplete STEP mesh archive data")
                    mesh[key] = list(struct.iter_unpack(format_string, raw))
                yield mesh


def read_archive(path, lazy=True):
    path = Path(path)
    length = path.stat().st_size
    entries, triangles, degenerate_faces, sliver_faces, sliver_area = [], 0, 0, 0, 0.0
    with path.open("rb") as stream:
        if stream.read(len(MAGIC)) != MAGIC:
            raise ValueError("Unsupported STEP mesh archive")
        payload = _read_header(stream)
        if not payload or payload.get("version") != 3 or payload.get("units") != "mm":
            raise ValueError("Invalid STEP mesh archive metadata")
        while True:
            header = _read_header(stream)
            if header is None:
                break
            counts = [header.get(key) for key in ("vertices", "triangles", "normals")]
            if any(type(count) is not int or not 0 < count <= 2**31 - 1 for count in counts):
                raise ValueError("Invalid STEP mesh archive counts")
            byte_count = counts[0] * 24 + counts[1] * 24 + counts[2] * 24
            uv_count = header.get("uvs")
            if type(uv_count) is not int or not 0 <= uv_count <= 2**31 - 1:
                raise ValueError("Invalid STEP mesh archive UV count")
            if uv_count:
                byte_count += uv_count * 16 + counts[1] * 12
            if stream.tell() + byte_count + HEADER.size > length:
                raise ValueError("Incomplete STEP mesh archive data")
            entries.append((stream.tell(), header))
            triangles += header["triangles"]
            degenerate_faces += header.get("degenerate_faces", 0)
            sliver_faces += header.get("sliver_faces", 0)
            sliver_area += header.get("sliver_area_mm2", 0.0)
            stream.seek(byte_count, 1)
        if not entries:
            raise ValueError("No polygon surfaces in STEP mesh archive")
        if stream.tell() < length:
            summary = _read_header(stream)
            if not summary or summary.get("bodies") != len(entries) or summary.get("triangles") != triangles:
                raise ValueError("Invalid STEP mesh archive summary")
            if stream.tell() != length:
                raise ValueError("Invalid STEP mesh archive length")
            degenerate_faces = summary.get("degenerate_faces", degenerate_faces)
            sliver_faces = summary.get("sliver_faces", sliver_faces)
            sliver_area = summary.get("sliver_area_mm2", sliver_area)
            payload["omitted_bodies"] = summary.get("omitted_bodies", 0)
    meshes = MeshArchive(path, entries)
    payload["meshes"] = meshes if lazy else list(meshes)
    payload["triangle_count"] = triangles
    payload["degenerate_faces"] = degenerate_faces
    payload.update(sliver_faces=sliver_faces, sliver_area_mm2=sliver_area)
    return payload
