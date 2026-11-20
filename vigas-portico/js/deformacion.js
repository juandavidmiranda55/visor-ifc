// Deformada a partir de modelo/analisis.json (sin dependencias, se puede probar en Node).
//
// El análisis está en coordenadas IFC (Z hacia arriba). web-ifc y three.js usan Y hacia
// arriba con la conversión (x, y, z)IFC -> (x, z, -y), así que todo se convierte al cargar.
// Para cada subelemento el JSON trae los 12 desplazamientos locales de sus extremos
// (u1 u2 u3 r1 r2 r3 en i y en j). Dentro del subelemento se interpola:
//   axial y torsión: lineal; flexión: polinomios cúbicos de Hermite.
// Un punto de la sección se mueve con el eje más el giro de la sección (θ × brazo).

export const aThree = (p) => [p[0], p[2], -p[1]];

export function prepararAnalisis(json) {
  const nsub = json.sub.a.length;
  const R = new Float64Array(nsub * 9);
  for (let k = 0; k < nsub; k++) {
    const r = json.sub.R[k];
    for (let fila = 0; fila < 3; fila++) {
      const v = aThree([r[3 * fila], r[3 * fila + 1], r[3 * fila + 2]]);
      R.set(v, 9 * k + 3 * fila);
    }
  }
  const casos = json.casos.map((c) => {
    const d = new Float64Array(nsub * 12);
    c.d.forEach((arr, k) => d.set(arr, 12 * k));
    return { id: c.id, nombre: c.nombre, d };
  });
  const miembros = new Map();
  for (const m of json.miembros) {
    const pI = aThree(m.pI);
    const pJ = aThree(m.pJ);
    const L = Math.hypot(pJ[0] - pI[0], pJ[1] - pI[1], pJ[2] - pI[2]);
    const e = [(pJ[0] - pI[0]) / L, (pJ[1] - pI[1]) / L, (pJ[2] - pI[2]) / L];
    miembros.set(m.guid, { ...m, pI3: pI, pJ3: pJ, L, e, k0: m.sub[0], nk: m.sub[1] });
  }
  return {
    nombre: json.modelo,
    nota: json.nota,
    nodos: json.nodos.map(aThree),
    subA: json.sub.a,
    subB: json.sub.b,
    t0: json.sub.t0,
    t1: json.sub.t1,
    R,
    casos,
    miembros,
    apoyos: json.apoyos,
    resumen: json.resumen,
  };
}

// Desplazamiento (coordenadas three.js) del punto p que pertenece al miembro, para cada
// caso base. Escribe 3 valores por caso en out (Float64Array de 3*ncasos).
export function desplazamientoPunto(an, miembro, p, out) {
  const { pI3: pI, e, L, k0, nk } = miembro;
  let t = ((p[0] - pI[0]) * e[0] + (p[1] - pI[1]) * e[1] + (p[2] - pI[2]) * e[2]) / L;
  t = Math.min(1, Math.max(0, t));
  let k = k0 + nk - 1;
  for (let j = k0; j < k0 + nk; j++) {
    if (t <= an.t1[j] + 1e-12) { k = j; break; }
  }
  const ta = an.t0[k];
  const tb = an.t1[k];
  const Ls = (tb - ta) * L;
  const xi = Math.min(1, Math.max(0, (t - ta) / (tb - ta)));
  const bx = p[0] - (pI[0] + e[0] * t * L);
  const by = p[1] - (pI[1] + e[1] * t * L);
  const bz = p[2] - (pI[2] + e[2] * t * L);
  const x2 = xi * xi, x3 = x2 * xi;
  const N1 = 1 - 3 * x2 + 2 * x3, N2 = xi - 2 * x2 + x3, N3 = 3 * x2 - 2 * x3, N4 = -x2 + x3;
  const D1 = -6 * xi + 6 * x2, D2 = 1 - 4 * xi + 3 * x2, D3 = 6 * xi - 6 * x2, D4 = -2 * xi + 3 * x2;
  const R = an.R, o = 9 * k;
  for (let c = 0; c < an.casos.length; c++) {
    const d = an.casos[c].d, b = 12 * k;
    const u1 = (1 - xi) * d[b] + xi * d[b + 6];
    const u2 = N1 * d[b + 1] + N2 * Ls * d[b + 5] + N3 * d[b + 7] + N4 * Ls * d[b + 11];
    const u3 = N1 * d[b + 2] - N2 * Ls * d[b + 4] + N3 * d[b + 8] - N4 * Ls * d[b + 10];
    const r1 = (1 - xi) * d[b + 3] + xi * d[b + 9];
    const r3 = (D1 * d[b + 1] + D2 * Ls * d[b + 5] + D3 * d[b + 7] + D4 * Ls * d[b + 11]) / Ls;
    const r2 = -(D1 * d[b + 2] - D2 * Ls * d[b + 4] + D3 * d[b + 8] - D4 * Ls * d[b + 10]) / Ls;
    const gx = R[o] * u1 + R[o + 3] * u2 + R[o + 6] * u3;
    const gy = R[o + 1] * u1 + R[o + 4] * u2 + R[o + 7] * u3;
    const gz = R[o + 2] * u1 + R[o + 5] * u2 + R[o + 8] * u3;
    const tx = R[o] * r1 + R[o + 3] * r2 + R[o + 6] * r3;
    const ty = R[o + 1] * r1 + R[o + 4] * r2 + R[o + 7] * r3;
    const tz = R[o + 2] * r1 + R[o + 5] * r2 + R[o + 8] * r3;
    out[3 * c] = gx + ty * bz - tz * by;
    out[3 * c + 1] = gy + tz * bx - tx * bz;
    out[3 * c + 2] = gz + tx * by - ty * bx;
  }
}

// Campos de desplazamiento por caso para un arreglo de posiciones (x,y,z,...).
export function camposDePosiciones(an, miembro, pos) {
  const n = pos.length / 3;
  const nc = an.casos.length;
  const campos = Array.from({ length: nc }, () => new Float32Array(n * 3));
  const tmp = new Float64Array(3 * nc);
  const p = [0, 0, 0];
  for (let i = 0; i < n; i++) {
    p[0] = pos[3 * i]; p[1] = pos[3 * i + 1]; p[2] = pos[3 * i + 2];
    desplazamientoPunto(an, miembro, p, tmp);
    for (let c = 0; c < nc; c++) {
      campos[c][3 * i] = tmp[3 * c];
      campos[c][3 * i + 1] = tmp[3 * c + 1];
      campos[c][3 * i + 2] = tmp[3 * c + 2];
    }
  }
  return campos;
}

// Desplazamiento de los nudos del análisis (traslaciones) por caso.
export function camposNodos(an) {
  const nn = an.nodos.length;
  const nc = an.casos.length;
  const campos = Array.from({ length: nc }, () => new Float32Array(nn * 3));
  for (let k = 0; k < an.subA.length; k++) {
    const o = 9 * k;
    for (const [nodo, off] of [[an.subA[k], 0], [an.subB[k], 6]]) {
      for (let c = 0; c < nc; c++) {
        const d = an.casos[c].d, b = 12 * k + off;
        for (let a = 0; a < 3; a++) {
          campos[c][3 * nodo + a] = an.R[o + a] * d[b] + an.R[o + 3 + a] * d[b + 1] + an.R[o + 6 + a] * d[b + 2];
        }
      }
    }
  }
  return campos;
}

// out = Σ coef[c] * campos[c]
export function combinar(campos, coefs, out) {
  out.fill(0);
  for (let c = 0; c < campos.length; c++) {
    const f = coefs[c];
    if (!f) continue;
    const src = campos[c];
    for (let i = 0; i < out.length; i++) out[i] += f * src[i];
  }
  return out;
}

// Escala "redonda" (1-2-5) para que el máximo desplazamiento se vea como 'objetivo'.
export function escalaAuto(umax, objetivo) {
  if (!(umax > 0)) return 1;
  const s = objetivo / umax;
  const p = Math.pow(10, Math.floor(Math.log10(s)));
  const m = s / p;
  const r = m < 1.5 ? 1 : m < 3.5 ? 2 : m < 7.5 ? 5 : 10;
  return r * p;
}

// Mapa de color tipo arcoíris (azul -> cian -> verde -> amarillo -> rojo).
export function colorArcoiris(v, out, i) {
  const t = Math.min(1, Math.max(0, v));
  const stops = [[0.16, 0.32, 0.86], [0.1, 0.72, 0.88], [0.25, 0.78, 0.35], [0.98, 0.83, 0.2], [0.89, 0.2, 0.15]];
  const x = t * (stops.length - 1);
  const k = Math.min(stops.length - 2, Math.floor(x));
  const f = x - k;
  for (let a = 0; a < 3; a++) out[i + a] = stops[k][a] + (stops[k + 1][a] - stops[k][a]) * f;
}
