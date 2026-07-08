# DJI Osmo 360 .OSV — structure de la piste métadonnées `djmd`

Fichier analysé : CAM_20260707202137_0001_D.OSV (Osmo 360, fw 10.00.25.29,
serial 95SXNAB0425CFN, proto `dvtm_oq101.proto` v2.0.8, 3840x3840 dual-fisheye,
98 frames @ 25 fps = 3.92 s).

## Pistes (ffprobe / mp4parse.py)
- 0:0, 0:1  video hvc1 (2 hémisphères fisheye)
- 0:2       audio AAC
- 0:3  djmd "CAM meta"  ~1 kB/éch, 98 éch  -> IMU + expo + CALIBRATION (1er éch = 7775 o)
- 0:4  djmd "CAM meta"  ~140 o/éch         -> expo seule (pas d'IMU)
- 0:5, 0:6 dbgi "CAM dbgi" ~9 kB/éch       -> télémétrie debug (stats capteur, AE), opaque
- boîte racine `camd` (128 Ko) = mini-MP4 ISOM autonome embarqué (proxy) qui
  reduplique les mêmes métadonnées djmd. Pas de calibration supplémentaire.

## Format : protobuf (aucun .proto nécessaire, décodage par wire-type)
Chaque échantillon djmd = un message protobuf. Voir mapping des champs en tête
de `extract_djmd.py`.

### Échantillon par frame (piste 0:3)
- top#3.#1.#2 : timestamp en microsecondes (delta ~40002 µs = 25 fps)
- top#3.#2.#3.#1 : ISO (float)
- top#3.#2.#4.#1 : vitesse d'obturation (2 octets, 01 64)
- top#3.#2.#6.#1 : température de couleur (K)
- top#3.#2.#9   : QUATERNION d'orientation, sous-champs #1..#4 = [w,x,y,z] float32, ||q||=1.000
- top#3.#2.#10  : ACCELEROMETRE, sous-champs #2,#3,#4 = [x,y,z] float32 en g (||a||≈1.0-1.16)
- top#3.#2.#15  : bloc AE (EV, gains...), #16.#1 = température capteur °C
- top#3.#3      : bloc HAUTE FREQUENCE = ~40 quaternions/frame (~1 kHz), chacun sous-msg #3 champs #1..#4

Valeurs vérifiées : ||quaternion||=1.0000, accéléro coïncide exactement avec
`exiftool -ee` (DocN Accelerometer X/Y/Z).

### 1er échantillon de 0:3 (7775 o) — EN-TETE + CALIBRATION OPTIQUE
- top#1.#1 : proto/fw/serial/modèle/boot_ts
- top#1.#3.#1 : quaternion d'orientation initiale (4 float)
- top#2.#6 : jusqu'à 16 blocs objectif (`msg[264]`). 2 objectifs physiques distincts
  répétés (8 variantes chacun, calibrations très proches — probablement par
  résolution/mode). Par bloc :
    #1 fx, #2 fy, #3 cx, #4 cy (pixels)
    #5..#8 coeffs de distorsion (k1,k2,p1,p2)
    #10 largeur, #11 hauteur (3840)
    #12 yaw°, #13 pitch° (objectif A yaw≈-180°, objectif B yaw≈0° -> avant/arrière)
    #21 quaternion extrinsèque de l'objectif (== #28)
    #22, #23 : LUT de distorsion radiale (14 float, courbe angle->rayon)

Objectif A : fx≈1048, cx≈1908, cy≈1921, dist≈[0.075,-0.022,0.017,-0.009]
Objectif B : fx≈1050, cx≈1917, cy≈1916, dist≈[0.067,-0.014,0.012,-0.008]

## GPS : ABSENT. Aucun champ lat/lon/gnss (Osmo 360 sans GPS interne). Les rares
doubles ~7.31 trouvés par scan brut sont des faux positifs (float32 mal alignés).

## Scripts
- mp4parse.py      : parseur de boîtes MP4 (stsd/stsz/stsc/stco) -> offsets d'échantillons
- dumpsamples.py   : extrait chaque échantillon d'un trak en fichiers séparés
- pbdecode.py      : décodeur protobuf générique (inspection hex/arbre)
- extract_djmd.py  : PRODUCTION -> calibration.json + imu_perframe.csv + imu_highrate.csv
  Usage : python3 extract_djmd.py <fichier.OSV> <outdir>
