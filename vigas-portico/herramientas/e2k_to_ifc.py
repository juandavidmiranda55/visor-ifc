#!/usr/bin/env python3
"""Convert an ETABS .e2k text model into an IFC4 physical model.

Covers what this model uses: stories, grids, steel materials, prismatic
sections (I / rod / pipe / tube / rectangle), nonprismatic (tapered)
I sections, beams, columns and braces with cardinal point and rotation.

Geometry rules (ETABS conventions):
  * POINT "p" X Y [DZ]   -> the point sits DZ *below* the level of the story
                            where it is used:  z = elev(story) - DZ
  * LINE "L" TYPE "pi" "pj" n -> pi is n stories below the assigned story,
                                 pj is at the assigned story.
  * Local 1 = I->J.  Local 2: +X for vertical members, otherwise in the
    vertical plane pointing up.  Local 3 = 1 x 2.  ANG rotates 2-3 about 1.
  * Section depth runs along local 2, width along local 3.
  * CARDINALPT places the analytical line on the section (8 = top center).
  * Nonprismatic segments vary their dimensions linearly from STARTSEC
    (I end) to ENDSEC (J end).
"""
import argparse
import math
import time
import uuid

import numpy as np
import ifcopenshell
import ifcopenshell.guid

G = 9.80665
LEN_FACTORS = {"M": 1.0, "CM": 0.01, "MM": 0.001, "FT": 0.3048, "IN": 0.0254}
FORCE_FACTORS = {"N": 1.0, "KN": 1000.0, "TONF": 9806.65, "KGF": G,
                 "KIP": 4448.2216, "LB": 4.4482216}


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
def tokenize(line):
    """Split an e2k line into tokens; quoted strings use "" for a quote."""
    toks, i, n = [], 0, len(line)
    while i < n:
        c = line[i]
        if c in " \t":
            i += 1
            continue
        if c == '"':
            i += 1
            buf = []
            while i < n:
                if line[i] == '"':
                    if i + 1 < n and line[i + 1] == '"':
                        buf.append('"')
                        i += 2
                        continue
                    i += 1
                    break
                buf.append(line[i])
                i += 1
            toks.append("".join(buf))
        else:
            j = i
            while j < n and line[j] not in ' \t"':
                j += 1
            toks.append(line[i:j])
            i = j
    return toks


def pairs(toks, start):
    d = {}
    i = start
    while i < len(toks):
        key = toks[i].upper()
        d[key] = toks[i + 1] if i + 1 < len(toks) else None
        i += 2
    return d


def is_num(s):
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


def parse_e2k(path):
    m = {
        "program": None, "title": None, "units": ("KN", "M", "C"),
        "stories": [], "gridsystems": {}, "grids": [], "materials": {},
        "framesecs": {}, "nonprismatic": {}, "points": {}, "lines": {},
        "lineassigns": [], "areas": 0, "unparsed": [],
    }
    section = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.rstrip("\r\n")
            if line.startswith("$"):
                section = line[1:].strip().upper()
                continue
            if section == "LOG":
                continue
            toks = tokenize(line)
            if not toks:
                continue
            key = toks[0].upper()
            if key == "PROGRAM":
                m["program"] = f'{toks[1]} {toks[3] if len(toks) > 3 else ""}'.strip()
            elif key == "UNITS":
                m["units"] = tuple(t.upper() for t in toks[1:4])
            elif key == "TITLE2":
                m["title"] = toks[1]
            elif key == "STORY":
                m["stories"].append((toks[1], pairs(toks, 2)))
            elif key == "GRIDSYSTEM":
                m["gridsystems"][toks[1]] = pairs(toks, 2)
            elif key == "GRID":
                d = pairs(toks, 2)
                d["SYSTEM"] = toks[1]
                m["grids"].append(d)
            elif key == "MATERIAL":
                m["materials"].setdefault(toks[1], {}).update(
                    {k: v for k, v in pairs(toks, 2).items()
                     if k not in ("STRAIN", "STRESS", "POINTID")})
            elif key == "FRAMESECTION":
                m["framesecs"].setdefault(toks[1], {}).update(pairs(toks, 2))
            elif key == "NONPRISMATICSECTION":
                m["nonprismatic"].setdefault(toks[1], []).append(pairs(toks, 2))
            elif key == "POINT" and section == "POINT COORDINATES":
                x, y = float(toks[2]), float(toks[3])
                dz = float(toks[4]) if len(toks) > 4 and is_num(toks[4]) else 0.0
                m["points"][toks[1]] = (x, y, dz)
            elif key == "LINE" and section == "LINE CONNECTIVITIES":
                m["lines"][toks[1]] = (toks[2].upper(), toks[3], toks[4],
                                       int(toks[5]) if len(toks) > 5 else 0)
            elif key == "LINEASSIGN":
                m["lineassigns"].append((toks[1], toks[2], pairs(toks, 3)))
            elif key == "AREA" and section == "AREA CONNECTIVITIES":
                m["areas"] += 1
    return m


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------
class Sec:
    """Physical cross-section, dimensions in metres."""

    def __init__(self, name, kind, material, **dims):
        self.name, self.kind, self.material = name, kind, material
        self.d = dims

    @property
    def depth(self):
        return self.d.get("D", 0.0)

    @property
    def width(self):
        return self.d.get("B", self.d.get("D", 0.0))

    def area(self):
        d = self.d
        if self.kind == "I":
            return 2 * d["B"] * d["TF"] + (d["D"] - 2 * d["TF"]) * d["TW"]
        if self.kind == "CIRCLE":
            return math.pi * d["D"] ** 2 / 4
        if self.kind == "PIPE":
            r = d["D"] / 2
            return math.pi * (r ** 2 - (r - d["T"]) ** 2)
        if self.kind == "TUBE":
            return d["D"] * d["B"] - (d["D"] - 2 * d["TF"]) * (d["B"] - 2 * d["TW"])
        if self.kind == "RECT":
            return d["D"] * d["B"]
        return 0.0


def build_sections(m, lf):
    secs, warnings = {}, []
    for name, p in m["framesecs"].items():
        shape = (p.get("SHAPE") or "").upper()
        mat = p.get("MATERIAL")
        g = {k: float(v) * lf for k, v in p.items()
             if k in ("D", "B", "TF", "TW", "T", "T2", "T3") and is_num(v)}
        if "NONPRISMATIC" in shape:
            continue
        if "I/WIDE FLANGE" in shape or shape.endswith(" I"):
            secs[name] = Sec(name, "I", mat, D=g["D"], B=g["B"], TF=g["TF"], TW=g["TW"])
        elif "ROD" in shape or "CIRCLE" in shape:
            secs[name] = Sec(name, "CIRCLE", mat, D=g["D"])
        elif "PIPE" in shape:
            secs[name] = Sec(name, "PIPE", mat, D=g["D"], T=g.get("TW", g.get("T", g.get("TF"))))
        elif "TUBE" in shape or "BOX" in shape:
            secs[name] = Sec(name, "TUBE", mat, D=g["D"], B=g["B"], TF=g["TF"], TW=g["TW"])
        elif "RECTANG" in shape:
            secs[name] = Sec(name, "RECT", mat, D=g["D"], B=g["B"])
        elif "D" in g and "B" in g:
            secs[name] = Sec(name, "RECT", mat, D=g["D"], B=g["B"])
            warnings.append(f"Sección '{name}' ({shape}) aproximada como rectángulo")
        else:
            warnings.append(f"Sección '{name}' ({shape}) no soportada")
    return secs, warnings


def nonprismatic_segments(name, m, secs):
    """Return list of (frac_start, frac_end, start_sec, end_sec) or None."""
    segs = m["nonprismatic"].get(name)
    if not segs:
        return None
    total_v = sum(float(s.get("VLENGTH", 0) or 0) for s in segs)
    out, acc = [], 0.0
    for s in segs:
        frac = float(s.get("VLENGTH", 0) or 0) / total_v if total_v else 1.0 / len(segs)
        out.append((acc, acc + frac, secs[s["STARTSEC"]], secs[s["ENDSEC"]]))
        acc += frac
    return out


# --------------------------------------------------------------------------
# Geometry helpers
# --------------------------------------------------------------------------
def local_axes(pi, pj, ang_deg):
    v = pj - pi
    L = float(np.linalg.norm(v))
    l1 = v / L
    if math.hypot(l1[0], l1[1]) < 1e-3:          # vertical member
        l2 = np.array([1.0, 0.0, 0.0])
    else:
        z = np.array([0.0, 0.0, 1.0])
        l2 = z - np.dot(z, l1) * l1
        l2 /= np.linalg.norm(l2)
    l3 = np.cross(l1, l2)
    a = math.radians(ang_deg)
    l2r = math.cos(a) * l2 + math.sin(a) * l3
    l3r = -math.sin(a) * l2 + math.cos(a) * l3
    return L, l1, l2r, l3r


def cardinal_offsets(cp, depth, width):
    """Position of the analytical line relative to the section bbox centre,
    as (offset along local 2, offset along local 3)."""
    row = {1: -1, 2: -1, 3: -1, 4: 0, 5: 0, 6: 0, 7: 1, 8: 1, 9: 1}.get(cp, 0)
    col = {1: 1, 4: 1, 7: 1, 3: -1, 6: -1, 9: -1}.get(cp, 0)   # left = +3
    return row * depth / 2.0, col * width / 2.0


def i_outline(D, B, tf, tw):
    b, w, h = B / 2, tw / 2, D / 2
    return [(-b, -h), (b, -h), (b, -h + tf), (w, -h + tf), (w, h - tf), (b, h - tf),
            (b, h), (-b, h), (-b, h - tf), (-w, h - tf), (-w, -h + tf), (-b, -h + tf)]


def lerp(a, b, t):
    return a + (b - a) * t


# --------------------------------------------------------------------------
# IFC writer
# --------------------------------------------------------------------------
class Writer:
    def __init__(self, project_name, schema="IFC4"):
        self.f = ifcopenshell.file(schema=schema)
        self.ns = uuid.uuid5(uuid.NAMESPACE_URL, "e2k-to-ifc/" + project_name)
        self.pt_cache = {}
        self.dir_cache = {}

    def gid(self, key):
        return ifcopenshell.guid.compress(uuid.uuid5(self.ns, key).hex)

    def c(self, *args, **kw):
        return self.f.create_entity(*args, **kw)

    def pt(self, xyz):
        key = tuple(round(float(v), 7) for v in xyz)
        if key not in self.pt_cache:
            self.pt_cache[key] = self.c("IfcCartesianPoint", Coordinates=key)
        return self.pt_cache[key]

    def dir(self, v):
        key = tuple(round(float(x), 9) for x in v)
        if key not in self.dir_cache:
            self.dir_cache[key] = self.c("IfcDirection", DirectionRatios=key)
        return self.dir_cache[key]

    def axis3(self, loc=(0.0, 0.0, 0.0), z=None, x=None):
        return self.c("IfcAxis2Placement3D", Location=self.pt(loc),
                      Axis=self.dir(z) if z is not None else None,
                      RefDirection=self.dir(x) if x is not None else None)

    def placement(self, rel_to, loc=(0.0, 0.0, 0.0), z=None, x=None):
        return self.c("IfcLocalPlacement", PlacementRelTo=rel_to,
                      RelativePlacement=self.axis3(loc, z, x))

    def val(self, kind, v):
        return self.c(kind, v)

    def pset(self, obj_key, name, props, objs):
        ps = self.c("IfcPropertySet", GlobalId=self.gid(obj_key + "/" + name), Name=name,
                    HasProperties=[self.c("IfcPropertySingleValue", Name=k, NominalValue=v)
                                   for k, v in props if v is not None])
        self.c("IfcRelDefinesByProperties", GlobalId=self.gid(obj_key + "/rel/" + name),
               RelatedObjects=objs, RelatingPropertyDefinition=ps)

    def qto(self, obj_key, name, quantities, objs):
        qs = []
        for qname, kind, value in quantities:
            if value is None:
                continue
            attr = {"IfcQuantityLength": "LengthValue", "IfcQuantityArea": "AreaValue",
                    "IfcQuantityVolume": "VolumeValue", "IfcQuantityWeight": "WeightValue"}[kind]
            qs.append(self.c(kind, Name=qname, **{attr: float(value)}))
        eq = self.c("IfcElementQuantity", GlobalId=self.gid(obj_key + "/" + name), Name=name,
                    MethodOfMeasurement="BaseQuantities", Quantities=qs)
        self.c("IfcRelDefinesByProperties", GlobalId=self.gid(obj_key + "/rel/" + name),
               RelatedObjects=objs, RelatingPropertyDefinition=eq)


def convert(e2k_path, ifc_path, project_name):
    m = parse_e2k(e2k_path)
    force_u, len_u = m["units"][0], m["units"][1]
    lf = LEN_FACTORS[len_u]
    ff = FORCE_FACTORS.get(force_u, 1000.0)
    log = []

    # ---- stories (listed from top) ----
    stories_top_down = m["stories"]
    elevs, names_bottom_up = {}, []
    base_name, base_props = stories_top_down[-1]
    z = float(base_props.get("ELEV", 0)) * lf
    elevs[base_name] = z
    names_bottom_up.append(base_name)
    for name, props in reversed(stories_top_down[:-1]):
        z += float(props["HEIGHT"]) * lf
        elevs[name] = z
        names_bottom_up.append(name)
    story_index = {n: i for i, n in enumerate(names_bottom_up)}

    secs, warn = build_sections(m, lf)
    log += warn

    def point_xyz(label, story):
        x, y, dz = m["points"][label]
        return np.array([x * lf, y * lf, elevs[story] - dz * lf])

    # ---- IFC skeleton ----
    W = Writer(project_name)
    f = W.f
    hdr = f.header
    hdr.file_description.description = ("ViewDefinition [DesignTransferView_V1.0]",)
    hdr.file_name.name = ifc_path.split("/")[-1]
    hdr.file_name.time_stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    hdr.file_name.preprocessor_version = f"IfcOpenShell {ifcopenshell.version}"
    hdr.file_name.originating_system = f"{m['program'] or 'ETABS'} (.e2k) -> IFC4"
    hdr.file_name.authorization = ""

    units = [
        W.c("IfcSIUnit", UnitType="LENGTHUNIT", Name="METRE"),
        W.c("IfcSIUnit", UnitType="AREAUNIT", Name="SQUARE_METRE"),
        W.c("IfcSIUnit", UnitType="VOLUMEUNIT", Name="CUBIC_METRE"),
        W.c("IfcSIUnit", UnitType="MASSUNIT", Prefix="KILO", Name="GRAM"),
    ]
    rad = W.c("IfcSIUnit", UnitType="PLANEANGLEUNIT", Name="RADIAN")
    deg = W.c("IfcConversionBasedUnit",
              Dimensions=W.c("IfcDimensionalExponents", 0, 0, 0, 0, 0, 0, 0),
              UnitType="PLANEANGLEUNIT", Name="DEGREE",
              ConversionFactor=W.c("IfcMeasureWithUnit",
                                   ValueComponent=W.val("IfcPlaneAngleMeasure", math.pi / 180),
                                   UnitComponent=rad))
    units.append(deg)

    model_ctx = W.c("IfcGeometricRepresentationContext", ContextType="Model",
                    CoordinateSpaceDimension=3, Precision=1e-5,
                    WorldCoordinateSystem=W.axis3(), TrueNorth=W.dir((0.0, 1.0)))
    body_ctx = W.c("IfcGeometricRepresentationSubContext", ContextIdentifier="Body",
                   ContextType="Model", ParentContext=model_ctx, TargetView="MODEL_VIEW")
    foot_ctx = W.c("IfcGeometricRepresentationSubContext", ContextIdentifier="FootPrint",
                   ContextType="Model", ParentContext=model_ctx, TargetView="MODEL_VIEW")

    project = W.c("IfcProject", GlobalId=W.gid("project"), Name=project_name,
                  Description=f"Convertido desde {m['program'] or 'ETABS'} (.e2k)",
                  RepresentationContexts=[model_ctx],
                  UnitsInContext=W.c("IfcUnitAssignment", Units=units))
    site_pl = W.placement(None)
    site = W.c("IfcSite", GlobalId=W.gid("site"), Name="Sitio", ObjectPlacement=site_pl,
               CompositionType="ELEMENT")
    bldg_pl = W.placement(site_pl)
    bldg = W.c("IfcBuilding", GlobalId=W.gid("building"), Name=project_name,
               ObjectPlacement=bldg_pl, CompositionType="ELEMENT")
    W.c("IfcRelAggregates", GlobalId=W.gid("agg/project"), RelatingObject=project,
        RelatedObjects=[site])
    W.c("IfcRelAggregates", GlobalId=W.gid("agg/site"), RelatingObject=site,
        RelatedObjects=[bldg])

    storeys, storey_pl, contained = {}, {}, {}
    for name in names_bottom_up:
        pl = W.placement(bldg_pl, (0.0, 0.0, elevs[name]))
        st = W.c("IfcBuildingStorey", GlobalId=W.gid("storey/" + name), Name=name,
                 ObjectPlacement=pl, CompositionType="ELEMENT", Elevation=elevs[name])
        storeys[name], storey_pl[name], contained[name] = st, pl, []
    W.c("IfcRelAggregates", GlobalId=W.gid("agg/building"), RelatingObject=bldg,
        RelatedObjects=[storeys[n] for n in names_bottom_up])

    # ---- materials and colours ----
    styles = {
        "beam": W.c("IfcSurfaceStyle", Name="Acero vigas", Side="BOTH", Styles=[
            W.c("IfcSurfaceStyleShading",
                SurfaceColour=W.c("IfcColourRGB", None, 0.42, 0.53, 0.66), Transparency=0.0)]),
        "rod": W.c("IfcSurfaceStyle", Name="Acero tensores", Side="BOTH", Styles=[
            W.c("IfcSurfaceStyleShading",
                SurfaceColour=W.c("IfcColourRGB", None, 0.86, 0.42, 0.12), Transparency=0.0)]),
    }
    materials, mat_members = {}, {}

    def material(name, st):
        if name not in materials:
            props = m["materials"].get(name, {})
            cat = (props.get("TYPE") or "").capitalize() or None
            cat = {"Steel": "Steel", "Coldformed": "Steel", "Concrete": "Concrete"}.get(cat, cat)
            mat = W.c("IfcMaterial", Name=name, Category=cat)
            materials[name], mat_members[name] = mat, []
            W.c("IfcMaterialDefinitionRepresentation", RepresentedMaterial=mat, Representations=[
                W.c("IfcStyledRepresentation", ContextOfItems=body_ctx,
                    RepresentationIdentifier="Style", RepresentationType="Material",
                    Items=[W.c("IfcStyledItem", Item=None, Styles=[st])])])
        return materials[name]

    def density(name):
        wpv = m["materials"].get(name, {}).get("WEIGHTPERVOLUME")
        return float(wpv) * ff / (lf ** 3) / G if wpv else None

    profile_cache = {}

    def profile(sec):
        if sec.name in profile_cache:
            return profile_cache[sec.name]
        d = sec.d
        if sec.kind == "I":
            p = W.c("IfcIShapeProfileDef", ProfileType="AREA", ProfileName=sec.name,
                    OverallWidth=d["B"], OverallDepth=d["D"], WebThickness=d["TW"],
                    FlangeThickness=d["TF"])
        elif sec.kind == "CIRCLE":
            p = W.c("IfcCircleProfileDef", ProfileType="AREA", ProfileName=sec.name,
                    Radius=d["D"] / 2)
        elif sec.kind == "PIPE":
            p = W.c("IfcCircleHollowProfileDef", ProfileType="AREA", ProfileName=sec.name,
                    Radius=d["D"] / 2, WallThickness=d["T"])
        elif sec.kind == "TUBE":
            p = W.c("IfcRectangleHollowProfileDef", ProfileType="AREA", ProfileName=sec.name,
                    XDim=d["B"], YDim=d["D"], WallThickness=d["TF"])
        else:
            p = W.c("IfcRectangleProfileDef", ProfileType="AREA", ProfileName=sec.name,
                    XDim=d["B"], YDim=d["D"])
        profile_cache[sec.name] = p
        return p

    def tapered_brep(L, cp, segments):
        """Faceted B-rep of a tapered I member in element coordinates
        (X = -local3, Y = local2, Z = local1)."""
        items = []
        for t0, t1, s0, s1 in segments:
            rings = []
            for t, s in ((t0, s0), (t1, s1)):
                D, B, tf, tw = s.d["D"], s.d["B"], s.d["TF"], s.d["TW"]
                o2, o3 = cardinal_offsets(cp, D, B)
                rings.append([(o3 + x, -o2 + y, t * L) for x, y in i_outline(D, B, tf, tw)])
            a, b = rings
            n = len(a)
            faces = [list(reversed(a)), list(b)]
            for k in range(n):
                k2 = (k + 1) % n
                faces.append([a[k], a[k2], b[k2], b[k]])
            ifc_faces = []
            for face in faces:
                loop = W.c("IfcPolyLoop", Polygon=[W.pt(p) for p in face])
                bound = W.c("IfcFaceOuterBound", Bound=loop, Orientation=True)
                ifc_faces.append(W.c("IfcFace", Bounds=[bound]))
            items.append(W.c("IfcFacetedBrep", Outer=W.c("IfcClosedShell", CfsFaces=ifc_faces)))
        return items

    # ---- frame members ----
    counts = {"IfcBeam": 0, "IfcColumn": 0, "IfcMember": 0}
    report = []
    for label, story, props in m["lineassigns"]:
        if label not in m["lines"]:
            log.append(f"LINEASSIGN sin conectividad: {label}")
            continue
        ltype, pi_lab, pj_lab, nbelow = m["lines"][label]
        sname = props.get("SECTION")
        if not sname or sname.upper() == "NONE":
            continue
        s_idx = story_index[story]
        story_i = names_bottom_up[s_idx - nbelow]
        pI, pJ = point_xyz(pi_lab, story_i), point_xyz(pj_lab, story)
        ang = float(props.get("ANG", 0) or 0)
        cp = int(float(props.get("CARDINALPT", 10) or 10))
        L, l1, l2, l3 = local_axes(pI, pJ, ang)
        X = -l3

        segs = nonprismatic_segments(sname, m, secs)
        if segs and all(s0.kind == "I" and s1.kind == "I" for _, _, s0, s1 in segs):
            mat_name = segs[0][2].material
            items = tapered_brep(L, cp, segs)
            rep_type = "Brep"
            area = None
            vol = sum((t1 - t0) * L * (s0.area() + s1.area()) / 2.0 for t0, t1, s0, s1 in segs)
        else:
            if segs:
                sec = segs[0][2]
                log.append(f"{label}: sección variable no-I, se usa '{sec.name}' constante")
                segs = None
            else:
                sec = secs.get(sname)
            if sec is None:
                log.append(f"{label}: sección '{sname}' no encontrada, omitido")
                continue
            mat_name = sec.material
            o2, o3 = cardinal_offsets(cp, sec.depth, sec.width)
            solid = W.c("IfcExtrudedAreaSolid", SweptArea=profile(sec),
                        Position=W.axis3((o3, -o2, 0.0)),
                        ExtrudedDirection=W.dir((0.0, 0.0, 1.0)), Depth=L)
            items = [solid]
            rep_type = "SweptSolid"
            area = sec.area()
            vol = area * L

        if ltype == "COLUMN":
            cls, ptype, pset_name, qto_name = "IfcColumn", "COLUMN", "Pset_ColumnCommon", "Qto_ColumnBaseQuantities"
        elif ltype == "BRACE":
            cls, ptype, pset_name, qto_name = "IfcMember", "BRACE", "Pset_MemberCommon", "Qto_MemberBaseQuantities"
        else:
            cls, ptype, pset_name, qto_name = "IfcBeam", "BEAM", "Pset_BeamCommon", "Qto_BeamBaseQuantities"
        counts[cls] += 1

        style = styles["rod"] if cls == "IfcMember" else styles["beam"]
        for it in items:
            W.c("IfcStyledItem", Item=it, Styles=[style])
        shape = W.c("IfcProductDefinitionShape", Representations=[
            W.c("IfcShapeRepresentation", ContextOfItems=body_ctx, RepresentationIdentifier="Body",
                RepresentationType=rep_type, Items=items)])

        local_loc = pI - np.array([0.0, 0.0, elevs[story]])
        pl = W.placement(storey_pl[story], tuple(local_loc), tuple(l1), tuple(X))
        key = f"{label}@{story}"
        el = W.c(cls, GlobalId=W.gid("el/" + key), Name=label, Description=sname,
                 ObjectType=sname, ObjectPlacement=pl, Representation=shape, Tag=label,
                 PredefinedType=ptype)
        contained[story].append(el)
        material(mat_name, style)
        mat_members[mat_name].append(el)

        slope = math.degrees(math.asin(max(-1.0, min(1.0, l1[2]))))
        common = [("Reference", W.val("IfcIdentifier", sname)),
                  ("LoadBearing", W.val("IfcBoolean", True))]
        if cls != "IfcColumn":
            common += [("Span", W.val("IfcPositiveLengthMeasure", L)),
                       ("Slope", W.val("IfcPlaneAngleMeasure", abs(slope)))]
        W.pset(key, pset_name, common, [el])

        etabs = [
            ("Etiqueta", W.val("IfcLabel", label)),
            ("Piso", W.val("IfcLabel", story)),
            ("Tipo", W.val("IfcLabel", ltype)),
            ("Seccion", W.val("IfcLabel", sname)),
            ("Material", W.val("IfcLabel", mat_name)),
            ("NudoI", W.val("IfcLabel", f"{pi_lab} ({story_i})")),
            ("NudoJ", W.val("IfcLabel", f"{pj_lab} ({story})")),
            ("PuntoCardinal", W.val("IfcInteger", cp)),
            ("Angulo", W.val("IfcPlaneAngleMeasure", ang)),
        ]
        if segs:
            etabs += [("SeccionInicio", W.val("IfcLabel", segs[0][2].name)),
                      ("SeccionFin", W.val("IfcLabel", segs[-1][3].name))]
        if props.get("RELEASE"):
            etabs.append(("Liberaciones", W.val("IfcLabel", props["RELEASE"])))
        W.pset(key, "ETABS", etabs, [el])

        rho = density(mat_name)
        W.qto(key, qto_name, [
            ("Length", "IfcQuantityLength", L),
            ("CrossSectionArea", "IfcQuantityArea", area),
            ("NetVolume", "IfcQuantityVolume", vol),
            ("NetWeight", "IfcQuantityWeight", vol * rho if rho else None),
        ], [el])

        report.append(dict(label=label, story=story, cls=cls, section=sname, L=L, vol=vol,
                           mass=vol * rho if rho else None, pI=pI.tolist(), pJ=pJ.tolist(),
                           l1=l1.tolist(), l2=l2.tolist(), l3=l3.tolist(), cp=cp,
                           segs=[(a, b, s0.name, s1.name) for a, b, s0, s1 in segs] if segs else None,
                           dims_i=(segs[0][2].d if segs else sec.d), dims_j=(segs[-1][3].d if segs else sec.d)))

    # ---- grids ----
    used = [m["points"][lab] for lab in m["points"]]
    xs = [p[0] * lf for p in used] + [float(g["COORD"]) * lf for g in m["grids"] if g.get("DIR") == "X"]
    ys = [p[1] * lf for p in used] + [float(g["COORD"]) * lf for g in m["grids"] if g.get("DIR") == "Y"]
    margin = 2.0
    x0, x1, y0, y1 = min(xs) - margin, max(xs) + margin, min(ys) - margin, max(ys) + margin
    for sysname, sysprops in m["gridsystems"].items():
        if (sysprops.get("TYPE") or "CARTESIAN").upper() != "CARTESIAN":
            log.append(f"Sistema de grillas '{sysname}' no cartesiano omitido")
            continue
        u_axes, v_axes, curves = [], [], []
        for g in m["grids"]:
            if g["SYSTEM"] != sysname or (g.get("VISIBLE") or "Yes").upper() == "NO":
                continue
            c = float(g["COORD"]) * lf
            if g.get("DIR") == "X":
                pts = [(c, y0), (c, y1)]
            elif g.get("DIR") == "Y":
                pts = [(x0, c), (x1, c)]
            else:
                continue
            curve = W.c("IfcPolyline", Points=[W.c("IfcCartesianPoint", Coordinates=p) for p in pts])
            curves.append(curve)
            ax = W.c("IfcGridAxis", AxisTag=g["LABEL"], AxisCurve=curve, SameSense=True)
            (u_axes if g.get("DIR") == "X" else v_axes).append(ax)
        if not u_axes or not v_axes:
            continue
        base = names_bottom_up[0]
        grid = W.c("IfcGrid", GlobalId=W.gid("grid/" + sysname), Name=sysname,
                   ObjectPlacement=W.placement(storey_pl[base]),
                   Representation=W.c("IfcProductDefinitionShape", Representations=[
                       W.c("IfcShapeRepresentation", ContextOfItems=foot_ctx,
                           RepresentationIdentifier="FootPrint",
                           RepresentationType="GeometricCurveSet",
                           Items=[W.c("IfcGeometricCurveSet", Elements=curves)])]),
                   UAxes=u_axes, VAxes=v_axes, PredefinedType="RECTANGULAR")
        contained[base].append(grid)

    # ---- relationships ----
    for name, els in contained.items():
        if els:
            W.c("IfcRelContainedInSpatialStructure", GlobalId=W.gid("contain/" + name),
                RelatedElements=els, RelatingStructure=storeys[name])
    for name, els in mat_members.items():
        if els:
            W.c("IfcRelAssociatesMaterial", GlobalId=W.gid("mat/" + name),
                RelatedObjects=els, RelatingMaterial=materials[name])

    if m["areas"]:
        log.append(f"{m['areas']} objetos de área no convertidos")
    f.write(ifc_path)
    return dict(counts=counts, log=log, report=report, elevs=elevs,
                n_lines=len(m["lines"]), n_assigns=len(m["lineassigns"]),
                materials=list(materials))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("e2k")
    ap.add_argument("ifc")
    ap.add_argument("--name", default="Modelo ETABS")
    ap.add_argument("--report")
    a = ap.parse_args()
    res = convert(a.e2k, a.ifc, a.name)
    if a.report:
        import json
        with open(a.report, "w", encoding="utf-8") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=1)
    print("Elementos:", res["counts"], "| líneas:", res["n_lines"], "| asignaciones:", res["n_assigns"])
    print("Elevaciones:", res["elevs"])
    print("Materiales:", res["materials"])
    for w in res["log"]:
        print("AVISO:", w)
