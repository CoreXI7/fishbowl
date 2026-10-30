# Third-party files in app/

| File | From | Version | License |
|---|---|---|---|
| `qrcodegen.py` | [Project Nayuki QR Code generator](https://github.com/nayuki/QR-Code-generator) (`python/qrcodegen.py`) | commit 777682a64202fdb837b50e351b25b7ddb27852c4 (2025-01-04), unmodified | MIT (header in the file) |
| `common-passwords.txt.gz` | [SecLists](https://github.com/danielmiessler/SecLists) `Passwords/Common-Credentials/Pwdb_top-1000000.txt` + `100k-most-used-passwords-NCSC.txt` | commit cb6d7645abf4a2c467bc80f84cf70bda3d60c2d6, filtered to the 5,531 entries of 15–64 printable characters, lower-cased | MIT |

`qrcodegen.py` only computes (no files, network or OS calls); it draws the QR code for an authenticator app.
The password list blocks common passwords long enough to pass the 15-character minimum.
