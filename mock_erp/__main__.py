from .app import create_app

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Run the AcmePay sandbox app")
    ap.add_argument("--port", type=int, default=5055)
    ap.add_argument("--faults", default="", help="e.g. flaky_get=2,commit_then_500=1,session_expiry=4")
    a = ap.parse_args()
    fl = {}
    for item in filter(None, a.faults.split(",")):
        k, _, v = item.partition("=")
        fl[k] = int(v) if v else 1
    print(f"AcmePay on http://127.0.0.1:{a.port}  faults={fl}  login: ap_clerk / sandbox-pass")
    create_app(fl).run(port=a.port, debug=False)
