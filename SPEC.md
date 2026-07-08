# PanoForge — Spécification d'architecture

> **Renommage (2026-07-08)** : le produit s'appelle désormais **PanoForge** (ancien nom
> « Osmo 360 Studio », abandonné pour éviter la marque DJI « Osmo »). « DJI » et « Osmo »
> ne doivent apparaître QUE dans des phrases de compatibilité (« pour les fichiers .OSV
> des caméras DJI Osmo 360 »), jamais dans le nom du produit, du dépôt, ni le logo.
> README : ajouter un disclaimer « projet indépendant, non affilié à DJI ».
> Migration config/cache : voir section « Renommage » plus bas.

Appli web **locale** (Ubuntu/GNOME) : backend Python (FastAPI) + interface navigateur.
Convertit les fichiers `.OSV` de la DJI Osmo 360 en MP4 360° équirectangulaire, avec
métadonnées sphériques, GPS optionnel depuis GPX externe (piste CAMM), file d'attente
par lot et aperçu 360° interactif. Public : un seul utilisateur, sur sa machine (pas
d'auth, bind 127.0.0.1).

## Faits établis (phase d'analyse — ne pas re-vérifier)

- `.OSV` = MP4 isom : stream 0 et 1 = fisheyes HEVC 10-bit 3840×3840 (>180°),
  stream 2 = AAC, streams djmd = métadonnées protobuf DJI, dernier stream = miniature
  équirectangulaire MJPEG 688×344 (référence de stitching).
- Calibration optique d'usine embarquée dans le 1er échantillon djmd : par objectif
  fx/fy, cx/cy, distorsion (4 coeffs type Brown-Conrady, à valider), yaw/pitch,
  quaternion extrinsèque, 2 LUT radiales. Extraction : `app/core/osv_meta/extract_djmd.py`
  (produit `calibration.json`, `imu_perframe.csv`, `imu_highrate.csv` ~995 Hz quaternions).
  Exemple réel : `tests/fixtures/calibration.json`.
- Pas de GPS interne dans la caméra → GPX externe = seule source géo.
- Stitching baseline validé empiriquement (ffmpeg 8.0) :
  `[0:0][0:1]hstack[s];[s]v360=input=dfisheye:output=e:ih_fov=190:iv_fov=190:yaw=90:w=7680:h=3840:interp=lanczos`
  → alignement correct vs miniature. Défaut : parallaxe visible sur objets proches (pas de blending).
- GTX 1650 : `hevc_nvenc`/`h264_nvenc` OK jusqu'à 7680×3840. Goulot = filtre v360 CPU
  (~3.6 fps en lanczos 8K, ~7.4 fps bilinéaire).
- Street View Studio : MP4/MOV équirect 2:1, durée 2–60 min, GPS sans trou > 5 s,
  ≥ 10 points, stabilisation DÉSACTIVÉE pour Street View. GPS embarqué = piste CAMM
  (paquets type 6 : `uint16 reserved=0, uint16 type=6, double time_gps_epoch,
  int32 gps_fix_type, double lat, double lon, float alt, float h_acc, float v_acc,
  float vel_e, float vel_n, float vel_up, float speed_acc`, little-endian, PTS monotone).
  Réf. code : trek-view/telemetry-injector (attention : il n'aligne PAS temporellement
  GPX↔vidéo, bug à ne pas reproduire).
- Métadonnées sphériques : V1 (boîte uuid XML GSpherical) + V2 (sv3d/proj/equi) — écrire les deux.

## Arborescence

```
osmo360-studio/
  SPEC.md  README.md  requirements.txt  run.sh
  app/
    main.py            # FastAPI app + uvicorn entry (port 8360, 127.0.0.1)
    api.py             # routes REST (contrat ci-dessous)
    jobs.py            # file d'attente + exécution ffmpeg + progression
    config.py          # chemins par défaut (source ; sortie = `xdg-user-dir VIDEOS`/osmo360, soit ~/Vidéos/osmo360 ici, repli ~/Videos puis ~)
    core/
      osv.py           # probe ffprobe + wrapper extract_djmd → OsvInfo, metadata
      osv_meta/        # extraction djmd (livré, ne pas réécrire) 
      maps.py          # calibration.json → cartes remap ffmpeg + masques de blending
      stitch.py        # constructeur de commandes ffmpeg (modes v360 / calibrated)
      gpx.py           # parsing GPX, fenêtrage, interpolation, offset
      camm.py          # muxage piste CAMM dans un MP4 existant
      spherical.py     # injection métadonnées sphériques V1+V2
    static/            # frontend (vanilla JS + three.js vendorisé dans static/vendor/)
  tests/               # pytest ; fixtures/ = calibration réelle
  tools/               # notes rétro-ingénierie
```

## Contrats des modules core (signatures à respecter)

```python
# osv.py
@dataclass
class OsvInfo:
    path: str; duration_s: float; fps: float; width: int; height: int
    creation_time_utc: datetime | None; size_bytes: int; audio: bool
def probe(path: str) -> OsvInfo
def extract_metadata(path: str, workdir: str) -> dict   # {"calibration": dict|None, "imu_perframe": str, "imu_highrate": str}
def extract_thumbnail(path: str, out_jpg: str) -> str    # miniature équirect embarquée

# maps.py
def generate_remap_maps(calibration: dict, out_w: int, out_h: int, workdir: str) -> MapSet
# MapSet: xmap/ymap 16-bit PGM par objectif (filtre ffmpeg `remap`) + masque(s) de blending
# (dégradé au niveau des coutures, PNG gris). Doit gérer le fallback calibration=None.

# stitch.py
@dataclass
class StitchOptions:
    out_w: int = 7680            # 7680, 6144 ou 3840 (h = w/2)
    codec: str = "hevc"          # "hevc" | "h264"
    encoder: str = "auto"        # "auto"→nvenc si dispo sinon cpu ; "nvenc" | "cpu"
    quality: int = 20            # cq nvenc / crf cpu
    interp: str = "lanczos"      # "lanczos" | "line"
    mode: str = "auto"           # "auto"→calibrated si maps dispo sinon v360 ; "v360" | "calibrated"
    fps_out: float | None = None # None = conserver ; sinon ex. 5 pour Street View
def build_command(input_path, output_path, opts, maps: MapSet | None) -> list[str]
# Mode v360 = commande baseline ci-dessus. Mode calibrated = split → remap par objectif
# → blend par masque (maskedmerge/mergeplanes ou blend) → sortie. Audio copié.
# Toujours ajouter -progress pipe:1 -nostats pour le suivi.

# gpx.py
@dataclass
class GpxPoint: t: datetime; lat: float; lon: float; ele: float | None; speed: float | None
def parse_gpx(path) -> list[GpxPoint]                    # stdlib xml, tri chronologique, UTC
def analyze(points, video_start_utc, duration_s) -> dict # {overlap_s, coverage_pct, gaps>5s, n_points_in_window, suggested_offset_s}
def resample(points, video_start_utc, duration_s, offset_s, rate_hz=1.0) -> list[GpxPoint]
# fenêtrage [start+offset, start+offset+durée], interpolation linéaire, vitesse E/N calculée

# camm.py
def inject_camm(mp4_in, mp4_out, samples: list[GpxPoint], video_start_utc) -> None
# nouvelle piste meta/camm, paquets type 6, timescale fin, PTS monotone

# spherical.py
def inject_spherical(mp4_in, mp4_out) -> None            # V1 uuid XML + V2 sv3d, mono
def export_windowed_gpx(points, video_start_utc, duration_s, offset_s, out_path) -> None
```

## Pipeline d'un job « convert »

1. `probe` + `extract_metadata` (workdir = dossier temp du job)
2. `generate_remap_maps` si mode calibrated
3. ffmpeg stitch → MP4 temporaire (progression parsée depuis `-progress`)
4. `inject_spherical` (toujours)
5. si GPX fourni : `resample` + `inject_camm` (+ `export_windowed_gpx` en side-car)
6. déplacement atomique vers le dossier de sortie : `<nom>_360.mp4`

Profil « Street View » = préréglage UI : fps_out=5, CAMM requis, stabilisation off, HEVC cq 20.

## API REST (contrat backend ↔ frontend)

- `GET  /api/config` → `{source_dir, output_dir, has_nvenc, version}`
- `POST /api/config` → maj dossiers
- `GET  /api/files?dir=` → `[{path, name, size_bytes, mtime, duration_s?, thumb_url}]` (*.OSV, récursif 1 niveau)
- `GET  /api/thumb?path=` → JPEG miniature embarquée (cache disque)
- `POST /api/probe {path}` → OsvInfo + `{has_calibration: bool}`
- `POST /api/gpx/analyze {gpx_path, video_path, offset_s?}` → retour de `gpx.analyze`
- `POST /api/jobs {inputs: [path], options: StitchOptions-like + {gpx_path?, gpx_offset_s?, embed_camm: bool, streetview: bool}}` → `[{job_id}]` (un job par fichier)
- `GET  /api/jobs` → liste `{id, input, output, status: queued|running|done|error|cancelled, progress: 0..1, fps, eta_s, error?}`
- `DELETE /api/jobs/{id}` → annule (kill ffmpeg si running)
- `GET  /api/media?path=` → sert un fichier vidéo avec support **Range** (aperçu three.js)
- `GET  /api/browse?dir=&filter=` → navigateur de fichiers/dossiers pour l'UI :
  `{dir, parent: str|null, dirs: [{name, path, mtime}], files: [{name, path, size_bytes, mtime}]}`
  (`mtime` = date de modification en secondes epoch, affichée dans le sélecteur ;
  le sélecteur tronque le radical mais garde l'extension visible + infobulle du nom complet).
  `dir` par défaut = home. `filter` = extensions séparées par des virgules, insensible à la
  casse (ex. `osv` ou `gpx`) ; sans `filter`, `files` reste vide (choix de dossier).
  Navigation restreinte à $HOME, /run/media et /media ; entrées cachées (.*) exclues ;
  dossiers triés avant fichiers, ordre alphabétique. Hors périmètre autorisé → 403.
- `GET  /` → frontend statique

Un seul job ffmpeg à la fois (worker thread + queue). État en mémoire (pas de BD).

## Frontend (app/static/)

Vanilla JS + three.js vendorisé (PAS de CDN à l'exécution). Interface en **français**.
3 vues : **Fichiers** (grille avec miniatures, sélection multiple, bouton Convertir),
**File d'attente** (progression, annulation, ouvrir le dossier), **Aperçu 360°**
(three.js : sphère inversée + VideoTexture, glisser pour tourner, molette = zoom/FOV,
lecture/pause ; fonctionne sur la sortie convertie ET en pré-visualisation de la
miniature embarquée). Panneau options de conversion (résolution, codec, qualité,
interpolation, mode stitching, profil Street View, GPX : fichier + analyse + curseur
d'offset en secondes avec retour visuel de couverture).
Polling `GET /api/jobs` toutes les 1 s. Thème sombre simple, CSS maison.

## Extraction de photos 360 (fonction « photo »)

Extraire des photos depuis trois sources : MP4 360° converti (seek au temps choisi),
`.OSV` brut (stitching calibré d'UNE frame, pleine résolution), photo JPEG 360° de la
caméra (équirect 2:1, utilisée telle quelle). Sortie : JPEG qualité 95 dans
`<output_dir>/photos/<basename>_<temps>_<projection>.jpg`.

- `app/core/photo.py` :
  - `get_equirect_frame(source_path, time_s, workdir) -> str` — PNG équirect pleine
    résolution selon le type de source (pour l'OSV : réutiliser maps/stitch existants).
  - `reproject(equirect_path, projection, params, out_jpg, quality=95) -> (w, h)` —
    via ffmpeg v360 :
    - `flat` : perspective (yaw/pitch/roll, `h_fov` 30–140°, ratio parmi 16:9, 21:9,
      32:9, 4:3, 1:1, 9:16 **ou libre : toute chaîne « a:b »** avec a, b > 0 et
      a/b ∈ [0.2, 8], sinon 400 explicite ; `v_fov = 2·atan(tan(h_fov/2)·h/w)` pour
      une perspective correcte, pas d'étirement).
    - `cylindrical` : tour complet 360°, `v_span_deg` réglable (déf. 60°), yaw de départ.
    - `equirect360` : équirect 2:1 complet + **XMP GPano** injecté (ProjectionType,
      UsePanoramaViewer, dimensions FullPano) → photo sphérique interactive
      Google Photos/Facebook.
    - `littleplanet` : stéréographique regard vers le bas (pitch −90°, roll = rotation).
  - `nav_proxy(source_path, cache_dir) -> str` — proxy de navigation équirect basse
    résolution (~688 px, H.264 rapide) pour choisir l'instant dans un OSV que le
    navigateur ne sait pas lire ; caché dans previews.
- API :
  - `POST /api/photo/extract {source_path, time_s, projection, yaw_deg, pitch_deg,
    roll_deg, h_fov_deg, ratio, v_span_deg, out_w}` → `{photo_path, preview_url,
    width, height}` (synchrone ; out_w défaut = largeur max de la source).
  - `GET /api/photo/navproxy?path=` → `{proxy_url}` (génère + cache).
- UI (vue Aperçu 360°) : bouton « Ouvrir un fichier… » (browse, filtre osv,mp4,jpg,jpeg)
  et panneau « Extraire une photo » : projection, ratio (préréglages + « Libre » avec
  champs a:b), FOV, champs numériques yaw/pitch/roll **synchronisés dans les deux
  sens** avec la vue de la visionneuse, overlay du cadre de capture (zone exacte selon
  ratio+FOV), résolution, bouton Extraire → aperçu du résultat + chemin du fichier.
  Pour cylindrical/equirect360/littleplanet, ne montrer que les réglages pertinents.
- **Cadre interactif** (projection flat) : l'overlay se manipule à la souris —
  glisser l'intérieur = déplace la visée (yaw/pitch), tirer les poignées coins/bords =
  ajuste le FOV (ratio préréglé : homothétie) ou le ratio (mode Libre : les champs a:b
  suivent). Curseurs adaptés (move/resize), synchro continue avec les champs numériques.
- **Prévisualisation temps réel des autres projections** (cylindrical, equirect360,
  littleplanet) : panneau d'aperçu rendu CÔTÉ CLIENT en WebGL (shader appliquant la
  même reprojection que le backend sur la texture équirect courante de la visionneuse,
  basse résolution ~512 px, mis à jour en continu — y compris pendant la lecture et
  quand les réglages changent). L'extraction serveur reste la référence pleine qualité.

## Prévisualisation des projections dans la vue principale (évolution)

Les prévisualisations cylindrique / équirect / petite planète ne doivent PLUS se limiter
au petit canvas du panneau : la **vue principale** (grand canvas de la visionneuse) rend
directement la projection sélectionnée.
- `viewer.js` gère deux modes : `sphere` (projection « flat » — sphère navigable +
  cadre interactif, comportement actuel) et `projection` (cylindrical/equirect360/
  littleplanet — rend la projection plein cadre via le shader de `projpreview.js`,
  fusionné dans viewer.js ou piloté par lui sur le canvas principal). Changer de
  projection bascule le mode ; revenir à « flat » restaure la sphère.
- Le petit canvas du panneau devient inutile → le retirer (ou le garder comme vignette
  seulement si trivial). La note « prévisualisation basse résolution » reste.
- **Yaw de départ à la molette** : en mode cylindrical (et rotation en petite planète),
  la molette sur la vue principale ajuste le yaw de départ / la rotation (au lieu du
  zoom FOV, sans objet pour ces projections). Le champ numérique reste synchronisé.
  Molette = zoom uniquement en mode sphere/flat.

## Navigation vers les volumes amovibles / caméra (évolution)

Le sélecteur de fichiers doit donner un accès rapide aux supports amovibles.
- `GET /api/browse/roots` → `{shortcuts: [{label, path, kind}]}` où `kind` ∈
  `home | removable | source | output | camera`. Détection live à chaque appel :
  - `home` : $HOME.
  - `removable` : chaque sous-dossier monté de `/run/media/$USER` et `/media/$USER`
    (label = nom du volume, ex. « SD_Card »).
  - `camera` (best-effort) : montages MTP sous `/run/user/<uid>/gvfs/` dont le nom
    contient l'appareil (préfixe `mtp:` / `gphoto2:`) — listés si présents, ignorés
    sinon ; documenter que le mode « stockage USB » de l'Osmo apparaît plutôt en
    `removable`.
  - `source`/`output` : dossiers configurés courants.
- Les racines autorisées du browse incluent déjà `/run/media` et `/media` ; ajouter
  `/run/user/<uid>/gvfs` à la liste blanche pour la caméra MTP.
- UI (`filebrowser.js`) : colonne/bandeau « Accès rapide » listant ces raccourcis
  (icône par kind), un clic navigue vers le dossier. Rafraîchi à l'ouverture du modal.

## Stabilisation gyroscopique (phase 2 — activation)

Objectif : niveler l'horizon et lisser les secousses en contre-tournant chaque frame
selon l'orientation IMU, comme le fait la caméra sur sa miniature. Données déjà
extraites par `extract_djmd.py` : `imu_highrate.csv` (~1 kHz quaternions) et
`imu_perframe.csv`. **Convention d'axes/ordre du quaternion à VALIDER empiriquement**
avant tout (cf. NOTES-djmd : `[w,x,y,z]` supposé, repère à confirmer).

- `app/core/stabilize.py` :
  - `load_orientations(imu_csv, fps, n_frames, time_base) -> list[quat]` — une
    orientation par frame vidéo (rééchantillonnage/slerp depuis le flux haute
    fréquence, aligné sur les PTS).
  - `compute_corrections(quats, mode, params) -> list[(yaw,pitch,roll)]` en degrés,
    à appliquer en sortie équirect (rotation v360 après stitch) :
    - `horizon` : neutralise pitch+roll (horizon nivelé), conserve le yaw (cap).
    - `lock` : verrouille l'orientation absolue (contre-rotation totale vers une réf).
    - `smooth` : suit une orientation lissée (filtre passe-bas / moyenne glissante,
      fenêtre réglable) → supprime les secousses en gardant les mouvements lents.
  - Doit gérer l'absence d'IMU (retourne des corrections nulles + drapeau).
- Intégration `stitch.py` : `StitchOptions.stabilize: bool` + `stabilize_mode` +
  `stabilize_strength`. Quand actif, injecter les rotations par frame dans le filtre
  `v360` via `sendcmd`/`zmq` (yaw/pitch/roll variables dans le temps) — en mode
  calibrated comme v360. Rester compatible NVENC.
- `jobs.py` : appeler stabilize dans le pipeline avant l'encodage quand l'option est
  active ; **le profil Street View force `stabilize=False`** (exigence Google).
- UI : dégriser le champ « Stabilisation » (case + choix du mode + curseur de force),
  désactivé et explicitement verrouillé quand « Profil Street View » est coché.
- Validation empirique OBLIGATOIRE sur un vrai clip à main levée
  (`~/Vidéos/osmo360/echantillons/CAM_20260708072843_0001_D.OSV`, 6,3 s) : comparer
  des frames avant/après, vérifier que l'horizon est nivelé et que la dérive est
  réduite, en REGARDANT les images. Documenter la convention d'axe retenue.

## Réorganisation ergonomie (2026-07-08 — maquette validée)

Maquette de référence validée par l'utilisateur : `work/ux/maquette_validee.html`
(REGARDER pour la disposition cible). Principe : **la vue « Fichiers » devient le point
d'entrée UNIQUE de tout chargement** ; la visionneuse n'est plus qu'une destination.

Changements à implémenter (frontend `app/static/`) :
1. **Barre d'outils en tête de la vue Fichiers** regroupant ce qui était éparpillé :
   - dossier source courant + bouton « Parcourir… » (ouvre le filebrowser, kind dossier) ;
   - bandeau « Accès rapide » (raccourcis home/removable/camera via /api/browse/roots) —
     le MÊME composant qu'aujourd'hui, mais affiché ici en permanence, pas seulement
     dans le modal ;
   - bouton **« Ouvrir un fichier… »** (filtre osv,mp4,jpg,jpeg) — **DÉPLACÉ** depuis la
     vue Aperçu vers cette barre. C'est le seul point d'ouverture d'un média arbitraire.
2. **Chaque carte de fichier expose deux actions explicites** : « Convertir » (→ panneau
   d'options, comportement actuel) et « Ouvrir en 360° » (→ charge le média dans la
   visionneuse et bascule sur l'onglet Aperçu). La sélection multiple + « Convertir la
   sélection » restent pour le lot.
3. **La vue Aperçu 360° perd son bouton « Ouvrir un fichier… »** : elle ne sert plus qu'à
   regarder + extraire. Quand aucun média n'est chargé, afficher un état vide qui renvoie
   vers l'onglet Fichiers (« Pour ouvrir un média, passez par l'onglet Fichiers »), pas un
   bouton d'ouverture. Les entrées existantes (aperçu d'un job terminé, « Ouvrir en 360° »
   d'une carte) restent les façons d'y charger un média.
4. Réglages ⚙ : le dossier source/sortie peut rester dans les Réglages, mais le dossier
   source doit AUSSI être pilotable depuis la barre d'outils Fichiers (source de vérité
   partagée). Éviter la duplication de logique.
Conserver l'accessibilité clavier, le thème sombre, et tout le reste du comportement
(extraction photo, préviews dans la vue principale, molette, stabilisation) intact.

## Renommage « Osmo 360 Studio » → « PanoForge »

- **Chaînes visibles** (titre HTML `<title>`, en-tête « Osmo 360 Studio » de l'UI,
  README, `run.sh`/`lancer.sh` messages, `.desktop` Name/Comment) → « PanoForge ».
- **Nom de dossier projet** : laisser `osmo360-studio/` tel quel pour ne pas casser les
  chemins de cette session (le dépôt Git pourra être nommé PanoForge au push ; hors
  périmètre code).
- **Config/cache utilisateur** : passer de `~/.config/osmo360-studio` et
  `~/.cache/osmo360-studio` vers `~/.config/panoforge` et `~/.cache/panoforge`, AVEC
  migration douce au démarrage : si l'ancien dossier existe et le nouveau non, le
  déplacer (ou copier config.json). Ne pas perdre la config actuelle de l'utilisateur
  (source_dir/output_dir déjà personnalisés).
- **Dossier de sortie par défaut** : pour un nouvel install → `<Vidéos>/PanoForge` ;
  mais NE PAS déplacer les fichiers déjà produits ni écraser un `output_dir` déjà
  enregistré dans la config (l'utilisateur garde `~/Vidéos/osmo360` s'il l'a déjà).
- **Compatibilité DJI** : les mentions « .OSV / DJI Osmo 360 » restent autorisées dans
  les textes descriptifs (README, sous-titre), jamais comme nom de produit.
- Mettre à jour les tests qui référencent l'ancien nom (chemins config/cache) en
  conséquence ; `.venv/bin/pytest` doit rester vert.

## Contraintes d'environnement

- Python 3.14, PEP 668 → `run.sh` crée/active un venv local `.venv` et installe requirements.
- Dépendances minimales : fastapi, uvicorn, (numpy pour maps.py). Pas de gpxpy (stdlib xml).
- ffmpeg 8.0 système. Fichier d'exemple : un `.OSV` sous
  `<volume amovible>/DCIM/CAM_001/` (chemins de test surchargeables via
  `PANOFORGE_TEST_DCIM` / `PANOFORGE_TEST_SAMPLES`).
- Phase 2 (hors périmètre initial, ne pas bloquer dessus) : stabilisation par quaternions
  (v360 sendcmd yaw/pitch/roll par frame) — prévoir le champ `stabilize: bool` dans les
  options mais le laisser inactif/grisé.
