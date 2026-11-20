// Visor IFC con simulación de la deformada.
// three.js dibuja; web-ifc lee el IFC en el navegador; deformacion.js interpola el análisis.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { CSS2DRenderer, CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';
import * as WebIFC from '../vendor/web-ifc/web-ifc-api.js';
import {
  prepararAnalisis, camposDePosiciones, camposNodos, combinar, escalaAuto, colorArcoiris,
} from './deformacion.js';

const RUTA_IFC = './modelo/Vigas_porticos_v3.ifc';
const RUTA_ANALISIS = './modelo/analisis.json';
const esMovil = () => window.matchMedia('(max-width: 900px)').matches;
const objetivo = () => (esMovil() ? 0.055 : 0.04);   // la deformada máxima se dibuja como ese % del tamaño del modelo
const PERIODO = 2600;           // ms de un ciclo cargar-descargar
const ESCALA_MAX = 100000;

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const num = (v, d = 2) => Number(v).toLocaleString('es-CO', { minimumFractionDigits: d, maximumFractionDigits: d });
const lin = (c) => (c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4));   // sRGB -> lineal

// ---------------------------------------------------------------- escena
const visor = $('visor');
let renderer;
try {
  renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setClearColor(0x000000, 0);
} catch (e) {
  mostrarError('Este navegador no puede mostrar gráficos 3D (WebGL). Prueba con Chrome, Edge o Firefox actualizados.');
  throw e;
}
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
visor.prepend(renderer.domElement);
const capaEtiquetas = new CSS2DRenderer();
capaEtiquetas.domElement.className = 'capa-etiquetas';
visor.appendChild(capaEtiquetas.domElement);

const escena = new THREE.Scene();
const camPersp = new THREE.PerspectiveCamera(40, 1, 0.05, 5000);
const camOrto = new THREE.OrthographicCamera(-10, 10, 10, -10, 0.01, 5000);
camPersp.position.set(-20, 16, 30);
let camara = camPersp;
let semiAltoOrto = 10;
const controles = new OrbitControls(camara, renderer.domElement);
function usarCamara(c) {
  camara = c;
  controles.object = c;
}
controles.enableDamping = true;
controles.dampingFactor = 0.12;
controles.screenSpacePanning = true;

escena.add(new THREE.HemisphereLight(0xe8f0ff, 0x203046, 1.7));
const sol = new THREE.DirectionalLight(0xffffff, 2.0);
sol.position.set(30, 60, 40);
const relleno = new THREE.DirectionalLight(0xc6d8ff, 0.8);
relleno.position.set(-40, 15, -30);
escena.add(sol, relleno);

const grupoModelo = new THREE.Group();
const grupoFantasma = new THREE.Group();
const grupoGrilla = new THREE.Group();
const grupoAnalitico = new THREE.Group();
grupoFantasma.visible = false;
grupoAnalitico.visible = false;
escena.add(grupoModelo, grupoFantasma, grupoGrilla, grupoAnalitico);

const matFantasma = new THREE.MeshBasicMaterial({ color: 0xdfe7f3, transparent: true, opacity: 0.16, depthWrite: false, side: THREE.DoubleSide });
const matGrilla = new THREE.LineDashedMaterial({ color: 0x7fa3d6, dashSize: 0.6, gapSize: 0.35, transparent: true, opacity: 0.75 });
const matEje = new THREE.LineBasicMaterial({ color: 0x9fb8db, transparent: true, opacity: 0.8 });
const matEjeDef = new THREE.LineBasicMaterial({ color: 0xff8a3d });

const estado = {
  ifc: null, modelID: null, analisis: null,
  mallas: [], elementos: new Map(), ejes: [], apoyos: [], nodos: null, nodoMiembro: [],
  conAnalisis: false, simulando: false, animar: true, original: true,
  coefs: { PP: 1, Q: 0, SX: 0 },
  escala: 200, escalaAuto: true, umax: 0, imax: -1, factor: 0, tInicio: 0,
  tamano: 30, vista: 'iso', seleccion: null,
};

let pendiente = true;
const pedirRender = () => { pendiente = true; };
controles.addEventListener('change', pedirRender);
let camaraMovida = false;
controles.addEventListener('start', () => { camaraMovida = true; });

function redimensionar() {
  const w = visor.clientWidth;
  const h = visor.clientHeight;
  if (!w || !h) return;
  renderer.setSize(w, h, false);
  capaEtiquetas.setSize(w, h);
  camPersp.aspect = w / h;
  camPersp.updateProjectionMatrix();
  ajustarOrto();
  pedirRender();
}
function ajustarOrto() {
  const a = camPersp.aspect || 1;
  camOrto.top = semiAltoOrto;
  camOrto.bottom = -semiAltoOrto;
  camOrto.left = -semiAltoOrto * a;
  camOrto.right = semiAltoOrto * a;
  camOrto.updateProjectionMatrix();
}
let tReencuadre = 0;
new ResizeObserver(() => {
  redimensionar();
  if (!camaraMovida && estado.mallas.length) {
    clearTimeout(tReencuadre);
    tReencuadre = setTimeout(() => encuadrar(estado.vista, false), 120);
  }
}).observe(visor);

renderer.setAnimationLoop((t) => {
  controles.update();
  if (estado.simulando) animar(t);
  if (pendiente) {
    renderer.render(escena, camara);
    capaEtiquetas.render(escena, camara);
    pendiente = false;
  }
});

// ---------------------------------------------------------------- carga / errores
function mostrarCarga(texto) {
  const c = $('cargando');
  c.hidden = false;
  c.classList.remove('error');
  c.querySelector('.spinner').hidden = false;
  $('txtCargando').textContent = texto;
}
function ocultarCarga() { $('cargando').hidden = true; }
function mostrarError(texto) {
  const c = $('cargando');
  c.hidden = false;
  c.classList.add('error');
  c.querySelector('.spinner').hidden = true;
  $('txtCargando').textContent = texto;
}

function limpiarGrupo(g) {
  for (const o of [...g.children]) {
    g.remove(o);
    o.traverse((x) => {
      if (x.geometry) x.geometry.dispose();
      if (x.material && ![matFantasma, matGrilla, matEje, matEjeDef].includes(x.material)) x.material.dispose();
      if (x.isCSS2DObject && x.element.parentNode) x.element.parentNode.removeChild(x.element);
    });
  }
}

// ---------------------------------------------------------------- lectura del IFC
function resolver(ref) {
  if (ref && typeof ref === 'object' && ref.type === 5 && typeof ref.value === 'number') {
    return estado.ifc.GetLine(estado.modelID, ref.value, true);
  }
  return ref;
}
const valores = (lista) => (lista || []).map((c) => (typeof c === 'object' ? c.value : c));

function matrizEjes(a) {
  const loc = valores(resolver(a.Location)?.Coordinates);
  const z = a.Axis ? valores(resolver(a.Axis).DirectionRatios) : [0, 0, 1];
  const x = a.RefDirection ? valores(resolver(a.RefDirection).DirectionRatios) : [1, 0, 0];
  const Z = z.length === 3 ? new THREE.Vector3(...z).normalize() : new THREE.Vector3(0, 0, 1);
  const X = new THREE.Vector3(x[0], x[1], x[2] ?? 0);
  X.addScaledVector(Z, -X.dot(Z)).normalize();
  const Y = new THREE.Vector3().crossVectors(Z, X);
  return new THREE.Matrix4().makeBasis(X, Y, Z).setPosition(loc[0] || 0, loc[1] || 0, loc[2] || 0);
}
function matrizColocacion(ref, prof = 0) {
  const pl = resolver(ref);
  const M = new THREE.Matrix4();
  if (!pl || prof > 30) return M;
  const rel = resolver(pl.RelativePlacement);
  if (rel) M.copy(matrizEjes(rel));
  if (pl.PlacementRelTo) M.premultiply(matrizColocacion(pl.PlacementRelTo, prof + 1));
  return M;
}
const aEscena = (v) => new THREE.Vector3(v.x, v.z, -v.y);    // IFC (Z arriba) -> escena (Y arriba)

function dibujarGrilla() {
  limpiarGrupo(grupoGrilla);
  const ids = estado.ifc.GetLineIDsWithType(estado.modelID, WebIFC.IFCGRID);
  for (let i = 0; i < ids.size(); i++) {
    const g = estado.ifc.GetLine(estado.modelID, ids.get(i), true);
    const M = matrizColocacion(g.ObjectPlacement);
    const ejes = [...(g.UAxes || []), ...(g.VAxes || []), ...(g.WAxes || [])].map(resolver);
    for (const eje of ejes) {
      const curva = resolver(eje.AxisCurve);
      const pts = (curva?.Points || []).map((p) => valores(resolver(p).Coordinates));
      if (pts.length < 2) continue;
      const v = pts.map(([x, y, z = 0]) => aEscena(new THREE.Vector3(x, y, z).applyMatrix4(M)));
      const linea = new THREE.Line(new THREE.BufferGeometry().setFromPoints(v), matGrilla);
      linea.computeLineDistances();
      grupoGrilla.add(linea);
      const div = document.createElement('div');
      div.className = 'eje';
      div.textContent = eje.AxisTag?.value ?? eje.AxisTag ?? '';
      const etiqueta = new CSS2DObject(div);
      etiqueta.position.copy(v[0]).addScaledVector(v[0].clone().sub(v[1]).normalize(), 0.7);
      grupoGrilla.add(etiqueta);
    }
  }
}

const TIPOS = {
  IFCBEAM: 'Viga (IfcBeam)', IFCMEMBER: 'Miembro / tensor (IfcMember)', IFCCOLUMN: 'Columna (IfcColumn)',
  IFCSLAB: 'Losa (IfcSlab)', IFCWALL: 'Muro (IfcWall)', IFCWALLSTANDARDCASE: 'Muro (IfcWall)',
  IFCPLATE: 'Placa (IfcPlate)', IFCFOOTING: 'Zapata (IfcFooting)', IFCMECHANICALFASTENER: 'Conector (IfcMechanicalFastener)',
};
const tipoLegible = (el) => TIPOS[el.tipo] || el.tipoIfc;

async function cargarModelo(datos, nombreArchivo) {
  mostrarCarga(`Leyendo ${nombreArchivo}…`);
  await new Promise((r) => setTimeout(r, 30));
  detenerSimulacion();
  seleccionar(null);
  limpiarGrupo(grupoModelo);
  limpiarGrupo(grupoFantasma);
  limpiarGrupo(grupoAnalitico);
  estado.mallas = [];
  estado.elementos = new Map();
  if (estado.modelID != null) estado.ifc.CloseModel(estado.modelID);
  estado.modelID = estado.ifc.OpenModel(datos, { COORDINATE_TO_ORIGIN: false, CIRCLE_SEGMENTS: 20 });
  const id = estado.modelID;

  const geos = [];
  estado.ifc.StreamAllMeshes(id, (flat) => {
    for (let g = 0; g < flat.geometries.size(); g++) {
      const pg = flat.geometries.get(g);
      const geo = estado.ifc.GetGeometry(id, pg.geometryExpressID);
      const vtx = estado.ifc.GetVertexArray(geo.GetVertexData(), geo.GetVertexDataSize());
      const idx = estado.ifc.GetIndexArray(geo.GetIndexData(), geo.GetIndexDataSize());
      const M = new THREE.Matrix4().fromArray(pg.flatTransformation);
      const N = new THREE.Matrix3().getNormalMatrix(M);
      const nv = vtx.length / 6;
      const pos = new Float32Array(nv * 3);
      const nor = new Float32Array(nv * 3);
      const v = new THREE.Vector3();
      for (let k = 0; k < nv; k++) {
        v.set(vtx[6 * k], vtx[6 * k + 1], vtx[6 * k + 2]).applyMatrix4(M);
        pos[3 * k] = v.x; pos[3 * k + 1] = v.y; pos[3 * k + 2] = v.z;
        v.set(vtx[6 * k + 3], vtx[6 * k + 4], vtx[6 * k + 5]).applyMatrix3(N).normalize();
        nor[3 * k] = v.x; nor[3 * k + 1] = v.y; nor[3 * k + 2] = v.z;
      }
      geos.push({ exp: flat.expressID, pos, nor, idx: Uint32Array.from(idx), color: { ...pg.color } });
      geo.delete();
    }
  });

  const conteo = {};
  for (const d of geos) {
    let el = estado.elementos.get(d.exp);
    if (!el) {
      const linea = estado.ifc.GetLine(id, d.exp);
      const tipoIfc = estado.ifc.GetNameFromTypeCode(estado.ifc.GetLineType(id, d.exp)) || '';
      const tipo = tipoIfc.toUpperCase();
      el = {
        exp: d.exp, guid: linea.GlobalId?.value, nombre: linea.Name?.value ?? '', descripcion: linea.Description?.value ?? '',
        tipo, tipoIfc, mallas: [], miembro: null,
      };
      estado.elementos.set(d.exp, el);
      conteo[tipo] = (conteo[tipo] || 0) + 1;
    }
    const base = [lin(d.color.x), lin(d.color.y), lin(d.color.z)];
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(d.pos.slice(), 3));
    geo.setAttribute('normal', new THREE.BufferAttribute(d.nor, 3));
    const col = new Float32Array(d.pos.length);
    for (let i = 0; i < col.length; i += 3) col.set(base, i);
    geo.setAttribute('color', new THREE.BufferAttribute(col, 3));
    geo.setIndex(new THREE.BufferAttribute(d.idx, 1));
    geo.computeBoundingSphere();
    const transparente = d.color.w < 0.999;
    const mat = new THREE.MeshStandardMaterial({
      vertexColors: true, roughness: 0.55, metalness: 0.2, side: THREE.DoubleSide,
      transparent: transparente, opacity: d.color.w, depthWrite: !transparente,
    });
    const malla = new THREE.Mesh(geo, mat);
    malla.userData.exp = d.exp;
    grupoModelo.add(malla);
    const gf = new THREE.BufferGeometry();
    gf.setAttribute('position', new THREE.BufferAttribute(d.pos, 3));
    gf.setIndex(geo.index);
    grupoFantasma.add(new THREE.Mesh(gf, matFantasma));
    const m = { malla, geo, mat, base: d.pos, colorBase: base, campos: null, comb: null, mag: null, el };
    el.mallas.push(m);
    estado.mallas.push(m);
  }

  // nombre del proyecto y resumen
  const proyectos = estado.ifc.GetLineIDsWithType(id, WebIFC.IFCPROJECT);
  const nombreProyecto = proyectos.size() ? estado.ifc.GetLine(id, proyectos.get(0)).Name?.value : '';
  $('nombreModelo').textContent = nombreProyecto || nombreArchivo;
  const partes = [];
  if (conteo.IFCBEAM) partes.push(`${conteo.IFCBEAM} vigas`);
  if (conteo.IFCMEMBER) partes.push(`${conteo.IFCMEMBER} tensores/miembros`);
  if (conteo.IFCCOLUMN) partes.push(`${conteo.IFCCOLUMN} columnas`);
  const otros = Object.entries(conteo).filter(([k]) => !['IFCBEAM', 'IFCMEMBER', 'IFCCOLUMN'].includes(k))
    .reduce((s, [, n]) => s + n, 0);
  if (otros) partes.push(`${otros} otros elementos`);
  $('resumenModelo').textContent = partes.join(' · ');

  estado.tamano = calcularTamano();
  dibujarGrilla();
  conectarAnalisis();
  encuadrar('iso');
  ocultarCarga();
}

function calcularTamano() {
  const caja = new THREE.Box3();
  for (const m of estado.mallas) caja.union(new THREE.Box3().setFromBufferAttribute(new THREE.BufferAttribute(m.base, 3)));
  return caja.isEmpty() ? 30 : caja.getSize(new THREE.Vector3()).length();
}

// ---------------------------------------------------------------- análisis
function conectarAnalisis() {
  const an = estado.analisis;
  let n = 0;
  if (an) {
    for (const el of estado.elementos.values()) {
      const mb = el.guid && an.miembros.get(el.guid);
      if (!mb) continue;
      el.miembro = mb;
      n++;
      for (const m of el.mallas) {
        m.campos = camposDePosiciones(an, mb, m.base);
        m.comb = new Float32Array(m.base.length);
        m.mag = new Float32Array(m.base.length / 3);
      }
    }
  }
  estado.conAnalisis = n > 0;
  $('btnSimular').disabled = !estado.conAnalisis;
  const aviso = $('avisoSim');
  if (!estado.conAnalisis) {
    aviso.hidden = false;
    aviso.textContent = an
      ? 'Este IFC no coincide con el análisis incluido: solo se puede visualizar.'
      : 'No se encontró el archivo de análisis (modelo/analisis.json): solo se puede visualizar.';
    $('chkAnalitico').disabled = true;
    return;
  }
  aviso.hidden = true;
  $('chkAnalitico').disabled = false;
  construirAnalitico();
  recalcular();
}

function construirAnalitico() {
  limpiarGrupo(grupoAnalitico);
  const an = estado.analisis;
  estado.ejes = [];
  // ejes de los miembros: extremos y punto medio de cada subelemento
  for (const el of estado.elementos.values()) {
    const mb = el.miembro;
    if (!mb) continue;
    const ts = [an.t0[mb.k0]];
    for (let k = mb.k0; k < mb.k0 + mb.nk; k++) ts.push((an.t0[k] + an.t1[k]) / 2, an.t1[k]);
    const pts = new Float32Array(ts.length * 3);
    ts.forEach((t, i) => {
      for (let a = 0; a < 3; a++) pts[3 * i + a] = mb.pI3[a] + mb.e[a] * t * mb.L;
    });
    const original = new THREE.Line(new THREE.BufferGeometry(), matEje);
    original.geometry.setAttribute('position', new THREE.BufferAttribute(pts, 3));
    const deformado = new THREE.Line(new THREE.BufferGeometry(), matEjeDef);
    deformado.geometry.setAttribute('position', new THREE.BufferAttribute(pts.slice(), 3));
    deformado.visible = false;
    deformado.frustumCulled = false;
    grupoAnalitico.add(original, deformado);
    estado.ejes.push({ el, pts, linea: deformado, campos: camposDePosiciones(an, mb, pts), comb: new Float32Array(pts.length) });
  }
  // nudo -> miembro (para describir dónde está el máximo)
  estado.nodoMiembro = new Array(an.nodos.length).fill(null);
  for (const mb of an.miembros.values()) {
    for (let k = mb.k0; k < mb.k0 + mb.nk; k++) {
      for (const n of [an.subA[k], an.subB[k]]) {
        if (!estado.nodoMiembro[n] || (estado.nodoMiembro[n].tipo !== 'viga' && mb.tipo === 'viga')) estado.nodoMiembro[n] = mb;
      }
    }
  }
  // apoyos
  estado.nodos = { campos: camposNodos(an), comb: new Float32Array(an.nodos.length * 3) };
  estado.apoyos = [];
  const s = Math.max(0.12, estado.tamano * 0.006);
  const geoCubo = new THREE.BoxGeometry(2.2 * s, 2.2 * s, 2.2 * s);
  const geoBola = new THREE.SphereGeometry(1.3 * s, 16, 12);
  const matEmp = new THREE.MeshStandardMaterial({ color: 0xe03131, roughness: 0.5 });
  const matPar = new THREE.MeshStandardMaterial({ color: 0xfab005, roughness: 0.5 });
  for (const ap of an.apoyos) {
    const empotrado = ap.r.split(' ').length === 6;
    const marca = new THREE.Mesh(empotrado ? geoCubo : geoBola, empotrado ? matEmp : matPar);
    const p = an.nodos[ap.n];
    marca.position.set(p[0], p[1], p[2]);
    grupoAnalitico.add(marca);
    estado.apoyos.push({ marca, n: ap.n, base: new THREE.Vector3(p[0], p[1], p[2]) });
  }
}

function recalcular() {
  if (!estado.conAnalisis) return;
  const an = estado.analisis;
  const coefs = an.casos.map((c) => estado.coefs[c.id] ?? 0);
  for (const m of estado.mallas) if (m.campos) combinar(m.campos, coefs, m.comb);
  for (const e of estado.ejes) combinar(e.campos, coefs, e.comb);
  combinar(estado.nodos.campos, coefs, estado.nodos.comb);
  let umax = 0;
  let imax = -1;
  const c = estado.nodos.comb;
  for (let i = 0; i < c.length / 3; i++) {
    const u = Math.hypot(c[3 * i], c[3 * i + 1], c[3 * i + 2]);
    if (u > umax) { umax = u; imax = i; }
  }
  estado.umax = umax;
  estado.imax = imax;
  if (estado.escalaAuto) estado.escala = Math.min(ESCALA_MAX, Math.max(1, escalaAuto(umax, objetivo() * estado.tamano)));
  actualizarControlesEscala();
  if (estado.simulando) {
    pintarColores();
    aplicarDeformada();
  }
  actualizarResultado();
  actualizarDespSeleccion();
}

function pintarColores() {
  const umax = estado.umax || 1;
  const tmp = [0, 0, 0];
  for (const m of estado.mallas) {
    const col = m.geo.attributes.color.array;
    if (estado.simulando && m.campos) {
      const nv = col.length / 3;
      for (let i = 0; i < nv; i++) {
        const u = Math.hypot(m.comb[3 * i], m.comb[3 * i + 1], m.comb[3 * i + 2]);
        m.mag[i] = u;
        colorArcoiris(u / umax, tmp, 0);
        col[3 * i] = lin(tmp[0]); col[3 * i + 1] = lin(tmp[1]); col[3 * i + 2] = lin(tmp[2]);
      }
    } else {
      for (let i = 0; i < col.length; i += 3) col.set(m.colorBase, i);
    }
    m.geo.attributes.color.needsUpdate = true;
  }
  // marcas de la leyenda
  const marcas = [0, 0.25, 0.5, 0.75, 1].map((f) => `<span>${num(f * estado.umax * 1000, estado.umax * 1000 < 1 ? 3 : 2)}</span>`);
  $('marcasLeyenda').innerHTML = marcas.join('');
  pedirRender();
}

function aplicarDeformada() {
  const s = estado.factor * estado.escala;
  for (const m of estado.mallas) {
    if (!m.campos) continue;
    const arr = m.geo.attributes.position.array;
    for (let i = 0; i < arr.length; i++) arr[i] = m.base[i] + s * m.comb[i];
    m.geo.attributes.position.needsUpdate = true;
  }
  for (const e of estado.ejes) {
    const arr = e.linea.geometry.attributes.position.array;
    for (let i = 0; i < arr.length; i++) arr[i] = e.pts[i] + s * e.comb[i];
    e.linea.geometry.attributes.position.needsUpdate = true;
    e.linea.visible = estado.simulando;
  }
  const c = estado.nodos?.comb;
  for (const a of estado.apoyos) {
    a.marca.position.set(a.base.x + s * c[3 * a.n], a.base.y + s * c[3 * a.n + 1], a.base.z + s * c[3 * a.n + 2]);
  }
  pedirRender();
}

function animar(t) {
  if (!estado.tInicio) estado.tInicio = t;
  const dt = t - estado.tInicio;
  if (estado.animar) {
    estado.factor = 0.5 - 0.5 * Math.cos((2 * Math.PI * dt) / PERIODO);
  } else {
    if (estado.factor === 1) return;
    const u = Math.min(1, dt / 700);
    const desde = estado.factorDesde || 0;
    estado.factor = u >= 1 ? 1 : desde + (1 - desde) * (1 - Math.pow(1 - u, 3));
  }
  aplicarDeformada();
}

function iniciarSimulacion() {
  if (!estado.conAnalisis) return;
  estado.simulando = true;
  estado.tInicio = 0;
  estado.factor = 0;
  estado.factorDesde = 0;
  recalcular();
  grupoFantasma.visible = estado.original;
  $('leyenda').hidden = false;
  $('resultado').hidden = false;
  const b = $('btnSimular');
  b.classList.add('activo');
  b.querySelector('.icono').textContent = '■';
  $('txtSimular').textContent = 'Detener simulación';
}

function detenerSimulacion() {
  if (!estado.simulando && estado.factor === 0) return;
  estado.simulando = false;
  estado.factor = 0;
  aplicarDeformada();
  pintarColores();
  grupoFantasma.visible = false;
  $('leyenda').hidden = true;
  const b = $('btnSimular');
  b.classList.remove('activo');
  b.querySelector('.icono').textContent = '▶';
  $('txtSimular').textContent = 'Simular deformación';
  actualizarDespSeleccion();
}

function actualizarResultado() {
  const r = $('resultado');
  if (!estado.conAnalisis) { r.hidden = true; return; }
  const mm = estado.umax * 1000;
  $('valUmax').textContent = estado.umax > 0 ? num(mm, mm < 1 ? 3 : 2) : '0';
  let txt = 'desplazamiento máximo';
  if (estado.umax === 0) {
    txt = 'Activa al menos una carga para ver la deformada.';
  } else if (estado.imax >= 0) {
    const p = estado.analisis.nodosIFC?.[estado.imax];
    const mb = estado.nodoMiembro[estado.imax];
    txt = `desplazamiento máximo${mb ? `, en ${mb.tipo === 'viga' ? 'la viga' : 'el tensor'} ${mb.etiqueta}` : ''}`
      + (p ? ` (x = ${num(p[0], 2)}; y = ${num(p[1], 2)}; z = ${num(p[2], 2)} m)` : '');
  }
  $('txtUmax').textContent = txt;
  $('txtEscala').textContent = `Escala ×${estado.escala.toLocaleString('es-CO')}${estado.escalaAuto ? ' (automática)' : ''}: `
    + `en pantalla ${num(mm * estado.escala / 1000, 2)} m`;
}

// ---------------------------------------------------------------- escala
function actualizarControlesEscala() {
  $('numEscala').value = estado.escala;
  $('rngEscala').value = Math.log10(estado.escala).toFixed(2);
  $('btnAuto').classList.toggle('activa', estado.escalaAuto);
  for (const b of document.querySelectorAll('[data-escala]')) {
    b.classList.toggle('activa', !estado.escalaAuto && Number(b.dataset.escala) === estado.escala);
  }
}
function fijarEscala(v) {
  if (!Number.isFinite(v)) return;
  estado.escala = Math.round(Math.min(ESCALA_MAX, Math.max(1, v)));
  estado.escalaAuto = false;
  actualizarControlesEscala();
  if (estado.simulando) aplicarDeformada();
  actualizarResultado();
}

// ---------------------------------------------------------------- selección
const raycaster = new THREE.Raycaster();
let puntoAbajo = null;
renderer.domElement.addEventListener('pointerdown', (e) => { puntoAbajo = [e.clientX, e.clientY]; });
renderer.domElement.addEventListener('pointerup', (e) => {
  if (!puntoAbajo || Math.hypot(e.clientX - puntoAbajo[0], e.clientY - puntoAbajo[1]) > 5) return;
  const r = renderer.domElement.getBoundingClientRect();
  const p = new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
  raycaster.setFromCamera(p, camara);
  for (const m of estado.mallas) m.geo.computeBoundingSphere();
  const hits = raycaster.intersectObjects(grupoModelo.children, false);
  let exp = hits.length ? hits[0].object.userData.exp : null;
  if (exp == null) {
    // los tensores son delgados: se aceptan clics cerca de su eje
    const tol = (mundoPorPixel() * 7) ** 2;
    let mejor = tol;
    const a = new THREE.Vector3();
    const b = new THREE.Vector3();
    for (const el of estado.elementos.values()) {
      const mb = el.miembro;
      if (!mb) continue;
      a.fromArray(mb.pI3);
      b.fromArray(mb.pJ3);
      const d = raycaster.ray.distanceSqToSegment(a, b);
      if (d < mejor) { mejor = d; exp = el.exp; }
    }
  }
  seleccionar(exp);
});
$('btnCerrarInfo').addEventListener('click', () => seleccionar(null));
window.addEventListener('keydown', (e) => { if (e.key === 'Escape') seleccionar(null); });

function mundoPorPixel() {
  const h = renderer.domElement.clientHeight || 1;
  if (camara.isOrthographicCamera) return (camOrto.top - camOrto.bottom) / camOrto.zoom / h;
  const d = camPersp.position.distanceTo(controles.target);
  return (2 * d * Math.tan(THREE.MathUtils.degToRad(camPersp.fov) / 2)) / h;
}

function filasHTML(filas) {
  return `<table class="props"><tbody>${filas.filter(([, v]) => v !== undefined && v !== null && v !== '')
    .map(([k, v]) => `<tr><th>${esc(k)}</th><td>${esc(v)}</td></tr>`).join('')}</tbody></table>`;
}
function valorProp(p) {
  const v = p.NominalValue ?? p.LengthValue ?? p.WeightValue ?? p.VolumeValue ?? p.AreaValue ?? p.CountValue;
  const x = v && typeof v === 'object' ? v.value : v;
  if (typeof x === 'boolean') return x ? 'Sí' : 'No';
  if (typeof x === 'number') {
    return Number.isInteger(x) ? String(x) : x.toLocaleString('es-CO', { maximumFractionDigits: Math.abs(x) >= 100 ? 1 : 3 });
  }
  return x ?? '';
}

async function seleccionar(exp) {
  if (estado.seleccion != null) {
    for (const m of estado.elementos.get(estado.seleccion)?.mallas || []) m.mat.emissive.setHex(0x000000);
  }
  estado.seleccion = exp;
  const info = $('info');
  const tarjeta = $('tarjetaInfo');
  if (exp == null) {
    tarjeta.hidden = true;
    info.innerHTML = '';
    pedirRender();
    return;
  }
  const el = estado.elementos.get(exp);
  for (const m of el.mallas) m.mat.emissive.setHex(0xb06a00);
  pedirRender();
  tarjeta.hidden = false;
  info.innerHTML = `<span class="etiqueta-tipo">${esc(tipoLegible(el))}</span><h3>${esc(el.nombre || 'Sin nombre')}</h3>`;
  const filas = [];
  let detalles = '';
  try {
    const psets = await estado.ifc.properties.getPropertySets(estado.modelID, exp, true);
    const mats = await estado.ifc.properties.getMaterialsProperties(estado.modelID, exp, true);
    const nombresMat = mats.map((m) => m.Name?.value).filter(Boolean);
    if (nombresMat.length) filas.push(['Material', nombresMat.join(', ')]);
    for (const ps of psets) {
      const items = (ps.HasProperties || ps.Quantities || []).map((p) => [p.Name?.value, valorProp(p)]);
      if (!items.length) continue;
      const abierto = ps.Name?.value === 'ETABS' ? ' open' : '';
      detalles += `<details class="pset"${abierto}><summary>${esc(ps.Name?.value)}</summary>${filasHTML(items)}</details>`;
    }
  } catch (e) {
    console.warn('No se pudieron leer las propiedades', e);
  }
  if (estado.seleccion !== exp) return;
  info.innerHTML = `<span class="etiqueta-tipo">${esc(tipoLegible(el))}</span><h3>${esc(el.nombre || 'Sin nombre')}</h3>`
    + (el.descripcion ? `<p class="seccion">${esc(el.descripcion)}</p>` : '')
    + '<div id="infoDesp"></div>' + filasHTML(filas) + detalles + filasHTML([['GlobalId', el.guid]]);
  actualizarDespSeleccion();
}

function actualizarDespSeleccion() {
  const caja = document.getElementById('infoDesp');
  if (!caja) return;
  const el = estado.elementos.get(estado.seleccion);
  const eje = estado.ejes.find((e) => e.el === el);
  if (!el || !eje || !estado.simulando) { caja.innerHTML = ''; return; }
  let u = 0;
  for (let i = 0; i < eje.comb.length; i += 3) u = Math.max(u, Math.hypot(eje.comb[i], eje.comb[i + 1], eje.comb[i + 2]));
  caja.innerHTML = filasHTML([['Desplazamiento máx. del eje', `${num(u * 1000, u * 1000 < 1 ? 3 : 2)} mm`]]);
}

// ---------------------------------------------------------------- vistas
const DIRECCIONES = { iso: [-1, 0.85, 1.25], isoVertical: [-0.35, 1.5, 1], planta: [0, 1, 0.0005], xz: [0, 0, 1], yz: [1, 0, 0] };
function cajaEscena() {
  const caja = new THREE.Box3();
  for (const m of estado.mallas) caja.union(new THREE.Box3().setFromBufferAttribute(new THREE.BufferAttribute(m.base, 3)));
  if (grupoGrilla.visible && !esMovil()) {
    for (const o of grupoGrilla.children) if (o.isLine) caja.union(new THREE.Box3().setFromObject(o));
  }
  return caja;
}
function encuadrar(vista = estado.vista, porUsuario = true) {
  estado.vista = vista;
  if (porUsuario) camaraMovida = false;
  redimensionar();
  const caja = cajaEscena();
  if (caja.isEmpty()) return;
  const centro = caja.getCenter(new THREE.Vector3());
  const radio = caja.getSize(new THREE.Vector3()).length() / 2;
  const vertical = vista === 'iso' && (camPersp.aspect || 1) < 0.85;
  const dir = new THREE.Vector3(...DIRECCIONES[vertical ? 'isoVertical' : vista]).normalize();
  const orto = vista !== 'iso';
  usarCamara(orto ? camOrto : camPersp);
  camara.up.set(0, 1, 0);
  camara.position.copy(centro).addScaledVector(dir, radio * 4);
  camara.lookAt(centro);
  camara.updateMatrixWorld();
  const ejeX = new THREE.Vector3().setFromMatrixColumn(camara.matrixWorld, 0);
  const ejeY = new THREE.Vector3().setFromMatrixColumn(camara.matrixWorld, 1);
  const esquinas = [];
  for (const x of [caja.min.x, caja.max.x]) for (const y of [caja.min.y, caja.max.y]) for (const z of [caja.min.z, caja.max.z]) {
    esquinas.push(new THREE.Vector3(x, y, z).sub(centro));
  }
  const aspecto = camPersp.aspect || 1;
  const margen = esMovil() ? 1.03 : 1.06;
  if (orto) {
    let mx = 0;
    let my = 0;
    for (const e of esquinas) { mx = Math.max(mx, Math.abs(e.dot(ejeX))); my = Math.max(my, Math.abs(e.dot(ejeY))); }
    semiAltoOrto = Math.max(my, mx / aspecto) * margen;
    camOrto.zoom = 1;
    camOrto.near = 0.01;
    camOrto.far = radio * 12;
    ajustarOrto();
  } else {
    const tv = Math.tan(THREE.MathUtils.degToRad(camPersp.fov) / 2);
    const th = tv * aspecto;
    let dist = 0;
    for (const e of esquinas) {
      const haciaCamara = e.dot(dir);
      dist = Math.max(dist, Math.abs(e.dot(ejeX)) / th + haciaCamara, Math.abs(e.dot(ejeY)) / tv + haciaCamara);
    }
    dist *= margen;
    camPersp.position.copy(centro).addScaledVector(dir, dist);
    camPersp.near = Math.max(0.01, dist / 1000);
    camPersp.far = dist * 30;
    camPersp.updateProjectionMatrix();
  }
  controles.target.copy(centro);
  controles.update();
  for (const b of document.querySelectorAll('[data-vista]')) b.classList.toggle('activa', b.dataset.vista === vista);
  pedirRender();
}

// ---------------------------------------------------------------- controles de la página
function enlazar(rango, numero, alCambiar) {
  rango.addEventListener('input', () => { numero.value = rango.value; alCambiar(parseFloat(rango.value)); });
  numero.addEventListener('input', () => {
    const v = parseFloat(numero.value);
    if (Number.isFinite(v)) { rango.value = v; alCambiar(v); }
  });
}
enlazar($('rngQ'), $('numQ'), (v) => { estado.coefs.Q = v; recalcular(); });
enlazar($('rngSX'), $('numSX'), (v) => { estado.coefs.SX = v; recalcular(); });
$('chkPP').addEventListener('change', (e) => { estado.coefs.PP = e.target.checked ? 1 : 0; recalcular(); });
$('rngEscala').addEventListener('input', (e) => fijarEscala(Math.pow(10, parseFloat(e.target.value))));
$('numEscala').addEventListener('change', (e) => fijarEscala(parseFloat(e.target.value)));
for (const b of document.querySelectorAll('[data-escala]')) b.addEventListener('click', () => fijarEscala(Number(b.dataset.escala)));
$('btnAuto').addEventListener('click', () => { estado.escalaAuto = true; recalcular(); });
$('chkAnimar').addEventListener('change', (e) => {
  estado.animar = e.target.checked;
  estado.tInicio = 0;
  estado.factorDesde = estado.factor;
  if (!estado.animar) estado.factor = Math.min(estado.factor, 0.999);
});
$('chkOriginal').addEventListener('change', (e) => {
  estado.original = e.target.checked;
  grupoFantasma.visible = estado.simulando && estado.original;
  pedirRender();
});
$('chkGrilla').addEventListener('change', (e) => { grupoGrilla.visible = e.target.checked; pedirRender(); });
$('chkAnalitico').addEventListener('change', (e) => {
  grupoAnalitico.visible = e.target.checked;
  $('leyendaApoyos').hidden = !e.target.checked;
  pedirRender();
});
$('btnSimular').addEventListener('click', () => (estado.simulando ? detenerSimulacion() : iniciarSimulacion()));
for (const b of document.querySelectorAll('[data-vista]')) b.addEventListener('click', () => encuadrar(b.dataset.vista));
$('btnEncuadrar').addEventListener('click', () => encuadrar());

$('btnMas').addEventListener('click', () => {
  const panel = $('panel');
  const abierta = panel.classList.toggle('expandida');
  $('btnMas').setAttribute('aria-expanded', String(abierta));
  $('txtMas').textContent = abierta ? 'Ocultar opciones' : 'Cargas y opciones';
  if (abierta) panel.scrollTop = 0;
});

async function abrirArchivo(archivo) {
  try {
    const datos = new Uint8Array(await archivo.arrayBuffer());
    await cargarModelo(datos, archivo.name.replace(/\.ifc$/i, ''));
  } catch (e) {
    console.error(e);
    mostrarError(`No se pudo abrir ${archivo.name}. ¿Es un archivo IFC válido?`);
  }
}
$('archivo').addEventListener('change', async (e) => {
  const f = e.target.files[0];
  e.target.value = '';
  if (f) await abrirArchivo(f);
});
visor.addEventListener('dragover', (e) => { e.preventDefault(); $('soltar').hidden = false; });
visor.addEventListener('dragleave', (e) => { if (!visor.contains(e.relatedTarget)) $('soltar').hidden = true; });
visor.addEventListener('drop', async (e) => {
  e.preventDefault();
  $('soltar').hidden = true;
  const f = [...(e.dataTransfer?.files || [])].find((x) => /\.ifc$/i.test(x.name));
  if (f) await abrirArchivo(f);
});

// ---------------------------------------------------------------- inicio
async function iniciar() {
  try {
    mostrarCarga('Iniciando el lector de IFC…');
    estado.ifc = new WebIFC.IfcAPI();
    estado.ifc.SetWasmPath(new URL('../vendor/web-ifc/', import.meta.url).href, true);
    await estado.ifc.Init(undefined, true);
    try {
      const r = await fetch(RUTA_ANALISIS);
      if (r.ok) {
        const json = await r.json();
        estado.analisis = prepararAnalisis(json);
        estado.analisis.nodosIFC = json.nodos;
        if (json.nota) $('notaAnalisis').textContent = `${json.nota}. Resultados aproximados, con fines didácticos.`;
      }
    } catch (e) {
      console.warn('Sin datos de análisis', e);
    }
    mostrarCarga('Descargando el modelo IFC…');
    const r = await fetch(RUTA_IFC);
    if (!r.ok) throw new Error(`No se encontró ${RUTA_IFC} (${r.status})`);
    await cargarModelo(new Uint8Array(await r.arrayBuffer()), RUTA_IFC.split('/').pop().replace(/\.ifc$/i, ''));
  } catch (e) {
    console.error(e);
    mostrarError(`No se pudo cargar el visor: ${e.message || e}`);
  }
}
// acceso desde la consola del navegador (útil para depurar o explorar)
window.visorIFC = { estado, encuadrar, seleccionar, controles, iniciarSimulacion, detenerSimulacion, camara: () => camara };
iniciar();
