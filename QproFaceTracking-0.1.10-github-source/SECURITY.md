# Security and privacy

Please report security or privacy concerns through a private GitHub security
advisory rather than a public issue.

QproFaceTracking processes inward-facing eye and mouth cameras and requires root on
the headset. Recordings and generated profiles can contain biometric data. They are
ignored by the supplied `.gitignore`, but users are responsible for checking commits
and archives before publishing.

The proof of concept uses ADB over USB and binds preview services to localhost. It
does not expose a direct LAN camera server. Wireless experiments are intentionally
excluded from the public v0.1 package.
