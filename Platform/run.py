from dotenv import load_dotenv
load_dotenv()

from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(
        host=app.config.get("PLATFORM_HOST", "127.0.0.1"),
        port=int(app.config.get("PLATFORM_PORT", 8000)),
        debug=False,
        use_reloader=False,
        threaded=True,
    )
