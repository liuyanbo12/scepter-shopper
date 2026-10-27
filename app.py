from shopper.server import create_app

app = create_app()

if __name__ == "__main__":
    import uvicorn
    from shopper.config import load_settings
    config = load_settings()
    uvicorn.run(app, host=config.host, port=config.port, log_level="info")
