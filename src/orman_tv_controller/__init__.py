"""orman-tv-controller: controls the TVs on a home network for Orman clients.

A client finds it by Bonjour (`_orman-tvctl._tcp`, the controller id in the TXT
record) and calls its HTTP API with a bearer token. Drivers do the talking to
TVs; pyatv (Apple TV) is the first.
"""

__version__ = "0.6.0"
