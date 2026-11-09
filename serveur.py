#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mini Race - serveur multijoueur.

Lancement :  python serveur.py          (port 8000)
             python serveur.py 9000     (autre port)
Il sert le jeu (mini-race.html, dans le même dossier) et relie les joueurs en direct.
Aucune installation : Python 3.8 ou plus suffit.

Ce qu'il garde dans joueurs.json : pseudo#numéro, couleur, classe et meilleurs tours de chaque pilote.
Les positions en direct restent en mémoire. N'importe qui ayant l'adresse peut jouer :
ne la donne qu'à tes amis.
"""
import json
import math
import os
import random
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DOSSIER = os.path.dirname(os.path.abspath(__file__))
JEU = os.path.join(DOSSIER, "mini-race.html")
FICHIER_JOUEURS = os.path.join(DOSSIER, "joueurs.json")
PORT = int(os.environ.get("PORT") or (sys.argv[1] if len(sys.argv) > 1 else 8000))
EN_LIGNE = 6.0        # secondes sans nouvelles avant de passer hors ligne
MAX_JOUEURS = 5000
MAX_CORPS = 16384     # taille maximale d'une requête, en octets

verrou = threading.Lock()
joueurs = {}          # id -> {"id","name","tag","color","cls","rec","vu"}
direct = {}           # id -> {"sel","lv","b","t","race","recu"}
version_annuaire = 1
a_sauver = False

RE_ID = re.compile(r"^[a-z0-9]{1,16}$")
RE_TAG = re.compile(r"^\d{4}$")
RE_COULEUR = re.compile(r"^#[0-9a-fA-F]{6}$")


def nom_propre(v):
    s = "".join(ch for ch in str(v or "") if ch >= " " and ch not in "#\x7f")
    return s.strip()[:16] or "Pilote"


def entier(v, bas, haut, defaut=0):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return defaut
    if not math.isfinite(x):
        return defaut
    return max(bas, min(haut, int(round(x))))


def nombre(v, bas, haut):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return bas
    return max(bas, min(haut, x)) if math.isfinite(x) else bas


def liste(v, n, bas, haut):
    v = v if isinstance(v, list) else []
    return [entier(v[i] if i < len(v) else 0, bas, haut) for i in range(n)]


def records_propres(v):
    out = {}
    if isinstance(v, dict):
        for k, ms in list(v.items())[:40]:
            j = entier(k, -1, 99, -1)
            t = entier(ms, 0, 3600000)
            if 0 <= j < 36 and t > 0:
                out[str(j)] = t
    return out


def numero_libre(nom, tag, pid):
    """Garde le #numéro demandé s'il est libre pour ce pseudo, sinon en tire un autre."""
    pris = {j["tag"] for j in joueurs.values() if j["id"] != pid and j["name"].lower() == nom.lower()}
    if RE_TAG.match(tag or "") and tag not in pris:
        return tag
    for _ in range(500):
        t = "%04d" % random.randint(0, 9999)
        if t not in pris:
            return t
    return "0000"


def fiche_publique(j):
    return {"id": j["id"], "name": j["name"], "tag": j["tag"], "color": j["color"], "cls": j["cls"], "rec": j["rec"]}


def enregistrer(d, pid):
    """Crée ou met à jour la fiche d'un joueur (le verrou est tenu par l'appelant)."""
    global version_annuaire, a_sauver
    j = joueurs.get(pid)
    nouveau = j is None
    if nouveau:
        if len(joueurs) >= MAX_JOUEURS:
            return None
        j = {"id": pid, "name": "Pilote", "tag": "0000", "color": "#9aa4b2", "cls": 0, "rec": {}, "vu": 0}
        joueurs[pid] = j
    nom = nom_propre(d.get("name"))
    couleur = str(d.get("color")) if RE_COULEUR.match(str(d.get("color") or "")) else j["color"]
    tag = numero_libre(nom, str(d.get("tag") or j["tag"]), pid)
    cls = entier(d.get("cls"), 0, 8, j["cls"]) if "cls" in d else j["cls"]
    rec = dict(j["rec"])
    for k, t in records_propres(d.get("rec")).items():
        if k not in rec or t < rec[k]:
            rec[k] = t
    change = nouveau or (nom, tag, couleur, cls, rec) != (j["name"], j["tag"], j["color"], j["cls"], j["rec"])
    j.update(name=nom, tag=tag, color=couleur, cls=cls, rec=rec, vu=time.time())
    if change:
        version_annuaire += 1
        a_sauver = True
    return j


def en_ligne(sauf):
    maintenant = time.time()
    out = []
    for pid, e in direct.items():
        j = joueurs.get(pid)
        if pid == sauf or not j or maintenant - e["recu"] > EN_LIGNE:
            continue
        out.append({"id": pid, "name": j["name"], "tag": j["tag"], "color": j["color"], "cls": j["cls"],
                    "sel": e["sel"], "lv": e["lv"], "b": e["b"], "t": e["t"], "race": e["race"],
                    "age": round(maintenant - e["recu"], 3)})
    return out


def charger():
    try:
        with open(FICHIER_JOUEURS, "r", encoding="utf-8") as f:
            data = json.load(f)
        for j in data.get("joueurs", []):
            pid = str(j.get("id", ""))
            if RE_ID.match(pid):
                joueurs[pid] = {"id": pid, "name": nom_propre(j.get("name")),
                                "tag": str(j.get("tag")) if RE_TAG.match(str(j.get("tag"))) else "0000",
                                "color": str(j.get("color")) if RE_COULEUR.match(str(j.get("color"))) else "#9aa4b2",
                                "cls": entier(j.get("cls"), 0, 8), "rec": records_propres(j.get("rec")),
                                "vu": nombre(j.get("vu"), 0, 4e9)}
    except FileNotFoundError:
        pass
    except (OSError, ValueError, AttributeError) as e:
        print("Attention : joueurs.json illisible (%s). On repart d'un annuaire vide." % e)


def sauver():
    global a_sauver
    with verrou:
        if not a_sauver:
            return
        data = {"joueurs": [dict(j, rec=dict(j["rec"])) for j in joueurs.values()]}
        a_sauver = False
    tmp = FICHIER_JOUEURS + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, FICHIER_JOUEURS)
    except OSError as e:
        print("Sauvegarde impossible :", e)


def entretien():
    while True:
        time.sleep(10)
        sauver()
        limite = time.time() - 60
        with verrou:
            for pid in [p for p, e in direct.items() if e["recu"] < limite]:
                del direct[pid]


class Requete(BaseHTTPRequestHandler):
    server_version = "MiniRace/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def envoyer(self, code, corps, type_="application/json; charset=utf-8"):
        data = corps if isinstance(corps, bytes) else json.dumps(corps, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", type_)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        if self.command != "HEAD" and data:
            self.wfile.write(data)

    def do_OPTIONS(self):
        self.envoyer(204, b"")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        chemin = self.path.split("?", 1)[0]
        if chemin in ("/", "/index.html", "/mini-race.html"):
            try:
                with open(JEU, "rb") as f:
                    data = f.read()
            except OSError:
                return self.envoyer(404, "mini-race.html est introuvable : mets-le dans le même dossier que serveur.py.".encode("utf-8"), "text/plain; charset=utf-8")
            return self.envoyer(200, data, "text/html; charset=utf-8")
        if chemin == "/api/dir":
            with verrou:
                corps = {"players": [fiche_publique(j) for j in joueurs.values()], "dirVer": version_annuaire}
            return self.envoyer(200, corps)
        if chemin == "/api/ping":
            return self.envoyer(200, {"ok": True, "jeu": "Mini Race"})
        self.envoyer(404, {"error": "introuvable"})

    def lire(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n <= 0 or n > MAX_CORPS:
            self.close_connection = True
            return None
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def do_POST(self):
        chemin = self.path.split("?", 1)[0]
        d = self.lire()
        if not isinstance(d, dict):
            return self.envoyer(400, {"error": "requête invalide"})
        pid = str(d.get("id") or "")
        if not RE_ID.match(pid):
            return self.envoyer(400, {"error": "identifiant invalide"})
        if chemin not in ("/api/hello", "/api/sync"):
            return self.envoyer(404, {"error": "introuvable"})
        with verrou:
            j = enregistrer(d, pid)
            if j is None:
                corps = None
            elif chemin == "/api/hello":
                corps = {"ok": True, "tag": j["tag"], "dirVer": version_annuaire}
            else:
                direct[pid] = {"sel": entier(d.get("sel"), 0, 35), "lv": liste(d.get("lv"), 3, 0, 100),
                               "b": liste(d.get("b"), 3, 0, 30), "t": nombre(d.get("t"), 0, 3600),
                               "race": 1 if d.get("race") else 0, "recu": time.time()}
                corps = {"ok": True, "tag": j["tag"], "dirVer": version_annuaire, "online": en_ligne(pid)}
        if corps is None:
            return self.envoyer(503, {"error": "serveur plein"})
        self.envoyer(200, corps)


def adresse_locale():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass
    charger()
    threading.Thread(target=entretien, daemon=True).start()
    try:
        serveur = ThreadingHTTPServer(("0.0.0.0", PORT), Requete)
    except OSError as e:
        print("Impossible d'ouvrir le port %d (%s). Essaie : python serveur.py 8001" % (PORT, e))
        return
    serveur.daemon_threads = True
    ip = adresse_locale()
    print("=" * 62)
    print("  MINI RACE - serveur multijoueur lancé")
    print("=" * 62)
    print("  Sur ce PC            : http://localhost:%d" % PORT)
    if ip:
        print("  Même Wi-Fi / réseau  : http://%s:%d" % (ip, PORT))
    print("  Par Internet         : ouvre un tunnel vers le port %d," % PORT)
    print("                         ex. cloudflared tunnel --url http://localhost:%d" % PORT)
    print("  Pilotes enregistrés  : %d" % len(joueurs))
    if not os.path.exists(JEU):
        print("  ATTENTION : mini-race.html est introuvable à côté de serveur.py !")
    print("  Pour arrêter : ferme cette fenêtre ou appuie sur Ctrl+C.")
    print("=" * 62, flush=True)
    try:
        serveur.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        serveur.server_close()
        sauver()
        print("Serveur arrêté.")


if __name__ == "__main__":
    main()
