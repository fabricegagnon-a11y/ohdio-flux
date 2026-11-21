# Flux RSS OHdio

Flux RSS des émissions de Radio-Canada Première, générés pour être écoutés dans Pocket Casts. Les fichiers XML sont hébergés ici, sur GitHub Pages ; les fichiers audio restent sur les serveurs de Radio-Canada.

## Les flux

| Émission | Fichier |
|---|---|
| Aujourd'hui l'histoire | `aujourd-hui-l-histoire-3855.xml` |
| Aujourd'hui l'histoire — Archives 2017-2020 | `aujourd-hui-l-histoire-archives-2017-2020.xml` |
| La journée est encore jeune | `la-journee-est-encore-jeune-9887.xml` |
| Pouvez-vous répéter la question ? | `humour-quiz-jeu-pierre-brassard-7791.xml` |
| Les pires moments de l'histoire | `pires-moments-histoire-charles-beauchesne-humoriste-7135.xml` |
| L'histoire en personne | `lhistoire-en-personne-13040.xml` |
| Hors des ondes | `hors-des-ondes-avec-patrice-roy-13843.xml` |

Adresse des flux : `https://fabricegagnon-a11y.github.io/ohdio-flux/<fichier>.xml`

## Mise à jour automatique

Les flux sont mis à jour chaque jour, automatiquement, par une tâche GitHub Actions (`.github/workflows/mise-a-jour-quotidienne.yml`) qui lance `rcrss.py` vers 15 h, heure de Toronto. Le programme (`rcrss.py`, `emissions.txt`) et son cache (`cache.json`) sont dans ce dépôt. Le flux d'archives 2017-2020 est figé et n'est jamais régénéré.
