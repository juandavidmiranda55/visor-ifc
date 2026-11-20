#!/usr/bin/env python3
"""Linear-elastic 3D frame analysis of an ETABS .e2k model for the IFC viewer.

Builds the analytical model the way ETABS does for this kind of model:
  * nodes at member ends; members meshed where another member's end lies on
    them (MESHATINTERSECTIONS), then subdivided for smooth deformed shapes;
  * tapered (nonprismatic) I members: dimensions interpolated linearly;
  * Timoshenko 3D frame elements, end releases by static condensation;
  * restraints from POINTASSIGN ... RESTRAINT;
  * self weight with the section weight modifier (WMOD), lateral body force
    with the mass modifier (MMOD).
Basis load cases (combined linearly in the browser):
  PP = self weight, Q = 1 kN/m downward on beams, SX/SY = 1.0 x weight
  acting horizontally in +X / +Y.
Writes a JSON with nodes, sub-elements (local axes) and, per case, the 12
local end displacements of each sub-element (released DOFs recovered).
"""
import argparse
import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e2k_to_ifc import (parse_e2k, build_sections, nonprismatic_segments,   # noqa: E402
                        local_axes, tokenize, LEN_FACTORS, FORCE_FACTORS, G)

OUTPUT_CASES = ("PP", "Q", "SX")   # SY is unstable out of plane in this model (no lateral bracing)
TOL = 1e-3          # m, same as ETABS MERGETOL in this model
MAX_SUB = 1.0       # m, maximum sub-element length for beams
ROD_SUBS = 4        # sub-elements per brace/rod

RELEASE_DOF = {"P": 0, "V2": 1, "V3": 2, "T": 3, "M2": 4, "M3": 5}


def parse_point_assigns(path):
    out = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            toks = tokenize(raw.rstrip("\r\n"))
            if len(toks) >= 3 and toks[0].upper() == "POINTASSIGN":
                d = {}
                i = 3
                while i < len(toks) - 1:
                    d[toks[i].upper()] = toks[i + 1]
                    i += 2
                if "RESTRAINT" in d:
                    out[(toks[1], toks[2])] = d["RESTRAINT"].split()
    return out


def sec_props(kind, d):
    if kind == "I":
        D, B, tf, tw = d["D"], d["B"], d["TF"], d["TW"]
        A = 2 * B * tf + (D - 2 * tf) * tw
        I33 = (B * D ** 3 - (B - tw) * (D - 2 * tf) ** 3) / 12.0
        I22 = (2 * tf * B ** 3 + (D - 2 * tf) * tw ** 3) / 12.0
        J = (2 * B * tf ** 3 + (D - 2 * tf) * tw ** 3) / 3.0
        As2, As3 = D * tw, 5.0 / 3.0 * tf * B
    elif kind == "CIRCLE":
        r = d["D"] / 2
        A = math.pi * r ** 2
        I33 = I22 = math.pi * r ** 4 / 4
        J = math.pi * r ** 4 / 2
        As2 = As3 = 0.9 * A
    elif kind == "PIPE":
        ro, ri = d["D"] / 2, d["D"] / 2 - d["T"]
        A = math.pi * (ro ** 2 - ri ** 2)
        I33 = I22 = math.pi * (ro ** 4 - ri ** 4) / 4
        J = 2 * I33
        As2 = As3 = 0.5 * A
    elif kind == "TUBE":
        D, B, tf, tw = d["D"], d["B"], d["TF"], d["TW"]
        A = D * B - (D - 2 * tf) * (B - 2 * tw)
        I33 = (B * D ** 3 - (B - 2 * tw) * (D - 2 * tf) ** 3) / 12
        I22 = (D * B ** 3 - (D - 2 * tf) * (B - 2 * tw) ** 3) / 12
        J = 2 * tw * tf * (B - tw) ** 2 * (D - tf) ** 2 / (B * tw + D * tf - tw ** 2 - tf ** 2)
        As2, As3 = 2 * D * tw, 2 * B * tf
    else:
        D, B = d["D"], d["B"]
        A = D * B
        I33, I22 = B * D ** 3 / 12, D * B ** 3 / 12
        a, b = max(D, B) / 2, min(D, B) / 2
        J = a * b ** 3 * (16 / 3 - 3.36 * b / a * (1 - b ** 4 / (12 * a ** 4)))
        As2 = As3 = 5.0 / 6.0 * A
    return dict(A=A, I33=I33, I22=I22, J=J, As2=As2, As3=As3)


def k_local(E, Gm, p, L):
    A, I33, I22, J, As2, As3 = p["A"], p["I33"], p["I22"], p["J"], p["As2"], p["As3"]
    K = np.zeros((12, 12))
    ea, gj = E * A / L, Gm * J / L
    K[0, 0] = K[6, 6] = ea
    K[0, 6] = K[6, 0] = -ea
    K[3, 3] = K[9, 9] = gj
    K[3, 9] = K[9, 3] = -gj
    # bending about local 3 (deflection along 2): dofs 1, 5, 7, 11
    py = 12 * E * I33 / (Gm * As2 * L ** 2)
    a = E * I33 / (L ** 3 * (1 + py))
    K[1, 1] = K[7, 7] = 12 * a
    K[1, 7] = -12 * a
    K[1, 5] = K[1, 11] = 6 * a * L
    K[5, 7] = K[7, 11] = -6 * a * L
    K[5, 5] = K[11, 11] = (4 + py) * a * L ** 2
    K[5, 11] = (2 - py) * a * L ** 2
    # bending about local 2 (deflection along 3): dofs 2, 4, 8, 10
    pz = 12 * E * I22 / (Gm * As3 * L ** 2)
    b = E * I22 / (L ** 3 * (1 + pz))
    K[2, 2] = K[8, 8] = 12 * b
    K[2, 8] = -12 * b
    K[2, 4] = K[2, 10] = -6 * b * L
    K[4, 8] = K[8, 10] = 6 * b * L
    K[4, 4] = K[10, 10] = (4 + pz) * b * L ** 2
    K[4, 10] = (2 - pz) * b * L ** 2
    iu = np.triu_indices(12, 1)
    K[(iu[1], iu[0])] = K[iu]
    return K


def fef_local(w, L):
    """Equivalent nodal loads of a uniform load w (local components)."""
    w1, w2, w3 = w
    return np.array([w1 * L / 2, w2 * L / 2, w3 * L / 2, 0, -w3 * L ** 2 / 12, w2 * L ** 2 / 12,
                     w1 * L / 2, w2 * L / 2, w3 * L / 2, 0, w3 * L ** 2 / 12, -w2 * L ** 2 / 12])


def analyse(e2k, ifc_path, out_json, project_name):
    m = parse_e2k(e2k)
    lf = LEN_FACTORS[m["units"][1]]
    ff = FORCE_FACTORS.get(m["units"][0], 1000.0) / 1000.0   # -> kN
    secs, _ = build_sections(m, lf)

    # stories
    top_down = m["stories"]
    elevs, bottom_up = {}, []
    z = float(top_down[-1][1].get("ELEV", 0)) * lf
    elevs[top_down[-1][0]] = z
    bottom_up.append(top_down[-1][0])
    for name, props in reversed(top_down[:-1]):
        z += float(props["HEIGHT"]) * lf
        elevs[name] = z
        bottom_up.append(name)
    sidx = {n: i for i, n in enumerate(bottom_up)}

    def pxyz(label, story):
        x, y, dz = m["points"][label]
        return np.array([x * lf, y * lf, elevs[story] - dz * lf])

    # GlobalIds from the IFC (label@story -> guid)
    guid = {}
    if ifc_path:
        import ifcopenshell
        import ifcopenshell.util.element as ue
        f = ifcopenshell.open(ifc_path)
        for el in f.by_type("IfcElement"):
            ps = ue.get_psets(el).get("ETABS")
            if ps:
                guid[f"{ps['Etiqueta']}@{ps['Piso']}"] = el.GlobalId

    # members
    members = []
    for label, story, props in m["lineassigns"]:
        ltype, pi, pj, nb = m["lines"][label]
        sname = props.get("SECTION")
        if not sname or sname.upper() == "NONE":
            continue
        story_i = bottom_up[sidx[story] - nb]
        PI, PJ = pxyz(pi, story_i), pxyz(pj, story)
        segs = nonprismatic_segments(sname, m, secs)
        base = segs[0][2] if segs else secs[sname]
        fsec = m["framesecs"].get(sname, {})
        mat = m["materials"][base.material]
        rel = set()
        for tok in (props.get("RELEASE") or "").split():
            end = 6 if tok.endswith("J") else 0
            rel.add(end + RELEASE_DOF[tok[:-1]])
        members.append(dict(
            label=label, story=story, type=ltype, section=sname, pI=PI, pJ=PJ,
            ang=float(props.get("ANG", 0) or 0), segs=segs, sec=base, rel=rel,
            E=float(mat["E"]) * ff / lf ** 2, nu=float(mat.get("U", 0.3)),
            gamma=float(mat.get("WEIGHTPERVOLUME", 0)) * ff / lf ** 3,
            wmod=float(fsec.get("WMOD", 1)), mmod=float(fsec.get("MMOD", 1)),
            guid=guid.get(f"{label}@{story}")))

    # nodes at member ends (merged by tolerance)
    nodes = []

    def node_at(p):
        for i, q in enumerate(nodes):
            if np.linalg.norm(q - p) < TOL:
                return i
        nodes.append(np.array(p, dtype=float))
        return len(nodes) - 1

    for mb in members:
        mb["nI"], mb["nJ"] = node_at(mb["pI"]), node_at(mb["pJ"])
    end_nodes = list(range(len(nodes)))

    # mesh at intersections + subdivision
    subs = []
    for mb in members:
        L, l1, l2, l3 = local_axes(mb["pI"], mb["pJ"], mb["ang"])
        mb.update(L=L, R=np.vstack([l1, l2, l3]))
        stations = [(0.0, mb["nI"]), (1.0, mb["nJ"])]
        for n in end_nodes:
            if n in (mb["nI"], mb["nJ"]):
                continue
            v = nodes[n] - mb["pI"]
            t = float(np.dot(v, l1)) / L
            if 1e-6 < t < 1 - 1e-6 and np.linalg.norm(v - t * L * l1) < TOL:
                stations.append((t, n))
        stations.sort()
        mb["split_nodes"] = [n for t, n in stations[1:-1]]
        sub_ids = []
        for (t0, n0), (t1, n1) in zip(stations[:-1], stations[1:]):
            seg_len = (t1 - t0) * L
            k = ROD_SUBS if mb["type"] == "BRACE" else max(1, math.ceil(seg_len / MAX_SUB - 1e-9))
            prev = n0
            for s in range(k):
                ta = t0 + (t1 - t0) * s / k
                tb = t0 + (t1 - t0) * (s + 1) / k
                nb_ = n1 if s == k - 1 else len(nodes)
                if s < k - 1:
                    nodes.append(mb["pI"] + tb * L * l1)
                subs.append(dict(mb=mb, a=prev, b=nb_, t0=ta, t1=tb))
                sub_ids.append(len(subs) - 1)
                prev = nb_
        mb["subs"] = sub_ids
        # releases only at the member ends
        first, last = subs[sub_ids[0]], subs[sub_ids[-1]]
        first["rel"] = {d for d in mb["rel"] if d < 6}
        last.setdefault("rel", set())
        last["rel"] = last["rel"] | {d for d in mb["rel"] if d >= 6}

    nn = len(nodes)
    ndof = 6 * nn
    K = np.zeros((ndof, ndof))
    case_ids = ["PP", "Q", "SX", "SY"]
    F = np.zeros((ndof, len(case_ids)))
    elem_data = []

    for s in subs:
        mb = s["mb"]
        tm = 0.5 * (s["t0"] + s["t1"])
        if mb["segs"]:
            for t0, t1, s0, s1 in mb["segs"]:
                if t0 - 1e-12 <= tm <= t1 + 1e-12:
                    u = (tm - t0) / (t1 - t0)
                    dims = {k: s0.d[k] + (s1.d[k] - s0.d[k]) * u for k in s0.d}
                    kind = s0.kind
                    break
        else:
            dims, kind = mb["sec"].d, mb["sec"].kind
        p = sec_props(kind, dims)
        Ls = (s["t1"] - s["t0"]) * mb["L"]
        Gm = mb["E"] / (2 * (1 + mb["nu"]))
        Kl = k_local(mb["E"], Gm, p, Ls)
        R = mb["R"]
        wpp = mb["gamma"] * p["A"]
        loads_g = [
            np.array([0, 0, -wpp * mb["wmod"]]),
            np.array([0, 0, -1.0]) if mb["type"] != "BRACE" else np.zeros(3),
            np.array([wpp * mb["mmod"], 0, 0]),
            np.array([0, wpp * mb["mmod"], 0]),
        ]
        fl = np.column_stack([fef_local(R @ w, Ls) for w in loads_g])
        rel = sorted(s.get("rel", set()))
        keep = [i for i in range(12) if i not in rel]
        if rel:
            Kcc = Kl[np.ix_(rel, rel)]
            Kcr = Kl[np.ix_(rel, keep)]
            Kc = np.zeros((12, 12))
            Kc[np.ix_(keep, keep)] = Kl[np.ix_(keep, keep)] - Kcr.T @ np.linalg.solve(Kcc, Kcr)
            fc = np.zeros_like(fl)
            fc[keep] = fl[keep] - Kcr.T @ np.linalg.solve(Kcc, fl[rel])
        else:
            Kc, fc = Kl, fl
        T = np.zeros((12, 12))
        for b in range(4):
            T[3 * b:3 * b + 3, 3 * b:3 * b + 3] = R
        Kg = T.T @ Kc @ T
        Fg = T.T @ fc
        idx = np.r_[6 * s["a"]:6 * s["a"] + 6, 6 * s["b"]:6 * s["b"] + 6]
        K[np.ix_(idx, idx)] += Kg
        F[idx] += Fg
        elem_data.append(dict(Kl=Kl, fl=fl, T=T, idx=idx, rel=rel, keep=keep, L=Ls, p=p,
                              w=loads_g))

    # restraints
    restr = parse_point_assigns(e2k)
    fixed = np.zeros(ndof, dtype=bool)
    supports = []
    dof_names = ["UX", "UY", "UZ", "RX", "RY", "RZ"]
    for (plab, story), rs in restr.items():
        if plab not in m["points"] or story not in elevs:
            continue
        p = pxyz(plab, story)
        hit = [i for i in range(nn) if np.linalg.norm(nodes[i] - p) < TOL]
        if not hit:
            continue                                   # point not used by any member
        n = hit[0]
        for r in rs:
            fixed[6 * n + dof_names.index(r)] = True
        supports.append(dict(n=n, r=" ".join(rs), label=plab))

    free = ~fixed
    Kff = K[np.ix_(free, free)]
    U = np.zeros((ndof, len(case_ids)))
    U[free] = np.linalg.solve(Kff, F[free])
    resid = np.abs(Kff @ U[free] - F[free]).max()
    cond = np.linalg.cond(Kff)

    # equilibrium: reactions vs applied loads
    Rxn = K @ U - F
    applied = np.zeros((3, len(case_ids)))
    for e, s in zip(elem_data, subs):
        for c, w in enumerate(e["w"]):
            applied[:, c] += w * e["L"]
    reac = np.zeros((3, len(case_ids)))
    for i in range(nn):
        for a in range(3):
            if fixed[6 * i + a]:
                reac[a] += Rxn[6 * i + a]

    # per sub-element local end displacements (released dofs recovered)
    sub_disp = np.zeros((len(subs), len(case_ids), 12))
    for k, (e, s) in enumerate(zip(elem_data, subs)):
        dl = e["T"] @ U[e["idx"]]                     # 12 x ncases (retained dofs valid)
        if e["rel"]:
            Kl, rel, keep = e["Kl"], e["rel"], e["keep"]
            dl[rel] = np.linalg.solve(Kl[np.ix_(rel, rel)],
                                      e["fl"][rel] - Kl[np.ix_(rel, keep)] @ dl[keep])
        sub_disp[k] = dl.T

    # report
    names = {"PP": "Peso propio", "Q": "Carga distribuida en vigas (1 kN/m)",
             "SX": "Sismo X (1.0 x peso propio)", "SY": "Sismo Y (1.0 x peso propio)"}
    summary = {}
    for c, cid in enumerate(case_ids):
        disp = U[:, c].reshape(-1, 6)[:, :3]
        mag = np.linalg.norm(disp, axis=1)
        i = int(np.argmax(mag))
        summary[cid] = dict(nombre=names[cid], umax_mm=float(mag[i] * 1000), nodo=i,
                            xyz=[round(v, 3) for v in nodes[i]],
                            carga=[float(v) for v in applied[:, c]],
                            reaccion=[float(v) for v in reac[:, c]])

    def r6(v):
        return float(f"{v:.6g}")

    out = dict(
        version=1, modelo=project_name, unidades=dict(longitud="m", fuerza="kN"),
        nota="Análisis lineal elástico (método de rigidez 3D) generado desde el .e2k de ETABS",
        nodos=[[round(float(c), 6) for c in p] for p in nodes],
        apoyos=[dict(n=s["n"], r=s["r"], etiqueta=s["label"]) for s in supports],
        miembros=[dict(guid=mb["guid"], etiqueta=mb["label"], piso=mb["story"],
                       tipo="tensor" if mb["type"] == "BRACE" else "viga", seccion=mb["section"],
                       pI=[round(float(c), 6) for c in mb["pI"]],
                       pJ=[round(float(c), 6) for c in mb["pJ"]],
                       L=round(mb["L"], 6), sub=[mb["subs"][0], len(mb["subs"])])
                  for mb in members],
        sub=dict(a=[s["a"] for s in subs], b=[s["b"] for s in subs],
                 t0=[round(s["t0"], 8) for s in subs], t1=[round(s["t1"], 8) for s in subs],
                 R=[[round(float(v), 9) for v in s["mb"]["R"].ravel()] for s in subs]),
        casos=[dict(id=cid, nombre=names[cid],
                    d=[[r6(v) for v in sub_disp[k, c]] for k in range(len(subs))])
               for c, cid in enumerate(case_ids) if cid in OUTPUT_CASES],
        resumen={k: v for k, v in summary.items() if k in OUTPUT_CASES},
    )
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))
    return dict(nodes=nn, subs=len(subs), dof=int(free.sum()), resid=float(resid), cond=float(cond),
                summary=summary, members=members, U=U, nodes_xyz=nodes, subs_list=subs,
                sub_disp=sub_disp, fixed=fixed)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("e2k")
    ap.add_argument("ifc")
    ap.add_argument("json")
    ap.add_argument("--name", default="Modelo ETABS")
    a = ap.parse_args()
    r = analyse(a.e2k, a.ifc, a.json, a.name)
    print(f"Nodos {r['nodes']}  subelementos {r['subs']}  GDL libres {r['dof']}")
    print(f"Residuo máx {r['resid']:.2e}   cond(K) {r['cond']:.2e}")
    for cid, s in r["summary"].items():
        print(f"{cid:3s} {s['nombre']:32s} umax={s['umax_mm']:12.4f} mm en nodo {s['nodo']} {s['xyz']}"
              f"  carga={np.round(s['carga'], 3)}  reacción={np.round(s['reaccion'], 3)}")
