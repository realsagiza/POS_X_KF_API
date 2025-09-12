from pospos_api_sale import app


if __name__ == "__main__":
    import os

    port = int(os.getenv("PORT", "5115"))
    app.run(host="0.0.0.0", port=port)


