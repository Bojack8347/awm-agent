"""
Entry point for running the API as a module: python -m api
"""
from .app import app, HOST, PORT

if __name__ == '__main__':
    app.run(host=HOST, port=PORT, debug=False)

