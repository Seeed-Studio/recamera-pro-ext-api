"""Put the repo root AND this app's own directory on sys.path.

On device `kit.run` does exactly this (kit/run.py :26-27) so an app may import
its sibling modules by plain name; the tests have to reproduce it or
`import scrfd` fails.
"""
import os
import sys

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(APP_DIR))

for p in (REPO, APP_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)
