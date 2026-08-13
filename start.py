import eventlet
from eventlet import wsgi
from scraper import app

# Monkey-patch the standard library for non-blocking I/O
eventlet.monkey_patch()

if __name__ == "__main__":
    # Run the Flask app using Eventlet's native WSGI server on port 8080
    # This completely bypasses Gunicorn, avoiding the entry point crash
    wsgi.server(eventlet.listen(('', 8080)), app)
