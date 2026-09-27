from . import health

if __name__ == "__main__":
    r = health.run()
    print(r.get("summary", ""))
