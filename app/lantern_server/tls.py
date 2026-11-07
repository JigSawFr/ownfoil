"""Lantern over TLS (SPEC.md 2, 5.3): a listener of its own, with a self-signed certificate.

Lantern is never served in plain HTTP beyond loopback, and Ownfoil's own port is plain HTTP. So the same
Flask app is also served on a second port over TLS, by a thread of the process that runs the app: Lantern's
state (a pairing waiting for approval, the event streams) lives in that process and is shared by both ports.

The certificate is made once, in `lantern-tls/` of the config folder, and clients pin the SHA-256 of its key
when they pair (the key's fingerprint, not the certificate, so a renewed certificate on the same key keeps
every pairing). A certificate and key put there by hand (`cert.pem`, `key.pem`) are used as they are.
"""
import datetime
import ipaddress
import logging
import os
import ssl
import threading

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from lantern.spki import spki_sha256
from werkzeug.serving import make_server

from constants import CONFIG_DIR
from settings import get_settings

logger = logging.getLogger('main')

TLS_DIR = os.path.join(CONFIG_DIR, 'lantern-tls')
CERT_FILE = os.path.join(TLS_DIR, 'cert.pem')
KEY_FILE = os.path.join(TLS_DIR, 'key.pem')
VALID_YEARS = 10

_lock = threading.Lock()
_server = None  # the running TlsServer


def ensure_certificate(cert_file=None, key_file=None, names=()):
    """Make the self-signed certificate and its key unless both exist; returns their paths."""
    cert_file, key_file = cert_file or CERT_FILE, key_file or KEY_FILE
    if os.path.exists(cert_file) and os.path.exists(key_file):
        return cert_file, key_file
    os.makedirs(os.path.dirname(cert_file), exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Ownfoil')])
    now = datetime.datetime.now(datetime.timezone.utc)
    alternatives = [x509.DNSName('localhost'), x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]
    for name in names:
        if name:
            alternatives.append(x509.DNSName(name))
    certificate = (x509.CertificateBuilder()
                   .subject_name(subject).issuer_name(subject)
                   .public_key(key.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(now - datetime.timedelta(days=1))
                   .not_valid_after(now + datetime.timedelta(days=365 * VALID_YEARS))
                   .add_extension(x509.SubjectAlternativeName(alternatives), critical=False)
                   .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                   .sign(key, hashes.SHA256()))
    descriptor = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'wb') as out:
        out.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    with open(cert_file, 'wb') as out:
        out.write(certificate.public_bytes(serialization.Encoding.PEM))
    logger.info(f'Lantern: made a self-signed certificate in {os.path.dirname(cert_file)}.')
    return cert_file, key_file


def spki(cert_file=None):
    """The SHA-256 of the certificate's key (hex): what clients pin, and what the pairing page shows."""
    with open(cert_file or CERT_FILE) as handle:
        return spki_sha256(handle.read())


def context(cert_file, key_file):
    """A server context: no protocol older than TLS 1.2 (RFC 8996)."""
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(cert_file, key_file)
    return tls


class TlsServer(threading.Thread):
    """Serves `app` over TLS on `port` until stopped."""

    def __init__(self, app, port, cert_file, key_file, host='0.0.0.0'):
        super().__init__(daemon=True, name='lantern-tls')
        self.server = make_server(host, port, app, threaded=True, ssl_context=context(cert_file, key_file))
        self.port = self.server.server_port

    def run(self):
        self.server.serve_forever()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.join(timeout=5)


def reconcile(app):
    """Start, move or stop the listener to match the settings."""
    global _server
    settings = get_settings()
    wanted = settings['lantern']['https_port'] if settings['lantern']['enabled'] else 0
    with _lock:
        if _server is not None and (not wanted or _server.port != wanted):
            _server.stop()
            _server = None
            logger.info('Lantern: TLS listener stopped.')
        if wanted and _server is None:
            try:
                cert, key = ensure_certificate(names=[settings['shop']['host']])
                server = TlsServer(app, wanted, cert, key)
            except (OSError, ssl.SSLError) as e:
                logger.warning(f'Lantern: cannot listen on TLS port {wanted} ({e}).')
                return
            server.start()
            _server = server
            logger.info(f'Lantern: listening on TLS port {server.port}, key {spki(cert)}.')


def stop():
    """Stop listening, whatever the settings say."""
    global _server
    with _lock:
        if _server is not None:
            _server.stop()
            _server = None
