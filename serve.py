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
DIR  = os.path.dirname(os.path.abspath(__file__))


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIR, **kwargs)

    def log_message(self, fmt, *args):
        # Silence les logs HTTP sauf erreurs
        try:
            if int(args[1]) >= 400:
                super().log_message(fmt, *args)
        except (IndexError, ValueError):
            pass

    def end_headers(self):
        # Désactive le cache pour que fetch() recharge toujours performance.json
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        # CORS ouvert pour accès depuis n'importe quel domaine
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()


def start_server(port: int = PORT) -> socketserver.TCPServer:
    """Démarre le serveur HTTP dans le thread courant (bloquant)."""
    os.chdir(DIR)
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("", port), Handler)
    print(f"  >> Dashboard HTTP sur le port {port}")
    httpd.serve_forever()
    return httpd


def start_server_thread(port: int = PORT) -> threading.Thread:
    """Lance le serveur HTTP en arrière-plan (non-bloquant)."""
    t = threading.Thread(target=start_server, args=(port,), daemon=True)
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
