"""Archive a music library you own, and publish recordings you made.

Stage one resolves a Spotify or YouTube reference into a tagged album folder.
Stage two, only on request, drives a real browser against soundcloud.com and
publishes that folder as one set.

The urllib3 warning filter below runs here, in the package root, because Python
executes this file before any submodule. That is the only place the ordering is
guaranteed no matter how the package is entered -- console script, ``python -m``
or a bare ``import spot2cloud.download.downloader`` from a test.
"""

import warnings

__version__ = "1.0.0"

__all__ = ["__version__"]

# urllib3 v2 warns at import time, under macOS system Python, that the ssl
# module is linked against LibreSSL rather than OpenSSL 1.1.1+. It reaches us
# through requests the moment spotipy is imported, the user can do nothing
# about it, and it lands in the middle of the progress output.
warnings.filterwarnings("ignore", message=".*OpenSSL.*")
