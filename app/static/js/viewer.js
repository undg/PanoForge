// PanoForge — visionneuse 360° (three.js vendorisé, sphère UV inversée + VideoTexture)
// Glisser = orienter la vue, molette = zoom (FOV 30-100°) en mode sphère ; lecture/pause
// sur <video>. Gère aussi un mode "projection" (cylindrique / équirect intégral /
// petite planète) : quad plein cadre + shader de reprojection appliqué directement sur
// la texture équirect courante (repris de l'ancien projpreview.js, fusionné ici pour que
// la GRANDE vue de la visionneuse affiche la projection choisie, pas seulement un petit
// aperçu de panneau).

import * as THREE from "/vendor/three.module.js";

const MIN_FOV = 30;
const MAX_FOV = 100;
const DEFAULT_FOV = 75;
const MAX_LAT = 85;

// ---- Mode "projection" : shader plein cadre (cylindrique / équirect / petite planète) ----

const PROJ_IDS = { cylindrical: 0, equirect360: 1, littleplanet: 2 };
const PLANET_FOV_DEG = 250; // champ couvert au bord du disque « petite planète »

const PROJ_FRAG = `
precision highp float;
uniform sampler2D map;
uniform int projMode;       // 0 = cylindrique, 1 = equirect, 2 = petite planète
uniform float yawStart;     // radians
uniform float vSpan;        // radians (hauteur verticale cylindrique)
uniform float rotation;     // radians (petite planète)
uniform float contentAspect; // largeur/hauteur intrinsèque de la projection
uniform float canvasAspect;  // largeur/hauteur du canvas (vue principale)
varying vec2 vUv;

const float PI = 3.141592653589793;

// Échantillonne l'équirect à partir d'une direction (lon, lat) en radians.
vec4 sampleEquirect(float lon, float lat) {
  float u = fract(lon / (2.0 * PI) + 0.5);
  float v = clamp(lat / PI + 0.5, 0.0, 1.0);
  return texture2D(map, vec2(u, v));
}

void main() {
  // Rendu "contain" : la projection garde son propre ratio, le canvas (souvent plus
  // large ou plus étroit) est complété par des bandes noires (letterbox).
  float scaleX, scaleY;
  if (canvasAspect > contentAspect) {
    scaleY = 1.0;
    scaleX = contentAspect / canvasAspect;
  } else {
    scaleX = 1.0;
    scaleY = canvasAspect / contentAspect;
  }
  vec2 uv = (vUv - 0.5) / vec2(scaleX, scaleY) + 0.5;
  if (uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0) {
    gl_FragColor = vec4(0.0, 0.0, 0.0, 1.0);
    return;
  }

  if (projMode == 1) {
    // Équirect intégral : recopie 2:1
    gl_FragColor = texture2D(map, uv);
    return;
  }
  if (projMode == 0) {
    // Cylindrique : x = tour complet 360°, y = tan(lat) borné à ±tan(vSpan/2)
    float lon = yawStart + (uv.x - 0.5) * 2.0 * PI;
    float t = (uv.y - 0.5) * 2.0 * tan(vSpan * 0.5);
    float lat = atan(t);
    gl_FragColor = sampleEquirect(lon, lat);
    return;
  }
  // Petite planète : stéréographique depuis le nadir, rotation autour de l'axe vertical
  vec2 p = (uv - 0.5) * 2.0; // [-1,1]²
  float r = length(p);
  float halfFov = radians(${(PLANET_FOV_DEG / 2).toFixed(1)});
  float theta = 2.0 * atan(r * tan(halfFov * 0.5)); // angle depuis le nadir
  float lat = theta - PI * 0.5;
  float lon = atan(p.y, p.x) + rotation;
  gl_FragColor = sampleEquirect(lon, clamp(lat, -PI * 0.5, PI * 0.5));
}
`;

const PROJ_VERT = `
varying vec2 vUv;
void main() {
  vUv = uv;
  gl_Position = vec4(position.xy, 0.0, 1.0);
}
`;

export class Viewer360 {
  /**
   * @param {HTMLCanvasElement} canvas
   */
  constructor(canvas) {
    this.canvas = canvas;
    this.videoEl = null;
    this.texture = null;
    this.mode = null; // "video" | "image"

    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: false });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;

    // --- Mode "sphere" (projection "flat") : sphère navigable, comportement historique ---
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(DEFAULT_FOV, 1, 0.1, 1000);

    const geometry = new THREE.SphereGeometry(500, 60, 40);
    // Inversion de la sphère (on regarde depuis l'intérieur)
    geometry.scale(-1, 1, 1);
    this.material = new THREE.MeshBasicMaterial({ color: 0x111318 });
    this.mesh = new THREE.Mesh(geometry, this.material);
    this.scene.add(this.mesh);

    // --- Mode "projection" (cylindrical / equirect360 / littleplanet) : quad plein
    //     cadre + shader de reprojection, partageant la même texture équirect. ---
    this.projScene = new THREE.Scene();
    this.projCamera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);
    this.projMaterial = new THREE.ShaderMaterial({
      vertexShader: PROJ_VERT,
      fragmentShader: PROJ_FRAG,
      uniforms: {
        map: { value: null },
        projMode: { value: 0 },
        yawStart: { value: 0 },
        vSpan: { value: (60 * Math.PI) / 180 },
        rotation: { value: 0 },
        contentAspect: { value: 2 },
        canvasAspect: { value: 1 },
      },
      depthTest: false,
      depthWrite: false,
    });
    this.projScene.add(new THREE.Mesh(new THREE.PlaneGeometry(2, 2), this.projMaterial));

    /** "sphere" (flat, comportement historique) | "projection" (plein cadre) */
    this.displayMode = "sphere";
    /** null | "cylindrical" | "equirect360" | "littleplanet" */
    this.projection = null;
    this.projParams = { yawStartDeg: 0, vSpanDeg: 60, rotationDeg: 0 };

    this.lon = 0;
    this.lat = 0;
    this.roll = 0;
    this.fov = DEFAULT_FOV;

    /** Callback optionnel appelé à chaque changement d'orientation/zoom
     *  (glisser, molette, clavier, setOrientation…) : (viewer) => void */
    this.onViewChange = null;
    /** Callback optionnel appelé quand la molette modifie yawStart/rotation en mode
     *  "projection" (permet de resynchroniser les champs numériques du panneau) :
     *  (viewer) => void */
    this.onProjectionParamsChange = null;

    this._dragging = false;
    this._lastX = 0;
    this._lastY = 0;
    this._raf = null;

    this._bindEvents();
    this._resizeToContainer();
    this._resizeObserver = new ResizeObserver(() => this._resizeToContainer());
    this._resizeObserver.observe(canvas.parentElement || canvas);

    this._animate = this._animate.bind(this);
    this._raf = requestAnimationFrame(this._animate);
  }

  _bindEvents() {
    const c = this.canvas;
    c.addEventListener("pointerdown", (e) => {
      this._dragging = true;
      this._lastX = e.clientX;
      this._lastY = e.clientY;
      c.setPointerCapture(e.pointerId);
    });
    c.addEventListener("pointermove", (e) => {
      if (!this._dragging) return;
      const dx = e.clientX - this._lastX;
      const dy = e.clientY - this._lastY;
      this._lastX = e.clientX;
      this._lastY = e.clientY;
      this.lon -= dx * 0.15;
      this.lat = Math.max(-MAX_LAT, Math.min(MAX_LAT, this.lat + dy * 0.15));
      this._emitViewChange();
    });
    const stop = () => {
      this._dragging = false;
    };
    c.addEventListener("pointerup", stop);
    c.addEventListener("pointercancel", stop);
    c.addEventListener("pointerleave", stop);

    c.addEventListener(
      "wheel",
      (e) => {
        e.preventDefault();
        if (this.displayMode === "projection") {
          // Sans objet pour ces projections : la molette ajuste le yaw de départ
          // (cylindrique) ou la rotation (petite planète) au lieu du zoom.
          if (this.projection === "cylindrical") {
            this.setProjectionParams({ yawStartDeg: this.projParams.yawStartDeg + e.deltaY * 0.1 });
          } else if (this.projection === "littleplanet") {
            const next = ((this.projParams.rotationDeg + e.deltaY * 0.1) % 360 + 360) % 360;
            this.setProjectionParams({ rotationDeg: next });
          }
          // equirect360 : aucun réglage orientable, la molette est sans effet.
          return;
        }
        this.setFov(this.fov + e.deltaY * 0.03);
      },
      { passive: false }
    );

    c.addEventListener("keydown", (e) => {
      const step = 6;
      switch (e.key) {
        case "ArrowLeft":
          this.lon -= step;
          break;
        case "ArrowRight":
          this.lon += step;
          break;
        case "ArrowUp":
          this.lat = Math.max(-MAX_LAT, this.lat - step);
          break;
        case "ArrowDown":
          this.lat = Math.min(MAX_LAT, this.lat + step);
          break;
        case "+":
        case "=":
          this.setFov(this.fov - 5);
          break;
        case "-":
          this.setFov(this.fov + 5);
          break;
        case " ":
          this.togglePlayPause();
          e.preventDefault();
          break;
        default:
          return;
      }
      this._emitViewChange();
      e.preventDefault();
    });
  }

  _emitViewChange() {
    if (typeof this.onViewChange === "function") this.onViewChange(this);
  }

  _resizeToContainer() {
    const el = this.canvas.parentElement || this.canvas;
    const w = Math.max(1, el.clientWidth);
    const h = Math.max(1, el.clientHeight);
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.projMaterial.uniforms.canvasAspect.value = w / h;
    this._emitViewChange();
  }

  // ---- Mode "projection" (cylindrical / equirect360 / littleplanet) ----

  /** Ratio largeur/hauteur intrinsèque de la projection courante (pour le letterbox). */
  _contentAspect() {
    if (this.projection === "littleplanet") return 1;
    if (this.projection === "equirect360") return 2;
    if (this.projection === "cylindrical") {
      const vspanRad = (Math.max(1, this.projParams.vSpanDeg) * Math.PI) / 180;
      return Math.PI / Math.max(0.001, Math.tan(vspanRad / 2));
    }
    return 1;
  }

  _syncProjUniforms() {
    const mode = PROJ_IDS[this.projection];
    const u = this.projMaterial.uniforms;
    u.projMode.value = mode === undefined ? 1 : mode;
    u.yawStart.value = (this.projParams.yawStartDeg * Math.PI) / 180;
    u.vSpan.value = (Math.max(1, this.projParams.vSpanDeg) * Math.PI) / 180;
    u.rotation.value = (this.projParams.rotationDeg * Math.PI) / 180;
    u.contentAspect.value = this._contentAspect();
  }

  /**
   * Bascule la vue principale entre "sphere" (flat, comportement historique) et
   * "projection" (cylindrical/equirect360/littleplanet rendus plein cadre).
   * @param {"cylindrical"|"equirect360"|"littleplanet"|"flat"|null} projection
   * @param {{yawStartDeg?: number, vSpanDeg?: number, rotationDeg?: number}} params
   */
  setProjectionMode(projection, params = {}) {
    const active = Boolean(projection) && projection !== "flat" && PROJ_IDS[projection] !== undefined;
    this.displayMode = active ? "projection" : "sphere";
    this.projection = active ? projection : null;
    if (active) {
      Object.assign(this.projParams, params);
      this._syncProjUniforms();
    }
  }

  /**
   * Met à jour les paramètres de la projection courante (yaw de départ, hauteur
   * verticale cylindrique, rotation petite planète). Utilisé par la synchro
   * champs → vue et par la molette.
   */
  setProjectionParams(partial = {}, { silent = false } = {}) {
    Object.assign(this.projParams, partial);
    this._syncProjUniforms();
    if (!silent && typeof this.onProjectionParamsChange === "function") {
      this.onProjectionParamsChange(this);
    }
  }

  setFov(v) {
    this.fov = Math.max(MIN_FOV, Math.min(MAX_FOV, v));
    this.camera.fov = this.fov;
    this.camera.updateProjectionMatrix();
    this._emitViewChange();
  }

  resetView() {
    this.lon = 0;
    this.lat = 0;
    this.roll = 0;
    this.setFov(DEFAULT_FOV);
  }

  /** Yaw courant normalisé dans [-180, 180]. */
  get yaw() {
    let y = this.lon % 360;
    if (y > 180) y -= 360;
    if (y < -180) y += 360;
    return y;
  }

  /** Pitch courant en degrés (positif = vers le haut). */
  get pitch() {
    return this.lat;
  }

  /** Oriente la vue (utilisé par la synchro champs → vue du panneau photo). */
  setOrientation(yawDeg, pitchDeg, rollDeg) {
    if (yawDeg != null && !Number.isNaN(yawDeg)) this.lon = yawDeg;
    if (pitchDeg != null && !Number.isNaN(pitchDeg))
      this.lat = Math.max(-MAX_LAT, Math.min(MAX_LAT, pitchDeg));
    if (rollDeg != null && !Number.isNaN(rollDeg)) this.roll = rollDeg;
    this._emitViewChange();
  }

  /** Temps courant de la vidéo en secondes (null si aucune vidéo). */
  get currentTime() {
    return this.videoEl ? this.videoEl.currentTime : null;
  }

  /** FOV horizontal courant de la vue, en degrés. */
  get hFov() {
    const vfovRad = (this.fov * Math.PI) / 180;
    return (2 * Math.atan(Math.tan(vfovRad / 2) * this.camera.aspect) * 180) / Math.PI;
  }

  _clearMedia() {
    if (this.videoEl) {
      this.videoEl.pause();
      this.videoEl.removeAttribute("src");
      this.videoEl.load();
      this.videoEl = null;
    }
    if (this.texture) {
      this.texture.dispose();
      this.texture = null;
    }
    // Ne pas laisser la sphère pointer vers une texture disposée (canvas noir/corrompu)
    this.material.map = null;
    this.material.color.set(0x111318);
    this.material.needsUpdate = true;
  }

  /**
   * Charge une vidéo (sortie convertie ou fichier source servi par /api/media).
   * @param {string} url
   * @returns {Promise<void>}
   */
  loadVideo(url) {
    this._clearMedia();
    this.mode = "video";
    const video = document.createElement("video");
    video.crossOrigin = "anonymous";
    video.loop = true;
    video.playsInline = true;
    video.preload = "auto";
    video.src = url;
    this.videoEl = video;

    return new Promise((resolve, reject) => {
      const onReady = () => {
        video.removeEventListener("loadeddata", onReady);
        // Chrome/Linux : sur un HEVC 10-bit non décodable, 'loadeddata' se déclenche
        // quand même mais videoWidth vaut 0 → il ne faut pas laisser un canvas noir.
        if (!video.videoWidth || !video.videoHeight) {
          this._clearMedia();
          reject(
            new Error(
              "Le navigateur ne peut pas décoder cette vidéo (HEVC). " +
                "L'aperçu H.264 est en cours de préparation ou utilisez un export H.264."
            )
          );
          return;
        }
        this.texture = new THREE.VideoTexture(video);
        this.texture.colorSpace = THREE.SRGBColorSpace;
        this.texture.minFilter = THREE.LinearFilter;
        this.texture.magFilter = THREE.LinearFilter;
        this.material.map = this.texture;
        this.material.color.set(0xffffff);
        this.material.needsUpdate = true;
        resolve();
      };
      video.addEventListener("loadeddata", onReady);
      video.addEventListener(
        "error",
        () => {
          // MEDIA_ERR_DECODE (3) / MEDIA_ERR_SRC_NOT_SUPPORTED (4) = codec non pris en
          // charge (typiquement HEVC 10-bit sous Chrome/Linux) → même message clair
          // que pour le cas loadeddata + videoWidth=0.
          const code = video.error ? video.error.code : 0;
          const msg =
            code === 3 || code === 4
              ? "Le navigateur ne peut pas décoder cette vidéo (HEVC). " +
                "L'aperçu H.264 est en cours de préparation ou utilisez un export H.264."
              : "Impossible de charger la vidéo pour l'aperçu 360°.";
          reject(new Error(msg));
        },
        { once: true }
      );
    });
  }

  /**
   * Charge une image statique (ex : miniature équirectangulaire embarquée).
   * @param {string} url
   * @returns {Promise<void>}
   */
  loadImage(url) {
    this._clearMedia();
    this.mode = "image";
    const loader = new THREE.TextureLoader();
    return new Promise((resolve, reject) => {
      loader.load(
        url,
        (tex) => {
          tex.colorSpace = THREE.SRGBColorSpace;
          this.texture = tex;
          this.material.map = tex;
          this.material.color.set(0xffffff);
          this.material.needsUpdate = true;
          resolve();
        },
        undefined,
        () => reject(new Error("Impossible de charger l'image d'aperçu."))
      );
    });
  }

  play() {
    if (this.videoEl) this.videoEl.play().catch(() => {});
  }

  pause() {
    if (this.videoEl) this.videoEl.pause();
  }

  togglePlayPause() {
    if (!this.videoEl) return;
    if (this.videoEl.paused) this.play();
    else this.pause();
  }

  get isPaused() {
    return !this.videoEl || this.videoEl.paused;
  }

  _animate() {
    this._raf = requestAnimationFrame(this._animate);
    if (this.texture && this.mode === "video") {
      // Marche aussi en mode "projection" : la texture vidéo doit être rafraîchie
      // dans les deux modes.
      this.texture.needsUpdate = true;
    }

    if (this.displayMode === "projection") {
      this.projMaterial.uniforms.map.value = this.texture;
      this.renderer.render(this.projScene, this.projCamera);
      return;
    }

    const phi = THREE.MathUtils.degToRad(90 - this.lat);
    const theta = THREE.MathUtils.degToRad(this.lon);
    const target = new THREE.Vector3(
      500 * Math.sin(phi) * Math.cos(theta),
      500 * Math.cos(phi),
      500 * Math.sin(phi) * Math.sin(theta)
    );
    this.camera.lookAt(target);
    if (this.roll) {
      // Roulis autour de l'axe de visée (synchro avec le champ « roll » du panneau photo)
      this.camera.rotateZ((-this.roll * Math.PI) / 180);
    }
    this.renderer.render(this.scene, this.camera);
  }

  dispose() {
    if (this._raf) cancelAnimationFrame(this._raf);
    this._resizeObserver.disconnect();
    this._clearMedia();
    this.mesh.geometry.dispose();
    this.material.dispose();
    this.projMaterial.dispose();
    this.renderer.dispose();
  }
}
