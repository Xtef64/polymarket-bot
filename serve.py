"""
serve.py - Serveur HTTP local pour le dashboard Polymarket
Lance : python serve.py  -> ouvre http://localhost:8765/dashboard.html
"""
import http.server
import socketserver
import webbrowser
import os
import sys

PORT = 8765
DIR  = os.path.dirname(os.path.abspath(__file__))


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIR, **kwargs)

    def log_message(self, fmt, *args):
        # Silence les logs sauf erreurs HTTP
        try:
            if int(args[1]) >= 400:
                super().log_message(fmt, *args)
        except (IndexError, ValueError):
            pass

    def end_headers(self):
        # Désactive le cache pour que fetch() recharge toujours performance.json
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        super().end_headers()


def main():
    os.chdir(DIR)
    url = f"http://localhost:{PORT}/dashboard.html"

    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        print(f"Dashboard disponible sur : {url}")
        print("Ctrl+C pour arrêter le serveur.")
        webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nServeur arrêté.")
            sys.exit(0)


if __name__ == "__main__":
    main()
