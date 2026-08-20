import os
from wsgiref.simple_server import make_server

from portfolio_app.server import PortfolioApplication


def main():
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    app = PortfolioApplication()
    with make_server(host, port, app) as server:
        print(f"Serving portfolio dashboard at http://{host}:{port}")
        server.serve_forever()


if __name__ == "__main__":
    main()

