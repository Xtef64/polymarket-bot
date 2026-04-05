"""
serve.py - Serveur HTTP pour le dashboard Polymarket
- Local  : python serve.py  -> http://localhost:8765/dashboard.html
- Railway: lancé en thread depuis main.py, écoute sur $PORT
"""
import http.server
import socketserver
import os
import sys
import threading

# Railway injecte $PORT automatiquement ; fallback 8765 en local
PORT = int(os.environ.get("PORT", 8765))
# Chemin absolu du dossier contenant ce fichier (= dossier du projet)
DIR  = os.path.dirname(os.path.abspath(__file__))


class Handler(http.server.SimpleHTTPRequestHandler):
    """Sert les fichiers statiques depuis DIR avec redirection / → /dashboard.html."""

    def __init__(self, *args, **kwargs):
        # directory= évite d'utiliser os.getcwd() (plus sûr en multi-thread)
        super().__init__(*args, directory=DIR, **kwargs)

    def do_GET(self):
        # Redirige la racine vers le dashboard
        if self.path in ("/", ""):
            self.send_response(302)
            self.send_header("Location", "/dashboard.html")
            self.end_headers()
            return
        super().do_GET()

    def log_message(self, fmt, *args):
        # Silence les logs HTTP sauf erreurs 4xx/5xx
        try:
            if int(args[1]) >= 400:
                super().log_message(fmt, *args)
        except (IndexError, ValueError):
            pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()


class _ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """TCPServer multi-thread + allow_reuse_address pour Railway."""
    allow_reuse_address = True
    daemon_threads      = True


def start_server(port: int = PORT) -> None:
    """Démarre le serveur HTTP dans le thread courant (bloquant)."""
    # Pas de os.chdir() : on utilise directory=DIR dans le Handler
    httpd = _ThreadingServer(("", port), Handler)
    print(f"  >> Dashboard HTTP port {port} — fichiers servis depuis {DIR}")
    httpd.serve_forever()


def start_server_thread(port: int = PORT) -> threading.Thread:
    """Lance le serveur HTTP en arrière-plan (non-bloquant). Appeler en tout premier."""
    t = threading.Thread(target=start_server, args=(port,), daemon=True, name="dashboard-http")
    t.start()
    return t


def main():
    """Point d'entrée standalone : python serve.py"""
    import webbrowser
    url = f"http://localhost:{PORT}/dashboard.html"
    print(f"Dashboard disponible sur : {url}")
    webbrowser.open(url)
    start_server()


if __name__ == "__main__":
    main()
