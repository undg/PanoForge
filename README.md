# PanoForge

> Projet indépendant, non affilié à DJI. « PanoForge » est le nom du produit ; les
> mentions « .OSV » et « DJI Osmo 360 » ci-dessous désignent uniquement le format de
> fichier et la caméra source pris en charge, pas une association ou un partenariat
> avec DJI.

Application web **locale** (usage mono-utilisateur, aucune authentification, le serveur
n'écoute que sur `127.0.0.1`) pour convertir les fichiers `.OSV` de la DJI Osmo 360 en
MP4 360° équirectangulaire, avec injection des métadonnées sphériques (Google Spherical
V1+V2), GPS optionnel depuis un fichier GPX externe (piste CAMM), file d'attente de
conversion par lot et aperçu 360° interactif dans le navigateur (three.js).

Le format `.OSV` (deux flux fisheye HEVC 10 bits + pistes de métadonnées propriétaires)
n'a pas d'outil de conversion officiel sous Linux ; PanoForge assemble les deux objectifs
en équirectangulaire à partir de la **calibration usine embarquée dans chaque fichier**.

## Fonctionnalités

- **Assemblage 360°** des deux fisheyes en équirectangulaire, avec fusion des coutures
  basée sur la calibration optique lue dans le fichier (mode `calibrated`), ou méthode
  géométrique `v360` en repli.
- **Stabilisation gyroscopique** (modes *horizon*, *verrouillé*, *lissé*) à partir des
  quaternions IMU ~1 kHz de la caméra — nivelle l'horizon et réduit les secousses.
- **Compatible Google Street View** : équirect 2:1, métadonnées sphériques, GPS depuis
  un GPX externe (aligné par horodatage + décalage manuel) injecté en piste CAMM.
- **Traitement par lot** avec file d'attente, progression, ETA, annulation.
- **Aperçu 360° interactif** dans le navigateur (proxy H.264 auto pour la lecture HEVC).
- **Extraction de photos** depuis un OSV/MP4/JPEG 360° : perspective (ratios prédéfinis
  ou libre), panorama cylindrique, photo sphérique GPano, « petite planète ».
- **Accélération GPU** optionnelle (NVENC/VAAPI/QSV détectés), repli CPU automatique.

## Prérequis

- **Linux** (développé et testé sur Ubuntu/GNOME), Python 3.11+.
- `ffmpeg` / `ffprobe` (8.0+, avec les filtres `v360`/`remap`/`sendcmd`) dans le `PATH`.
- Pour l'accélération GPU (encodage NVENC) : pilotes NVIDIA + `hevc_nvenc`/`h264_nvenc`
  visibles dans `ffmpeg -encoders` (détecté automatiquement, sinon repli CPU).
- `exiftool` (facultatif) pour vérifier les métadonnées GPano/CAMM injectées.

## Plateformes

Le cœur (Python + ffmpeg + interface navigateur) est intrinsèquement multiplateforme.
La version actuelle cible **Linux** : certains branchements système sont spécifiques —
lanceurs `run.sh`/`lancer.sh` (bash), raccourci `.desktop` (GNOME), détection des
volumes amovibles (`/run/media`, `/media`, gvfs), dossiers `~/.config` / `~/.cache` /
`xdg-user-dir`. Un portage macOS/Windows ne demande pas de réécriture du moteur, mais
l'adaptation de ces points (chemins, lanceurs, détection des lecteurs).

## Installation et lancement

```bash
./run.sh
```

Le script :
1. crée le virtualenv `.venv` s'il n'existe pas encore ;
2. installe les dépendances de `requirements.txt` ;
3. démarre le serveur FastAPI/uvicorn sur `http://127.0.0.1:8360` ;
4. ouvre automatiquement le navigateur par défaut (`xdg-open`).

Aux lancements suivants, `run.sh` **saute l'installation** des dépendances si elles sont
déjà présentes (démarrage quasi instantané). Pour forcer une réinstallation/mise à jour :
`PANOFORGE_FORCE_INSTALL=1 ./run.sh`.

Deux lanceurs complémentaires :
- `./lancer.sh` : ouvre simplement le navigateur si l'appli tourne déjà, sinon démarre
  via `run.sh` — pratique pour un raccourci de bureau.
- Un raccourci GNOME (`~/.local/share/applications/panoforge.desktop`) peut pointer sur
  `lancer.sh` pour lancer PanoForge depuis le menu d'applications.

Pour lancer manuellement (venv déjà prêt) :

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8360
```

## Configuration

Au premier lancement, les dossiers par défaut sont :
- **Source** (carte SD / caméra) : **auto-détectée** — le premier volume amovible monté
  sous `/run/media/<user>` ou `/media/<user>` (en préférant son sous-dossier `DCIM`),
  sinon le dossier personnel.
- **Sortie** (vidéos converties) : dossier Vidéos de l'utilisateur détecté via
  `xdg-user-dir VIDEOS` + `/PanoForge` — soit `~/Vidéos/PanoForge` sur un système en
  français (replis : `~/Vidéos`, `~/Videos`, puis `~`) ; créé automatiquement.
  Une config existante pointant encore sur un ancien défaut (`~/Videos/osmo360`)
  est migrée automatiquement au démarrage ; un dossier de sortie déjà personnalisé
  par l'utilisateur (y compris `~/Vidéos/osmo360`) n'est jamais déplacé ni écrasé.

Ces chemins sont modifiables depuis l'interface (panneau de configuration) ou via
`POST /api/config`. Ils sont persistés dans `~/.config/panoforge/config.json`.

Le cache (miniatures extraites et proxys d'aperçu) est stocké dans
`~/.cache/panoforge/`.

> **Renommage** : le produit s'appelait auparavant « Osmo 360 Studio ». Si vous
> mettez à jour depuis une ancienne version, la configuration et le cache sont
> déplacés automatiquement de `~/.config/osmo360-studio` et `~/.cache/osmo360-studio`
> vers `~/.config/panoforge` et `~/.cache/panoforge` au premier démarrage (rien n'est
> perdu ; l'opération ne s'exécute que si l'ancien dossier existe et que le nouveau
> n'existe pas encore).

## Utilisation

1. **Fichiers** (point d'entrée unique) : la barre d'outils en tête réunit le dossier
   source, les raccourcis d'accès rapide (Accueil, volumes amovibles, caméra) et le
   bouton **« Ouvrir un fichier… »** (OSV/MP4/JPEG). La grille affiche les `.OSV` du
   dossier source avec leur miniature ; chaque fiche propose deux actions : **Convertir**
   et **Ouvrir en 360°**. Sélection multiple possible pour la conversion par lot.
2. Réglez les options de conversion : résolution de sortie (7680/6144/3840),
   codec (HEVC/H.264), encodeur (auto/NVENC/CPU), qualité, interpolation, mode de
   stitching (`v360` baseline ou `calibrated` à partir de la calibration usine),
   **stabilisation** (horizon/verrouillé/lissé + force ; désactivée automatiquement en
   profil Street View), profil **Street View** (5 fps, CAMM obligatoire), et
   optionnellement un fichier GPX avec curseur de décalage temporel (offset).
3. Lancez la conversion : un job est créé par fichier et traité par la file
   d'attente (un seul `ffmpeg` actif à la fois).
4. **File d'attente** : suivez la progression (0-100 %, fps, ETA), annulez un job
   en cours ou en attente, ouvrez le dossier de sortie une fois terminé.
5. **Aperçu 360°** : visualisez la miniature embarquée ou le résultat converti dans
   une sphère three.js interactive (glisser pour tourner, molette pour zoomer).

## API REST

Voir `SPEC.md` pour le contrat complet. Résumé :

| Méthode | Route              | Description                                             |
|---------|---------------------|----------------------------------------------------------|
| GET     | `/api/config`       | Configuration courante (dossiers, NVENC, version)         |
| POST    | `/api/config`       | Mise à jour des dossiers source/sortie                    |
| GET     | `/api/files`        | Liste des `.OSV` (récursif 1 niveau)                       |
| GET     | `/api/thumb`        | Miniature JPEG embarquée (cache disque)                    |
| POST    | `/api/probe`        | Infos techniques + calibration disponible ou non           |
| GET     | `/api/browse`       | Navigation dossiers/fichiers pour l'UI (`dir`, `filter`) — restreinte à `$HOME`, `/run/media`, `/media` |
| POST    | `/api/gpx/analyze`  | Analyse de couverture GPX vs vidéo                          |
| POST    | `/api/jobs`         | Crée un job de conversion par fichier                       |
| GET     | `/api/jobs`         | Liste des jobs (statut, progression, fps, ETA)              |
| DELETE  | `/api/jobs/{id}`    | Annule un job (tue le process ffmpeg si en cours)           |
| POST    | `/api/photo/extract` | Extraction **synchrone** d'une photo (flat/cylindrical/equirect360/littleplanet) |
| GET     | `/api/photo/navproxy` | Proxy équirect léger pour naviguer dans un `.OSV` (cache disque) |
| GET     | `/api/media`        | Sert un fichier vidéo avec support **Range** (lecture navigateur) |
| GET     | `/`                 | Frontend statique                                          |

## Pipeline d'un job de conversion

1. `probe` (ffprobe) + `extract_metadata` (calibration + IMU depuis la piste `djmd`).
2. Génération des cartes de remap si mode `calibrated` (et calibration disponible).
3. Exécution `ffmpeg` (stitching), progression suivie via `-progress pipe:1`.
4. Injection des métadonnées sphériques (V1 XML + V2 `sv3d`).
5. Si un GPX est fourni : ré-échantillonnage + injection de la piste CAMM (+ export
   GPX fenêtré en side-car).
6. Déplacement atomique vers le dossier de sortie : `<nom>_360.mp4`.
7. Génération d'un **proxy d'aperçu** H.264 8-bit 1920×960 (`yuv420p`, `faststart`)
   dans `~/.cache/panoforge/previews/` — la sortie HEVC 10-bit n'étant pas
   décodable par Chrome/Linux, c'est ce proxy que lit l'aperçu 360° du navigateur.
   Étape non bloquante : si elle échoue, le job reste `done` et le champ
   `preview_error` explique le problème ; sinon `preview_url` (servi par
   `/api/media`) est renseigné dans `GET /api/jobs`. La progression du job couvre
   le stitching sur 0 → 0,95 puis le proxy sur 0,95 → 1,0.

Les modules `app/core/{maps,stitch,gpx,camm,spherical}.py` implémentent chacun une
étape de ce pipeline ; en leur absence ou en cas d'erreur, le job concerné passe à
l'état `error` avec un message explicite (pas de plantage du serveur).

## Extraction de photos 360

`POST /api/photo/extract` (synchrone, ~8 s à chaud / ~14 s au premier appel pour un
OSV 8K) extrait une photo JPEG (qualité 95) vers `<sortie>/photos/` depuis :
- un **MP4 360° converti** (seek précis à l'instant choisi) ;
- un **`.OSV` brut** : stitching calibré d'une seule frame pleine résolution
  (7680×3840) en réutilisant les cartes de calibration usine — mises en cache par
  (fichier, résolution) dans `~/.cache/panoforge/maps/` ;
- une **photo JPEG 360°** de la caméra (équirect 2:1, utilisée telle quelle ;
  un JPEG d'un autre ratio est refusé avec une erreur explicite).

Quatre projections (ffmpeg `v360`) :
- `flat` : perspective classique (yaw/pitch/roll, FOV horizontal 30–140°, ratios
  prédéfinis 16:9, 21:9, 32:9, 4:3, 1:1, 9:16 ou **ratio libre** « a:b » avec a et
  b numériques > 0, décimaux acceptés (ex. `2.35:1`), a/b borné à [0.2, 8]) — le
  FOV vertical est calculé depuis le ratio pour une perspective sans étirement ;
- `cylindrical` : panorama tour complet 360°, bande verticale réglable (déf. 60°) ;
- `equirect360` : équirect 2:1 complet avec **XMP GPano** injecté (photo sphérique
  interactive reconnue par Google Photos/Facebook, vérifiable avec `exiftool`) ;
- `littleplanet` : stéréographique regard vers le bas (« petite planète »).

`GET /api/photo/navproxy?path=` fournit un proxy équirect 688×344 H.264 (généré et
mis en cache) pour choisir l'instant dans un `.OSV` que le navigateur ne sait pas
lire. Remarque : le fichier `.LRF` basse résolution écrit par la caméra à côté de
chaque `.OSV` pourrait accélérer ce proxy à l'avenir, mais n'a pas pu être testé.

## Tests

```bash
.venv/bin/pytest
```

Les tests nécessitant le fichier d'exemple réel (`tests/conftest.py::requires_example_file`)
sont ignorés automatiquement si la carte SD n'est pas montée.

## Structure

```
app/
  main.py     # app FastAPI + entrée uvicorn
  api.py      # routes REST
  jobs.py     # file d'attente + exécution ffmpeg + progression
  config.py   # configuration persistée
  core/
    osv.py       # probe + wrapper extraction métadonnées djmd
    osv_meta/    # extraction bas niveau (protobuf djmd), livré
    maps.py stitch.py gpx.py camm.py spherical.py  # pipeline de stitching/GPS
  static/     # frontend (vanilla JS + three.js vendorisé)
tests/        # pytest (API + core/osv)
```

## Limites connues

- Stabilisation : le mode *horizon* à force maximale peut donner un rendu « petite
  planète » passager quand la caméra pointe vers le ciel/sol ; le mode *lissé* est plus
  naturel pour une vidéo. La stabilisation multiplie environ par 4 le temps de conversion
  (le filtre `v360` reconstruit sa projection à chaque image).
- L'injection GPS/sphérique charge le fichier en mémoire : à revoir en flux pour des
  MP4 8K de plusieurs Go (vidéos longues).
- Lecture navigateur : le HEVC 10 bits n'étant pas décodable par Chrome/Linux, l'aperçu
  passe par un proxy H.264.

## Licence

Distribué sous licence **MIT** — voir [`LICENSE`](LICENSE).

PanoForge est un projet indépendant. « DJI » et « Osmo » sont des marques de leurs
détenteurs respectifs ; elles ne sont employées ici que pour décrire la compatibilité
avec le format de fichier `.OSV` et la caméra source, sans affiliation ni approbation.
