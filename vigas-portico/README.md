# Visor IFC · Vigas pórtico

Aplicación web para que los estudiantes vean el modelo IFC en 3D y simulen cómo se deforma la estructura, eligiendo las cargas y la escala de la deformada. Funciona en el navegador (computador o celular), sin instalar nada.

**Enlace:** https://juandavidmiranda55.github.io/visor-ifc/vigas-portico/

## Publicar en GitHub Pages

1. Crea un repositorio nuevo en GitHub (por ejemplo `visor-ifc`). Con una cuenta gratuita debe ser **público** para poder usar Pages.
2. En el repositorio: **Add file → Upload files** y arrastra **todo el contenido** de esta carpeta (`index.html`, `README.md` y las carpetas `css`, `js`, `modelo`, `vendor`, `herramientas`). Luego **Commit changes**.
3. Ve a **Settings → Pages**. En *Build and deployment* elige *Source: Deploy from a branch*, rama `main` y carpeta `/ (root)`. Guarda.
4. En uno o dos minutos la app queda en `https://TU-USUARIO.github.io/visor-ifc/`. Ese es el enlace para los estudiantes.

> Si el repositorio es público, cualquiera puede descargar el archivo IFC de la carpeta `modelo`.

**Probar en tu computador:** abrir `index.html` con doble clic no funciona (el navegador bloquea la lectura de archivos locales). En esta carpeta ejecuta `python -m http.server` y abre `http://localhost:8000`.

## Cómo se usa

- **Navegar:** arrastrar para girar, clic derecho (o dos dedos) para mover, rueda (o pellizco) para acercar. Botones de vista: 3D, Planta, Elevación X-Z y Elevación Y-Z (estas tres en proyección ortográfica).
- **Simular deformación:** activa la deformada animada y la pinta con colores según el desplazamiento (leyenda en mm).
  - *Cargas:* peso propio, carga distribuida sobre las vigas (kN/m, hacia abajo) y sismo en X (coeficiente × peso propio).
  - *Escala:* deslizador, valor exacto o botones ×1, ×10, ×100, ×1000. **Auto** ajusta la escala para que la deformada se vea clara.
  - *Animar:* alterna entre cargar y descargar; sin animación queda la deformada fija.
  - *Mostrar posición original:* deja la estructura sin deformar como una sombra.
- **Clic en un elemento:** muestra su sección, material, propiedades de ETABS, cantidades y su desplazamiento máximo.
- **Capas:** ejes (grilla) y modelo analítico con apoyos (cubo rojo = empotramiento; esfera amarilla = apoyo restringido solo en X e Y).
- **Abrir IFC…:** abre otro IFC desde el computador para verlo (no se sube a ningún servidor). La simulación solo funciona con el modelo incluido o con una versión que conserve los mismos GlobalId.

## Cómo se calcula la deformada

`modelo/analisis.json` se generó a partir del archivo `.e2k` de ETABS con un análisis lineal elástico por el método de rigidez (pórtico 3D con deformación por cortante):

- apoyos y liberaciones de extremo tal como están en ETABS; los tensores se conectan a las vigas donde se apoyan (mallado en intersecciones);
- las vigas de sección variable se dividen en tramos con su altura real;
- casos base: peso propio (los tensores tienen modificador de peso 0 en ETABS), 1 kN/m sobre las vigas y sismo X con coeficiente 1,0. La app los combina linealmente con los valores que se elijan.

Limitaciones: es un modelo simplificado con fines didácticos. No considera efectos de segundo orden ni el desfase del punto cardinal en la rigidez. **No incluye sismo en Y**: el modelo no tiene arriostramiento fuera del plano de los pórticos, y con carga en Y la respuesta es inestable.

## Actualizar el modelo

Si el modelo de ETABS cambia, exporta de nuevo el `.e2k` y regenera los dos archivos (requiere Python con `pip install ifcopenshell numpy`):

```
python herramientas/e2k_to_ifc.py MiModelo.e2k modelo/Vigas_porticos_v3.ifc --name "Vigas porticos v3"
python herramientas/analisis_e2k.py MiModelo.e2k modelo/Vigas_porticos_v3.ifc modelo/analisis.json --name "Vigas porticos v3"
```

Usa el mismo `--name` en los dos comandos (de él dependen los GlobalId que conectan el IFC con el análisis). Si cambias el nombre del archivo IFC, actualiza `RUTA_IFC` al inicio de `js/app.js`. Los scripts convierten vigas, columnas y riostras; los objetos de área (losas y muros) no se convierten.

## Estructura

```
index.html            página principal
css/estilos.css       estilos
js/app.js             visor 3D, interfaz y simulación
js/deformacion.js     interpolación de la deformada a partir del análisis
modelo/               IFC y resultados del análisis
vendor/               three.js 0.187 y web-ifc 0.0.78 (incluidos para no depender de internet)
herramientas/         scripts para regenerar el IFC y el análisis desde el .e2k
```

## Licencias de terceros

- [three.js](https://threejs.org) — licencia MIT (`vendor/three/LICENSE`).
- [web-ifc](https://github.com/ThatOpen/engine_web-ifc) — licencia MPL-2.0 (`vendor/web-ifc/LICENSE.md`).
