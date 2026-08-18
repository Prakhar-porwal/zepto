import os
import sys

# make the project root importable (server.py, config.py, clients/, static/)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import app  # noqa: E402,F401  (Vercel serves this ASGI `app`)
