from wsgiref.simple_server import make_server

from portfolio_app.server import PortfolioApplication


def main():
    app = PortfolioApplication()
    with make_server("127.0.0.1", 8000, app) as server:
        print("Serving portfolio dashboard at http://127.0.0.1:8000")
        server.serve_forever()


if __name__ == "__main__":
    main()

