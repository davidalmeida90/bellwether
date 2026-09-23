"""Launch the local interface. py -3 serve.py [--port 8077] [--no-browser]"""
import argparse
import webbrowser

import uvicorn


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8077)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--reload", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    url = f"http://{args.host}:{args.port}/"
    if not args.no_browser:
        webbrowser.open(url)
    print(f"Bellwether on {url}")
    uvicorn.run("api.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
