"""Proceso auxiliar cancelable para una única request HTTP(S) de Ollama."""

import sys
import time
import urllib.parse
import urllib.request

from .procesador_texto import (
    _leer_json_ollama,
    host_es_loopback,
    host_no_especificado,
)


class _SinRedirecciones(urllib.request.HTTPRedirectHandler):
    """Una respuesta 3xx termina la request: nunca se acepta otro origen."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _abridor(url: str):
    """Opener sin redirecciones; hacia loopback, además, sin proxies."""
    partes = urllib.parse.urlsplit(url)
    if (partes.scheme not in ("http", "https") or "@" in partes.netloc
            or host_no_especificado(partes.hostname)):
        return None
    # None: proxies del entorno (y su no_proxy) para destinos remotos.
    proxies = {} if host_es_loopback(partes.hostname) else None
    return urllib.request.build_opener(
        urllib.request.ProxyHandler(proxies), _SinRedirecciones)


def main() -> int:
    if len(sys.argv) != 2:
        return 2

    try:
        timeout = float(sys.argv[1])
        if timeout <= 0:
            return 2

        entrada = sys.stdin.buffer.read()
        url_bytes, separador, cuerpo = entrada.partition(b"\n")
        if not separador or not url_bytes:
            return 2

        url = url_bytes.decode("utf-8")
        abridor = _abridor(url)
        if abridor is None:
            return 2

        request = urllib.request.Request(
            url,
            data=cuerpo,
            headers={"Content-Type": "application/json"},
        )

        deadline = time.monotonic() + timeout
        with abridor.open(request, timeout=timeout) as respuesta:
            data = _leer_json_ollama(respuesta, deadline)

        texto = data.get("response", "")
        if not isinstance(texto, str):
            return 1

        sys.stdout.buffer.write(texto.encode("utf-8"))
        return 0

    except BaseException:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
