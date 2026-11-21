#!/usr/bin/env python3
"""rcrss — Générateur de flux RSS pour les émissions et balados de Radio-Canada (OHdio).

Radio-Canada ne publie plus de flux RSS officiels pour la plupart de ses
émissions de Première et de ses balados depuis le passage à OHdio. Ce petit
programme reconstruit un flux RSS 2.0 standard à partir des pages publiques
d'OHdio, en utilisant les mêmes services publics que le site web lui-même :

  1. la page de l'émission donne la liste des épisodes, avec pour chacun
     son titre, son résumé, sa date, sa durée et son identifiant média
     (données embarquées dans la page, JSON-LD + tableau d'épisodes) ;
     pour les balados à plusieurs saisons, chaque page de saison est aussi
     récupérée ;
  2. l'API média publique de Radio-Canada fournit alors le fichier audio
     progressif (MP4/AAC, 128 kb/s) de chaque épisode ;
  3. le programme assemble un fichier .xml par émission, prêt à héberger
     (par exemple sur Google Drive) et à ajouter dans Pocket Casts.

Usage personnel seulement. Ne nécessite aucune clé, aucun compte et aucune
dépendance : Python 3.10+ suffit (bibliothèque standard uniquement).

Exemples :
  python3 rcrss.py "https://ici.radio-canada.ca/ohdio/premiere/emissions/3855/aujourd-hui-l-histoire"
  python3 rcrss.py --liste emissions.txt --max-episodes 100 --sortie flux
"""

import argparse
import email.utils
import html as html_lib
import http.client
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

# Clé publique utilisée par le site web de Radio-Canada lui-même pour son API
# média (la même que celle employée par l'extracteur open source yt-dlp).
CLIENT_KEY = "773aea60-0e80-41bb-9c7f-e6d7c3ad17fb"
MEDIA_API = "https://services.radio-canada.ca/media/"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
# Débit du flux audio (128 kb/s) : sert à estimer la taille du fichier
# quand le serveur ne la fournit pas (128 000 bits/s = 16 000 octets/s).
OCTETS_PAR_SECONDE = 16_000

EPISODE_URL_RE = re.compile(r"/episodes/(\d+)/")
BALADO_EPISODE_URL_RE = re.compile(r"/balados/\d+/[^/]+/(\d+)/")
SHOW_URL_RE = re.compile(r"/ohdio/(?:premiere/emissions|balados)/(\d+)/([^/?#]+)")
SEASON_URL_RE = re.compile(
    r"((?:/ohdio)?/(?:balados|premiere/emissions)/\d+/[^\"'<>\s]+?/saison-\d+)"
)
JSON_LD_RE = re.compile(
    r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.S
)
MEDIA_ID_RE = re.compile(r'"mediaId":"(\d+)"')
PLAYLIST_ITEM_RE = re.compile(r'"playlistItemId":')
SUMMARY_RE = re.compile(r'"summary":"((?:[^"\\]|\\.)*)"')
DURATION_HEADER_RE = re.compile(
    r'"header":\{"typename":"ProductHeaderNewSchema".{0,1500}?"durationInSeconds":(\d+)',
    re.S,
)
BROADCASTED_AT_RE = re.compile(r'"broadcastedFirstTimeAt":"([^"]+)"')
REPLAY_RE = re.compile(r'"isBroadcastedReplay":(true|false)')


# ---------------------------------------------------------------------------
# Réseau
# ---------------------------------------------------------------------------

class FetchError(Exception):
    """Erreur de récupération (réseau ou HTTP)."""


def http_get(url, delai=0.0, tentatives=3):
    """GET avec en-tête de navigateur, réessais et petit délai de politesse."""
    if delai:
        time.sleep(delai)
    derniere_erreur = None
    for essai in range(tentatives):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.8",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            derniere_erreur = FetchError(f"HTTP {e.code} pour {url}")
            if e.code in (429, 500, 502, 503, 504) and essai < tentatives - 1:
                time.sleep(2 * (essai + 1))
                continue
            raise derniere_erreur from e
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as e:
            derniere_erreur = FetchError(f"{e} pour {url}")
            if essai < tentatives - 1:
                time.sleep(2 * (essai + 1))
                continue
            raise derniere_erreur from e
    raise derniere_erreur


def http_get_json(url, delai=0.0):
    """GET + décodage JSON (renvoie None si le corps n'est pas du JSON)."""
    texte = http_get(url, delai=delai)
    try:
        return json.loads(texte)
    except json.JSONDecodeError:
        return None


def http_head_size(url, delai=0.0):
    """Taille d'un fichier distant (Content-Length) via HEAD, ou None."""
    if delai:
        time.sleep(delai)
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            taille = resp.headers.get("Content-Length")
            return int(taille) if taille else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Pages OHdio
# ---------------------------------------------------------------------------

def extraire_json_ld(page_html):
    """Renvoie le graphe JSON-LD de la page (ou {} si introuvable)."""
    for m in JSON_LD_RE.finditer(page_html):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "@graph" in data:
            return data["@graph"]
        if isinstance(data, dict) and "@type" in data:
            return [data]
    return []


def extraire_tableau_episodes(page_html):
    """Extrait le tableau d'épisodes embarqué dans la page de l'émission.

    Chaque élément contient déjà tout ce qu'il faut : identifiant de
    l'épisode et du média, titre, résumé, dates, durée, rediffusion.
    Renvoie une liste (vide si le tableau est introuvable).
    """
    decodeur = json.JSONDecoder()
    for m in re.finditer(r'"episodes":\[', page_html):
        try:
            tableau, _ = decodeur.raw_decode(page_html[m.end() - 1:])
        except json.JSONDecodeError:
            continue
        if (
            isinstance(tableau, list)
            and tableau
            and isinstance(tableau[0], dict)
            and tableau[0].get("typename") == "EpisodeListItem"
        ):
            return tableau
    return []


def url_absolue(url):
    """Rend absolue une URL relative du site OHdio."""
    if not url:
        return ""
    if url.startswith("http"):
        return url
    if url.startswith("/ohdio"):
        return "https://ici.radio-canada.ca" + url
    if url.startswith("/"):
        return "https://ici.radio-canada.ca/ohdio" + url
    return url


def extraire_saisons(page_html):
    """URL des pages de saisons liées depuis une page d'émission (balados).

    La page principale ne liste que la saison la plus récente ; les autres
    saisons ont leur propre page (/saison-1, /saison-2, ...).
    """
    saisons = []
    vus = set()
    for m in SEASON_URL_RE.finditer(page_html):
        url = url_absolue(m.group(1))
        if url not in vus:
            vus.add(url)
            saisons.append(url)
    return saisons


def sans_date_epoque(valeur):
    """None si la valeur est une date « époque » (1970-01-01), sinon la valeur.

    Radio-Canada marque les dates manquantes des vieux épisodes par
    l'époque Unix ; les traiter comme absentes évite des pubDate de 1970
    et permet de récupérer la vraie date depuis l'API média.
    """
    if valeur and str(valeur).startswith("1970-01-01"):
        return None
    return valeur


def analyser_page_emission(page_html):
    """Extrait les infos de l'émission et la liste des épisodes d'une page.

    Renvoie (infos_emission, episodes) où episodes est une liste de
    dictionnaires {id, url, titre, resume, duree, diffuse_le (ISO),
    rediffusion, media_ids}.
    """
    graphe = extraire_json_ld(page_html)
    infos = {}
    episodes = []
    for noeud in graphe:
        if not isinstance(noeud, dict):
            continue
        if noeud.get("@type") in ("RadioSeries", "PodcastSeries"):
            image = noeud.get("image") or {}
            infos = {
                "titre": noeud.get("name", ""),
                "description": noeud.get("description", "") or "",
                "image": image.get("url", "") if isinstance(image, dict) else "",
                "total": noeud.get("numberOfEpisodes"),
            }
    for item in extraire_tableau_episodes(page_html):
        global_id = item.get("globalId") or {}
        eid = str(global_id.get("id", ""))
        if not eid:
            continue
        url = url_absolue(item.get("url", ""))
        # Date de diffusion retenue : la plus récente (une rediffusion
        # apparaît alors comme un nouvel épisode, comme sur le site).
        diffuse_le = sans_date_epoque(
            item.get("broadcastedLastTimeAt") or item.get("broadcastedFirstTimeAt")
        )
        duree = (item.get("duration") or {}).get("durationInSeconds")
        episodes.append(
            {
                "id": eid,
                "url": url,
                "titre": item.get("title", "") or "",
                "resume": nettoyer_html(item.get("summary", "") or ""),
                "duree": duree,
                "diffuse_le": diffuse_le,
                "rediffusion": bool(item.get("isBroadcastedReplay")),
                "jouable": bool(item.get("isPlayable", True)),
                "media_ids": [str(x) for x in item.get("mediaIds") or []],
            }
        )
    # Secours : si le tableau est absent, utiliser le JSON-LD (titres et
    # liens seulement ; les identifiants média seront récupérés ensuite
    # sur les pages d'épisodes).
    if not episodes:
        for noeud in graphe:
            if not isinstance(noeud, dict) or noeud.get("@type") not in (
                "RadioEpisode",
                "PodcastEpisode",
            ):
                continue
            url = noeud.get("url", "")
            m = EPISODE_URL_RE.search(url) or BALADO_EPISODE_URL_RE.search(url)
            if not m:
                continue
            episodes.append(
                {
                    "id": m.group(1),
                    "url": url,
                    "titre": noeud.get("name", "") or "",
                    "resume": "",
                    "duree": None,
                    "diffuse_le": None,
                    "rediffusion": False,
                    "jouable": True,
                    "media_ids": [],
                }
            )
    # Dernier recours : repérer les liens d'épisodes dans le HTML.
    if not episodes:
        vus = set()
        for m in re.finditer(
            r"/ohdio/(?:premiere/emissions/[^\"'/\s<>]+/episodes|balados/\d+/[^\"'/\s<>]+)/(\d+)/[^\"'\s<>]+",
            page_html,
        ):
            eid = m.group(1)
            if eid not in vus:
                vus.add(eid)
                episodes.append(
                    {
                        "id": eid,
                        "url": "",
                        "titre": "",
                        "resume": "",
                        "duree": None,
                        "diffuse_le": None,
                        "rediffusion": False,
                        "jouable": True,
                        "media_ids": [],
                    }
                )
    return infos, episodes


def nettoyer_html(texte_html):
    """Texte brut à partir d'un fragment HTML (résumé d'épisode)."""
    if not texte_html:
        return ""
    try:
        texte = json.loads(f'"{texte_html}"')
    except json.JSONDecodeError:
        texte = texte_html
    texte = re.sub(r"<[^>]+>", " ", texte)
    texte = html_lib.unescape(texte)
    return re.sub(r"\s+", " ", texte).strip()


def analyser_page_episode(page_html):
    """Extrait les données utiles d'une page d'épisode.

    Renvoie un dictionnaire : media_ids (liste), resume, duree (secondes),
    diffuse_le (datetime), rediffusion (bool).
    """
    resultat = {
        "media_ids": [],
        "resume": "",
        "duree": None,
        "diffuse_le": None,
        "rediffusion": False,
    }
    # Identifiants média : seulement ceux des blocs playlistItemId, dans
    # l'ordre du document (le premier est l'audio de l'épisode ; un épisode
    # peut en compter plusieurs s'il est découpé en segments).
    vus = set()
    for m in PLAYLIST_ITEM_RE.finditer(page_html):
        bloc = page_html[m.start():m.start() + 500]
        mm = MEDIA_ID_RE.search(bloc)
        if mm and mm.group(1) not in vus:
            vus.add(mm.group(1))
            resultat["media_ids"].append(mm.group(1))
    # Résumé (premier champ summary non vide).
    for m in SUMMARY_RE.finditer(page_html):
        resume = nettoyer_html(m.group(1))
        if resume:
            resultat["resume"] = resume
            break
    # Durée (dans l'en-tête de l'épisode).
    m = DURATION_HEADER_RE.search(page_html)
    if m:
        resultat["duree"] = int(m.group(1))
    # Date et heure de première diffusion (ISO 8601).
    m = BROADCASTED_AT_RE.search(page_html)
    if m:
        try:
            dt = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
            resultat["diffuse_le"] = dt
        except ValueError:
            pass
    # Rediffusion ?
    m = REPLAY_RE.search(page_html)
    if m:
        resultat["rediffusion"] = m.group(1) == "true"
    return resultat


# ---------------------------------------------------------------------------
# API média de Radio-Canada
# ---------------------------------------------------------------------------

def api_media(chemin, params, delai=0.0):
    """Appel GET vers l'API média publique (renvoie le JSON décodé)."""
    query = {
        "client_key": CLIENT_KEY,
        "output": "json",
        "appCode": "medianet",
    }
    query.update(params)
    url = MEDIA_API + chemin + "?" + urllib.parse.urlencode(query)
    return http_get_json(url, delai=delai)


def url_audio_progressif(media_id, delai=0.0):
    """URL du fichier audio MP4 d'un média, ou None si indisponible."""
    data = api_media(
        "validation/v2/",
        {
            "idMedia": media_id,
            "tech": "progressive",
            "connectionType": "hd",
            "deviceType": "ipad",
            "multibitrate": "true",
        },
        delai=delai,
    )
    if not data or data.get("errorCode") != 0:
        return None
    url = data.get("url") or ""
    if not url.lower().endswith(".mp4"):
        return None
    return url


def infos_media(media_id, delai=0.0):
    """Métadonnées d'un média (durée, date) — secours si la page ne les donne pas."""
    data = api_media("meta/v1/index.ashx", {"idMedia": media_id}, delai=delai)
    infos = {}
    if not data:
        return infos
    for meta in data.get("Metas", []):
        nom = meta.get("name")
        texte = meta.get("text")
        if nom == "length" and texte:
            try:
                infos["duree"] = int(texte)
            except ValueError:
                pass
        elif nom == "Date" and texte:
            try:
                dt = datetime.strptime(texte, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if dt.year != 1970:  # date « époque » = date manquante
                    infos["diffuse_le"] = dt
            except ValueError:
                pass
    return infos


# ---------------------------------------------------------------------------
# Flux RSS
# ---------------------------------------------------------------------------

ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM_NS = "http://www.w3.org/2005/Atom"


def formater_date_rfc2822(dt):
    """Datetime -> date RFC 2822 (GMT), comme l'exige RSS."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return email.utils.format_datetime(dt.astimezone(timezone.utc), usegmt=True)


def construire_rss(emission, episodes, url_autoreference=None):
    """Assemble le document RSS 2.0 (+ balises iTunes) d'une émission.

    `episodes` : liste de dictionnaires {guid, titre, lien, description,
    diffuse_le (datetime), duree (secondes), audio_url, taille}.
    """
    ET.register_namespace("itunes", ITUNES_NS)
    ET.register_namespace("atom", ATOM_NS)
    rss = ET.Element("rss", {"version": "2.0"})
    canal = ET.SubElement(rss, "channel")
    ET.SubElement(canal, "title").text = emission["titre"]
    ET.SubElement(canal, "link").text = emission["url"]
    ET.SubElement(canal, "description").text = (
        emission["description"] or emission["titre"]
    )
    ET.SubElement(canal, "language").text = "fr-CA"
    ET.SubElement(canal, "copyright").text = (
        "© ICI Radio-Canada — usage personnel seulement"
    )
    ET.SubElement(canal, f"{{{ITUNES_NS}}}author").text = "ICI Radio-Canada Première"
    ET.SubElement(canal, f"{{{ITUNES_NS}}}explicit").text = "no"
    # Flux à usage personnel : ne pas l'inscrire dans les annuaires publics.
    ET.SubElement(canal, f"{{{ITUNES_NS}}}block").text = "Yes"
    if emission.get("image"):
        image = ET.SubElement(canal, "image")
        ET.SubElement(image, "url").text = emission["image"]
        ET.SubElement(image, "title").text = emission["titre"]
        ET.SubElement(image, "link").text = emission["url"]
        ET.SubElement(canal, f"{{{ITUNES_NS}}}image", {"href": emission["image"]})
    if url_autoreference:
        ET.SubElement(
            canal,
            f"{{{ATOM_NS}}}link",
            {
                "href": url_autoreference,
                "rel": "self",
                "type": "application/rss+xml",
            },
        )
    dates = [e["diffuse_le"] for e in episodes if e.get("diffuse_le")]
    if dates:
        ET.SubElement(canal, "lastBuildDate").text = formater_date_rfc2822(max(dates))

    for ep in episodes:
        item = ET.SubElement(canal, "item")
        ET.SubElement(item, "title").text = ep["titre"]
        ET.SubElement(item, "link").text = ep["lien"]
        ET.SubElement(item, "description").text = ep.get("description", "") or ep["titre"]
        guid = ET.SubElement(item, "guid", {"isPermaLink": "false"})
        guid.text = ep["guid"]
        if ep.get("diffuse_le"):
            ET.SubElement(item, "pubDate").text = formater_date_rfc2822(ep["diffuse_le"])
        if ep.get("duree"):
            ET.SubElement(item, f"{{{ITUNES_NS}}}duration").text = str(ep["duree"])
        ET.SubElement(
            item,
            "enclosure",
            {
                "url": ep["audio_url"],
                "type": "audio/mpeg",
                "length": str(ep.get("taille") or 0),
            },
        )
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)


# ---------------------------------------------------------------------------
# Traitement d'une émission
# ---------------------------------------------------------------------------

def sauver_cache(chemin_cache, cache):
    """Sauvegarde atomique du cache (écriture temporaire puis renommage)."""
    tmp = chemin_cache.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(chemin_cache)


def traiter_emission(url_emission, options, cache, chemin_cache=None):
    """Génère le flux d'une émission. Renvoie (nom_fichier, statistiques)."""
    m = SHOW_URL_RE.search(url_emission)
    if not m:
        raise FetchError(f"URL d'émission non reconnue : {url_emission}")
    show_id, slug = m.group(1), m.group(2)
    url_base = url_emission.split("?")[0].rstrip("/")

    infos_emission = {}
    episodes_trouves = []
    vus = set()
    page1_html = ""
    for page in range(1, options.max_pages + 1):
        url_page = url_base if page == 1 else f"{url_base}?pageNumber={page}"
        try:
            page_html = http_get(url_page, delai=options.delai)
        except FetchError as e:
            print(f"  [!!] page {page} inaccessible ({e}), arrêt de la pagination.")
            break
        infos, episodes_page = analyser_page_emission(page_html)
        if page == 1:
            infos_emission = infos
            page1_html = page_html
        nouveaux = [e for e in episodes_page if e["id"] not in vus]
        for e in nouveaux:
            vus.add(e["id"])
        episodes_trouves.extend(nouveaux)
        print(f"  page {page} : {len(nouveaux)} épisodes")
        if len(episodes_page) < 50 or len(episodes_trouves) >= options.max_episodes:
            break

    # Saisons (balados) : la page principale ne liste que la saison la plus
    # récente ; récupérer les autres saisons, une page chacune.
    for url_saison in extraire_saisons(page1_html):
        if url_saison.rstrip("/") == url_base:
            continue
        try:
            saison_html = http_get(url_saison, delai=options.delai)
        except FetchError as e:
            print(f"  [!!] saison inaccessible ({e}) : {url_saison}")
            continue
        _, episodes_saison = analyser_page_emission(saison_html)
        nouveaux = [e for e in episodes_saison if e["id"] not in vus]
        for e in nouveaux:
            vus.add(e["id"])
        episodes_trouves.extend(nouveaux)
        print(f"  saison {url_saison.rsplit('/', 1)[-1]} : {len(nouveaux)} épisodes")

    infos_emission.setdefault("titre", slug.replace("-", " ").title())
    infos_emission.setdefault("description", "")
    infos_emission.setdefault("image", "")
    infos_emission["url"] = url_base

    episodes_flux = []
    stats = {"sans_audio": 0, "rediffusions_exclues": 0, "erreurs": 0}
    for index_ep, ep in enumerate(episodes_trouves[: options.max_episodes], start=1):
        eid = ep["id"]
        entree_cache = cache.get(eid)
        try:
            if entree_cache and not options.rafraichir:
                donnees = entree_cache
            else:
                donnees = {
                    "titre": ep.get("titre", ""),
                    "lien": ep.get("url", ""),
                    "resume": ep.get("resume", ""),
                    "duree": ep.get("duree"),
                    "diffuse_le": ep.get("diffuse_le"),
                    "rediffusion": ep.get("rediffusion", False),
                    "media_urls": {},
                    "tailles": {},
                }
                media_ids = list(ep.get("media_ids") or [])
                if not media_ids and ep.get("url"):
                    # Secours : la page de l'épisode contient l'identifiant média.
                    page_html = http_get(ep["url"], delai=options.delai)
                    page_ep = analyser_page_episode(page_html)
                    media_ids = page_ep["media_ids"]
                    if not donnees["resume"]:
                        donnees["resume"] = page_ep["resume"]
                    if donnees["duree"] is None:
                        donnees["duree"] = page_ep["duree"]
                    if donnees["diffuse_le"] is None and page_ep.get("diffuse_le"):
                        donnees["diffuse_le"] = page_ep["diffuse_le"].isoformat()
                    donnees["rediffusion"] = page_ep["rediffusion"]
                if not ep.get("jouable", True):
                    stats["sans_audio"] += 1
                    print(f"  [..] non jouable : {donnees.get('titre') or eid}")
                    cache[eid] = donnees
                    continue
                for media_id in media_ids:
                    audio = url_audio_progressif(media_id, delai=options.delai)
                    if audio:
                        donnees["media_urls"][str(media_id)] = audio
                if donnees["media_urls"]:
                    # Taille du fichier (HEAD, sans télécharger l'audio).
                    for media_id, audio in donnees["media_urls"].items():
                        taille = http_head_size(audio, delai=options.delai)
                        if taille is None and donnees.get("duree"):
                            taille = donnees["duree"] * OCTETS_PAR_SECONDE
                        donnees["tailles"][media_id] = taille or 0
                cache[eid] = donnees
            # Compléments (durée/date) si la page ne les donnait pas. Vaut
            # aussi pour les entrées du cache dont la date est une « époque »
            # 1970 : l'API média a généralement la vraie date.
            if (donnees.get("diffuse_le") or "").startswith("1970-01-01"):
                donnees["diffuse_le"] = None
            if donnees.get("media_urls") and (
                donnees.get("duree") is None or donnees.get("diffuse_le") is None
            ):
                premier_media = next(iter(donnees["media_urls"]))
                secours = infos_media(premier_media, delai=options.delai)
                if donnees.get("duree") is None:
                    donnees["duree"] = secours.get("duree")
                if donnees.get("diffuse_le") is None and secours.get("diffuse_le"):
                    donnees["diffuse_le"] = secours["diffuse_le"].isoformat()
                cache[eid] = donnees
            if donnees.get("rediffusion") and options.exclure_rediffusions:
                stats["rediffusions_exclues"] += 1
                continue
            media_urls = donnees.get("media_urls", {})
            if not media_urls:
                stats["sans_audio"] += 1
                print(f"  [..] sans audio : {donnees.get('titre') or eid}")
                continue
            diffuse_le = None
            if donnees.get("diffuse_le"):
                diffuse_le = datetime.fromisoformat(donnees["diffuse_le"])
            medias = list(media_urls.items())
            for index, (media_id, audio_url) in enumerate(medias):
                titre = donnees.get("titre") or f"Épisode {eid}"
                if len(medias) > 1:
                    titre = f"{titre} (partie {index + 1})"
                episodes_flux.append(
                    {
                        "guid": f"{eid}_{index}",
                        "titre": titre,
                        "lien": donnees.get("lien") or ep["url"],
                        "description": donnees.get("resume", ""),
                        "diffuse_le": diffuse_le,
                        "duree": donnees.get("duree"),
                        "audio_url": audio_url,
                        "taille": (donnees.get("tailles") or {}).get(media_id, 0),
                    }
                )
        except FetchError as e:
            stats["erreurs"] += 1
            print(f"  [!!] épisode {eid} ignoré ({e})")
        except Exception as e:  # un épisode ne doit pas faire échouer l'émission
            stats["erreurs"] += 1
            print(f"  [!!] épisode {eid} ignoré ({type(e).__name__}: {e})")
        # Sauvegarde périodique : une longue génération reste reprenable
        # si elle est interrompue en cours de route.
        if chemin_cache and index_ep % 100 == 0:
            sauver_cache(chemin_cache, cache)

    # Tri : le plus récent d'abord (les épisodes sans date vont à la fin).
    episodes_flux.sort(
        key=lambda e: e["diffuse_le"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    nom_fichier = f"{slug}-{show_id}.xml"
    url_ref = None
    if options.url_base:
        url_ref = options.url_base.rstrip("/") + "/" + nom_fichier
    xml = construire_rss(infos_emission, episodes_flux, url_autoreference=url_ref)
    chemin = options.sortie / nom_fichier
    chemin.write_bytes(xml)
    return nom_fichier, len(episodes_flux), stats


# ---------------------------------------------------------------------------
# Programme principal
# ---------------------------------------------------------------------------

def lire_liste(chemin):
    urls = []
    for ligne in Path(chemin).read_text(encoding="utf-8").splitlines():
        ligne = ligne.strip()
        if ligne and not ligne.startswith("#"):
            urls.append(ligne)
    return urls


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Génère des flux RSS pour des émissions et balados de Radio-Canada (OHdio).",
    )
    parser.add_argument(
        "emissions",
        nargs="*",
        help="URL d'une page d'émission ou de balado OHdio (ex. https://ici.radio-canada.ca/ohdio/premiere/emissions/3855/aujourd-hui-l-histoire)",
    )
    parser.add_argument(
        "--liste",
        metavar="FICHIER",
        help="Fichier texte avec une URL d'émission par ligne (les lignes # sont ignorées).",
    )
    parser.add_argument(
        "--sortie",
        type=Path,
        default=Path("flux"),
        help="Dossier de sortie des fichiers .xml (défaut : ./flux).",
    )
    parser.add_argument(
        "--max-episodes",
        type=int,
        default=50,
        help="Nombre maximal d'épisodes par émission (défaut : 50).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=1,
        help="Nombre maximal de pages d'épisodes à parcourir par émission, 50 épisodes par page (défaut : 1).",
    )
    parser.add_argument(
        "--exclure-rediffusions",
        action="store_true",
        help="Exclure les épisodes signalés comme rediffusions.",
    )
    parser.add_argument(
        "--rafraichir",
        action="store_true",
        help="Ignorer le cache et tout récupérer à nouveau.",
    )
    parser.add_argument(
        "--delai",
        type=float,
        default=0.4,
        help="Délai en secondes entre les requêtes (défaut : 0,4).",
    )
    parser.add_argument(
        "--url-base",
        metavar="URL",
        help="Adresse publique où les flux seront hébergés (utilisée pour le lien atom:self).",
    )
    options = parser.parse_args(argv)

    urls = list(options.emissions)
    if options.liste:
        urls.extend(lire_liste(options.liste))
    if not urls:
        parser.error("indiquez au moins une URL d'émission ou un fichier --liste.")
    options.sortie.mkdir(parents=True, exist_ok=True)

    chemin_cache = options.sortie / "cache.json"
    cache = {}
    if chemin_cache.exists() and not options.rafraichir:
        try:
            cache = json.loads(chemin_cache.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cache = {}
    total_fichiers = 0
    for url in urls:
        print(f"\nÉmission : {url}")
        try:
            nom, nb, stats = traiter_emission(url, options, cache, chemin_cache)
            print(f"  -> {nom} : {nb} épisodes avec audio "
                  f"({stats['sans_audio']} sans audio, "
                  f"{stats['rediffusions_exclues']} rediffusions exclues, "
                  f"{stats['erreurs']} erreurs)")
            total_fichiers += 1
        except FetchError as e:
            print(f"  [!!] émission ignorée ({e})")
        except Exception as e:  # une émission ne doit pas faire échouer les autres
            print(f"  [!!] émission ignorée ({type(e).__name__}: {e})")
        sauver_cache(chemin_cache, cache)
    print(f"\nTerminé : {total_fichiers} flux généré(s) dans {options.sortie}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
