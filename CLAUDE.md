# CLAUDE.md — repères pour travailler sur PanoForge

Guide condensé pour un agent Claude Code intervenant sur ce dépôt. Voir `SPEC.md` pour
le contrat détaillé (modules, API, formats binaires) ; ce fichier résume l'essentiel et
surtout les **pièges** découverts empiriquement.

## Quoi

Appli web **locale** (FastAPI + frontend vanilla JS/three.js, interface en **français**)
qui convertit les fichiers `.OSV` de la DJI Osmo 360 en MP4 360° équirectangulaire, avec
stabilisation, métadonnées sphériques, GPS depuis GPX (CAMM), et extraction de photos.
Serveur sur `127.0.0.1:8360`, mono-utilisateur, sans authentification.

> Nommage : le produit s'appelle **PanoForge**. « DJI »/« Osmo » ne doivent apparaître
> que comme mentions de compatibilité (format `.OSV`, caméra source), jamais dans le nom
> du produit, du dépôt ou le logo. Projet indépendant, non affilié à DJI.

## Lancer / tester

```bash
./run.sh                      # venv + deps (conditionnel) + uvicorn + navigateur
.venv/bin/pytest              # suite complète (~106 tests)
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8360   # manuel
```

- `run.sh` saute l'install si `import fastapi, uvicorn, numpy` réussit ; forcer avec
  `PANOFORGE_FORCE_INSTALL=1`.
- Certains tests d'intégration exigent un `.OSV`/`.JPG` réel ; ils se **skip** si absent
  (surchargeables via `PANOFORGE_TEST_DCIM` / `PANOFORGE_TEST_SAMPLES`).
- **Ne pas** démarrer un 2e serveur sur 8360 s'il en tourne déjà un ; utiliser un autre
  port pour les tests navigateur.

## Architecture

```
app/main.py      # app FastAPI + montage statique
app/api.py       # routes REST (voir SPEC.md)
app/jobs.py      # file d'attente FIFO, 1 ffmpeg à la fois, parsing -progress
app/config.py    # config persistée ~/.config/panoforge, cache ~/.cache/panoforge
app/core/
  osv.py         # probe ffprobe + wrapper extraction métadonnées
  osv_meta/      # extraction bas niveau des pistes djmd (protobuf) — NE PAS réécrire
  maps.py        # calibration -> cartes de remap ffmpeg + masque de fusion
  stitch.py      # construction des commandes ffmpeg (modes v360 / calibrated)
  stabilize.py   # corrections d'orientation par frame (quaternions IMU -> sendcmd)
  gpx.py camm.py spherical.py   # GPS/GPX -> CAMM, métadonnées sphériques V1+V2
  photo.py       # extraction de photos (4 projections) + navproxy
app/static/      # frontend : index.html, js/{app,viewer,filebrowser,api}.js, style.css
```

Les modules `core` ont des **signatures contractuelles** (SPEC.md) ; `jobs.py` les
importe paresseusement et transforme toute erreur en job `error` explicite, sans planter.

## Faits établis (ne pas re-vérifier)

- `.OSV` = MP4 : 2 fisheyes HEVC 10 bits 3840×3840 (>180°), audio AAC, pistes `djmd`
  (protobuf DJI), et une miniature équirect MJPEG (référence de stitching).
- La **calibration optique usine** (fx/fy, cx/cy, distorsion, quaternion extrinsèque,
  LUT de couture) est embarquée dans le 1er échantillon `djmd` de chaque fichier.
- La caméra n'a **pas de GPS** : le GPX externe est la seule source géo.
- IMU : quaternions d'orientation ~1 kHz (pas de gyro brut) — suffisant pour stabiliser.

## Pièges à connaître (chèrement acquis)

1. **v360 + sendcmd composent, ne remplacent pas.** Les commandes `yaw/pitch/roll`
   envoyées à un filtre `v360` se **post-multiplient** avec l'orientation courante. Pour
   une stabilisation par frame il faut émettre des **deltas** (`C_i = T_{i-1}ᵀ·T_i`) et
   initialiser le v360 en neutre, sinon dérive cumulative croissante. Voir `stabilize.py`.
2. **Conventions d'angles visionneuse ↔ v360.** La sphère three.js échantillonne
   `u = yaw/360` (yaw=0 → bord gauche) ; `v360 output=flat` vise le centre. Mapping
   appliqué dans `photo.py` : flat `yaw_v360 = yaw+180`, roll inversé, pitch identique ;
   cylindrical yaw inchangé ; littleplanet rotation+90, +hflip, `h_fov=250` fixe. Le
   frontend WebGL est la référence, le backend s'y aligne.
3. **HEVC 10 bits non lisible par Chrome/Linux.** Chaque job et l'extraction OSV génèrent
   un **proxy H.264** (`~/.cache/panoforge/previews/`) que lit l'aperçu 360°.
4. **Quaternion djmd = ordre `[w,x,y,z]`, repère boîtier→monde, vertical monde = −Z.**
   Validé empiriquement ; convention documentée en tête de `stabilize.py`.
5. **Stitching calibré** : les LUT embarquées décrivent le cercle de couture (θ=90°) et
   servent à recaler l'échelle optique — meilleures coutures que le polynôme seul.
6. Config/cache ont été renommés depuis `osmo360-studio` → `panoforge` avec **migration
   douce** au démarrage (`config._migrate_legacy_dirs`) ; ne pas casser cette migration.

## Conventions

- Interface et messages utilisateur en **français**. Pas de dépendance CDN à l'exécution
  (three.js est vendorisé dans `app/static/js/`).
- Ajouter/mettre à jour un test pytest pour tout changement de comportement ; garder la
  suite verte. Vérifier visuellement les changements d'UI dans un navigateur avant de
  conclure (ne pas se fier au seul code).
- `work/` (gitignoré) contient des artefacts de travail et images personnelles : ne pas
  le committer.
