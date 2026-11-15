"""Entry point for Vercel, which runs the Flask app named `app` in this file.
On your own computer, use `clipz app` instead."""
from clipz.app import create_vercel_app

app = create_vercel_app()
