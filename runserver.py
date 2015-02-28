from api import create_app
app = create_app()
if __name__ == "__main__":
    import sys
    try:
        port = int(sys.argv[1])
    except IndexError:
        port = 5000
    app.run('0.0.0.0', debug=True, port=port)
